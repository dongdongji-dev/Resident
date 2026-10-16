#!/usr/bin/env python3
"""notice 通知配置化（url / timeout_s）的真实测试。

运行：cd /home/sandbox/.skills/subagent && python3 tests/test_notice_config.py

覆盖（都是真调用，不是 mock 断言）：
1. 配置加载：config/config.yaml 的 notice 段能被 hub.load_config 读出，
   且含 url / timeout_s。
2. url 生效：起一个本地 HTTP 服务，把 manager.notice_cfg 指过去 →
   调 _default_notice_hook → 服务端必须真收到 POST，且
   X-Api-Token = /home/sandbox/.secrets/core_api_key.txt 里的真 token，
   body 是 {content, due_at, kind}。
3. 默认值：不传 notice_cfg 时 url/超时回落到原硬编码值（core 端点 / 6s）。
4. timeout_s 生效：把 url 指向黑洞地址（10.255.255.1:9，不可路由），
   timeout_s=1.0 → 实际阻塞 ≈1s（若仍是硬编码 6s，耗时会 ≈6s 判 FAIL）。

背景：原代码把 core 端点 URL 与 timeout=6 硬编码在 _default_notice_hook 里，
2026-09-16 按"变量进配置而非硬编码"的设计原则下沉到 config.yaml。
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))  # skill 根，import hub 模块

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def build_manager(notice_cfg, context_root):
    from agent_framework.hub import AgentManager
    from kernel.agent_framework.tools import ToolRegistry
    return AgentManager(
        llm_profiles={"default": {"model": "test-model"}},
        default_profile="default",
        agent_cfg={},
        system_prompt="t",
        context_root=context_root,
        registry=ToolRegistry(),
        notice_cfg=notice_cfg,
    )


def real_token():
    p = "/home/sandbox/.secrets/core_api_key.txt"
    if not os.path.exists(p):
        return ""
    return open(p, encoding="utf-8").read().strip().split("=")[-1].strip()


def start_capture_server():
    """本地 HTTP 服务：记录收到的 (path, headers, body)，返回 200。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append({
                "path": self.path,
                "token": self.headers.get("X-Api-Token", ""),
                "ctype": self.headers.get("Content-Type", ""),
                "body": self.rfile.read(n).decode("utf-8"),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):  # 静音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    skill_root = os.path.dirname(here)

    # ── 1. 配置加载 ──────────────────────────────────────────────
    print("[1] config/config.yaml 的 notice 段")
    from agent_framework.hub import load_config
    cfg = load_config(__import__("pathlib").Path(skill_root) /
                      "config" / "config.yaml")
    ncfg = cfg.get("notice") or {}
    check("notice 段存在", bool(ncfg), ncfg)
    check("url 是 core 端点",
          ncfg.get("url") == "http://host.docker.internal:18086/api/notice",
          ncfg.get("url"))
    check("timeout_s 是数字", isinstance(ncfg.get("timeout_s"), (int, float)),
          ncfg.get("timeout_s"))

    tmp = tempfile.mkdtemp(prefix="notice_cfg_test_")

    # ── 2. url 生效（真发到本地服务） ─────────────────────────────
    print("[2] notice_cfg.url 生效 + 真 token 头")
    srv, got = start_capture_server()
    port = srv.server_address[1]
    m = build_manager({"url": f"http://127.0.0.1:{port}/api/notice",
                       "timeout_s": 5}, tmp)
    check("notice_url 读自配置", m.notice_url.endswith(f":{port}/api/notice"),
          m.notice_url)
    check("notice_timeout_s 读自配置", m.notice_timeout_s == 5.0,
          m.notice_timeout_s)
    m._default_notice_hook("cfgtest", {"type": "notice",
                                       "kind": "completed",
                                       "model_output": "结果ABC"})
    check("服务端收到 1 条 POST", len(got) == 1, len(got))
    if got:
        g = got[0]
        check("path 正确", g["path"] == "/api/notice", g["path"])
        check("token = core_api_key", g["token"] == real_token() != "",
              g["token"][:8] + "***" if g["token"] else "(空)")
        try:
            body = json.loads(g["body"])
        except Exception as exc:  # noqa: BLE001
            body = {}
            check("body 是合法 JSON", False, str(exc))
        check("body.kind=once", body.get("kind") == "once", body.get("kind"))
        check("body.due_at 立即", body.get("due_at") == "", body.get("due_at"))
        check("content 含实例名与结果",
              "cfgtest" in body.get("content", "")
              and "结果ABC" in body.get("content", ""),
              body.get("content", "")[:40].replace("\n", "\\n"))
    srv.shutdown()

    # ── 3. 默认值回落 ────────────────────────────────────────────
    print("[3] 不传 notice_cfg → 回落原硬编码值")
    m2 = build_manager(None, tmp)
    check("默认 url = core 端点",
          m2.notice_url == "http://host.docker.internal:18086/api/notice",
          m2.notice_url)
    check("默认 timeout = 6", m2.notice_timeout_s == 6.0,
          m2.notice_timeout_s)

    # ── 4. timeout_s 真的生效（黑洞地址计时） ────────────────────
    print("[4] timeout_s 生效（黑洞地址 10.255.255.1:9）")
    m3 = build_manager({"url": "http://10.255.255.1:9/api/notice",
                        "timeout_s": 1.0}, tmp)
    t0 = time.time()
    m3._default_notice_hook("slowtest", {"type": "notice", "kind": "tool_error",
                                         "tool": "exec", "code": 500})
    dt = time.time() - t0
    check("阻塞 ≈1.0s（而非硬编码 6s）", 0.5 <= dt <= 2.5, f"{dt:.2f}s")

    print(f"\n== {PASS} passed / {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
