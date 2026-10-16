"""Segment renderers: onebot message segment -> llm_content fragment.

``SEGMENT_RENDERERS[type] -> renderer(seg, ctx)``; renderers return either
a plain string (the fragment) or a dict with ``text`` (fragment) + optional
``store`` side effects (media downloads etc, collected by the assembler).
Unknown segment types fall back to a placeholder - never break the message.

All user-controlled text is escaped (V2 rule: ``& < > " '``) before it
enters llm_content, preventing system-marker closure attacks.
"""

from __future__ import annotations

from typing import Any, Callable

from qq_adapter.logging_utils import get_logger

logger = get_logger("qq_adapter.segments")

SegmentRenderer = Callable[[dict[str, Any], Any], Any]

SEGMENT_RENDERERS: dict[str, SegmentRenderer] = {}


def register_segment(segment_type: str) -> Callable[[SegmentRenderer], SegmentRenderer]:
    def wrap(fn: SegmentRenderer) -> SegmentRenderer:
        SEGMENT_RENDERERS[segment_type] = fn
        return fn

    return wrap


def escape(text: Any) -> str:
    """Escape user-controlled text (V2 rule: & < > " ')."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


@register_segment("text")
def render_text(seg: dict[str, Any], ctx: Any) -> str:
    """正常文本：转义后原样显示。"""
    return escape((seg.get("data") or {}).get("text", ""))


@register_segment("at")
def render_at(seg: dict[str, Any], ctx: Any) -> str:
    """at：渲染为 <at qq=xxx/>（all = 全体成员）。"""
    qq = (seg.get("data") or {}).get("qq", "")
    return f'<at qq="{escape(qq)}"/>'


@register_segment("reply")
def render_reply(seg: dict[str, Any], ctx: Any) -> str:
    """reply：渲染为 <reply msg_id=xxx/>。"""
    msg_id = (seg.get("data") or {}).get("id", "")
    return f'<reply msg_id="{escape(msg_id)}"/>'


@register_segment("face")
@register_segment("mface")
def render_face(seg: dict[str, Any], ctx: Any) -> str:
    """表情：<face id="xxx"/> / <mface id="xxx"/>（V2 规则）。"""
    segment_type = seg.get("type", "face")
    face_id = (seg.get("data") or {}).get("id", "")
    return f'<{segment_type} id="{escape(face_id)}"/>'


@register_segment("image")
@register_segment("video")
@register_segment("record")
@register_segment("file")
def render_media(seg: dict[str, Any], ctx: Any) -> str:
    """媒体段：段内属性（file/url/summary/description）。url 指向系统内
    files/...（下载后真实存在）；description 识别后回填（暂无=未识别）。

    2026-08-25 整改：入站媒体已改为**提交前下载**（adapter 侧闭环，新 core
    无 PATCH 回填），下载成功时 url 直接用真实落盘绝对路径（local_path），
    只有未下载成功时才退回 files/ 内部路径占位。"""
    segment_type = seg.get("type", "")
    data = seg.get("data") or {}
    file_name = str(data.get("file", ""))
    platform = str(getattr(ctx, "platform", "") or "")
    conversation_id = str(getattr(ctx, "current_conversation_id", "") or "")
    internal_url = (f"files/{platform}/{conversation_id}/{segment_type}/"
                    f"{file_name}")
    # 已下载媒体（前置下载注入 local_path）→ 真实绝对路径
    local_path = str(data.get("local_path", ""))
    # 未落盘（超上限跳过 / 下载失败，2026-10-16 设计约定）→ 给**真实直链**，
    # 而不是 files/ 内部占位，这样我拿到的就是能直接下载的 url
    remote = str(data.get("url", ""))
    remote_url = remote if remote.startswith(("http://", "https://")) else ""
    url = local_path or remote_url or internal_url
    # 缩略信息（V2 语义）：summary 优先（如 [动画表情]），缺省按类型兜底
    summary = str(data.get("summary", ""))
    if not summary:
        summary = {"image": "[图片]", "video": "[视频]",
                   "record": "[语音]", "file": "[文件]"}.get(segment_type,
                                                             f"[{segment_type}]")
    description = str(data.get("description", "")) or "暂无"
    parts = [f'<{segment_type} file="{escape(file_name)}"',
             f'url="{escape(url)}"']
    if summary:
        parts.append(f'summary="{escape(summary)}"')
    parts.append(f'description="{escape(description)}"/>')
    return " ".join(parts)


def render_segment(seg: dict[str, Any], ctx: Any) -> str:
    """Render one segment (registry lookup + fallback placeholder)."""
    segment_type = seg.get("type", "")
    renderer = SEGMENT_RENDERERS.get(segment_type)
    if renderer is None:
        logger.debug("unknown segment type: %s", segment_type)
        return f"[未知段:{segment_type}]"
    result = renderer(seg, ctx)
    if isinstance(result, dict):
        return str(result.get("text", ""))
    return str(result)


@register_segment("json")
def render_json(seg: dict[str, Any], ctx: Any) -> str:
    """json 卡片（QQ 小程序/分享/红包等杂物类）：完整 JSON 结构渲染。

    直接输出 payload 全量（prompt/app/meta 等）——事件/工具通道有最大
    字符截断兜底，不怕长，模型可见全部信息。"""
    import json as _json
    data = seg.get("data") or {}
    payload = data.get("data")
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    if not isinstance(payload, dict):
        return "[json]"
    return _json.dumps(payload, ensure_ascii=False)


@register_segment("dice")
def render_dice(seg: dict[str, Any], ctx: Any) -> str:
    """骰子：{[骰子: result]}。"""
    result = (seg.get("data") or {}).get("result", "")
    return f"[骰子: {escape(result)}]"


@register_segment("rps")
def render_rps(seg: dict[str, Any], ctx: Any) -> str:
    """猜拳：1布 2剪刀 3石头（V2 映射）。"""
    mapping = {"1": "布", "2": "剪刀", "3": "石头"}
    result = str((seg.get("data") or {}).get("result", ""))
    return f"[猜拳: {mapping.get(result, escape(result))}]"


@register_segment("forward")
def render_forward(seg: dict[str, Any], ctx: Any) -> str:
    """合并转发：内联 content/messages 优先（递归 JSON 渲染，嵌套≤3）；
    转发内媒体段带内部 url（files/...）并同步入库（media enqueue）；
    无内联内容 → 占位 [转发消息]（adapter 异步 get_forward_msg 后回填）。"""
    data = seg.get("data") or {}
    nodes = data.get("messages") or data.get("content") or []
    if not nodes:
        return "[转发消息]"
    return render_forward_nodes(nodes, depth=0, ctx=ctx)


def render_forward_nodes(nodes: list[Any], depth: int,
                         ctx: Any = None) -> str:
    """转发渲染（V3 约定）：
    {"forward_count": N, "data": [消息节点|嵌套转发…]}

    - 消息节点：仅保留 msg_time/sender_data/content 三字段
      （转发消息的 msg_id 无效——不是本群消息，不引用）；
    - 嵌套转发：data 元素为 {forward_count, data} 递归（深度 ≤3）；
    - content 按段类型走各自处理逻辑（text 原文/媒体占位/json 渲染）。"""
    import json as _json
    data = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if "messages" in node and isinstance(node.get("messages"), list):
            inner = [n for n in node["messages"] if isinstance(n, dict)]
            data.append({"forward_count": len(inner), "data": [
                _render_forward_msg_node(n, depth + 1, ctx) for n in inner]})
        elif "content" in node and isinstance(node.get("content"), list):
            inner = [n for n in node["content"] if isinstance(n, dict)]
            data.append({"forward_count": len(inner), "data": [
                _render_forward_msg_node(n, depth + 1, ctx) for n in inner]})
        else:
            data.append(_render_forward_msg_node(node, depth, ctx))
    return _json.dumps({"forward_count": len(data), "data": data},
                       ensure_ascii=False)


def _render_forward_msg_node(node: dict[str, Any], depth: int,
                             ctx: Any = None) -> dict[str, Any]:
    """转发消息节点：{msg_time, sender_data, content}（仅三字段）。
    媒体段复用 render_media 渲染（带内部 files/ url）——转发内媒体
    随消息入库（core 递归提取 enqueue）。"""
    segments = node.get("message") or []
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        seg_data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(seg_data.get("text", "")))
        elif seg_type == "forward":
            if depth < 3:
                nodes = seg_data.get("messages") or seg_data.get("content") or []
                parts.append(render_forward_nodes(nodes, depth + 1, ctx))
            else:
                parts.append("[合并转发]该消息不支持查看")
        elif seg_type in ("image", "video", "record", "file"):
            parts.append(render_media(seg, ctx))
        elif seg_type in ("face", "mface"):
            parts.append("[表情]")
        elif seg_type == "json":
            import json as _json
            payload = seg_data.get("data")
            if isinstance(payload, str):
                try:
                    payload = _json.loads(payload)
                except (TypeError, ValueError):
                    payload = None
            if isinstance(payload, dict):
                prompt = str(payload.get("prompt", "")).strip()
                parts.append(f"[json:{prompt}]" if prompt else "[json]")
            else:
                parts.append("[json]")
        else:
            parts.append(f"[{seg_type}]")
    sender_obj = node.get("sender") or {}
    sender = {
        "nickname": str(node.get("sender_nickname")
                        or sender_obj.get("nickname") or ""),
        "user_id": str(node.get("sender_qq") or sender_obj.get("user_id")
                       or node.get("user_id") or ""),
    }
    return {"msg_time": str(node.get("time", "")),
            "sender_data": sender,
            "content": "".join(parts)}