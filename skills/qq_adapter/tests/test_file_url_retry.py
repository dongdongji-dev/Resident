#!/usr/bin/env python3
r"""handlers._try_fetch_file_url 重试回归测试。

背景：napcat `get_private_file_url` 偶发单次 15s 超时 → adapter 拿不到 url
→ 文件不落盘（日志 `file url fetch failed` → `segment file no url, skip file=...`）。
该 API 属偶发慢，值得重试。本测试断言重试逻辑。


本测试只碰 `_try_fetch_file_url` 的重试逻辑（fake client），不发任何真实请求。
运行：python3 .skills/qq_adapter/tests/test_file_url_retry.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_adapter import handlers  # noqa: E402

PRIVATE_EVENT = {"message_type": "private", "user_id": 1000000002}
GROUP_EVENT = {"message_type": "group", "group_id": 200000003}
SEG = {"type": "file", "data": {"file": "aaa.txt", "file_id": "aaa.txt"}}


class FakeClient:
    """按序返回预设结果：Exception 实例＝抛错；dict＝api 返回；None＝无 url。"""

    def __init__(self, seq: list):
        self.seq = list(seq)
        self.calls: list[tuple[str, dict]] = []

    async def api(self, action, params):
        self.calls.append((action, params))
        nxt = self.seq.pop(0) if self.seq else None
        if isinstance(nxt, Exception):
            raise nxt
        if nxt is None:
            return {"status": "ok", "retcode": 0, "data": {}}
        return nxt


def run(seq, event=PRIVATE_EVENT, seg=SEG, attempts=3, sleep=0.0):
    client = FakeClient(seq)
    ctx = SimpleNamespace(client=client)
    real_sleep = asyncio.sleep
    if sleep == 0.0:
        asyncio.sleep = lambda *_a, **_k: real_sleep(0)  # 测试里不真等退避
    try:
        url = asyncio.run(handlers._try_fetch_file_url(event, seg, ctx,
                                                       attempts=attempts))
    finally:
        asyncio.sleep = real_sleep
    return url, client


OK = {"status": "ok", "retcode": 0,
      "data": {"url": "http://example.invalid/x?a=1"}}

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("首次成功 → 只调一次")
def _():
    url, c = run([OK])
    assert url == OK["data"]["url"], url
    assert len(c.calls) == 1, c.calls


@case("前两次超时、第三次成功 → 调用 3 次并返回 url（本次修复的核心场景）")
def _():
    url, c = run([TimeoutError("onebot api timeout"), TimeoutError("again"), OK])
    assert url == OK["data"]["url"], url
    assert len(c.calls) == 3, c.calls
    assert c.calls[0][0] == "get_private_file_url", c.calls
    assert c.calls[0][1] == {"user_id": 1000000002, "file_id": "aaa.txt"}


@case("全部失败 → 返回空串且调用次数＝attempts")
def _():
    url, c = run([TimeoutError("t"), TimeoutError("t"), TimeoutError("t")])
    assert url == "", url
    assert len(c.calls) == 3, c.calls


@case("返回里没有 url → 视为失败并重试")
def _():
    url, c = run([{"status": "ok", "retcode": 0, "data": {}}, OK])
    assert url == OK["data"]["url"], url
    assert len(c.calls) == 2, c.calls


@case("attempts=1 时退化为不重试（保持旧行为可用）")
def _():
    url, c = run([TimeoutError("t"), OK], attempts=1)
    assert url == "", url
    assert len(c.calls) == 1, c.calls


@case("群文件走 get_group_file_url 且带 group_id")
def _():
    url, c = run([OK], event=GROUP_EVENT)
    assert url == OK["data"]["url"]
    assert c.calls[0][0] == "get_group_file_url", c.calls
    assert c.calls[0][1] == {"group_id": 200000003, "file_id": "aaa.txt"}


@case("没有 file_id / file → 不调 API，直接返回空串")
def _():
    url, c = run([OK], seg={"type": "file", "data": {}})
    assert url == "" and c.calls == []


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {name}\n    {exc}")
        else:
            print(f"✓ {name}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
