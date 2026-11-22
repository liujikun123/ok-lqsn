"""ok-script MCP 本地客户端（开发用）。

框架自带的 MCP 服务只监听 127.0.0.1，需要 Bearer token 才能连。
官方文档建议：没有原生 MCP 客户端时，用 Python SDK 写一个本地脚本 —— 就是这个。

用法（在 bsg-auto 目录下）：
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py list_tasks
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py screenshot
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py run_task --file src/tasks/AutoTask.py
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py status --run <run_id>
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py stop --run <run_id>
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py reload
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py skills
    ../ok-bsg/.venv/Scripts/python.exe tools/mcp_client.py skill --id develop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def load_connection(project_dir: str) -> dict:
    """从 logs/mcp/connection.json 读 URL 和 token。"""
    path = os.path.join(project_dir, "logs", "mcp", "connection.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"找不到 {path}\n先启动服务：\n"
            f"  ../ok-bsg/.venv/Scripts/python.exe -m ok mcp --config src.config:config"
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _show(result):
    """把工具返回内容尽量友好地打出来。"""
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
            except Exception:
                print(text)
    sc = getattr(result, "structuredContent", None)
    if sc:
        print(json.dumps(sc, ensure_ascii=False, indent=2))


async def call_tool(conn: dict, name: str, args: dict):
    url = conn["url"]
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {conn['token']}"}, timeout=600.0
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(name, args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action")
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--file")
    ap.add_argument("--run")
    ap.add_argument("--id")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--reason", default="dev")
    args = ap.parse_args()

    conn = load_connection(args.project)
    print(f"[mcp] {conn['url']}", file=sys.stderr)

    if args.action == "list_tasks":
        name, payload = "list_tasks", {}
    elif args.action == "skills":
        name, payload = "list_skills", {}
    elif args.action == "screenshot":
        name, payload = "screenshot", {}
    elif args.action == "run_task":
        if not args.file:
            raise SystemExit("--file 必填")
        name, payload = "run_task", {"file_path": os.path.abspath(args.file)}
    elif args.action == "status":
        if not args.run:
            raise SystemExit("--run 必填")
        name, payload = "get_run_status", {"run_id": args.run}
    elif args.action == "stop":
        if not args.run:
            raise SystemExit("--run 必填")
        name, payload = "stop_task", {"run_id": args.run}
    elif args.action == "reload":
        name, payload = "reload_tasks", ({"task_ids": args.ids} if args.ids else {})
    elif args.action == "skill":
        if not args.id:
            raise SystemExit("--id 必填")
        name, payload = "get_skill", {"skill_id": args.id}
    elif args.action == "exit":
        name, payload = "exit_process", {"reason": args.reason}
    else:
        raise SystemExit(f"未知动作 {args.action}")

    result = asyncio.run(call_tool(conn, name, payload))
    _show(result)


if __name__ == "__main__":
    main()
