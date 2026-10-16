"""Media downloader: inbound image/record/file/video segments -> local.

New architecture dropped the core-side media pipeline (data/files root,
media_* tools, PATCH backfill). The adapter must download media itself
so the agent can read the actual files from the workspace.

Dedup by content: files are stored as ``{md5}{ext}`` under

    <workspace>/group_{conversation_id}/{type}/{md5}{ext}

Same image sent twice (by anyone) stores only one copy. The downloader
returns (local_path, md5); callers may record the md5 in an index.
Failure never blocks message submission.

2026-09-13 整改（大文件入站一律超时）:
- 旧实现用**阻塞式** ``urllib.request.urlopen``，且整体被 handlers 的
  12s ``asyncio.wait_for`` 包住 → ①6.9MB/78MB 的 PDF 必然超时
  （实证 msg 1389753804 / 139783277，09-08 三连超时同因）；
  ②阻塞调用期间整个事件循环停摆（心跳/其他入站事件都被卡）。
- 现改为 **aiohttp 异步流式**下载（不占用事件循环），并把超时改成
  **按段类型**可配（``timeouts`` 参数，默认见 DEFAULT_TIMEOUTS）：
  image 30s / record 120s / video 180s / file 600s。
- 调用方（handlers）用 ``budget_for(types)`` 取"本条消息该等多久"，
  不再用写死的 12s。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import aiohttp

from qq_adapter.logging_utils import get_logger

logger = get_logger("qq_adapter.media")

MEDIA_TYPES = ("image", "record", "video", "file")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (NapCat OneBot11 adapter)",
    "Accept": "*/*",
}

# 入站媒体落盘根目录（换部署/换账号时用环境变量覆盖，别改代码）
_WORKSPACE_ROOT = os.environ.get(
    "QQ_ADAPTER_MEDIA_ROOT", "/home/sandbox/adapter_data/qq_adapter")

# 每类型"下载总超时"（秒）。file 段动辄几 MB~上百 MB，旧 12s 全局预算
# 必然失败；单列放宽。可在 config/napcat.yaml 的 media_download.timeouts
# 覆盖（键=类型，值=秒）。
DEFAULT_TIMEOUTS: dict[str, float] = {
    "image": 30.0,
    "record": 120.0,
    "video": 180.0,
    "file": 600.0,
}
# file 段补拉真实 url（get_*_file_url API）额外预留的等待秒数
URL_FETCH_SLACK = 20.0
# 大文件进度日志步长
_PROGRESS_STEP = 20 * 1024 * 1024
_CHUNK = 1 << 16


def timeout_for(s_type: str, timeouts: dict | None = None) -> float:
    """取某媒体类型的下载超时（秒）。``timeouts`` 为调用方覆盖表。"""
    table = dict(DEFAULT_TIMEOUTS)
    for key, value in (timeouts or {}).items():
        try:
            table[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return float(table.get(str(s_type), DEFAULT_TIMEOUTS["file"]))


def budget_for(types: Any, timeouts: dict | None = None) -> float:
    """一条消息的媒体下载总预算 = 各段类型超时的最大值（含 url 补拉余量）。
    无媒体段 → 0。"""
    wanted = [str(t) for t in (types or []) if str(t) in MEDIA_TYPES]
    if not wanted:
        return 0.0
    budget = max(timeout_for(t, timeouts) for t in wanted)
    if "file" in wanted:
        budget += URL_FETCH_SLACK
    return budget


def _sanitize_name(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", str(name or "media"))
    return name[:160]


def _ext_of(file_name: str, url: str = "") -> str:
    """取扩展名（含点，小写）；无则按类型兜底。"""
    raw = file_name or url.split("?")[0]
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", raw)
    if m:
        return "." + m.group(1).lower()
    return ""


async def _stream_to_file(url: str, tmp_path: str, timeout: float,
                          s_type: str, src_name: str) -> int:
    """aiohttp 流式下载到 tmp_path（异步，不阻塞事件循环）。返回字节数。"""
    client_timeout = aiohttp.ClientTimeout(
        total=timeout, sock_connect=15.0, sock_read=90.0)
    written = 0
    next_log = _PROGRESS_STEP
    async with aiohttp.ClientSession(timeout=client_timeout,
                                     headers=HEADERS) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    fh.write(chunk)
                    written += len(chunk)
                    if written >= next_log:
                        logger.info("media downloading type=%s file=%s %.1fMB",
                                    s_type, src_name, written / 1048576)
                        next_log += _PROGRESS_STEP
    return written


async def download_segment(seg: dict[str, Any], conversation_id: str,
                           loop: Any = None,
                           timeouts: dict | None = None,
                           max_bytes: int | None = None) -> tuple[str, str]:
    """下载一段媒体 → 按 md5 命名入库。返回 (本地绝对路径, md5)；
    失败或无 url 返回 ("", "")。``timeouts`` 可按类型覆盖超时（秒）；
    ``max_bytes`` >0 且段声明的 size 超过它时**不下载**。

    无论走哪条分支，都在 ``seg['data']['media_status']`` 留一个状态：
    ``ok`` / ``oversize``（超上限跳过）/ ``no_url`` / ``failed``——
    调用方（handlers）据此决定是否发 notice、渲染是否给直链。"""
    data = seg.get("data") or {}
    s_type = str(seg.get("type", ""))
    if s_type not in MEDIA_TYPES:
        return "", ""
    src_name = _sanitize_name(data.get("file", ""))
    url = str(data.get("url", ""))
    # 大小上限：声明大小已知且超限 → 不下载（data.url 保留真实直链供渲染）
    declared = _declared_size(data)
    if max_bytes and declared and declared > max_bytes:
        data["media_status"] = "oversize"
        logger.info("media skip oversize type=%s file=%s size=%d max=%d",
                    s_type, src_name, declared, max_bytes)
        return "", ""
    if not url:
        data["media_status"] = "no_url"
        logger.info("segment %s no url, skip file=%s", s_type, src_name)
        return "", ""
    ext = _ext_of(src_name, url)
    timeout = timeout_for(s_type, timeouts)
    # 目标目录
    conv_dir = conversation_id if \
        conversation_id.startswith(("group_", "private_")) else \
        f"group_{conversation_id}"
    target_dir = os.path.join(_WORKSPACE_ROOT, conv_dir, s_type)
    tmp_path = os.path.join(target_dir, ".tmp_" + (src_name or "media"))
    try:
        os.makedirs(target_dir, exist_ok=True)
        written = await _stream_to_file(url, tmp_path, timeout, s_type,
                                        src_name)
        # 计算 md5 → 最终文件名
        md5 = _md5_of(tmp_path)
        final_path = os.path.join(target_dir, md5 + ext)
        if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            # 已存在（同内容去重）：删临时文件复用
            os.remove(tmp_path)
        else:
            os.replace(tmp_path, final_path)
        size = os.path.getsize(final_path)
        data["media_status"] = "ok"
        logger.info("media saved type=%s md5=%s size=%d path=%s",
                    s_type, md5, size, final_path)
        return final_path, md5
    except Exception as exc:  # noqa: BLE001
        data["media_status"] = "failed"
        logger.warning("media download failed type=%s file=%s timeout=%.0fs: %s",
                       s_type, src_name, timeout, exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return "", ""


def _declared_size(data: dict[str, Any]) -> int:
    """段里声明的大小（字节）：file 段是 ``size``，图片等是 ``file_size``。
    没有/非法 → 0（无上限判据，照常下载）。"""
    for key in ("size", "file_size"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def download_message_media(segments: list[dict],
                                 conversation_id: str,
                                 loop: Any = None,
                                 timeouts: dict | None = None,
                                 max_bytes: int | None = None) -> dict:
    """下载一条消息的全部媒体段。返回 {type: {md5: local_path}} 映射，
    并原地给 seg['data'] 写入 local_path / md5 / media_status。"""
    results: dict[str, dict[str, str]] = {}
    for seg in (segments or []):
        s_type = seg.get("type", "")
        if s_type not in MEDIA_TYPES:
            continue
        path, md5 = await download_segment(seg, conversation_id, loop,
                                           timeouts, max_bytes)
        if path and md5:
            seg.setdefault("data", {})["local_path"] = path
            seg["data"]["md5"] = md5
            results.setdefault(s_type, {})[md5] = path
    return results
