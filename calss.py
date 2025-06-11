# -*- coding: utf-8 -*-
"""
Uedu 虛擬教室 v2.5 · AutoGen v0.4 (Logic & Bug Fix)
====================================================
• [修正] 解決 'RoundRobinGroupChat' object has no attribute 'last_task_result' 的崩潰錯誤
• [優化] 升級共識停止機制，需全員發言後才進行判斷，並使用更嚴謹的提示詞
• 完整實現：分組討論 -> 老師評論 -> 最終評估
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence

import aiomysql
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TerminationCondition, TerminatedException
from autogen_agentchat.messages import BaseChatMessage, StopMessage, TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

# ------------------------------------------------------------------------------
# MySQL 連線 & DDL
# ------------------------------------------------------------------------------

MYSQL_CFG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "55665566"), # 請替換為你的密碼
    "db": os.getenv("MYSQL_DB", "classroom_discussion"),
    "charset": "utf8mb4",
    "autocommit": True,
}

_DDL = {
    "discussion_groups": "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64) NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "messages": "id INT AUTO_INCREMENT PRIMARY KEY, group_id INT, sender VARCHAR(64), content MEDIUMTEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, INDEX (group_id)",
    "teacher_comments": "id INT AUTO_INCREMENT PRIMARY KEY, group_id INT, teacher_name VARCHAR(64), comment LONGTEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, INDEX (group_id)",
    "final_evaluations": "id INT AUTO_INCREMENT PRIMARY KEY, content LONGTEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
}

_mysql_pool: aiomysql.Pool | None = None

async def _ensure_pool() -> aiomysql.Pool:
    global _mysql_pool
    if _mysql_pool is None: _mysql_pool = await aiomysql.create_pool(**MYSQL_CFG, maxsize=10)
    return _mysql_pool

async def setup_classroom_db(recreate: bool = False) -> None:
    cfg, dbname = MYSQL_CFG.copy(), MYSQL_CFG["db"]
    cfg.pop("db")
    conn = await aiomysql.connect(**cfg)
    async with conn.cursor() as cur:
        await cur.execute(f"CREATE DATABASE IF NOT EXISTS {dbname} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        await cur.execute(f"USE {dbname}")
        for tbl, cols in _DDL.items():
            if recreate: await cur.execute(f"DROP TABLE IF EXISTS {tbl}")
            await cur.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({cols}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    await conn.ensure_closed()
    print("✅ MySQL schema ready (v2.5)")

# ------------------------------------------------------------------------------
# 資料庫操作函式 (DAO)
# ------------------------------------------------------------------------------

async def save_message(group_id: int, sender: str, content: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO messages (group_id, sender, content) VALUES (%s, %s, %s)", (group_id, sender, content))

async def save_teacher_comment(group_id: int, teacher_name: str, comment: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO teacher_comments (group_id, teacher_name, comment) VALUES (%s, %s, %s)", (group_id, teacher_name, comment))

async def save_final_evaluation(content: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO final_evaluations (content) VALUES (%s)", (content,))

async def get_or_create_group(group_name: str) -> int:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM discussion_groups WHERE name=%s", (group_name,))
        row = await cur.fetchone()
        if row: return row[0]
        await cur.execute("INSERT INTO discussion_groups (name) VALUES (%s)", (group_name,))
        return cur.lastrowid

# ------------------------------------------------------------------------------
# AutoGen 相關
# ------------------------------------------------------------------------------

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY: raise EnvironmentError("請先設定環境變數 OPENAI_API_KEY")

model_client = OpenAIChatCompletionClient(model="gpt-4o-mini", api_key=API_KEY)
DATA_DIR = Path(__file__).parent.resolve()

def _load_json(file: str, default: Any) -> Any:
    fp = DATA_DIR / file
    return json.load(fp.open("r", encoding="utf-8")) if fp.exists() else default

class ConsensusTermination(TerminationCondition):
    def __init__(self, model_client: OpenAIChatCompletionClient, members: List[AssistantAgent], check_interval: int = 3):
        self.model_client = model_client
        self.check_interval = check_interval
        self.all_members = {agent.name for agent in members}
        self.speakers = set()
        self._ct = CancellationToken()
        self._terminated = False
        self._message_count = 0

    @property
    def terminated(self) -> bool: return self._terminated
    async def reset(self) -> None:
        self._terminated = False
        self._message_count = 0
        self.speakers = set()

    async def __call__(self, messages: Sequence[BaseChatMessage]) -> StopMessage | None:
        if self.terminated: raise TerminatedException("Termination condition has already been reached")
        
        self._message_count += 1
        
        last_speaker = messages[-1].source
        if last_speaker in self.all_members:
            self.speakers.add(last_speaker)

        # 條件一：確保全員都已發言
        if len(self.speakers) < len(self.all_members):
            return None

        # 條件二：在全員發言後，定期檢查共識
        if self._message_count % self.check_interval != 0:
            return None

        print(f"\n[共識檢查] 全員已發言，正在分析最近 {self.check_interval} 則訊息...")
        conversation_text = "\n".join(f"- {msg.source}: {msg.to_text()}" for msg in messages[-self.check_interval:])
        prompt = f"""
        你的任務是判斷一個討論小組是否已達成最終共識。請基於以下對話紀錄，嚴格判斷：
        討論是否已經收斂，並且最近的發言沒有提出任何新的、需要進一步討論的觀點或反對意見？

        如果結論已經形成且無新觀點，請只回答「是」。
        如果討論仍在發散或有人提出新想法/疑慮，請只回答「否」。

        對話記錄：
        {conversation_text}
        """
        
        try:
            checker_agent = AssistantAgent(name="ConsensusChecker", system_message="你是一個共識分析師，請根據指示只回答'是'或'否'。", model_client=self.model_client)
            response = await checker_agent.on_messages([TextMessage(content=prompt, source="user")], self._ct)
            response_text = response.chat_message.to_text().strip()
            
            print(f"[共識檢查] 模型判斷: {response_text}")
            if "是" in response_text:
                print("[共識檢查] 偵測到共識，即將結束本組討論。")
                self._terminated = True
                return StopMessage(content="Consensus reached.", source="ConsensusTermination")
        except Exception as e:
            print(f"[共識檢查] 錯誤: {e}")
        return None

def sanitize_name(idx: int, raw: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "", raw)
    return base if base and (base[0].isalpha() or base[0] == "_") else f"S{idx:03d}"

def create_student_agents(students: List[Dict[str, Any]]) -> List[AssistantAgent]:
    return [
        AssistantAgent(
            name=sanitize_name(i, s.get("name", f"Student{i}")),
            description=f"DisplayName:{s.get('name', 'N/A')}",
            system_message=s["llm_persona_prompt"],
            model_client=model_client,
        ) for i, s in enumerate(students, 1)
    ]

# ------------------------------------------------------------------------------
# 討論流程
# ------------------------------------------------------------------------------

async def sequential_group_discussion(students: List[AssistantAgent], task: str) -> None:
    if not students: return

    groups, all_teacher_comments = [students[i:i+6] for i in range(0, len(students), 6)], []

    for idx, members in enumerate(groups, 1):
        group_name = f"Group{idx}"
        print(f"\n{'='*20} {group_name} 討論開始 {'='*20}")
        group_id = await get_or_create_group(group_name)

        consensus_checker = ConsensusTermination(model_client=model_client, members=members, check_interval=3)
        chat = RoundRobinGroupChat(members, termination_condition=consensus_checker)
        
        group_messages = [] # [修正] 用於收集該組所有訊息
        async for event in chat.run_stream(task=f"這是 {group_name} 的內部討論。任務：{task}"):
            if isinstance(event, BaseChatMessage):
                sender, content = event.source or "Unknown", event.to_text()
                await save_message(group_id, sender, content)
                snippet = content.replace('\n', ' ')[:80]
                print(f"{sender:>15}: {snippet}")
                group_messages.append(event) # [修正] 將訊息收集起來

        print(f"\n-- {group_name} 討論結束 --")

        if not group_messages: continue # [修正] 判斷是否有訊息

        transcript = "\n".join(f"- {m.source}: {m.to_text()}" for m in group_messages) # [修正] 使用收集到的訊息
        teacher_prompt = f"我是老師。請檢視「{group_name}」的討論紀錄，並給出評論。\n\n任務：{task}\n\n紀錄：\n{transcript}\n\n你的評論："

        print(f"\n[老師評論] 正在生成對 {group_name} 的評論...")
        commenter = AssistantAgent(name="TeacherCommenter", system_message="你是經驗豐富的老師，專長分析學生討論並給出評論。", model_client=model_client)
        comment_msg = await commenter.on_message(teacher_prompt, CancellationToken())
        comment_txt = comment_msg.chat_message.to_text()

        await save_teacher_comment(group_id, "Teacher", comment_txt)
        all_teacher_comments.append({"group_name": group_name, "comment": comment_txt})
        print(f"\n-- 老師對 {group_name} 的評論 --\n{comment_txt}")

    print("\n\n" + "*"*20 + " 所有小組討論與評論完成 " + "*"*20)
    if not all_teacher_comments: return

    joined_comments = "\n\n".join(f"【對 {c['group_name']} 的評論】\n{c['comment']}" for c in all_teacher_comments)
    eval_prompt = f"身為教育評估專家，請根據以下老師對各組的評論，評估教案「{task}」的可行性，包含優點、挑戰與改進建議。\n\n評論如下：\n{joined_comments}"
    
    evaluator = AssistantAgent(name="FinalEvaluator", system_message="你是教育方案評估專家，專長分析教學活動成效。", model_client=model_client)
    print("\n[最終評估] 正在生成教案可行性評估報告...")
    eval_msg = await evaluator.on_message(eval_prompt, CancellationToken())
    eval_txt = eval_msg.chat_message.to_text()

    await save_final_evaluation(eval_txt)
    print(f"\n{'='*20} 教案可行性最終評估 {'='*20}\n{eval_txt}")

# ------------------------------------------------------------------------------
# 主程式入口
# ------------------------------------------------------------------------------

async def main() -> None:
    await setup_classroom_db(recreate=False)

    students_data = _load_json("simulated_students.json", [])[:30]
    lesson_plans = _load_json("lesson_plans.json", [])
    
    if not students_data or not lesson_plans:
        print("[錯誤] 請確保 simulated_students.json 和 lesson_plans.json 存在且不為空。")
        return

    students = create_student_agents(students_data)
    
    print("可用的教案：")
    for i, lp in enumerate(lesson_plans, 1): print(f"  {i}. {lp['title']}")
    
    try:
        sel = int(input("請選擇要模擬的教案編號: ")) - 1
        task = lesson_plans[sel]["initial_prompt"]
        print(f"\n已選擇教案：『{lesson_plans[sel]['title']}』\n任務提示：{task}\n")
    except (ValueError, IndexError):
        print("選擇無效，將結束程式。")
        return

    await sequential_group_discussion(students, task)

    if _mysql_pool: _mysql_pool.close(); await _mysql_pool.wait_closed()
    await model_client.close()
    print("\n✅ 模擬完成，所有連線已關閉。")

if __name__ == "__main__":
    asyncio.run(main())
