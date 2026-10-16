#!/usr/bin/env python3
"""test_inputs_notice.py — 验证 inputs.yaml 真的在管 notice（v2 输入层）。

跑法：
    cd .skills/subagent && PYTHONPATH=src python3 tests/test_inputs_notice.py

覆盖：
  1. 未声明的 kind → 不推（保守默认）
  2. 声明了但 wakeup:false → 不推
  3. 声明了 wakeup:true → 真推，且正文 = inputs.yaml 里的 prompt 模板渲染结果
  4. dedup_s 生效：同一 kind 短时间内第二次不推
  5. inputs_cfg 为空（老配置）→ 不过滤，保持旧行为
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "src"))

from agent_framework.hub import AgentManager, load_inputs  # noqa: E402

RECEIVED: list[dict] = []


class Collector(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        RECEIVED.append({"body": body, "token": self.headers.get("X-Api-Token")})
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build(inputs_cfg: dict | None):
    tmp = tempfile.mkdtemp(prefix="inputs_notice_")
    return AgentManager(
        llm_profiles={"default": {"model": "x"}},
        default_profile="default",
        agent_cfg={},
        system_prompt="t",
        context_root=Path(tmp),
        notice_cfg={"url": f"http://127.0.0.1:{PORT}/api/notice",
                    "timeout_s": 3},
        inputs_cfg=inputs_cfg,
    )


def fire(mgr: AgentManager, kind: str, **extra) -> None:
    ev = {"type": "notice", "kind": kind, "agent": "t1"}
    ev.update(extra)
    mgr._default_notice_hook("t1", ev)


def main() -> int:
    global PORT
    srv = HTTPServer(("127.0.0.1", 0), Collector)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    inputs = load_inputs(SKILL / "config" / "inputs.yaml")
    assert inputs.get("inputs"), "inputs.yaml 没读到"
    mgr = build(inputs)
    fails = []

    def check(name, cond, extra=""):
        print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    # 1. 未声明 kind → 不推
    RECEIVED.clear()
    fire(mgr, "no_such_kind", message="x")
    check("未声明 kind 不推送", len(RECEIVED) == 0, f"got={len(RECEIVED)}")

    # 2. wakeup:false（compressed）→ 不推
    RECEIVED.clear()
    fire(mgr, "compressed", message="压缩了")
    check("wakeup:false 不推送", len(RECEIVED) == 0, f"got={len(RECEIVED)}")

    # 3. wakeup:true（completed）→ 推，正文走 prompt 模板
    RECEIVED.clear()
    fire(mgr, "completed", model_output="答案是 42", message="实例 t1 subagent 完成。")
    ok = len(RECEIVED) == 1
    check("completed 推送一次", ok, f"got={len(RECEIVED)}")
    if ok:
        content = RECEIVED[0]["body"].get("content", "")
        check("正文含 agent 名", "t1" in content, content[:60])
        check("正文含 model_output", "答案是 42" in content, content[:80])
        check("正文以 [subagent] 开头", content.startswith("[subagent]"),
              content[:20])
        check("带 X-Api-Token 头", bool(RECEIVED[0]["token"]))

    # 4. dedup_s：completed 的 dedup_s=5 → 立刻再发一次应被去重
    RECEIVED.clear()
    fire(mgr, "completed", model_output="again", message="again")
    check("completed 去重（dedup_s=5）", len(RECEIVED) == 0,
          f"got={len(RECEIVED)}")

    # 5. tool_error（dedup_s=30）首次应推
    RECEIVED.clear()
    fire(mgr, "tool_error", tool="exec", code=403, message="工具 exec 失败")
    check("tool_error 推送", len(RECEIVED) == 1, f"got={len(RECEIVED)}")

    # 6. inputs_cfg 为空 → 不过滤（旧行为）
    mgr_legacy = build({})
    RECEIVED.clear()
    fire(mgr_legacy, "compressed", message="旧行为照推")
    check("无 inputs_cfg 时保持旧行为", len(RECEIVED) == 1,
          f"got={len(RECEIVED)}")

    srv.shutdown()
    print()
    if fails:
        print(f"❌ {len(fails)} 项失败: {fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
