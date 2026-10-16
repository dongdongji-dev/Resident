"""Outbound action execution: hook action -> napcat API -> out 消息回写.

Symmetric to the inbound MessageDraft: a parsed <action> document
(elements: message blocks with conversation_id + segments) is translated
into OneBot11 message segments, sent via the WS api(), and the successful
delivery is reported back as an out message (POST /api/v1/messages,
direction=out, remote_message_id = napcat 返回的 message_id).

Media sending (pure protocol, deployment-agnostic - adapter and napcat
may live on different servers, NO container mounts):
- image/record: base64:// (OneBot standard; verified working);
- file: small (<= base64_max_bytes) -> upload_group_file(file=base64://)
  then the file segment carries the returned file_id;
  large (> base64_max_bytes) -> the adapter serves the file over HTTP
  (FileServer) and the file segment carries the URL (napcat fetches it;
  verified working).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .whitelist import Whitelist
from . import outbound

logger = logging.getLogger("qq_adapter.actions")


class ArbitrationRejected(Exception):
    """仲裁拒绝（未读超阈值且未 force_exec）：主动发言被打断保护。"""


class ActionExecutor:
    def __init__(self, client, submitter, whitelist: Whitelist,
                 files_root: str = "data/files",
                 base64_max_bytes: int = 5 * 1024 * 1024,
                 serve_url: str = "",
                 core_url: str = "", core_api_key: str = "",
                 unread_threshold: int = 5, platform: str = "",
                 actions: dict | None = None,
                 attention: Any = None) -> None:
        self._client = client          # OneBotClient（api 调用）
        self._submitter = submitter    # MessageSubmitter（out 上报）
        self._whitelist = whitelist
        self._files_root = Path(files_root)
        self._base64_max_bytes = int(base64_max_bytes)
        # adapter 文件服务对外 URL 前缀（大文件 URL 直传；如
        # 形如 http://<对外可达地址>:<端口>）
        self._serve_url = str(serve_url or "").rstrip("/")
        # 仲裁：发送前查 core 会话未读数（force_exec=true 屏蔽仲裁）
        self._core_url = str(core_url or "").rstrip("/")
        self._core_api_key = str(core_api_key or "")
        self._unread_threshold = int(unread_threshold)
        self._platform = platform
        self._actions = dict(actions or {})
        self._attention = attention  # AttentionStore（set/get_attention 动作）
        self._loop = None
        # 出站上报的 sender（get_login_info 后由 set_self_identity 补全）
        self._self_sender: dict[str, Any] = {"identity_id": "agent"}

    def attach_loop(self, loop) -> None:
        """绑定 WS 连接所属事件循环（动作执行必须在该循环内调度）。"""
        self._loop = loop

    def set_platform(self, platform: str) -> None:
        """登录信息获取后更新平台实例标识（仲裁/上报用实际 QQ）。"""
        self._platform = platform

    def set_self_identity(self, self_id: str, nickname: str = "") -> None:
        """登录信息获取后回填 bot 自身身份——出站上报 sender_data 用。

        出站消息有两条入库路径：①/hook 上报（本模块 _report_out）、
        ②NapCat message_sent 回显（handlers）。①原先把 sender 写死成
        {"identity_id": "agent"}，②带完整 sender，于是同一批消息在库里
        出现两种格式（2026-10-09 约定发现）。这里把 ① 也补全，两条路径一致。
        """
        sender: dict[str, Any] = {"identity_id": "agent"}
        if self_id:
            sender = {
                "user_id": int(self_id) if str(self_id).isdigit() else self_id,
                "nickname": nickname or "",
                "card": "",
                "identity_id": "agent",
            }
        self._self_sender = sender

    async def _arbitrate(self, attributes: dict[str, Any],
                         conversation_id: str) -> None:
        """主动发言仲裁：message force_exec=true 屏蔽；否则查 core 会话未读
        数，超阈值拒绝（防高频打断）。core 不可达/查询失败 → 放行。"""
        force = str(attributes.get("force_exec", "")).lower() in (
            "true", "1", "yes")
        if force:
            return
        if not self._core_url:
            return  # 无 core 地址（独立测试/离线）→ 不仲裁
        unread = await self._query_unread(conversation_id)
        if unread is None:
            return  # 查询失败 → 放行（仲裁不可用不阻断）
        logger.info("arbitration check conv=%s unread=%s threshold=%s "
                    "force=%s", conversation_id, unread,
                    self._unread_threshold, force)
        if unread > self._unread_threshold:
            raise ArbitrationRejected(
                f"会话 {conversation_id} 未读 {unread} 条，请先读取未读消息"
                f"了解全貌后再回复，勿鲁莽发言（确认必须立即发送可标注 "
                f'force_exec="true" 强制执行）')

    async def _query_unread(self, conversation_id: str) -> int | None:
        try:
            import json
            import aiohttp
            headers = {"Authorization": f"Bearer {self._core_api_key}"} \
                if self._core_api_key else {}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        self._core_url + "/api/messages/unread?limit=200",
                        headers=headers, timeout=aiohttp.ClientTimeout(5)) \
                        as response:
                    body = await response.text()
                    logger.info("unread query url=%s status=%s body=%s",
                                response.url, response.status, body[:150])
                    if response.status != 200:
                        return None
                    payload = json.loads(body or "{}")
                    unread_list = payload.get("unread") or []
                    return sum(1 for m in unread_list
                               if m.get("conversation_id") == conversation_id)
        except Exception:  # noqa: BLE001
            logger.warning("unread query failed: %s", conversation_id,
                           exc_info=True)
            return None

    def execute_sync(self, action: dict[str, Any]) -> dict[str, Any]:
        """跨线程入口（HTTP 回调线程）：投递到主事件循环执行并等待结果。"""
        import asyncio
        if self._loop is None:
            return {"ok": False, "errors": ["executor 未绑定事件循环"]}
        future = asyncio.run_coroutine_threadsafe(self.execute(action),
                                                  self._loop)
        try:
            return future.result(timeout=90)
        except Exception as exc:  # noqa: BLE001
            logger.error("action execute timeout/failed: %s", exc)
            return {"ok": False, "errors": [str(exc)]}

    async def execute(self, action: dict[str, Any]) -> dict[str, Any]:
        """执行一个解析后的 <action>：逐元素分派（message/poke 发送；
        kick/ban/query 等管理查询操作）。返回 {ok, sent, errors}。"""
        results: list[dict[str, Any]] = []
        for index, element in enumerate(
                action.get("elements") or [], start=1):
            tag = element.get("tag", "")
            if not self._is_enabled(tag):
                logger.warning("action disabled by config: %s", tag)
                results.append(self._element_result(
                    element, index, None,
                    [f"动作 {tag} 未启用（config/napcat.yaml actions 段"
                     f"未打开，或高风险动作默认禁用）"]))
                continue
            handler = getattr(self, f"_action_{tag}", None)
            if handler is None:
                logger.warning("unsupported action element: %s", tag)
                results.append(self._element_result(
                    element, index, None,
                    [f"不支持的 action 元素: {tag}"]))
                continue
            try:
                result = await handler(element)
                results.append(self._element_result(element, index,
                                                    result, None))
                self._after_action(tag, element, result)
            except ArbitrationRejected as exc:
                logger.warning("arbitration rejected: %s", exc)
                results.append(self._element_result(
                    element, index, None, [f"仲裁拒绝：{exc}"]))
            except Exception as exc:  # noqa: BLE001
                logger.error("action element %s failed: %s", tag, exc)
                results.append(self._element_result(
                    element, index, None, [str(exc)]))
        return {"ok": all(item.get("ok") for item in results),
                "result": results}

    # 发送类动作：成功后自动把注意力切到目标群（自动跟随）
    SEND_AUTO_FOLLOW_TAGS = frozenset((
        "send_messages", "message", "send_forward",
        "send_forward_nodes", "send_group_notice",
    ))

    def _after_action(self, tag: str, element: dict[str, Any],
                      result: dict[str, Any] | None) -> None:
        """出站动作成功后的注意力联动（2026-09-01 约定方案）：
        - 发送类动作成功且目标是群会话 → follow() 自动切注意力到该群
          （该群新消息无需 @ 即唤醒）；
        - 其他针对群会话的成功动作 → touch() 刷新当前注意力群的
          活跃时间戳（防 10 分钟闲置误过期）。
        注意：follow 只在发送成功时发生（异常已在 execute 捕获跳过）。"""
        if self._attention is None:
            return
        if not isinstance(result, dict):
            return
        conversation_id = str(result.get("conversation_id", ""))
        if not conversation_id:
            attrs = element.get("attributes") or {}
            conversation_id = str(attrs.get("conversation_id", "")) or \
                str(element.get("conversation_id", ""))
        if not conversation_id.startswith("group_"):
            return
        if tag in self.SEND_AUTO_FOLLOW_TAGS:
            self._attention.follow(conversation_id)
        else:
            self._attention.touch(conversation_id)

    def _is_enabled(self, action: str) -> bool:
        """action 级开关：配置未打开直接拒绝（不执行）。"""
        return bool(self._actions.get(str(action), True))

    def _element_result(self, element: dict[str, Any], index: int,
                        result: dict[str, Any] | None,
                        errors: list[str] | None) -> dict[str, Any]:
        """紧凑结果项（编译器报错式定位）：模型仅需知道第几个元素、
        开头特征。index=元素序号（1-based）；head=元素序列化开头前
        48 有效字符（任何 tag 通用，含属性与文本开头）。成功带
        message_id（发送）或 result（查询数据）；失败带 errors。"""
        item: dict[str, Any] = {"ok": errors is None, "index": index,
                                "head": self._element_head(element)}
        if errors:
            item["errors"] = errors
            return item
        if isinstance(result, dict) and result.get("message_id"):
            item["message_id"] = str(result["message_id"])
        elif isinstance(result, dict) and "result" in result:
            # 查询类：携带数据（同步工具直接给模型）
            item["result"] = result["result"]
        return item

    def _element_head(self, element: dict[str, Any],
                      limit: int = 48) -> str:
        """元素开头特征：<tag 属性...> + 首个 text 文本开头。"""
        tag = str(element.get("tag", ""))
        attrs = element.get("attributes") or {}
        head = "<" + tag
        if attrs:
            head += " " + " ".join(
                f'{key}="{value}"' for key, value in attrs.items())
        head += ">"
        for seg in element.get("content") or []:
            if isinstance(seg, dict) and seg.get("tag") == "text":
                text = self._text_of(seg)
                if text:
                    head += text
                break
        return head[:limit]

    async def _action_message(self, element: dict[str, Any]) -> dict[str, Any]:
        """message 元素（兼容旧名）→ send_messages 语义。"""
        return await self._send_message_element(element)

    async def _action_send_messages(self, element: dict[str, Any]) -> dict[str, Any]:
        """send_messages 元素：消息容器（QQ 文本框语义）。子元素段：text
        （内可混排 reply/at/face/image/sticker）/poke/image/record/video/
        file/sticker/face/dice/rps/json。"""
        return await self._send_message_element(element)

    async def _action_poke(self, element: dict[str, Any]) -> dict[str, Any]:
        """独立 poke 元素（非消息段）：戳指定 QQ。"""
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or \
            str(element.get("conversation_id", ""))
        target = str(attrs.get("target_id", ""))
        if not conversation_id or not target:
            raise ValueError("poke 需要 conversation_id 和 target_id")
        conversation_type = "group" if conversation_id.startswith("group_") \
            else "private"
        self._whitelist.guard_outbound(conversation_type, conversation_id)
        if conversation_type == "group":
            group_id = conversation_id[len("group_"):]
            await self._client.api(
                "group_poke", {"group_id": int(group_id),
                               "user_id": int(target)})
        else:
            await self._client.api("friend_poke", {"user_id": int(target)})
        import time as _t
        mid = f"poke_out_{int(_t.time())}_{target}"
        self._report_out(conversation_id, conversation_type, mid,
                         [{"type": "poke", "data": {"target_id": target}}])
        return {"conversation_id": conversation_id, "message_id": mid}

    # ── 管理/审批/删除（异步） ────────────────────────────────────────

    async def _action_kick(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("kick 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):]
                        if conversation_id.startswith("group_") else attrs["group_id"])
        await self._client.api("set_group_kick", {
            "group_id": group_id, "user_id": int(attrs["user_id"]),
            "reject_add_request": bool(attrs.get("reject_add", False))})
        return {"conversation_id": conversation_id,
                "message_id": ""}

    async def _action_ban(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("ban 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):]
                        if conversation_id.startswith("group_") else attrs["group_id"])
        if str(attrs.get("whole", "")).lower() in ("true", "1", "yes"):
            # 全员禁言开关（【需管理员权限】）
            await self._client.api("set_group_whole_ban", {
                "group_id": group_id,
                "enable": bool(str(attrs.get("enable", "true")).lower()
                               in ("true", "1", "yes"))})
            return {"conversation_id": conversation_id, "message_id": ""}
        await self._client.api("set_group_ban", {
            "group_id": group_id, "user_id": int(attrs["user_id"]),
            "duration": int(attrs.get("duration", 0))})
        return {"conversation_id": conversation_id,
                "message_id": ""}

    async def _action_set_card(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("set_card 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):])
        await self._client.api("set_group_card", {
            "group_id": group_id, "user_id": int(attrs["user_id"]),
            "card": str(attrs.get("card", ""))})
        return {"conversation_id": conversation_id, "message_id": ""}

    async def _action_set_group_name(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("set_group_name 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):])
        await self._client.api("set_group_name", {
            "group_id": group_id,
            "group_name": str(attrs.get("name", ""))})
        return {"conversation_id": conversation_id, "message_id": ""}

    async def _action_set_essence(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("set_essence 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):])
        await self._client.api("set_essence_msg", {
            "group_id": group_id,
            "message_id": int(attrs["message_id"])})
        return {"conversation_id": conversation_id, "message_id": ""}

    async def _action_delete_essence(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        if not conversation_id:
            raise ValueError("delete_essence 需要 conversation_id（group_xxx）")
        group_id = int(conversation_id[len("group_"):])
        await self._client.api("delete_essence_msg", {
            "group_id": group_id,
            "message_id": int(attrs["message_id"])})
        return {"conversation_id": conversation_id, "message_id": ""}

    async def _action_delete_msg(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        await self._client.api("delete_msg",
                               {"message_id": int(attrs["message_id"])})
        return {"message_id": str(attrs.get("message_id", ""))}

    async def _action_handle_friend_request(
            self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        await self._client.api("set_friend_add_request", {
            "flag": str(attrs["flag"]),
            "approve": bool(attrs.get("approve", True)),
            "remark": str(attrs.get("remark", ""))})
        return {"message_id": ""}

    async def _action_handle_group_request(
            self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        await self._client.api("set_group_add_request", {
            "flag": str(attrs["flag"]),
            "approve": bool(attrs.get("approve", True)),
            "reason": str(attrs.get("reason", "")),
            "sub_type": str(attrs.get("sub_type", "add"))})
        return {"message_id": ""}

    # ── 查询（同步：快、确定成功） ────────────────────────────────────

    async def _action_get_group_list(self, element: dict[str, Any]) -> dict[str, Any]:
        result = await self._client.api("get_group_list", {})
        data = result.get("data") or []
        return {"result": data, "message_id": ""}

    async def _action_get_group_member_list(
            self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        group_id = int(conversation_id[len("group_"):]) if conversation_id \
            else int(attrs["group_id"])
        result = await self._client.api("get_group_member_list",
                                        {"group_id": group_id})
        return {"result": result.get("data") or [], "message_id": ""}

    async def _action_get_group_member_info(
            self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        group_id = int(conversation_id[len("group_"):]) if conversation_id \
            else int(attrs["group_id"])
        result = await self._client.api("get_group_member_info", {
            "group_id": group_id, "user_id": int(attrs["user_id"])})
        return {"result": result.get("data") or {}, "message_id": ""}

    async def _action_get_msg(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        result = await self._client.api("get_msg",
                                        {"message_id": int(attrs["message_id"])})
        return {"result": result.get("data") or {}, "message_id": ""}

    async def _action_get_group_msg_history(
            self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = str(attrs.get("conversation_id", "")) or             str(element.get("conversation_id", ""))
        group_id = int(conversation_id[len("group_"):]) if conversation_id \
            else int(attrs["group_id"])
        result = await self._client.api("get_group_msg_history", {
            "group_id": group_id,
            "count": int(attrs.get("count", 20))})
        return {"result": result.get("data") or [], "message_id": ""}

    async def _action_set_attention(
            self, element: dict[str, Any]) -> dict[str, Any]:
        """切换注意力群：conversation_id="group_xxx"（空串/缺省=关闭）。
        该群新消息一律走 wakeup，无需 @；QQ 同时只有一个注意力群。"""
        if self._attention is None:
            raise ValueError("attention store 未初始化")
        attrs = element.get("attributes") or {}
        element_conversation_id = element.get("conversation_id")
        has_attr = "conversation_id" in attrs or element_conversation_id is not None
        conversation_id = str(attrs.get("conversation_id", "")
                              or element_conversation_id or "").strip()
        if not has_attr:
            raise ValueError("set_attention 需要 conversation_id"
                             "（group_xxx；显式传空串=关闭注意力）")
        if not conversation_id:
            # 显式空串 → 关闭注意力（不唤醒任意群）
            previous = self._attention.set("")
            return {"result": {"previous": previous, "current": ""},
                    "message_id": ""}
        if not conversation_id.startswith("group_"):
            raise ValueError("set_attention 仅支持群会话（group_xxx）："
                             f"收到 {conversation_id}")
        previous = self._attention.set(conversation_id)
        return {"result": {"previous": previous,
                           "current": conversation_id},
                "message_id": ""}

    async def _action_get_attention(
            self, element: dict[str, Any]) -> dict[str, Any]:
        """查询当前注意力群（空串=未设置），含闲置剩余秒数。"""
        if self._attention is None:
            raise ValueError("attention store 未初始化")
        return {"result": self._attention.info(),
                "message_id": ""}

    # ── 通用解析辅助 ──────────────────────────────────────────────

    def _group_id_of(self, attrs: dict[str, Any],
                     element: dict[str, Any]) -> int:
        """conversation_id（group_xxx）或 group_id 属性 → 群号。"""
        conversation_id = str(attrs.get("conversation_id", ""))
        if conversation_id.startswith("group_"):
            return int(conversation_id[len("group_"):])
        if conversation_id.startswith("private_"):
            raise ValueError("该操作仅支持群会话（group_xxx）")
        if attrs.get("group_id"):
            return int(attrs["group_id"])
        raise ValueError("需要 conversation_id（group_xxx）")

    def _user_id_of(self, attrs: dict[str, Any]) -> int:
        if not attrs.get("user_id"):
            raise ValueError("需要 user_id 属性")
        return int(attrs["user_id"])

    def _conversation_of(self, attrs: dict[str, Any],
                         element: dict[str, Any]) -> str:
        return str(attrs.get("conversation_id", "")) or \
            str(element.get("conversation_id", ""))

    async def _query(self, action: str, params: dict[str, Any],
                     default: Any = None) -> dict[str, Any]:
        result = await self._client.api(action, params)
        return {"result": result.get("data", default), "message_id": ""}

    # ── 发送类新增 ────────────────────────────────────────────────

    async def _action_send_forward(self, element: dict[str, Any]) -> dict[str, Any]:
        """单条转发：message_id + 目标会话（conversation_id）。"""
        attrs = element.get("attributes") or {}
        conversation_id = self._conversation_of(attrs, element)
        message_id = str(attrs.get("message_id", ""))
        if not message_id or not conversation_id:
            raise ValueError("send_forward 需要 conversation_id 和 message_id")
        params = {"message_id": message_id}
        if conversation_id.startswith("group_"):
            params["group_id"] = int(conversation_id[len("group_"):])
        else:
            params["user_id"] = int(conversation_id[len("private_"):])
        await self._client.api("forward_friend_single_msg", params)
        return {"conversation_id": conversation_id, "message_id": ""}

    async def _action_send_forward_nodes(self, element: dict[str, Any]) -> dict[str, Any]:
        """合并转发：conversation_id + <node> 段列表。"""
        attrs = element.get("attributes") or {}
        conversation_id = self._conversation_of(attrs, element)
        if not conversation_id:
            raise ValueError("send_forward_nodes 需要 conversation_id")
        nodes = []
        for seg in element.get("content") or []:
            if not isinstance(seg, dict) or seg.get("tag") != "node":
                continue
            nattrs = seg.get("attributes") or {}
            inner = [inner_seg for inner_seg in seg.get("content") or []
                     if isinstance(inner_seg, dict) and inner_seg.get("tag")]
            onebot_inner, _ = self._assemble_segments(
                inner, "group", conversation_id)
            node = {"type": "node", "data": {
                "user_id": str(nattrs.get("sender_id", "")),
                "nickname": str(nattrs.get("sender_name", ""))}}
            node["data"]["content"] = onebot_inner or []
            nodes.append(node)
        if not nodes:
            raise ValueError("send_forward_nodes 需要至少一个 <node> 段")
        params = {"message": nodes}
        if conversation_id.startswith("group_"):
            params["message_type"] = "group"
            params["group_id"] = int(conversation_id[len("group_"):])
        else:
            params["message_type"] = "private"
            params["user_id"] = int(conversation_id[len("private_"):])
        result = await self._client.api("send_forward_msg", params)
        data = result.get("data") or {}
        return {"conversation_id": conversation_id,
                "message_id": str(data.get("message_id", ""))}

    # ── 群管理新增 ────────────────────────────────────────────────

    async def _action_set_admin(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        await self._client.api("set_group_admin", {
            "group_id": group_id, "user_id": self._user_id_of(attrs),
            "enable": bool(str(attrs.get("enable", "true")).lower()
                           in ("true", "1", "yes"))})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_set_special_title(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("title"):
            raise ValueError("set_special_title 需要 title 属性")
        await self._client.api("set_group_special_title", {
            "group_id": group_id, "user_id": self._user_id_of(attrs),
            "special_title": str(attrs["title"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_set_group_remark(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("remark"):
            raise ValueError("set_group_remark 需要 remark 属性")
        await self._client.api("set_group_remark",
                               {"group_id": group_id,
                                "remark": str(attrs["remark"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_set_friend_remark(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        if not attrs.get("remark"):
            raise ValueError("set_friend_remark 需要 remark 属性")
        await self._client.api("set_friend_remark", {
            "user_id": self._user_id_of(attrs),
            "remark": str(attrs["remark"])})
        return {"conversation_id": f"private_{attrs['user_id']}",
                "message_id": ""}

    async def _action_send_group_notice(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("content"):
            raise ValueError("send_group_notice 需要 content 属性")
        await self._client.api("_send_group_notice", {
            "group_id": group_id, "content": str(attrs["content"]),
            "pinned": int(attrs.get("pinned", 0)),
            "type": int(attrs.get("type", 1)),
            "confirm_required": int(attrs.get("confirm_required", 1)),
            "is_show_edit_card": int(attrs.get("show_edit_card", 0)),
            "tip_window_type": int(attrs.get("tip_window_type", 0))})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_delete_group_notice(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("notice_id"):
            raise ValueError("delete_group_notice 需要 notice_id 属性")
        await self._client.api("_del_group_notice",
                               {"group_id": group_id,
                                "notice_id": str(attrs["notice_id"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_group_sign(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        await self._client.api("set_group_sign", {"group_id": group_id})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_group_todo(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("message_id"):
            raise ValueError("group_todo 需要 message_id 属性")
        await self._client.api("set_group_todo", {
            "group_id": group_id,
            "message_id": str(attrs["message_id"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_send_like(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        await self._client.api("send_like", {
            "user_id": self._user_id_of(attrs),
            "times": int(attrs.get("times", 1))})
        return {"conversation_id": f"private_{attrs['user_id']}",
                "message_id": ""}

    async def _action_set_msg_emoji_like(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        if not attrs.get("emoji_id"):
            raise ValueError("set_msg_emoji_like 需要 emoji_id 属性")
        await self._client.api("set_msg_emoji_like", {
            "message_id": str(attrs["message_id"]),
            "emoji_id": str(attrs["emoji_id"]),
            "set": bool(str(attrs.get("set", "true")).lower()
                        in ("true", "1", "yes"))})
        return {"conversation_id": "", "message_id": ""}

    async def _action_delete_friend(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        user_id = self._user_id_of(attrs)
        await self._client.api("delete_friend", {"user_id": user_id})
        return {"conversation_id": f"private_{user_id}", "message_id": ""}

    async def _action_set_group_leave(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        await self._client.api("set_group_leave", {
            "group_id": group_id,
            "is_dismiss": bool(str(attrs.get("dismiss", "false")).lower()
                               in ("true", "1", "yes"))})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_delete_group_file(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("file_id"):
            raise ValueError("delete_group_file 需要 file_id 属性")
        await self._client.api("delete_group_file",
                               {"group_id": group_id,
                                "file_id": str(attrs["file_id"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    async def _action_delete_group_folder(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        group_id = self._group_id_of(attrs, element)
        if not attrs.get("folder_id"):
            raise ValueError("delete_group_folder 需要 folder_id 属性")
        await self._client.api("delete_group_folder",
                               {"group_id": group_id,
                                "folder_id": str(attrs["folder_id"])})
        return {"conversation_id": f"group_{group_id}", "message_id": ""}

    # ── 同步查询新增 ──────────────────────────────────────────────

    async def _action_get_group_info(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_info",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default={})

    async def _action_get_group_info_ex(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_info_ex",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default={})

    async def _action_get_stranger_info(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_stranger_info",
                                 {"user_id": self._user_id_of(attrs)}, default={})

    async def _action_get_user_status(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("nc_get_user_status",
                                 {"user_id": self._user_id_of(attrs)}, default={})

    async def _action_get_friend_list(self, element: dict[str, Any]) -> dict[str, Any]:
        return await self._query("get_friend_list", {}, default=[])

    async def _action_get_unidirectional_friend_list(self, element: dict[str, Any]) -> dict[str, Any]:
        return await self._query("get_unidirectional_friend_list", {}, default=[])

    async def _action_get_group_notice(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("_get_group_notice",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default=[])

    async def _action_get_essence_msg_list(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_essence_msg_list",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default=[])

    async def _action_get_group_shut_list(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_shut_list",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default=[])

    async def _action_get_group_system_msg(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_system_msg",
                                 {"count": int(attrs.get("count", 50))}, default={})

    async def _action_get_group_ignore_add_request(self, element: dict[str, Any]) -> dict[str, Any]:
        return await self._query("get_group_ignore_add_request", {}, default=[])

    async def _action_get_doubt_friends_add_request(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_doubt_friends_add_request",
                                 {"count": int(attrs.get("count", 50))}, default=[])

    async def _action_get_file_url(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        conversation_id = self._conversation_of(attrs, element)
        if not attrs.get("file_id"):
            raise ValueError("get_file_url 需要 file_id 属性")
        params = {"file_id": str(attrs["file_id"])}
        if conversation_id.startswith("group_"):
            params["group_id"] = int(conversation_id[len("group_"):])
            result = await self._client.api("get_group_file_url", params)
        else:
            result = await self._client.api("get_private_file_url", params)
        return {"result": result.get("data") or {}, "message_id": ""}

    async def _action_get_group_root_files(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_root_files", {
            "group_id": self._group_id_of(attrs, element),
            "file_count": int(attrs.get("file_count", 50))}, default={})

    async def _action_get_group_file_system_info(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_file_system_info",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default={})

    async def _action_get_qun_album_list(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_qun_album_list", {
            "group_id": self._group_id_of(attrs, element),
            "attach_info": str(attrs.get("attach_info", ""))}, default={})

    async def _action_get_group_signed_list(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_signed_list",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default=[])

    async def _action_get_emoji_likes(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        if not attrs.get("message_id") or not attrs.get("emoji_id"):
            raise ValueError("get_emoji_likes 需要 message_id 和 emoji_id")
        return await self._query("get_emoji_likes", {
            "message_id": str(attrs["message_id"]),
            "emoji_id": str(attrs["emoji_id"]),
            "count": int(attrs.get("count", 0))}, default={})

    async def _action_get_profile_like(self, element: dict[str, Any]) -> dict[str, Any]:
        return await self._query("get_profile_like", {}, default={})

    async def _action_get_group_at_all_remain(self, element: dict[str, Any]) -> dict[str, Any]:
        attrs = element.get("attributes") or {}
        return await self._query("get_group_at_all_remain",
                                 {"group_id": self._group_id_of(attrs, element)},
                                 default={})

    async def _action_get_status(self, element: dict[str, Any]) -> dict[str, Any]:
        return await self._query("get_status", {}, default={})

    async def _send_message_element(self, element: dict[str, Any]) -> dict[str, Any]:
        attributes = element.get("attributes") or {}
        conversation_id = str(attributes.get("conversation_id", ""))
        await self._arbitrate(attributes, conversation_id)
        segments = [seg for seg in element.get("content") or []
                    if isinstance(seg, dict) and seg.get("tag")]
        if not conversation_id:
            raise ValueError("message 元素缺少 conversation_id")

        conversation_type = "group" if conversation_id.startswith("group_") \
            else "private"
        # 执行侧白名单
        self._whitelist.guard_outbound(conversation_type, conversation_id)

        onebot_segments, pokes = self._assemble_segments(
            segments, conversation_type, conversation_id)
        if not onebot_segments and not pokes:
            raise ValueError(f"message 没有可发送的段: {conversation_id}")
        # file 段（纯协议，部署无关）：统一流式上传（upload_file_stream
        # 分块 + 完成确认）→ napcat temp file_path（文件名保留）。
        # 不依赖 base64 upload（丢文件名）或容器挂载路径。
        for file_seg in onebot_segments:
            if file_seg.get("type") != "file":
                continue
            data = file_seg["data"]
            if not data.pop("_pending", False):
                continue  # 已传输直发（file 已含路径）
            host = data.pop("_host", "")
            name = str(data.pop("name", "") or "file")
            file_path = await self._upload_stream(host, name)
            if not file_path:
                raise ValueError(f"文件流式上传失败: {name}")
            data["file"] = file_path
        last_message_id = ""
        # poke 独立动作（group_poke / friend_poke；V2 坑：send_group_poke 不支持）
        for poke in pokes:
            target = poke["target_id"]
            if conversation_type == "group":
                group_id = conversation_id[len("group_"):]
                response = await self._client.api(
                    "group_poke", {"group_id": int(group_id),
                                   "user_id": int(target)})
            else:
                response = await self._client.api(
                    "friend_poke", {"user_id": int(target)})
            data = response.get("data") or {}
            mid = str(data.get("message_id", ""))
            if mid:
                last_message_id = mid
            else:
                # poke 动作无平台消息 id：生成稳定 id（_report_out 兜底入库）
                import time as _t
                mid = f"poke_out_{int(_t.time())}_{target}"
            self._report_out(conversation_id, conversation_type, mid,
                             [{"type": "poke",
                               "data": {"target_id": target}}])
            logger.info("poke sent conv=%s target=%s msg_id=%s",
                        conversation_id, target, mid)

        # 文件消息（file 段）是 QQ 特殊消息，不能与普通段混发（混发时
        # text/image 被 QQ 端丢弃）——file 段逐个独立发送，普通段合并发送。
        file_segments = [seg for seg in onebot_segments
                         if seg.get("type") == "file"]
        normal_segments = [seg for seg in onebot_segments
                           if seg.get("type") != "file"]
        # 文件：逐个独立发送
        for file_seg in file_segments:
            response = await self._send_file_message(
                conversation_id, conversation_type, file_seg)
            data = response.get("data") or {}
            mid = str(data.get("message_id", ""))
            if mid:
                last_message_id = mid
            self._report_out(conversation_id, conversation_type, mid,
                             [file_seg])
            logger.info("file sent conv=%s name=%s msg_id=%s",
                        conversation_id,
                        file_seg.get("data", {}).get("name", ""), mid)
        # 普通段：合并一次发送
        if normal_segments:
            if conversation_type == "group":
                group_id = conversation_id[len("group_"):]
                response = await self._client.api(
                    "send_group_msg", {"group_id": int(group_id),
                                       "message": normal_segments})
            else:
                user_id = conversation_id[len("private_"):]
                response = await self._client.api(
                    "send_private_msg", {"user_id": int(user_id),
                                         "message": normal_segments})
            data = response.get("data") or {}
            message_id = str(data.get("message_id", ""))
            if message_id:
                last_message_id = message_id
            # 发送成功 → out 消息上报（direction=out，remote_message_id=平台 id）
            self._report_out(conversation_id, conversation_type, message_id,
                             normal_segments)
        logger.info("sent %s conv=%s msg_id=%s", conversation_type,
                    conversation_id, last_message_id)
        return {"conversation_id": conversation_id,
                "message_id": last_message_id}

    async def _send_file_message(self, conversation_id: str,
                                 conversation_type: str,
                                 file_seg: dict[str, Any]) -> dict:
        """文件消息独立发送（QQ 文件消息特殊，不可混发）。"""
        if conversation_type == "group":
            group_id = conversation_id[len("group_"):]
            return await self._client.api(
                "send_group_msg", {"group_id": int(group_id),
                                   "message": [file_seg]})
        user_id = conversation_id[len("private_"):]
        return await self._client.api(
            "send_private_msg", {"user_id": int(user_id),
                                 "message": [file_seg]})

    def _assemble_segments(self, segments: list[dict[str, Any]],
                           conversation_type: str,
                           conversation_id: str) -> tuple[list[dict], list[dict]]:
        """action 段 → (OneBot11 消息段数组, 独立动作列表)。

        text/reply/at：直接映射（at 后补空格防粘连）；
        image/record：base64://；
        file：流式上传（_pending，发送时 upload_file_stream → file_path）；
        poke：独立动作（group_poke / friend_poke，不混消息段）。"""
        onebot: list[dict[str, Any]] = []
        pokes: list[dict[str, Any]] = []
        for seg in segments:
            tag = seg.get("tag", "")
            attributes = seg.get("attributes") or {}
            if tag == "text":
                # text 文本框：内嵌 reply/at/face/image/sticker（混排保序）
                onebot.extend(self._text_segments(seg))
            elif tag == "reply":
                onebot.append({"type": "reply",
                               "data": {"id": attributes.get("msg_id", "")}})
            elif tag == "at":
                onebot.append({"type": "at",
                               "data": {"qq": attributes.get("target_id", "")}})
                onebot.append({"type": "text", "data": {"text": " "}})  # 防粘连
            elif tag in ("image", "record"):
                self._append_media(onebot, seg, tag)
            elif tag == "sticker":
                # sticker：快速引用（core 已按 sticker_base 解析传输）
                # → image 协议发送（napcat image 段）
                self._append_media(onebot, seg, "image")
            elif tag == "file":
                if attributes.get("_transfer_error"):
                    raise ValueError(
                        f"文件传输失败: {attributes['_transfer_error']}")
                if attributes.get("_transferred") and attributes.get("file"):
                    # 已传输：napcat temp 路径直发（不再上传）
                    name = str(attributes.get("name", "")) or \
                        str(attributes["file"]).split("/")[-1] or "file"
                    onebot.append({"type": "file", "data": {
                        "file": str(attributes["file"]), "name": name}})
                    continue
                host = self._host_of(seg)
                if host is not None:
                    name = str(attributes.get("name", "") or host.name)
                    onebot.append({"type": "file", "data": {
                        "file": "", "name": name, "_host": str(host),
                        "_pending": True}})
                else:
                    logger.warning("媒体文件不可读: %s", tag)
            elif tag == "poke":
                target = str(attributes.get("target_id", ""))
                if target:
                    pokes.append({"target_id": target,
                                  "conversation_type": conversation_type,
                                  "conversation_id": conversation_id})
                else:
                    logger.warning("poke 段缺少 target_id")
            elif tag == "face":
                face_id = str(attributes.get("id", ""))
                if face_id:
                    onebot.append({"type": "face", "data": {"id": face_id}})
                else:
                    logger.warning("face 段缺少 id")
            elif tag == "dice":
                onebot.append({"type": "dice", "data": {}})
            elif tag == "rps":
                onebot.append({"type": "rps", "data": {}})
            elif tag == "json":
                data = str(attributes.get("data", "")) or str(
                    attributes.get("json", ""))
                if data:
                    onebot.append({"type": "json", "data": {"data": data}})
                else:
                    logger.warning("json 段缺少 data")
            else:
                logger.warning("发送侧暂不支持段: %s", tag)
        return onebot, pokes

    async def upload_chunk(self, stream_id: str, chunk: bytes,
                           index: int, total: int, size: int,
                           sha: str, name: str) -> None:
        """单块转发 napcat upload_file_stream（流式管道，内存 O(chunk)）。"""
        import base64
        await self._client.api("upload_file_stream", {
            "stream_id": stream_id,
            "chunk_data": base64.b64encode(chunk).decode("ascii"),
            "chunk_index": index, "total_chunks": total,
            "file_size": size, "expected_sha256": sha,
            "is_complete": index == total - 1, "filename": name,
            "file_retention": 3600000,
        })

    async def complete_upload(self, stream_id: str, total: int,
                              size: int, sha: str, name: str) -> str:
        """完成确认：verify_only → file_created；再确认 → file_complete+path。"""
        await self._client.api("upload_file_stream", {
            "stream_id": stream_id, "is_complete": True,
            "total_chunks": total, "file_size": size,
            "expected_sha256": sha, "verify_only": True,
            "filename": name})
        final = await self._client.api("upload_file_stream", {
            "stream_id": stream_id, "is_complete": True,
            "total_chunks": total, "file_size": size,
            "expected_sha256": sha, "filename": name})
        file_path = str((final.get("data") or {}).get("file_path", ""))
        if not file_path:
            logger.error("stream upload complete failed: %s", final)
            return ""
        logger.info("file stream uploaded name=%s path=%s", name, file_path)
        return file_path

    async def upload_bytes(self, data: bytes, name: str) -> str:
        """整块字节转发（兼容：内存持有，小文件用）。"""
        import hashlib
        import uuid
        if not data:
            raise ValueError("文件为空")
        stream_id = str(uuid.uuid4())
        chunk_size = 64 * 1024
        total = max(1, (len(data) + chunk_size - 1) // chunk_size)
        sha = hashlib.sha256(data).hexdigest()
        for index in range(total):
            chunk = data[index * chunk_size:(index + 1) * chunk_size]
            await self.upload_chunk(stream_id, chunk, index, total,
                                    len(data), sha, name)
        return await self.complete_upload(stream_id, total, len(data),
                                          sha, name)

    async def _upload_stream(self, host: str, name: str) -> str:
        """宿主文件 → upload_bytes（兼容旧调用）。"""
        try:
            with open(host, "rb") as handle:
                data = handle.read()
        except OSError:
            logger.warning("file read failed: %s", host)
            return ""
        return await self.upload_bytes(data, name)
        chunks = [data[i:i + chunk_size]
                  for i in range(0, len(data), chunk_size)]
        sha = hashlib.sha256(data).hexdigest()
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            is_last = index == total - 1
            await self._client.api("upload_file_stream", {
                "stream_id": stream_id,
                "chunk_data": base64.b64encode(chunk).decode("ascii"),
                "chunk_index": index, "total_chunks": total,
                "file_size": len(data), "expected_sha256": sha,
                "is_complete": is_last, "filename": name,
                "file_retention": 3600000,
            })
        # 完成确认：verify_only → file_created；再确认 → file_complete+path
        await self._client.api("upload_file_stream", {
            "stream_id": stream_id, "is_complete": True,
            "total_chunks": total, "file_size": len(data),
            "expected_sha256": sha, "verify_only": True,
            "filename": name})
        final = await self._client.api("upload_file_stream", {
            "stream_id": stream_id, "is_complete": True,
            "total_chunks": total, "file_size": len(data),
            "expected_sha256": sha, "filename": name})
        file_path = str((final.get("data") or {}).get("file_path", ""))
        if not file_path:
            logger.error("stream upload complete failed: %s", final)
            return ""
        logger.info("file stream uploaded name=%s path=%s", name, file_path)
        return file_path

    def _media_to_base64(self, seg: dict[str, Any]) -> str:
        """媒体段（image/record）→ base64://。"""
        host = self._host_of(seg)
        if host is None:
            return ""
        import base64
        try:
            data = host.read_bytes()
        except OSError:
            return ""
        return "base64://" + base64.b64encode(data).decode("ascii")

    def _host_of(self, seg: dict[str, Any]) -> Optional[Path]:
        """媒体/文件宿主路径：优先直接本地路径（core 传输的临时文件 /
        url 指向的宿主文件），其次 files/ 内部路径。"""
        import re
        attributes = seg.get("attributes") or {}
        candidates = [
            str(attributes.get("file", "")),
            str(attributes.get("url", "")),
            str(attributes.get("path", "")),
        ]
        if not any(candidates) and "content" in seg:
            for item in seg.get("content") or []:
                if isinstance(item, dict) and "url" in item:
                    candidates.insert(0, str(item["url"]))
                    break
        for raw in candidates:
            if not raw:
                continue
            # 本地绝对路径（传输临时文件）
            if raw.startswith("/"):
                host = Path(raw).resolve()
                if host.is_file():
                    return host
                continue
            # files/ 内部路径 → data/files 下
            match = re.match(r"files/(.+)", raw)
            if match:
                host = (self._files_root / match.group(1)).resolve()
                if host.is_file():
                    return host
        logger.warning("media file missing: %s", attributes)
        return None

    def _text_segments(self, seg: dict[str, Any]) -> list[dict]:
        """text 文本框混排 content → OneBot 段（保序）。

        内嵌元素：reply（msg_id）/ at（target_id，后补空格防粘连）/
        face（id，QQ 内置表情）/ image（file，已传输直发否则 base64）/
        sticker（快速引用 → image 协议）。"""
        out: list[dict[str, Any]] = []
        tail = ""
        for item in seg.get("content") or []:
            if isinstance(item, dict) and "text" in item:
                # 字面 \n（模型 JSON 序列化产物，反斜杠+n 两字符）→ 真实
                # 换行；XML 实体（&lt; 等）已在解析层解码
                tail += str(item["text"]).replace("\\n", "\n")
            elif isinstance(item, dict) and item.get("tag") in (
                    "reply", "at", "face", "image", "sticker"):
                if tail:
                    out.append({"type": "text", "data": {"text": tail}})
                    tail = ""
                attributes = item.get("attributes") or {}
                tag = item["tag"]
                if tag == "reply":
                    out.append({"type": "reply",
                                "data": {"id": str(attributes.get("msg_id", ""))}})
                elif tag == "at":
                    out.append({"type": "at",
                                "data": {"qq": str(attributes.get("target_id", ""))}})
                    out.append({"type": "text", "data": {"text": " "}})
                elif tag == "face":
                    out.append({"type": "face",
                                "data": {"id": str(attributes.get("id", ""))}})
                else:  # image / sticker → image 段（传输后直发，否则 base64）
                    self._append_media(onebot=out, seg=item, tag="image")
        if tail:
            out.append({"type": "text", "data": {"text": tail}})
        return out

    def _append_media(self, onebot: list[dict], seg: dict[str, Any],
                      tag: str) -> None:
        """媒体段（image/record/sticker）：已传输（_transferred）→ 路径直发；
        否则本地读 base64。"""
        attributes = seg.get("attributes") or {}
        if attributes.get("_transferred") and attributes.get("file"):
            onebot.append({"type": tag,
                           "data": {"file": str(attributes["file"])}})
            return
        encoded = self._media_to_base64(seg)
        if encoded:
            onebot.append({"type": tag, "data": {"file": encoded}})

    def _text_of(self, seg: dict[str, Any]) -> str:
        """text 段内容：content 混排片段（{text}|{tag}）→ 纯文本。"""
        parts: list[str] = []
        for item in seg.get("content") or []:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif item.get("tag") == "reply":
                    mid = (item.get("attributes") or {}).get("msg_id", "")
                    parts.append(f"[引用 {mid}]")
                elif item.get("tag") == "at":
                    uid = (item.get("attributes") or {}).get("target_id", "")
                    parts.append(f"@{uid}")
        return "".join(parts)

    def _report_out(self, conversation_id: str, conversation_type: str,
                    message_id: str, segments: list[dict]) -> None:
        """发送成功 → out 消息入库（direction=out，不计未读）。

        llm_content 含媒体占位符（image/record 等），与入站渲染一致——
        agent 回溯时能看到自己发了什么。"""
        try:
            parts = []
            for seg in segments:
                seg_type = seg.get("type", "")
                data = seg.get("data", {}) or {}
                if seg_type == "text":
                    parts.append(str(data.get("text", "")))
                elif seg_type == "image":
                    parts.append('<image file="%s"/>' % data.get("file", ""))
                elif seg_type == "record":
                    parts.append('<record file="%s"/>' % data.get("file", ""))
                else:
                    parts.append("[%s]" % seg_type)
            text = "".join(parts)
            from .message import MessageDraft
            draft = MessageDraft(
                platform="",
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                remote_message_id=message_id,
                sender=dict(self._self_sender),
                content=text,
                llm_content=text,
                raw_segments=[{"type": seg.get("type"),
                               "data": seg.get("data", {})}
                              for seg in segments],
                direction="out",
            )
            result = self._submitter.submit(draft)
            # 入库成功才登记：万一 core 挂了，回显那条还能把消息记进来（自我修复）。
            # 登记后 message_sent 回显会被 handlers 直接丢弃（见 qq_adapter.outbound）。
            if result is not None:
                outbound.register(message_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("out message report failed conv=%s", conversation_id)
