#!/usr/bin/env python3
"""send_text 正文传入的元字符安全性测试（2026-10-17 转义修复时固化）。

覆盖两条推荐通道，断言「正文逐字进 payload、不被 shell 展开」：
  ① --from-file <文件>      正文完全不过 shell
  ② `-` + 单引号 heredoc    标记带单引号 ⇒ 不展开 $ 与反引号
反例（不测、只作口径）：双引号内联会被 shell 做命令替换（2026-09-26 / 10-03 各踩一次）。

跑法：python3 tests/test_send_text_payload.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SEND = Path(__file__).resolve().parents[1] / "actions" / "send_text" / "send_text.py"
CONV = "private_1000000002"

# 对抗性正文：shell 元字符 + HTML 实体 + 标签 + 单独一行的 heredoc 标记 + 中文长串 + emoji + 像参数的行
ADVERSARIAL = (
    "第一行：含 `反引号` 与 $HOME 与 $(whoami) 与 !1 与 \\n 字面\n"
    "第二行：含 \"双引号\" '单引号' 与 &quot; 与 &#39; 与 <msg msg_id=\"1\"/>\n"
    "第三行：标记冲突测试 → 下面那行就是 heredoc 结束标记原文\n"
    "MSG\n"
    "第四行：中文长串：抑郁识别研究现状、PHQ-8 切点 10、敏感度 0.88、特异度 0.85、"
    "Rosenhan 1973、DAIC-WOZ 189 场访谈、eGeMAPS 25 维、ComParE 1582 维\n"
    "第五行：emoji 与符号 😀 🤡 → ← ≥ ≤ ± ×\n"
    "第六行：以 - 开头、以 --force 结尾（防被当参数解析）\n"
    "--force"
)

# heredoc 通道用的正文（不能含单独一行的 MSG 标记，否则截断 heredoc 本身）
HEREDOC_BODY = (
    "含 $HOME 与 $(whoami) 与 `反引号` 与 \"双引号\" '单引号' 与 \\n 与 中文长串：抑郁识别 PHQ-8 敏感度 0.88\n"
    "第二行 emoji 😀 ≥ ≤"
)

fails: list[str] = []


def payload_text(stdout: str) -> str:
    """从 --dry-run 输出里取真正要发的正文。"""
    d = json.loads(stdout)
    seg = d["action"]["elements"][0]["content"][0]["content"][0]
    return seg["text"]


def check(name: str, got: str, want: str) -> None:
    if got == want:
        print(f"  ✓ {name}")
    else:
        fails.append(name)
        print(f"  ✗ {name}\n    期望 {want!r}\n    实得 {got!r}")


def main() -> int:
    print("send_text 正文传入元字符安全性")

    # ① --from-file
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(ADVERSARIAL)
        path = f.name
    r = subprocess.run(
        [sys.executable, str(SEND), CONV, "--from-file", path, "--dry-run"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    check("--from-file 全元字符逐字保留", payload_text(r.stdout), ADVERSARIAL)
    # 未展开断言（显式写死，防实现改成展开后仍"看起来对"）
    t = payload_text(r.stdout)
    for lit in ("$HOME", "$(whoami)", "`反引号`", "&quot;", "MSG", "--force", "😀"):
        if lit not in t:
            fails.append(f"--from-file 缺字面量 {lit}")
    Path(path).unlink(missing_ok=True)

    # ② stdin + 单引号 heredoc（经 bash 跑，模拟真实调用姿势）
    cmd = (
        f"{sys.executable} {SEND} {CONV} - --dry-run <<'MSG'\n{HEREDOC_BODY}\nMSG\n"
    )
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    check("stdin + <<'MSG' 逐字保留（不展开 $ 与反引号）", payload_text(r.stdout), HEREDOC_BODY)
    t = payload_text(r.stdout)
    for lit in ("$HOME", "$(whoami)", "`反引号`"):
        if lit not in t:
            fails.append(f"stdin 缺字面量 {lit}（heredoc 被展开）")

    # ③ 反例口径：双引号内联会被 bash 展开（证明「双引号内联」确实不安全）
    r = subprocess.run(
        ["bash", "-c", f"{sys.executable} {SEND} {CONV} \"值 $HOME 与 `echo x`\" --dry-run"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode == 0:
        t = payload_text(r.stdout)
        if "$HOME" in t:
            fails.append("双引号内联竟然没展开——测试环境异常，判据失效")
        else:
            print("  ✓ 反例确认：双引号内联被 shell 展开（$HOME 已消失）⇒ 禁止内联")
    else:
        print("  ✓ 反例确认：双引号内联直接失败（bash 报错）⇒ 禁止内联")

    # ④ 末尾换行（2026-10-18 修复：每条消息末尾多一个换行）
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("两行正文\n带内部换行\n\n\n")
        p2 = f.name
    r = subprocess.run(
        [sys.executable, str(SEND), CONV, "--from-file", p2, "--dry-run"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    check("--from-file 末尾换行已去掉（内部换行保留）", payload_text(r.stdout), "两行正文\n带内部换行")
    Path(p2).unlink(missing_ok=True)

    cmd = f"{sys.executable} {SEND} {CONV} - --dry-run <<'MSG'\n第一行\n第二行\nMSG\n"
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    check("heredoc 末尾换行已去掉", payload_text(r.stdout), "第一行\n第二行")

    print(f"\n共 5 组，失败 {len(fails)}")
    for f_ in fails:
        print(" -", f_)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
