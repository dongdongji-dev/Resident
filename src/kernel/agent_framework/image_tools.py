"""readimage — native vision ingestion for the Agent (local venv only).

The Agent may discover image files inside its sandbox workspace. Calling
``readimage(path)`` reads the file on the HOST side of the workspace mount
(no container/docker involved), verifies it decodes, compresses it with
Pillow to fit the byte budget, and pushes one user event carrying an
OpenAI-style ``image_url`` block. The image is visible to the model on the
NEXT round (the current round's LLM call has already returned).

Byte-budget rules (all from YAML, no hidden defaults):

- re-read decoding first: path safety, size cap and Pillow verification all
  happen BEFORE any compression work (no wasted CPU);
- single-image target = ``min(max_base64_bytes, raw base64 length,
  remaining context budget)``;
- if the remaining context budget cannot hold even ``min_base64_bytes`` the
  tool fails immediately and asks the Agent to wait for compression;
- the final admission is still ``Agent.push_event`` (exact byte check), so a
  race is reported as a normal tool failure instead of overfilling context.
"""

from __future__ import annotations

import base64
import functools
import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import certifi
from PIL import Image

from kernel.agent_framework.events import EventTooLargeError
from kernel.agent_framework.logging_setup import get_logger
from kernel.agent_framework.paths import (PathEscapeError, WorkspacePath,
                                          open_file_inside)

logger = get_logger("kernel.agent_framework.image_tools")

# Formats forwarded verbatim when they already fit the target (no quality
# loss); everything else is re-encoded to JPEG by the compressor.
_PASSTHROUGH_FORMATS = ("image/png", "image/jpeg", "image/webp", "image/gif")

# 图片入队时的文字标签（runtime_block 注入说明共用）
READIMAGE_LABEL_TEMPLATE = "（readimage: {label}）"

_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
]


@dataclass(frozen=True, slots=True)
class ReadImageConfig:
    enabled: bool
    # "local"：base64 直接入队；"imgbed"：上传图床后以 URL 入队
    mode: str
    max_input_bytes: int
    max_edge: int
    jpeg_quality: int
    quality_floor: int
    max_base64_bytes: int
    min_base64_bytes: int
    # 预算预检的保守包装开销（text+image 块包装；权威校验在 push_event）
    block_overhead_bytes: int
    # 外部 URL 读取（下载后走同一压缩/入队流程）
    allow_url: bool
    # SSRF 防护：false 时拒绝回环/内网/链路本地/保留地址（含重定向后的最终地址）
    allow_private_hosts: bool
    url_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ImgBedConfig:
    """Generic multipart image-bed client config (different beds vary in
    endpoint, auth header, form field and response link field)."""
    enabled: bool
    upload_url: str
    api_key: str
    api_key_header: str
    form_field: str
    response_url_field: str
    timeout_seconds: float
    # 额外尝试次数（总尝试 = retries + 1），针对网络类瞬时错误
    retries: int


def _dotted_get(mapping: Any, path: str) -> Any:
    value = mapping
    for part in str(path or "").split("."):
        if not part or not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/svg+xml": ".svg",
}


def _filename_for(mime: str, filename: str) -> str:
    """Filename whose extension matches the actual payload type — some
    image beds (415 content does not match declared type) compare the part's
    filename/type against the bytes (e.g. a PNG compressed to JPEG must not
    keep its ``.png`` name)."""
    stem = os.path.splitext(os.path.basename(filename or "image"))[0]
    stem = stem or "image"
    ext = _EXT_BY_MIME.get(str(mime or "").lower(), "")
    if not ext:
        ext = os.path.splitext(filename or "")[1] or ".bin"
    return stem + ext


