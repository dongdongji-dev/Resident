#!/usr/bin/env python3
"""anthropic 协议（Claude）LLM 客户端自测 —— 真实打通 8318 → Go 上游。

用法:
    cd .skills/subagent && PYTHONPATH=src python3 tests/test_anthropic_llm.py

覆盖（均为极短 prompt，额度消耗可忽略）:
  1) 纯文本流式
  2) **usage 记账**：output_tokens 必须来自 message_delta（2026-10-14 修的点：
     只读 message_start 时 22 轮只统计到 107 输出 token），并归一化成
     prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens，
     否则 session.json 的 usage_total 永远是 0。
"""
from __future__ import annotations

import os

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kernel.agent_framework.llm import create_llm_client  # noqa: E402

TOKEN_FILE = Path("/home/sandbox/.secrets/llm_keys/proxy_api_token.key")
BASE_URL = os.environ.get("SUBAGENT_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
MODEL = "claude-haiku-5-5"


def make_client():
    return create_llm_client({
        "protocol": "anthropic",
        "base_url": BASE_URL,
        "api_key": TOKEN_FILE.read_text(encoding="utf-8").strip(),
        "model": MODEL,
        "timeout_seconds": 120,
        "retries": 1,
        "retry_delay_seconds": 1.0,
        "options": {"max_tokens": 256},
    })


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
    print(f"  [文本] text={result.text!r} deltas={len(deltas)} -> "
          f"{'OK' if ok else 'FAIL'}")
    return ok


async def case_usage() -> bool:
    """usage 三件事：输出数不为 0、归一化字段齐、cached 字段存在。"""
    client = make_client()
    result = await client.stream(
        [{"role": "system", "content": "只回答一个词。"},
         {"role": "user", "content": "用一个词形容冬天。"}],
        tools=[],
    )
    u = result.usage or {}
    out = int(u.get("output_tokens") or 0)
    mapped = all(k in u for k in (
        "prompt_tokens", "completion_tokens", "prompt_tokens_details"))
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    ok = out > 0 and mapped and isinstance(cached, int)
    print(f"  [记账] usage={u}")
    print(f"          output_tokens={out}（>0 才是 message_delta 生效）"
          f" 归一化字段齐={mapped} cached={cached} -> {'OK' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    print(f"anthropic 客户端自测 → {BASE_URL} model={MODEL}")
    results = [await case_text(), await case_usage()]
    print(f"\n{'ALL PASS' if all(results) else '有失败项'}"
          f" ({sum(results)}/{len(results)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
