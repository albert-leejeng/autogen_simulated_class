# -*- coding: utf-8 -*-
"""
Uedu 虛擬教室 · AutoGen v0.4 + aiomysql 版
==========================================
• 30 位數位孿生學生 → AssistantAgent
• 老師 (主持人)      → UserProxyAgent
• 分組討論『串行』，邊說邊寫 MySQL
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import aiomysql
from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseChatMessage
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
    "password": os.getenv("MYSQL_PASSWORD", "55665566"),
    "db": os.getenv("MYSQL_DB", "classroom_discussion"),
    "charset": "utf8mb4",
    "autocommit": True,
}

_DDL = {
    "discussion_groups": """
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(64) NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """,
    "messages": """
        id INT AUTO_INCREMENT PRIMARY KEY,
        group_id INT,
        sender VARCHAR(32),
        content MEDIUMTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX (group_id)
    """,
    "summaries": """
        id INT AUTO_INCREMENT PRIMARY KEY,
        group_id INT,
        content MEDIUMTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX (group_id)
    """,
    "reports": """
        id INT AUTO_INCREMENT PRIMARY KEY,
        content LONGTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """,
}

_mysql_pool: aiomysql.Pool | None = None


async def _ensure_pool() -> aiomysql.Pool:
    global _mysql_pool
    if _mysql_pool is None:
        _mysql_pool = await aiomysql.create_pool(**MYSQL_CFG, maxsize=6)
    return _mysql_pool


async def setup_classroom_db(recreate: bool = False) -> None:
    """建立資料庫 & 表格 (手寫 DDL)。"""
    cfg = MYSQL_CFG.copy()
    dbname = cfg.pop("db")
    conn = await aiomysql.connect(**cfg)
    async with conn.cursor() as cur:
        await cur.execute(f"CREATE DATABASE IF NOT EXISTS {dbname} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        await cur.execute(f"USE {dbname}")
        for tbl, cols in _DDL.items():
            if recreate:
                await cur.execute(f"DROP TABLE IF EXISTS {tbl}")
            await cur.execute(f"CREATE TABLE IF NOT EXISTS {tbl} ({cols}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    await conn.ensure_closed()
    print("✅ MySQL schema ready")


# ------------------------------------------------------------------------------
# DAO functions
# ------------------------------------------------------------------------------

async def save_message(group_id: int, sender: str, content: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO messages (group_id, sender, content) VALUES (%s, %s, %s)",
            (group_id, sender, content),
        )


async def save_summary(group_id: int, content: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO summaries (group_id, content) VALUES (%s, %s)", (group_id, content))


async def save_final_report(content: str) -> None:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("INSERT INTO reports (content) VALUES (%s)", (content,))


async def get_all_summaries() -> List[str]:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT content FROM summaries ORDER BY id")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_or_create_group(group_name: str) -> int:
    pool = await _ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM discussion_groups WHERE name=%s", (group_name,))
        row = await cur.fetchone()
        if row:
            return row[0]
        await cur.execute("INSERT INTO discussion_groups (name) VALUES (%s)", (group_name,))
        return cur.lastrowid


# ------------------------------------------------------------------------------
# AutoGen 相關
# ------------------------------------------------------------------------------

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise EnvironmentError("請先匯入 OPENAI_API_KEY")

model_client = OpenAIChatCompletionClient(model="gpt-4o-mini", api_key=API_KEY)

DATA_DIR = Path(__file__).parent


def _load_json(file: str, default: Any) -> Any:
    fp = DATA_DIR / file
    return json.load(fp.open("r", encoding="utf-8")) if fp.exists() else default


# ----- 代理人生成 -----

def sanitize_name(idx: int, raw: str) -> str:
    """轉成合法識別字：字母開頭，僅 [A-Za-z0-9_]。"""
    base = re.sub(r"[^A-Za-z0-9_]", "", raw)
    if not base or not (base[0].isalpha() or base[0] == "_"):
        base = f"S{idx:03d}"
    return base


def create_student_agents(students: List[Dict[str, Any]]) -> List[AssistantAgent]:
    agents = []
    for i, s in enumerate(students, 1):
        agents.append(
            AssistantAgent(
                name=sanitize_name(i, s["name"]),
                description=f"DisplayName:{s['name']}",
                system_message=s["llm_persona_prompt"],
                model_client=model_client,
            )
        )
    return agents


def create_teacher_agent() -> UserProxyAgent:
    return UserProxyAgent(name="Teacher", description="課程主持人，可引導討論")


# ------------------------------------------------------------------------------
# 討論流程
# ------------------------------------------------------------------------------

async def sequential_group_discussion(teacher: UserProxyAgent, students: List[AssistantAgent], task: str) -> None:
    if not students:
        print("[!] 沒有學生")
        return

    groups = [students[i : i + 6] for i in range(0, len(students), 6)]

    for idx, members in enumerate(groups, 1):
        group_name = f"Group{idx}"
        print(f"\n{'='*10} {group_name} 討論開始 {'='*10}")

        # 先登記 / 取得 group_id
        group_id = await get_or_create_group(group_name)

        chat = RoundRobinGroupChat(
            members,
            termination_condition=MaxMessageTermination(max_messages=20),
        )

        # 1️⃣ 逐訊息寫入 DB
        async for event in chat.run_stream(task=f"這是 {group_name} 的內部討論。任務：{task}"):
            if isinstance(event, BaseChatMessage):
                await save_message(group_id, event.source, event.to_text())

            # 2️⃣ 立刻印到螢幕（⚠︎ 新增）
                snippet = event.to_text().replace("\n", " ")[:80]
                print(f"{event.source:>10}: {snippet}")

        # 2️⃣ 小組摘要
        reporter = AssistantAgent(
            name=f"{group_name}_Reporter",
            system_message="請條列摘要以上討論重點與結論。",
            model_client=model_client,
        )
        ct = CancellationToken()
        result: TaskResult = chat.last_task_result  # type: ignore
        summary_msg = await reporter.on_messages(result.messages, ct)
        summary_txt = summary_msg.chat_message.to_text()

        await save_summary(group_id, summary_txt)
        print(f"\n-- {group_name} 匯總 --\n{summary_txt}")

    print("\n*** 所有小組討論完成 ***")

    # 3️⃣ 老師做全班總結
    summaries = await get_all_summaries()
    join_txt = "\n\n".join(f"【小組 {i+1}】\n{txt}" for i, txt in enumerate(summaries))
    teacher_reporter = AssistantAgent(
        name="TeacherReporter",
        system_message="請統整所有小組報告，給出整體評語與建議。",
        model_client=model_client,
    )
    ct = CancellationToken()
    final_msg = await teacher_reporter.on_message(join_txt, ct)
    final_report = final_msg.chat_message.to_text()
    await save_final_report(final_report)

    print("\n===== 全班總結 =====\n", final_report)


# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------

async def main() -> None:
    await setup_classroom_db(recreate=False)

    students_data = _load_json("simulated_students.json", [])[:30]
    lesson_plans = _load_json("lesson_plans.json", [])

    teacher = create_teacher_agent()
    students = create_student_agents(students_data)

    if lesson_plans:
        for i, lp in enumerate(lesson_plans, 1):
            print(f"{i}. {lp['title']}")
        sel = int(input("請選擇教案編號: ")) - 1
        task_prompt = lesson_plans[sel]["initial_prompt"]
    else:
        task_prompt = input("請輸入討論任務: ")

    await sequential_group_discussion(teacher, students, task_prompt)

    # 收尾
    if _mysql_pool:
        _mysql_pool.close()
        await _mysql_pool.wait_closed()
    await model_client.close()


if __name__ == "__main__":
    asyncio.run(main())
