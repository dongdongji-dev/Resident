#!/usr/bin/env python3
r"""submit 失败分支「落原始响应体」回归测试（2026-10-17 建）。

背景：2026-10-17 08:52~08:56 给群 200000003 发长消息连续被拒（`message_id` 空、
库无 out 记录），当时失败分支只打异常摘要、丢掉了 NapCat / core 的响应体，
事后只能二分猜。本次改动两处：
  ① `message.py::MessageSubmitter.submit()` 遇 HTTPError 时打印响应体（前 500 字）；
  ② `actions.py` 发送返回空 `message_id` 时打印原始 response。

运行：python3 .skills/qq_adapter/tests/test_submit_failure_log.py
"""
from __future__ import annotations

import io
import logging
import sys
import urllib.error

sys.path.insert(0, "/home/sandbox/.skills/qq_adapter/src")

from qq_adapter import message as message_mod  # noqa: E402
from qq_adapter.message import MessageDraft, MessageSubmitter  # noqa: E402

ACTIONS_SRC = "/home/sandbox/.skills/qq_adapter/src/qq_adapter/actions.py"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _BadBody:
    def read(self) -> bytes:
        raise OSError("boom")

    def close(self) -> None:  # 避免解释器退出时 tempfile 的 GC 噪声
        return None


def _submitter() -> MessageSubmitter:
    return MessageSubmitter("http://127.0.0.1:1", "dummy-key", "qq[10001]")


def _draft() -> MessageDraft:
    return MessageDraft(platform="qq[10001]", conversation_id="group_1",
                        conversation_type="group", remote_message_id="m1")


def _capture() -> tuple[_Capture, logging.Logger]:
    handler = _Capture()
    logger = logging.getLogger("qq_adapter.message")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return handler, logger


def _patch(fn) -> object:
    orig = message_mod.urllib.request.urlopen
    message_mod.urllib.request.urlopen = fn
    return orig


def case_normal() -> int:
    """正常提交：返回值透传，且不打错误日志。"""
    handler, logger = _capture()
    orig = _patch(lambda *a, **k: _Resp(b'{"id": 7}'))
    try:
        result = _submitter().submit(_draft())
    finally:
        message_mod.urllib.request.urlopen = orig
        logger.removeHandler(handler)
    assert result == {"id": 7}, result
    assert not [x for x in handler.lines if "submit failed" in x], handler.lines
    print("✓ 正常提交：返回值透传、无错误日志")
    return 0


def case_http_error_body() -> int:
    """边界：HTTPError 必须把响应体打进日志。"""
    handler, logger = _capture()
    err = urllib.error.HTTPError(
        "http://x/api/messages", 400, "Bad Request", {},
        io.BytesIO(b'{"detail":"bad payload"}'))
    orig = _patch(lambda *a, **k: (_ for _ in ()).throw(err))
    try:
        result = _submitter().submit(_draft())
    finally:
        message_mod.urllib.request.urlopen = orig
        logger.removeHandler(handler)
    assert result is None, result
    hits = [x for x in handler.lines if "submit failed" in x]
    assert hits, handler.lines
    assert '{"detail":"bad payload"}' in hits[0], hits[0]
    print("✓ 边界：HTTPError 响应体已落日志")
    return 0


def case_unreadable_body() -> int:
    """边界：响应体读不出来时也不能让异常分支再炸。"""
    handler, logger = _capture()
    err = urllib.error.HTTPError(
        "http://x/api/messages", 500, "Server Error", {}, _BadBody())
    orig = _patch(lambda *a, **k: (_ for _ in ()).throw(err))
    try:
        result = _submitter().submit(_draft())
    finally:
        message_mod.urllib.request.urlopen = orig
        logger.removeHandler(handler)
    assert result is None, result
    hits = [x for x in handler.lines if "submit failed" in x]
    assert hits, handler.lines
    assert "<body unreadable>" in hits[0], hits[0]
    print("✓ 边界：坏响应体走 <body unreadable>，不二次抛错")
    return 0


def case_actions_guard_present() -> int:
    """出站侧：发送返回空 message_id 时必须落原始 response（源码级断言）。"""
    src = open(ACTIONS_SRC, encoding="utf-8").read()
    assert "send returned no message_id" in src, "actions.py 缺少空 id 告警分支"
    assert src.count("send returned no message_id") == 1, "该分支应只有一处"
    print("✓ 出站侧：空 message_id 告警分支存在且唯一")
    return 0


def main() -> int:
    cases = (case_normal, case_http_error_body, case_unreadable_body,
             case_actions_guard_present)
    failed = 0
    for case in cases:
        try:
            case()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {case.__name__}: {exc}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
