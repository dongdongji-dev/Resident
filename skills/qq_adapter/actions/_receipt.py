#!/usr/bin/env python3
"""_receipt.py — actions/ 共用：发送回执对账（对着 core 消息库逐字核对）。

为什么要有：只看到 ok:true/message_id 不够——必须能确认「真正发出去的是什么」。
2026-09-28 添加；2026-10-12 从 scripts/send_msg.py 搬进 actions，
让「走 action 发消息」也自带回执（否则容易绕回老脚本，约定过）。

判据（四情形）：
  ① 正常：有 message_id + **按 message_id 命中库记录**（首选）或逐字对账通过
  ② 「无 out 消息」＝**回执未落库**：落库延迟，等一会儿重取；
     ⚠️ 取记录必须**翻页翻到头**（`_api_messages_all`）——lastseq 落后超过一页时
     单页 300 条根本够不到最新那条（2026-10-19 09:2x 实测误报，见该函数注释）。
  ③ 真·静默失败：ok:true 但 message_id 为 None、库里没有 out 记录 ⇒ 没发出去，原文重发
  ④ 同号另一端活跃：库里 out 序列里混着另一端的消息，**内容比对会误报「不一致」**——
     故先按 message_id 配对（2026-10-17 回执误报修复时加）；
     两者都不中时提示用 `.skills/qq_adapter/tools/verify_self_send.py <msg_id>` 复核。

对账前会抹掉适配器的两类改写（HTML 转义、`<reply/>`/`<at/>` 前缀段），
并跳过系统类 out 记录（`[消息撤回]` / `[notice` / `[系统`）。
状态（各会话已对账到的 seq）落 <state_dir>/lastseq.json。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SANDBOX = Path("/home/sandbox")
CORE_BASE = os.environ.get("CORE_API_URL", "http://host.docker.internal:18086")
PLATFORM = os.environ.get("QQ_ADAPTER_PLATFORM", "qq[1000000001]")

# 适配器**真的会加**的前缀（只剥这些；见 norm() 的踩坑说明）
_ADAPTER_PREFIX_RE = re.compile(
    r"^(?:\[(?:reply|at\b[^\]]*|notice\b[^\]]*|系统[^\]]*|消息撤回)\]"
    r"|<(?:reply|at)\b[^>]*>)\s*")


def _state_dir() -> Path:
    return Path(os.environ.get("QQ_ADAPTER_STATE_DIR",
                               SANDBOX / "skill_data" / "qq_adapter"))


def _lastseq_path() -> Path:
    return Path(os.environ.get("QQ_ADAPTER_LASTSEQ",
                               _state_dir() / "lastseq.json"))


def _core_key() -> str | None:
    p = SANDBOX / ".secrets" / "core_api_key.txt"
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            val = line.rsplit("=", 1)[1].strip()
            if val:
                return val
        else:
            return line
    return None


def _api_messages(conv: str, after_seq: int, limit: int = 300) -> list:
    """经 core API 读消息库。conv 形如 private_1000000002 / group_200000001。"""
    key = _core_key()
    if not key:
        return []
    url = (f"{CORE_BASE}/api/messages?platform={urllib.parse.quote(PLATFORM)}"
           f"&conversation_id={urllib.parse.quote(conv)}"
           f"&after_seq={after_seq}&limit={limit}")
    req = urllib.request.Request(url, headers={"X-Api-Token": key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 内网绕代理
    with opener.open(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("messages", []) if isinstance(data, dict) else data


def _api_messages_all(conv: str, after_seq: int, page: int = 300,
                      max_pages: int = 20) -> list:
    """从 after_seq 往后**翻页取全**（首页只有 300 条）。

    2026-10-19 09:2x 实测坑：lastseq 落后很多时（群 200000005 停在 6574，
    实际已到 7427），`after_seq=6574&limit=300` 只返回 6575~6874 这一页，
    我刚发的那条根本不在返回集里 ⇒ 三次轮询取的都是同一页（循环里没推进
    after_seq），outs 里全是别人的 in 消息 → 误报「⚠️ 回执不一致（无 out 消息）」，
    而 `verify_self_send.py` 复核＝本 adapter 正常发出。⇒ 必须翻页翻到头。
    """
    out: list = []
    cur = after_seq
    for _ in range(max_pages):
        msgs = _api_messages(conv, cur, page)
        if not msgs:
            break
        out.extend(msgs)
        mx = max(int(m.get("seq", 0)) for m in msgs)
        if mx <= cur:                      # 端点不推进（防死循环）
            break
        cur = mx
        if len(msgs) < page:               # 最后一页
            break
    return out


def _load_seq(conv: str) -> int:
    try:
        state = json.loads(_lastseq_path().read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        state = {}
    return int(state.get(conv, 0))


def _save_seq(conv: str, seq: int) -> None:
    try:
        state = json.loads(_lastseq_path().read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        state = {}
    state[conv] = seq
    path = _lastseq_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _bootstrap_seq(conv: str) -> int:
    """首次：二分找该会话当前最大 seq。"""
    lo, hi = 0, 10_000_000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            if _api_messages(conv, mid, 1):
                lo = mid
            else:
                hi = mid - 1
        except Exception:                                 # noqa: BLE001
            return 0
    return lo


def norm(s: str) -> str:
    """抹掉适配器落库时加的前缀段（HTML 转义 + reply/at 前缀）。

    ⚠️ 只剥**适配器真正会加的**前缀（`[reply]` / `[at xxx]` / `<reply .../>`
    / `<at .../>` 与几个系统前缀），不能见 `[` 就剥——2026-10-13 00:16 实测
    踩坑：我发的正文以 `[自测]` 开头，被当成前缀剥掉 → 回执误报「不一致」。
    """
    s = html.unescape((s or "").strip())
    while True:
        m = _ADAPTER_PREFIX_RE.match(s)
        if not m:
            break
        s = s[m.end():].lstrip()
    return s


def _is_system_out(text: str) -> bool:
    t = (text or "").lstrip()
    return (t.startswith("[消息撤回]") or t.startswith("[notice")
            or t.startswith("[系统"))


def verify(conv: str, text: str, response: dict | str | None,
           tries: int = 3) -> str:
    """对账并返回一行结论（不抛异常；core 不可达时如实说明）。"""
    mid = None
    try:
        if isinstance(response, str):
            response = json.loads(response)
        mid = (response or {}).get("result", [{}])[0].get("message_id")
    except Exception:                                     # noqa: BLE001
        mid = None
    head = text.strip()[:80].replace("\n", " / ")
    line = f"✅ 已发送 {len(text)} 字 · message_id={mid}\n   正文开头：{head}"
    if mid is None:
        line = (f"⚠️ 发送回执可疑：message_id 为空（可能没真发出去）\n"
                f"   正文开头：{head}")
    try:
        before = _load_seq(conv) or _bootstrap_seq(conv)
        want = text.strip()
        want_mid = str(mid) if mid is not None else None
        outs: list = []
        for attempt in range(tries):
            msgs = _api_messages_all(conv, before)
            if msgs:
                newest = max(int(m.get("seq", 0)) for m in msgs)
                _save_seq(conv, newest)
                before = newest   # 下一轮从最新往后翻，别反复取同一页
            outs = [m for m in msgs if m.get("direction") == "out"
                    and not _is_system_out(m.get("content") or "")]
            # ① 首选：按 message_id 配对（同号另一端活跃时，内容比对会误报）
            if want_mid:
                for m in msgs:
                    if (str(m.get("remote_message_id") or "") == want_mid
                            and m.get("direction") == "out"):
                        return line + (f"\n   ✅ 回执对账通过（按 message_id 配对）："
                                       f"消息库 seq {m.get('seq')} / 落库 "
                                       f"{len(norm(m.get('content') or ''))} 字")
            # ② 回退：逐字/前缀内容比对
            for m in reversed(outs):
                got = norm(m.get("content") or "")
                if got == want or (want[:30] and got.startswith(want[:30])):
                    return line + (f"\n   ✅ 回执对账通过：消息库 seq "
                                   f"{m.get('seq')} / 落库 {len(got)} 字，与发送正文一致")
            if attempt < tries - 1:
                time.sleep(1.5)
        last = ((outs[-1].get("content") or "")[:80].replace("\n", " / ")
                if outs else "(无 out 消息)")
        hint = ""
        if want_mid:
            hint = (f"\n   ↳ 判据补充：用 `.skills/qq_adapter/tools/verify_self_send.py "
                    f"{want_mid}` 复核（同号另一端活跃时内容比对会误报）")
        if not outs:
            return (line + "\n   ⚠️ 回执未落库：这段时间库里没有该会话的 out 记录"
                    "（多半是落库延迟，先按 message_id 复核，别急着重发）" + hint)
        return line + f"\n   ⚠️ 回执不一致：消息库最新 out 内容是「{last}」" + hint
    except Exception as exc:                              # noqa: BLE001
        return line + f"\n   ⚠️ 回执未对账（core API 不可达：{exc}）"