class ImgBedClient:
    """Upload one image via multipart/form-data; return the public URL taken
    from ``response_url_field`` (dotted path, e.g. ``url`` / ``data.url``)."""

    def __init__(self, config: ImgBedConfig,
                 user_agent: str = "resident-agent/1.0") -> None:
        self.config = config
        self.user_agent = user_agent
        # 框架版 Python 常缺系统 CA；与 LLM 适配器一致用 certifi
        self.ssl_context = ssl.create_default_context(
            cafile=certifi.where())
        # 显式直连：urllib 默认会读 macOS 系统代理（mihomo 7890），实测
        # 并发上传时间歇性 EOF/超时；图床是公网直连可达的，绕过系统代理。
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self.ssl_context))

    def upload(self, data: bytes, mime: str, filename: str) -> str:
        last: Optional[BaseException] = None
        attempts = max(0, int(self.config.retries)) + 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                url = self._upload_once(data, mime, filename)
                elapsed = time.monotonic() - started
                if elapsed > 3.0:
                    logger.warning("imgbed upload slow: %.2fs (%d bytes)",
                                   elapsed, len(data))
                elif attempt:
                    logger.info("imgbed upload recovered on retry %d "
                                "(%.2fs)", attempt + 1, elapsed)
                return url
            except (urllib.error.HTTPError, RuntimeError):
                # 服务器已响应（4xx/5xx/响应格式错误）：重试无意义
                raise
            except Exception as exc:  # noqa: BLE001 — 网络类瞬时错误
                last = exc
                elapsed = time.monotonic() - started
                if attempt < attempts - 1:
                    logger.warning(
                        "imgbed upload retry %d/%d after %.2fs: %s",
                        attempt + 1, attempts - 1, elapsed, exc)
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"图床请求失败: {last}") from last

    def _upload_once(self, data: bytes, mime: str, filename: str) -> str:
        boundary = "----resident" + uuid.uuid4().hex
        # 以字节为准声明类型/后缀（图床校验 content vs declared type）
        actual_mime = mime_of(data) or mime or "application/octet-stream"
        safe_name = _filename_for(actual_mime, filename)
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{self.config.form_field}";'
            f' filename="{safe_name}"\r\n'
            f"Content-Type: {actual_mime}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + data + tail
        headers = {
            self.config.api_key_header: self.config.api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": self.user_agent,
            "Content-Length": str(len(body)),
        }
        request = urllib.request.Request(
            self.config.upload_url, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(
                    request, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read()
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"图床 HTTP {exc.code}: {detail}") from exc
        if status != 200:
            raise RuntimeError(f"图床 HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise RuntimeError("图床响应不是 JSON") from exc
        url = _dotted_get(payload, self.config.response_url_field)
        if not url or not str(url).startswith(("http://", "https://")):
            raise RuntimeError(
                f"图床响应缺少链接字段 {self.config.response_url_field!r}")
        return str(url)


def b64_len(nbytes: int) -> int:
    """Exact base64 character count for ``nbytes`` input bytes."""
    return 4 * ((int(nbytes) + 2) // 3)


def mime_of(data: bytes) -> str:
    if data.startswith(b"RIFF") and len(data) >= 12:
        if data[8:12] in (b"WEBP",):
            return "image/webp"
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    return ""


def sniff_and_verify(data: bytes) -> str:
    """Return the image mime when ``data`` really decodes as an image,
    otherwise ''. Used by the HTTP upload endpoint."""
    mime = mime_of(data)
    if not mime:
        return ""
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
    except Exception:  # noqa: BLE001
        return ""
    return mime


_TLS_CONTEXT: Optional[ssl.SSLContext] = None


def _tls_context() -> ssl.SSLContext:
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        _TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return _TLS_CONTEXT


def _is_private_host(host: str) -> bool:
    """True when the host resolves to a loopback/private/link-local/reserved
    address (or cannot be resolved at all) — SSRF protection."""
    value = str(host or "").strip().strip("[]")
    if not value:
        return True
    try:
        infos = socket.getaddrinfo(value, None)
    except OSError:
        return True
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Dial a pre-validated IP; the Host header still uses the URL host."""

    def __init__(self, host: str, *, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Same as above for TLS; SNI/certificate still use the URL host."""

    def __init__(self, host: str, *, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req: Any) -> Any:
        factory = functools.partial(_PinnedHTTPConnection,
                                    pinned_ip=self._pinned_ip)
        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__(context=_tls_context())
        self._pinned_ip = pinned_ip

    def https_open(self, req: Any) -> Any:
        factory = functools.partial(_PinnedHTTPSConnection,
                                    pinned_ip=self._pinned_ip)
        return self.do_open(factory, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None  # 重定向上层手动逐跳处理（每跳重新解析+校验）


def _resolve_public_ip(hostname: str) -> str:
    """Resolve ONCE and validate; returns the address to pin. Any private /
    loopback / link-local / reserved result refuses the request — the same
    policy as before, but the validated address is reused for the actual
    connection, so a second DNS answer (rebinding) can never be dialed."""
    value = str(hostname or "").strip().strip("[]")
    if not value:
        raise ValueError("图片地址缺少主机名")
    try:
        infos = socket.getaddrinfo(value, None)
    except OSError as exc:
        raise ValueError(f"域名解析失败：{value}") from exc
    pinned = ""
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"无法识别的地址：{address}") from exc
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError("拒绝访问内网/回环地址"
                             "（readimage.allow_private_hosts=false）")
        if not pinned:
            pinned = address
    if not pinned:
        raise ValueError(f"域名解析不到地址：{value}")
    return pinned


def _pinned_opener(scheme: str, pinned_ip: str) -> Any:
    """Opener whose connections dial ONLY the validated IP (no proxy env:
    a proxy would re-resolve the name and defeat the pinning)."""
    if scheme == "https":
        return urllib.request.build_opener(
            _PinnedHTTPSHandler(pinned_ip), urllib.request.ProxyHandler({}),
            _NoRedirect())
    return urllib.request.build_opener(
        _PinnedHTTPHandler(pinned_ip), urllib.request.ProxyHandler({}),
        _NoRedirect())


def _read_capped(resp: Any, config: ReadImageConfig) -> bytes:
    length = resp.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > config.max_input_bytes:
        raise ValueError(
            f"远程图片 {length} 字节超过上限 "
            f"{config.max_input_bytes} 字节")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > config.max_input_bytes:
            raise ValueError(
                f"远程图片超过上限 {config.max_input_bytes} 字节")
        chunks.append(chunk)
    return b"".join(chunks)


def _download_plain(url: str, config: ReadImageConfig) -> bytes:
    """Direct download when the user explicitly allows private hosts."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "resident-agent/1.0", "Accept": "image/*"})
    try:
        with urllib.request.urlopen(
                request, timeout=config.url_timeout_seconds,
                context=_tls_context()) as resp:
            return _read_capped(resp, config)
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        raise ValueError(f"下载失败 HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"下载失败：{exc}") from exc


def _download_image(url: str, config: ReadImageConfig) -> bytes:
    """Download an external image with scheme/host/size guards. Raises
    ValueError with a user-facing reason.

    防 SSRF 的方式：每一跳先解析（getaddrinfo）→ 校验 → **把校验过的 IP
    钉在连接上**（Host/SNI 仍用域名），域名不会被第二次解析，DNS rebinding
    无从生效；重定向手动逐跳处理，每一跳都重新解析+校验。
    allow_private_hosts=true 时退回普通 urlopen。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http(s) 图片地址：{url}")
    if config.allow_private_hosts:
        return _download_plain(url, config)
    current = url
    for _ in range(5):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"仅支持 http(s) 图片地址：{current}")
        pinned_ip = _resolve_public_ip(parsed.hostname or "")
        opener = _pinned_opener(parsed.scheme, pinned_ip)
        request = urllib.request.Request(current, headers={
            "User-Agent": "resident-agent/1.0", "Accept": "image/*"})
        try:
            resp = opener.open(request,
                               timeout=config.url_timeout_seconds)
        except ValueError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = str(exc.headers.get("Location") or "").strip()
                if not location:
                    raise ValueError(
                        f"下载失败：重定向缺少 Location"
                        f"（HTTP {exc.code}）") from exc
                current = urllib.parse.urljoin(current, location)
                continue
            raise ValueError(f"下载失败 HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"下载失败：{exc}") from exc
        with resp:
            return _read_capped(resp, config)
    raise ValueError("重定向次数过多，已中止")


def _flatten(img: Image.Image) -> Image.Image:
    """RGB canvas; alpha composites over white (JPEG has no alpha)."""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _jpeg_bytes(img: Image.Image, quality: int) -> bytes:
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def compress_to_base64_target(data: bytes, target_b64: int, max_edge: int,
                              q_start: int,
                              q_floor: int) -> Optional[bytes]:
    """Compress to ``<= target_b64`` base64 characters, or return None when
    even the smallest sensible encoding cannot fit."""
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001 — caller already verified, be safe
        return None
    img = _flatten(img)
    width, height = img.size
    if max(width, height) > max_edge:
        scale = max_edge / max(width, height)
        img = img.resize((max(1, int(width * scale)),
                          max(1, int(height * scale))), Image.LANCZOS)
    while True:
        quality = int(q_start)
        while True:
            buf = _jpeg_bytes(img, quality)
            if b64_len(len(buf)) <= target_b64:
                return buf
            if quality <= q_floor:
                break
            quality = max(int(q_floor), quality - 10)
        width, height = img.size
        if min(width, height) <= 64:
            return None
        img = img.resize((max(1, int(width * 0.8)),
                          max(1, int(height * 0.8))), Image.LANCZOS)


def prepare_image(data: bytes, target_b64: int,
                  config: ReadImageConfig) -> tuple[Optional[bytes], str]:
    """Return (payload, mime) fitting ``target_b64``; original bytes pass
    through untouched when already small enough and in a supported format."""
    raw_mime = mime_of(data)
    if raw_mime in _PASSTHROUGH_FORMATS and b64_len(len(data)) <= target_b64:
        return data, raw_mime
    payload = compress_to_base64_target(
        data, target_b64, config.max_edge,
        config.jpeg_quality, config.quality_floor)
    if payload is None:
        return None, ""
    return payload, "image/jpeg"


def make_readimage(agent: Any, workspace: WorkspacePath,
                   config: ReadImageConfig,
                   input_modalities: tuple[str, ...],
                   imgbed: Optional[Any] = None) -> Any:
    modalities = tuple(str(item) for item in (input_modalities or ()))
    use_imgbed = str(config.mode).strip().lower() == "imgbed"

    def readimage(path: str) -> str:
        """读取图片并加入对话（下一轮即可看到该图片）。

        参数:
          path: 图片来源。可以是沙箱工作区内的文件路径（如
            /home/sandbox/...），也可以是外部 http(s) 图片地址（需
            readimage.allow_url=true；受 SSRF 与大小限制保护）。

        用途: 需要查看图片内容时调用。图片处理后作为消息进入对话队列：
        本地模式压缩到预算内后内联 base64；图床模式不做压缩、上传原图后
        放 URL。如果上下文剩余预算不足（或模型未开启图片输入），会返回
        失败并说明原因，此时请改用 media_vision 技能或调用 context_compress
        等待压缩。

        返回: 成功/失败的文字说明。
        """
        if not config.enabled:
            return "读取失败：readimage 未启用（config readimage.enabled=false）"
        if "image" not in modalities:
            return ("读取失败：当前模型未开启图片输入（llm.input 不含 image）；"
                    "请使用 media_vision 技能识别图片内容")
        source = str(path or "").strip()
        if not source:
            return "读取失败：path 不能为空"
        # 1) 获取原始字节：外部 URL 下载 / 工作区文件读取（先读入再压缩）
        if source.startswith(("http://", "https://")):
            if not config.allow_url:
                return ("读取失败：未开启外部图片读取"
                        "（readimage.allow_url=false）")
            try:
                raw = _download_image(source, config)
            except ValueError as exc:
                return f"读取失败：{exc}"
            filename = os.path.basename(
                urllib.parse.urlparse(source).path) or "external"
            label = source
        else:
            try:
                host = workspace.to_host(source)
            except PathEscapeError:
                return f"读取失败：路径不在工作区内：{source}"
            except Exception as exc:  # noqa: BLE001
                return f"读取失败：{exc}"
            if not os.path.isfile(host):
                return f"读取失败：文件不存在或不是普通文件：{source}"
            try:
                size = os.path.getsize(host)
            except OSError as exc:
                return f"读取失败：{exc}"
            if size > config.max_input_bytes:
                return (f"读取失败：原图 {size} 字节超过上限 "
                        f"{config.max_input_bytes} 字节")
            try:
                with open_file_inside(host, workspace.host_root,
                                      "rb") as fh:
                    raw = fh.read()
            except (OSError, ValueError) as exc:
                return f"读取失败：{exc}"
            filename = os.path.basename(host)
            label = source
        if len(raw) > config.max_input_bytes:
            return (f"读取失败：图片 {len(raw)} 字节超过上限 "
                    f"{config.max_input_bytes} 字节")
        # 2) 解码校验（不是图片直接失败，不做无用压缩）
        try:
            with Image.open(BytesIO(raw)) as probe:
                probe.verify()
        except Exception:  # noqa: BLE001
            return "读取失败：不是可识别的图片（解码失败）"
        raw_b64 = b64_len(len(raw))
        if use_imgbed:
            # 图床模式：请求体只有 URL，不需要为省字节压缩；直接上传原图
            # （只做解码校验与 max_input_bytes 上限）
            payload, mime = raw, (mime_of(raw) or "application/octet-stream")
        else:
            # 3) 预算预检：剩余放不下最小可用图 → 提示等压缩
            remaining = int(agent.context_max_bytes) \
                - int(agent.pending_bytes())
            allowed = remaining - config.block_overhead_bytes
            if allowed < config.min_base64_bytes:
                return ("读取失败：上下文剩余字节预算不足；"
                        "请先调用 context_compress 工具触发压缩，"
                        "等待压缩完成后再读取图片")
            # 4) 目标 = min(单图上限, 当前图片字节, 剩余预算)
            target = min(config.max_base64_bytes, raw_b64, allowed)
            payload, mime = prepare_image(raw, target, config)
        if payload is None or not mime:
            return "读取失败：图片无法压缩进目标字节，请等待上下文压缩后再试"
        if use_imgbed:
            if imgbed is None:
                return "读取失败：图床模式已开启但 imgbed 未配置"
            try:
                url = imgbed.upload(payload, mime, filename)
            except Exception as exc:  # noqa: BLE001
                return f"读取失败：上传图床失败：{exc}"
        else:
            url = ("data:" + mime + ";base64,"
                   + base64.b64encode(payload).decode("ascii"))
        # 5) 入队（最终字节准入在 push_event 内精确校验）
        try:
            agent.push_event({
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": READIMAGE_LABEL_TEMPLATE.format(label=label)},
                    {"type": "image_url",
                     "image_url": {"url": url}},
                ],
                "kind": "readimage",
                "source": "runtime",
            })
        except EventTooLargeError as exc:
            return (f"读取失败：{exc}；"
                    "请先调用 context_compress 工具触发压缩后再重试")
        except Exception as exc:  # noqa: BLE001
            return f"读取失败：入队异常：{exc}"
        logger.info("readimage queued source=%s mode=%s raw=%d url=%d",
                    label, config.mode, len(payload), len(url))
        if use_imgbed:
            return (f"读取成功：图片已上传图床并加入对话队列（{url}），"
                    "下一轮你将直接看到该图片")
        return (f"读取成功：图片已加入对话队列（base64 {len(url)} 字节），"
                "下一轮你将直接看到该图片")

    return readimage
