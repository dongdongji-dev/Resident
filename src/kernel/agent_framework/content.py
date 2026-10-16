"""Multimodal content helpers.

A message ``content`` is either a plain string or a list of OpenAI-style
blocks::

    {"type": "text", "text": "..."}
    {"type": "image_url", "image_url": {"url": "data:... | http(s)://..."}}

Blocks are stored verbatim (data URLs and remote URLs alike); the kernel
never parses, downloads or re-encodes image bytes. This module is the single
place that understands the mixed shape: events, context, protocol,
compression and adapters all go through it.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Union

Content = Union[str, list[dict[str, Any]]]

TEXT = "text"
IMAGE_URL = "image_url"

_DATA_PREFIX = "data:"
_URL_SUMMARY_CHARS = 200


def is_blocks(content: Any) -> bool:
    return isinstance(content, list)


def is_text_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == TEXT


def is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == IMAGE_URL


def has_image(content: Any) -> bool:
    """True when content carries at least one image block."""
    if is_blocks(content):
        return any(is_image_block(block) for block in content)
    return is_image_block(content)


def image_url(block: Any) -> str:
    """The URL of an image block (nested ``image_url.url`` or plain value)."""
    if not isinstance(block, dict):
        return ""
    raw = block.get("image_url")
    if isinstance(raw, dict):
        return str(raw.get("url") or "")
    return str(raw or "")


def _url_summary(url: str) -> str:
    url = str(url or "")
    if url.startswith(_DATA_PREFIX):
        return "data-url"
    if len(url) > _URL_SUMMARY_CHARS:
        return url[:_URL_SUMMARY_CHARS] + "…"
    return url


def placeholder(url: str, removed: bool = False) -> str:
    """Human-readable stand-in for an image block; never contains base64."""
    label = "图片已移除" if removed else "图片"
    return f"[{label}: {_url_summary(url)}]"


def _blocks_text(content: list, removed: bool) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == TEXT:
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
        elif is_image_block(block):
            parts.append(placeholder(image_url(block), removed))
    return "\n".join(parts)


def text_of(content: Any, removed: bool = False) -> str:
    """Flatten content to plain text; image blocks become placeholders."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _blocks_text([content], removed)
    if isinstance(content, list):
        return _blocks_text(content, removed)
    return str(content)


def gate_modalities(content: Any, allowed: Iterable[str]) -> Any:
    """Downgrade image blocks to placeholder text when ``image`` is not in
    ``allowed``; otherwise return content unchanged (blocks normalized to a
    list so adapters never see a bare block dict)."""
    if isinstance(content, dict):
        content = [content]
    allowed = tuple(str(item) for item in (allowed or ()))
    if "image" in allowed or not has_image(content):
        return content
    return text_of(content)


def strip_images(content: Any) -> str:
    """Permanently drop image blocks (413 degradation): flatten to text with
    ``图片已移除`` placeholders, preserving any text the message carried."""
    return text_of(content, removed=True)


def merge_contents(contents: list[Any], separator: str,
                   notice: str) -> Content:
    """Merge several event contents. Pure-text batches keep the historical
    string form; any batch containing blocks produces a block list with the
    notice first, per-event blocks in order and separators between events."""
    normalized = [[item] if isinstance(item, dict) else item
                  for item in contents]
    if not any(is_blocks(item) for item in normalized):
        body = separator.join(str(item) for item in normalized) \
            .strip(separator).strip()
        if notice:
            return f"{notice}{separator}{body}" if body else notice
        return body
    blocks: list[dict[str, Any]] = []
    if notice:
        blocks.append({"type": TEXT, "text": notice})
    for index, content in enumerate(normalized):
        if index:
            blocks.append({"type": TEXT, "text": separator})
        if is_blocks(content):
            for block in content:
                if isinstance(block, dict):
                    blocks.append(dict(block))
        else:
            blocks.append({"type": TEXT, "text": str(content)})
    return blocks


def content_preview(content: Any, limit: int = 120) -> str:
    """Log/telemetry-safe preview: image blocks collapse to placeholders."""
    text = text_of(content).replace("\n", " ")
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def content_bytes(content: Any) -> int:
    """Serialized byte size of content (UTF-8), for byte-budget admission.
    Plain strings count their UTF-8 bytes; blocks count their JSON form."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    try:
        return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(content).encode("utf-8"))
