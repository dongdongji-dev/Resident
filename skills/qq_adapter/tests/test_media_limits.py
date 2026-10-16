#!/usr/bin/env python3
r"""入站媒体自动下载上限 + 未落盘直链 + notice 提醒 回归测试。

三条设计约定：
  ① 设一个自动下载大小上限，超过多少 MB 不自动下载；
  ② 未落盘时消息里的路径改成**实际 url 直链**（只链）；
  ③ 下载失败要 **notice 提醒**（并给直链）。


本测试全部离线（不发起任何网络请求）。
运行：python3 .skills/qq_adapter/tests/test_media_limits.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_adapter import handlers, media, segments  # noqa: E402

MB = 1048576

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def _ctx(**kw):
    kw.setdefault("platform", "qq[10001]")
    kw.setdefault("current_conversation_id", "private_1000000002")
    return SimpleNamespace(**kw)


# ---------- ① 大小上限 ----------

@case("超上限：不下载、返回空、media_status=oversize")
def _():
    seg = {"type": "file", "data": {
        "file": "big.zip", "url": "http://example.invalid/big.zip",
        "size": 80 * MB}}
    path, md5 = asyncio.run(media.download_segment(
        seg, "private_1", None, None, max_bytes=50 * MB))
    assert (path, md5) == ("", ""), (path, md5)
    assert seg["data"]["media_status"] == "oversize", seg["data"]


@case("超上限时 url 原样保留（供渲染给直链）")
def _():
    seg = {"type": "file", "data": {
        "file": "big.zip", "url": "http://example.invalid/big.zip",
        "size": 51 * MB}}
    asyncio.run(media.download_segment(seg, "private_1", None, None,
                                       max_bytes=50 * MB))
    assert seg["data"]["url"] == "http://example.invalid/big.zip"


@case("图片用 file_size 判上限（不同段类型字段不同）")
def _():
    seg = {"type": "image", "data": {
        "file": "a.png", "url": "http://example.invalid/a.png",
        "file_size": 9 * MB}}
    path, _ = asyncio.run(media.download_segment(
        seg, "private_1", None, None, max_bytes=5 * MB))
    assert path == "" and seg["data"]["media_status"] == "oversize"


@case("max_bytes=None（不限制）时不因大小跳过")
def _():
    seg = {"type": "file", "data": {
        "file": "big.zip", "url": ""}}   # 无 url → 走 no_url，而不是 oversize
    asyncio.run(media.download_segment(seg, "private_1", None, None,
                                       max_bytes=None))
    assert seg["data"]["media_status"] == "no_url", seg["data"]


@case("声明大小缺失 → 不判上限（照常尝试）")
def _():
    seg = {"type": "file", "data": {"file": "x.bin", "url": ""}}
    asyncio.run(media.download_segment(seg, "private_1", None, None,
                                       max_bytes=1))
    assert seg["data"]["media_status"] == "no_url", seg["data"]


@case("声明大小非法字符串 → 视为未知（不跳过）")
def _():
    seg = {"type": "file", "data": {"file": "x.bin", "url": "",
                                    "size": "abc"}}
    asyncio.run(media.download_segment(seg, "private_1", None, None,
                                       max_bytes=1))
    assert seg["data"]["media_status"] == "no_url", seg["data"]


@case("_declared_size：size 优先，其次 file_size")
def _():
    assert media._declared_size({"size": 10}) == 10
    assert media._declared_size({"file_size": 20}) == 20
    assert media._declared_size({"size": 10, "file_size": 20}) == 10
    assert media._declared_size({}) == 0
    assert media._declared_size({"size": -3}) == 0


# ---------- ② 渲染（未落盘给直链） ----------

@case("已落盘 → url 用本地绝对路径")
def _():
    seg = {"type": "image", "data": {
        "file": "a.png", "url": "http://example.invalid/a.png",
        "local_path": "/data/files/qq/1/image/a.png"}}
    out = segments.render_segment(seg, _ctx())
    assert 'url="/data/files/qq/1/image/a.png"' in out, out


@case("未落盘但有 http url → url 用真实直链（本次核心）")
def _():
    seg = {"type": "file", "data": {
        "file": "big.zip", "url": "http://example.invalid/big.zip",
        "size": 80 * MB, "media_status": "oversize"}}
    out = segments.render_segment(seg, _ctx())
    assert 'url="http://example.invalid/big.zip"' in out, out
    assert "files/qq" not in out, out


@case("既无本地又无 http url → 退回内部占位（不崩）")
def _():
    seg = {"type": "image", "data": {"file": "a.png"}}
    out = segments.render_segment(seg, _ctx())
    assert 'url="files/qq[10001]/private_1000000002/image/a.png"' in out, out


# ---------- ③ notice 提醒 ----------

class FakeSubmitter:
    def __init__(self):
        self.notices = []

    def create_notice(self, content, notification=""):
        self.notices.append((content, notification))
        return {"id": 1}


def _notify(segs, max_mb=50, event=None):
    sub = FakeSubmitter()
    ctx = _ctx(submitter=sub, media_download_max_mb=max_mb)
    handlers._notify_media_issue(
        event or {"message_id": 123}, "private_1000000002", segs, ctx)
    return sub


@case("超上限 → 发 notice，含大小、上限、直链")
def _():
    sub = _notify([{"type": "file", "data": {
        "file": "big.zip", "url": "http://example.invalid/big.zip",
        "size": 80 * MB, "media_status": "oversize"}}])
    assert len(sub.notices) == 1, sub.notices
    content, notification = sub.notices[0]
    assert "超过自动下载上限（50 MB）" in content, content
    assert "80.0 MB" in content, content
    assert "http://example.invalid/big.zip" in content, content
    assert "big.zip" in notification, notification


@case("下载失败 → 发 notice，措辞为下载失败")
def _():
    sub = _notify([{"type": "record", "data": {
        "file": "v.amr", "media_status": "failed"}}])
    content, _ = sub.notices[0]
    assert "下载失败，未落盘" in content, content
    assert "大小未知" in content, content


@case("全部落盘（ok）→ 不发 notice")
def _():
    sub = _notify([{"type": "image", "data": {
        "file": "a.png", "local_path": "/x/a.png",
        "media_status": "ok"}}])
    assert sub.notices == [], sub.notices


@case("多个问题段 → 一条 notice 里都列出")
def _():
    sub = _notify([
        {"type": "file", "data": {"file": "a.zip", "size": 60 * MB,
                                  "media_status": "oversize"}},
        {"type": "video", "data": {"file": "b.mp4",
                                   "media_status": "failed"}},
    ])
    content, _ = sub.notices[0]
    assert "2 个段" in content, content
    assert "a.zip" in content and "b.mp4" in content, content


@case("没有 submitter（离线/测试环境）→ 静默返回不抛")
def _():
    handlers._notify_media_issue(
        {"message_id": 1}, "c", [{"type": "file", "data": {
            "file": "x", "media_status": "failed"}}],
        _ctx(media_download_max_mb=50))


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
