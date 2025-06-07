import asyncio
import json
import os
from typing import List, Sequence, Dict, Any

from autogen_agentchat.agents import AssistantAgent, UserProxyAgent, BaseChatAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.ui import Console
from autogen_agentchat.base import Response
from autogen_agentchat.messages import TextMessage, BaseChatMessage

from autogen_core import CancellationToken
# 請注意: 在 autogen v0.4+ 中，模型客戶端被移至 autogen_ext 套件
from autogen_ext.models.openai import OpenAIChatCompletionClient

# --- 0. 環境與配置設定 ---

# 建議從環境變數讀取 API 金鑰以策安全
# 在終端機設定: export OPENAI_API_KEY="sk-..."
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("請設定 OPENAI_API_KEY 環境變數")

# 全局模型客戶端實例
# 使用 gpt-4o 或 gpt-4-turbo 以獲得更好的角色扮演和遵循指令的能力
model_client = OpenAIChatCompletionClient(model="gpt-4o", api_key=API_KEY)


# --- 1. 基礎建設：代理人與資料載入 ---

def load_student_data(filepath: str = "simulated_students.json") -> List[Dict[str, Any]]:
    """從 JSON 檔案載入學生資料"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"錯誤: 找不到學生資料檔案 '{filepath}'。請先建立此檔案。")
        return []

def create_student_agents(student_data: List[Dict[str, Any]]) -> List[AssistantAgent]:
    """根據學生資料建立 AssistantAgent 列表"""
    if not student_data:
        return []
    return [
        AssistantAgent(
            name=student['name'],
            system_message=student['llm_persona_prompt'],
            model_client=model_client
        )
        for student in student_data
    ]

def create_teacher_agent() -> UserProxyAgent:
    """建立代表老師的人類代理人"""
    return UserProxyAgent(
        name="老師",
        human_input_mode="ALWAYS",  # 確保老師可以隨時透過終端機輸入來引導討論
        description="課堂的主持人，負責提出教案主題並在必要時引導討論。"
    )


# --- 2. 核心功能：三種討論模式 ---

async def run_full_class_discussion(teacher: UserProxyAgent, students: List[AssistantAgent]):
    """模式一：全班討論"""
    print("\n" + "="*50)
    print("模式一：全班討論 (RoundRobinGroupChat)")
    print("="*50)

    # 將老師加入討論，讓他可以輪流發言
    all_participants = [teacher] + students
    
    # 設定終止條件：最多進行 (學生人數+1) * 2 輪對話
    termination_condition = MaxMessageTermination(max_messages=len(all_participants) * 2)

    # 建立包含所有人的圓桌會議式小組
    group_chat = RoundRobinGroupChat(
        agents=all_participants,
        termination_condition=termination_condition
    )

    initial_task = await teacher.get_human_input(
        "--- 全班討論 ---\n請老師輸入您想模擬的教案主題或討論問題："
    )

    # 使用 run_stream 即時觀察對話過程
    stream = group_chat.run_stream(task=initial_task)
    await Console(stream)
    print("\n--- 全班討論已結束 ---")


async def run_group_discussion(teacher: UserProxyAgent, students: List[AssistantAgent]):
    """模式二：分組討論"""
    print("\n" + "="*50)
    print("模式二：分組討論 (5個獨立的 RoundRobinGroupChat)")
    print("="*50)

    if not students:
        print("沒有學生資料，無法進行分組討論。")
        return

    initial_task = await teacher.get_human_input(
        "--- 分組討論 ---\n請老師輸入您想讓各小組討論的教案主題或問題："
    )

    groups = [students[i:i+6] for i in range(0, len(students), 6)]
    group_chat_simulations = []

    for i, group_agents in enumerate(groups):
        group_name = f"第 {i+1} 組"
        group_chat = RoundRobinGroupChat(
            agents=group_agents,
            termination_condition=MaxMessageTermination(max_messages=15) # 每組最多15輪
        )
        print(f"\n--- 準備執行 {group_name} 的討論 ---")
        stream = group_chat.run_stream(task=f"這是 {group_name} 的內部討論。任務：{initial_task}")
        # 將每個小組的模擬任務加入列表
        group_chat_simulations.append(Console(stream, title=group_name))

    # 使用 asyncio.gather 同時執行所有分組討論
    await asyncio.gather(*group_chat_simulations)

    print("\n--- 所有分組討論已結束，開始進行匯總報告 ---")
    
    # 收集結果並要求 AI 進行匯總
    all_summaries = []
    for i, group_agents in enumerate(groups):
        group_name = f"第 {i+1} 組"
        # 從 RoundRobinGroupChat 物件中獲取最後的任務結果
        # 假設 group_chat_simulations 中的 Console 物件保留了對 stream 的存取，進而能追溯到 group_chat 物件
        # 為了更穩健，我們直接從創建的 group_chat 列表獲取結果
        # (這裡的實作簡化了，假設能拿到對應的 group_chat 物件)
        # 實際上，我們應該儲存 group_chat 物件本身
        group_chat_instance = [gc._stream._coro.cr_frame.f_locals['self'] for gc in group_chat_simulations if gc.title == group_name][0]
        task_result = group_chat_instance.last_task_result
        
        if task_result and task_result.messages:
            # 建立一個一次性的匯總代理人
            summary_agent = AssistantAgent(
                name=f"{group_name}_匯總者",
                system_message=f"你是專業的報告員。請根據以下 {group_name} 的對話紀錄，產出一份條理分明、涵蓋主要觀點和最終結論的報告。報告開頭請標明 '{group_name} 報告'。",
                model_client=model_client,
            )
            # 將完整的對話歷史作為新任務，讓匯總代理人處理
            cancellation_token = CancellationToken()
            summary_response = await summary_agent.on_messages(messages=task_result.messages, cancellation_token=cancellation_token)
            summary_text = summary_response.chat_message.to_text()
            print("\n" + "-"*20 + f" {group_name} 報告 " + "-"*20)
            print(summary_text)
            all_summaries.append(f"<{group_name} 報告>\n{summary_text}")
    
    print("\n--- 分組討論模式結束 ---")
    
    # 將所有報告呈現給老師
    return "\n\n".join(all_summaries)

# --- 模式三的特殊組件 ---
class ClassModeratorAgent(BaseChatAgent):
    """
    v0.4 嵌套聊天概念的實作。
    這個「總主持人」代理人接收老師的任務後，會將任務分派給內部的多個小組進行討論，
    最後收集所有小組的結果並進行最終匯總。
    """
    def __init__(self, name: str, group_chats: List[RoundRobinGroupChat]):
        super().__init__(name, description="一個能主持多個小組討論的總主持人。")
        self._group_chats = group_chats
        self._chief_summarizer = AssistantAgent(
            name="總報告員",
            system_message="你是首席報告員。你的任務是整合來自多個小組的報告，並產出一份全面的、涵蓋所有小組核心發現和差異的最終總結報告。",
            model_client=model_client
        )

    async def on_messages(self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken) -> Response:
        # 取得老師發起的初始任務
        initial_task = messages[-1].to_text()
        print(f"\n總主持人收到任務：{initial_task}")

        # --- 內部觸發5個分組討論 (核心嵌套邏輯) ---
        group_simulations = []
        for i, group_chat in enumerate(self._group_chats):
            group_name = f"第 {i+1} 組"
            print(f"總主持人正在啟動 {group_name} 的討論...")
            # 這裡使用 .run() 而非 .run_stream()，因為我們需要等待結果回來再處理
            # 每個 group_chat.run 都是一個 awaitable
            group_simulations.append(group_chat.run(task=f"這是 {group_name} 的內部討論。任務：{initial_task}", cancellation_token=cancellation_token))

        # 平行等待所有小組討論完成
        group_results = await asyncio.gather(*group_simulations)
        print("\n--- 所有內部小組討論完成，總主持人開始匯總報告 ---")

        # --- 收集並匯總結果 ---
        group_summaries = []
        for i, result in enumerate(group_results):
            if result and result.messages:
                group_name = f"第 {i+1} 組"
                # 為每個小組產生初步總結
                summarizer = AssistantAgent(
                    name=f"{group_name}_匯總者",
                    system_message=f"請簡潔地總結以下對話的核心觀點。",
                    model_client=model_client
                )
                summary_response = await summarizer.on_messages(messages=result.messages, cancellation_token=cancellation_token)
                summary_text = summary_response.chat_message.to_text()
                group_summaries.append(f"<{group_name} 的總結>\n{summary_text}")

        # --- 最終匯總 ---
        final_summary_task = "請整合以下所有小組的總結，產出一份給老師的最終報告。\n\n" + "\n\n".join(group_summaries)
        final_response = await self._chief_summarizer.on_messages(
            messages=[TextMessage(content=final_summary_task, source=self.name)],
            cancellation_token=cancellation_token
        )
        
        # 將最終報告作為自己的回應返回
        return Response(chat_message=final_response.chat_message, inner_messages=messages)

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        """重置所有內部小組的狀態"""
        for gc in self._group_chats:
            await gc.reset()
        await self._chief_summarizer.reset()
        print("總主持人已重置所有內部狀態。")

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)


async def run_nested_chat_discussion(teacher: UserProxyAgent, students: List[AssistantAgent]):
    """模式三：嵌套聊天"""
    print("\n" + "="*50)
    print("模式三：嵌套聊天 (ClassModeratorAgent)")
    print("="*50)

    if not students:
        print("沒有學生資料，無法進行嵌套聊天。")
        return

    # 1. 建立5個分組討論實例，但不執行
    groups = [students[i:i+6] for i in range(0, len(students), 6)]
    group_chats = [
        RoundRobinGroupChat(
            agents=group_agents,
            termination_condition=MaxMessageTermination(max_messages=15)
        )
        for group_agents in groups
    ]

    # 2. 建立總主持人，並將5個小組"注入"
    class_moderator = ClassModeratorAgent(
        name="總主持人",
        group_chats=group_chats
    )

    # 3. 建立一個只包含老師和總主持人的頂層對話
    #    這是一個簡單的 Request-Response 流程
    top_level_chat = RoundRobinGroupChat(
        agents=[teacher, class_moderator],
        termination_condition=MaxMessageTermination(max_messages=2) # 老師提問 -> 主持人回答
    )
    
    initial_task = await teacher.get_human_input(
        "--- 嵌套聊天 ---\n請老師輸入您想指派給總主持人的教案任務："
    )

    # 4. 啟動頂層對話，這將觸發內部所有流程
    stream = top_level_chat.run_stream(task=initial_task)
    await Console(stream, title="老師與總主持人的對話")

    print("\n--- 嵌套聊天模式結束 ---")


# --- 主程式入口 ---
async def main():
    """主執行函式，提供模式選擇"""
    # 載入並建立代理人
    all_student_data = load_student_data()
    if not all_student_data:
        return
        
    student_agents = create_student_agents(all_student_data)
    teacher_agent = create_teacher_agent()

    while True:
        print("\n" + "#"*60)
        print("歡迎使用優學院 Autogen 虛擬教室模擬器")
        print("#"*60)
        print("請選擇要執行的模擬模式：")
        print("1. 全班討論 (老師與30位學生一同討論)")
        print("2. 分組討論 (5組，每組6人，平行討論後分別匯總)")
        print("3. 嵌套聊天 (老師僅對話總主持人，由其內部 orchestrate 分組討論與匯總)")
        print("4. 退出")
        
        choice = input("請輸入您的選擇 (1/2/3/4): ")

        if choice == '1':
            await run_full_class_discussion(teacher_agent, student_agents)
        elif choice == '2':
            await run_group_discussion(teacher_agent, student_agents)
        elif choice == '3':
            await run_nested_chat_discussion(teacher_agent, student_agents)
        elif choice == '4':
            print("感謝使用，系統退出。")
            break
        else:
            print("無效的輸入，請重新選擇。")
        
        # 重置所有代理人狀態以便進行下一次模擬
        await teacher_agent.reset()
        for student in student_agents:
            await student.reset()
        print("\n*** 所有代理人狀態已重置，可以開始新的模擬。 ***\n")

    # 關閉模型客戶端連線
    await model_client.close()


if __name__ == "__main__":
    # 執行主異步函式
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程式被使用者中斷。")