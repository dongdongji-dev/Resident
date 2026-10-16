#!/usr/bin/env python3
"""google 协议（Gemini）LLM 客户端自测 —— 真实打通 8318 → Go 上游。

用法:
    cd .skills/subagent && PYTHONPATH=src python3 tests/test_gemini_google_llm.py

覆盖:
  1) 纯文本流式（含 thinking 分流）
  2) 工具调用解析（functionCall → ToolCall）
  3) 工具结果回传后继续（functionResponse → 再出文本）

每次只发极短 prompt，额度消耗可忽略。
"""
from __future__ import annotations

import os

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kernel.agent_framework.llm import create_llm_client  # noqa: E402

TOKEN_FILE = Path("/home/sandbox/.secrets/llm_keys/proxy_api_token.key")
BASE_URL = os.environ.get("SUBAGENT_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
MODEL = "gemini-4-argon-contributor"


def make_client():
    return create_llm_client({
        "protocol": "google",
        "base_url": BASE_URL,
        "api_key": TOKEN_FILE.read_text(encoding="utf-8").strip(),
        "model": MODEL,
        "timeout_seconds": 180,
        "retries": 1,
        "retry_delay_seconds": 1.0,
        "options": {"max_tokens": 1024},
    })


TOOLS = [{
    "type": "function",
    "function": {
        "name": "echo",
        "description": "把文本原样返回",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}]


async def case_text() -> bool:
    client = make_client()
    deltas = []
    result = await client.stream(
        [{"role": "system", "content": "你只回答一个数字，不要解释。"},
         {"role": "user", "content": "1+1=?"}],
        tools=[],
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )
    ok = "2" in (result.text or "")
    print(f"  [文本] text={result.text!r} thinking_len={len(result.thinking)} "
          f"usage={result.usage} deltas={len(deltas)} -> {'OK' if ok else 'FAIL'}")
    return ok


async def case_tool_call() -> tuple[bool, list]:
    client = make_client()
    result = await client.stream(
        [{"role": "system", "content": "必须调用工具，不要凭空回答。"},
         {"role": "user", "content": "用 echo 工具把 hello 原样返回。"}],
        tools=TOOLS,
    )
    calls = result.tool_calls
    ok = bool(calls) and calls[0].name == "echo"
    print(f"  [工具] calls={[(c.name, c.arguments) for c in calls]} "
          f"text={result.text[:60]!r} -> {'OK' if ok else 'FAIL'}")
    return ok, calls


async def case_tool_result(calls: list) -> bool:
    if not calls:
        print("  [工具回传] 跳过（上一步没拿到调用）")
        return False
    client = make_client()
    call = calls[0]
    messages = [
        {"role": "system", "content": "必须调用工具，不要凭空回答。"},
        {"role": "user", "content": "用 echo 工具把 hello 原样返回。"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call.call_id, "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
            **({"signature": call.signature} if call.signature else {})}]},
        {"role": "tool", "tool_call_id": call.call_id, "name": call.name,
         "content": json.dumps({"result": "hello"})},
        {"role": "user", "content": "工具返回了什么？只回那个词。"},
    ]
    assert call.signature, "Gemini 未返回 thoughtSignature —— 回传会 400"
    result = await client.stream(messages, tools=TOOLS)
    ok = "hello" in (result.text or "").lower()
    print(f"  [工具回传] text={result.text[:120]!r} -> {'OK' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    print(f"== google 客户端自测（model={MODEL} via {BASE_URL}）")
    results = [await case_text()]
    ok_tool, calls = await case_tool_call()
    results.append(ok_tool)
    results.append(await case_tool_result(calls))
    print(f"== {sum(1 for r in results if r)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
