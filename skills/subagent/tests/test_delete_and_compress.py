#!/usr/bin/env python3
"""file_delete（安全删除到 trash）+ keep_recent_by_usage_diff 的真实测试。

运行：cd /home/sandbox/.skills/subagent && python3 tests/test_delete_and_compress.py

- file_delete：直接调函数（thread-local 绑定 workdir），覆盖
  工作区内删除 / 重名后缀 / 越界 403（无 broker）/ 不存在 / 目录拒绝 / 相对路径。
- keep_recent_by_usage_diff：用 2026-09-05 匿名模型_wordle2 会话的**真实**
  累计 prompt_tokens 序列（1276/19911/26354/26571/26855/27550/27707）
  回归"旧代码把累计值当单轮成本 → recent 恒空"的 bug。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from kernel.agent_framework.compression import keep_recent_by_usage_diff
from kernel.agent_framework.workspace_tools import (file_delete,
                                                    set_session_state)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def test_file_delete():
    print("== file_delete ==")
    tmp = tempfile.mkdtemp(prefix="subagent_del_test_")
    workdir = os.path.join(tmp, "work")
    os.makedirs(workdir)
    trash = os.path.join(tmp, "trash")
    cfg = {"trash_dir": trash}
    set_session_state("deltest", workdir, None)

    # 1. 工作区内正常删除
    p = os.path.join(workdir, "a.txt")
    with open(p, "w") as f:
        f.write("hello")
    r = file_delete(cfg, p)
    check("工作区内删除成功", isinstance(r, str) and r.startswith("已安全删除"), str(r))
    check("原文件消失", not os.path.exists(p))
    check("移入 trash", os.path.isfile(os.path.join(trash, "a.txt")))
    with open(os.path.join(trash, "a.txt")) as f:
        check("trash 内容完整", f.read() == "hello")

    # 2. 重名 → 时间戳后缀，不覆盖
    with open(p, "w") as f:
        f.write("world")
    r = file_delete(cfg, p)
    check("重名删除成功", isinstance(r, str) and r.startswith("已安全删除"), str(r))
    files = sorted(os.listdir(trash))
    check("trash 有两个文件", len(files) == 2 and "a.txt" in files, str(files))
    check("重名加了后缀", any(f != "a.txt" and f.startswith("a_")
                              and f.endswith(".txt") for f in files), str(files))

    # 3. 越界路径无 broker → 403 且不执行
    outside = os.path.join(tmp, "outside.txt")
    with open(outside, "w") as f:
        f.write("x")
    r = file_delete(cfg, outside)
    check("越界返回 403", isinstance(r, dict) and r["code"] == 403, str(r))
    check("越界文件未被动", os.path.exists(outside))

    # 4. 不存在
    r = file_delete(cfg, os.path.join(workdir, "nope.txt"))
    check("不存在报错", isinstance(r, str) and "不存在" in r, str(r))

    # 5. 目录拒绝
    d = os.path.join(workdir, "adir")
    os.makedirs(d)
    r = file_delete(cfg, d)
    check("目录拒绝且未动", isinstance(r, str) and "目录" in r
          and os.path.isdir(d), str(r))

    # 6. 相对路径（按 workdir 解析）
    with open(os.path.join(workdir, "rel.txt"), "w") as f:
        f.write("r")
    r = file_delete(cfg, "rel.txt")
    check("相对路径删除成功", isinstance(r, str) and r.startswith("已安全删除"), str(r))
    check("trash 累计 3 个", len(os.listdir(trash)) == 3, str(os.listdir(trash)))

    shutil.rmtree(tmp)


def _a(cum, content="assistant"):
    return {"role": "assistant", "content": content,
            "usage": {"prompt_tokens": cum, "completion_tokens": 10}}


def _u(content="user"):
    return {"role": "user", "content": content}


def test_keep_recent():
    print("== keep_recent_by_usage_diff ==")
    # 匿名模型_wordle2 真实会话（2026-09-05 20:4x）assistant 累计 prompt_tokens
    # 位于 entries 索引 1,7,11,13,15,20,22（共 23 条）
    entries = [_u(), _a(1276), _u(), _u(), _u(), _u(), _u(), _u(), _a(19911),
               _u(), _u(), _u(), _a(26354), _u(), _a(26571), _u(), _a(26855),
               _u(), _u(), _u(), _u(), _a(27550), _u(), _a(27707)]
    budget = 16000  # config compression.keep_recent_tokens
    recent = keep_recent_by_usage_diff(entries, budget)

    # 差值: i1=1276, i7=18635, i11=643, i13=217, i15=284, i20=695, i22=157
    # newest→oldest: 157+695+284+217+643=1996 可容纳；i7=18635 超 → break
    # 期望保留 entries[8:]（15 条），旧 bug 下返回 []
    check("bug 回归: recent 非空", len(recent) > 0, f"len={len(recent)}")
    check("保留最新 5 轮 (entries[8:])", len(recent) == 15, f"len={len(recent)}")
    check("保留的是尾部连续段", recent == entries[len(entries) - len(recent):])
    check("时间顺序保持", [e["role"] for e in recent] ==
          [e["role"] for e in entries][len(entries) - len(recent):])

    # 最新一轮单独超预算 → []
    r2 = keep_recent_by_usage_diff([_u(), _a(50000)], 1000)
    check("最新轮超预算 → 空", r2 == [], str(r2))

    # 完全无 usage → 全部保留（旧行为一致）
    r3 = keep_recent_by_usage_diff([_u(), _u(), _u()], 10)
    check("无 usage → 全部保留", len(r3) == 3, f"len={len(r3)}")

    # 空输入
    check("空输入 → 空", keep_recent_by_usage_diff([], 100) == [])

    # 累计值下降（压缩后新上下文）→ diff 夹到 0，不崩
    r5 = keep_recent_by_usage_diff([_a(30000), _a(10000)], 5000)
    check("累计下降不崩且保留最新", len(r5) == 1
          and r5[0]["usage"]["prompt_tokens"] == 10000, str(r5))

    # 单条 assistant 恰好在预算内 → 保留
    r6 = keep_recent_by_usage_diff([_a(1000)], 1000)
    check("恰好在预算内 → 保留", len(r6) == 1)


def main():
    test_file_delete()
    test_keep_recent()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
