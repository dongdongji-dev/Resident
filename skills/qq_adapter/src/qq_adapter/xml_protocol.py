"""XML action protocol parser: <action>...</action> -> executor dict.

Old architecture: the core parsed the XML and POSTed the resolved dict
to the adapter's /hook. New architecture (Resident Kernel): no core-side
parsing anymore - the adapter receives the raw XML (or JSON) directly
from the agent and resolves it into the same dict structure the
ActionExecutor expects:

    {"elements": [{"tag": ..., "attributes": {...}, "content": [
        {"text": ...} | {"tag": ..., "attributes": {...}, "content": [...]}
    ]}]}

Text inside an element yields {"text": ...} nodes; child elements yield
{"tag"/"attributes"/"content"} nodes. Works for both <text>...</text>
mixed content and one-shot <action> documents.
"""

from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree as ET


def _parse_node(node: ET.Element) -> dict[str, Any]:
    """One XML element -> {"tag", "attributes", "content"} (mixed)."""
    content: list[Any] = []
    for child in node:
        if child.tag == "text" and not child.attrib and \
                len(list(child)) == 0:
            # 纯文本子节点 → {"text": ...}
            content.append({"text": child.text or ""})
        else:
            content.append(_parse_node(child))
        # 元素间的文本（<a>xx<b/>yy</a> 的 xx/yy）
        if child.tail and child.tail.strip():
            content.append({"text": child.tail.strip()})
    # 节点自身的文本（<text>hello</text> 的 hello）
    if not content and node.text and node.text.strip():
        content.append({"text": node.text.strip()})
    return {
        "tag": node.tag,
        "attributes": dict(node.attrib),
        "content": content,
    }


def parse_action_xml(xml_str: str) -> dict[str, Any]:
    """Parse a full <action> document -> executor action dict."""
    root = ET.fromstring(xml_str)
    elements = [_parse_node(child) for child in root]
    return {"elements": elements, "platform": root.attrib.get("platform", "")}


def parse_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve an incoming /hook payload into an executor action dict.

    Supports:
    - payload["xml"]         : raw <action> XML string
    - payload["action"]      : already-resolved action dict (pass-through)
    - payload["elements"]    : bare elements list
    """
    raw_xml = payload.get("xml") or payload.get("action_xml")
    if raw_xml:
        return parse_action_xml(str(raw_xml))
    action = payload.get("action")
    if isinstance(action, dict):
        return action
    elements = payload.get("elements")
    if isinstance(elements, list):
        return {"elements": elements}
    raise ValueError(
        "无法解析动作：请提供 xml=<action>...</action> 或 "
        "action/elements JSON 结构")


def dump_action_xml(action: dict[str, Any]) -> str:
    """Inverse (debug/audit): executor action dict -> compact XML."""
    lines = []
    for elem in action.get("elements") or []:
        lines.append(_dump_node(elem, 0))
    return "<action%s>\n%s\n</action>" % (
        f' platform="{action.get("platform", "")}"' if action.get("platform") else "",
        "\n".join(lines))


def _dump_node(node: dict[str, Any], depth: int) -> str:
    pad = "  " * depth
    attrs = "".join(
        f' {k}="{str(v).replace("&", "&amp;").replace("<", "&lt;")}"'
        for k, v in (node.get("attributes") or {}).items())
    content = node.get("content") or []
    if not content:
        return f"{pad}<{node.get('tag', '')}{attrs}/>"
    inner = []
    for item in content:
        if isinstance(item, dict):
            if "text" in item and len(item) == 1:
                inner.append(_esc(str(item["text"])))
            else:
                inner.append(_dump_node(item, depth + 1))
    return (f"{pad}<{node.get('tag', '')}{attrs}>\n"
            + "\n".join(inner) + f"\n{pad}</{node.get('tag', '')}>")


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
