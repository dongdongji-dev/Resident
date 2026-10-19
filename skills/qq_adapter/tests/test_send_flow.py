#!/usr/bin/env python3
"""send_flow 的离线测试（不真发消息）。

跑法：python3 -m pytest tests/test_send_flow.py  或  python3 tests/test_send_flow.py
覆盖：markdown 清洗 / 长句切分 / 切分计划（表格切图、图片引用）/
      按哈希删块（原子替换＋.bak）/ 单实例锁 / 启动对账补删。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "actions" / "send_flow"))
sys.path.insert(0, str(HERE / "actions"))

import send_flow as sf  # noqa: E402

TMP = Path("/home/sandbox/tmp/test_send_flow")

def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")




def write(name: str, text: str) -> Path:
    TMP.mkdir(parents=True, exist_ok=True)
    p = TMP / name
    p.write_text(text, encoding="utf-8")
    for extra in (Path(str(p) + ".bak"), Path(str(p) + ".journal.jsonl"),
                  Path(str(p) + ".lock")):
        if extra.exists():
            extra.unlink()
    return p


class Clean(unittest.TestCase):
    def test_markdown_stripped(self):
        self.assertEqual(sf.clean_line("## **标题** 说明"), "标题 说明")
        self.assertEqual(sf.clean_line("- 列表项 `code`"), "列表项 code")
        self.assertEqual(sf.clean_line("> 引用"), "引用")
        self.assertEqual(sf.clean_line("[文字](https://x.y)"), "文字（https://x.y）")
        self.assertEqual(sf.clean_line("---"), "")
        self.assertEqual(sf.clean_line(""), "")

    def test_split_long_by_sentence(self):
        s = "。".join(["句" * 30] * 6) + "。"
        parts = sf.split_long(s, 100)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 100)
        self.assertEqual("".join(parts), s)
        self.assertEqual(sf.split_long("短句", 160), ["短句"])


class Plan(unittest.TestCase):
    MD = """# 标题

第一行正文。

| A | B |
| --- | --- |
| 1 | 二 |
| 3 | 四 |

![图](/home/sandbox/tmp/小主机_out2.png)
/home/sandbox/tmp/小主机_out2.png
"""

    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.steps = sf.plan(self.MD.splitlines(), TMP / "plan", 6, 160)

    def test_kinds_and_counts(self):
        kinds = [s["kind"] for s in self.steps]
        self.assertEqual(kinds.count("text"), 2)
        self.assertIn("image", kinds)

    def test_image_paths_absolute(self):
        for s in self.steps:
            if s["kind"] == "image":
                self.assertTrue(s["payload"].startswith("/"), s["payload"])

    def test_table_block_shared_hash(self):
        imgs = [s for s in self.steps if s["kind"] == "image"]
        tbl = [s for s in imgs if "plan/tbl" in s["payload"]]
        self.assertTrue(tbl)
        self.assertEqual(len({s["hash"] for s in tbl}), 1)
        self.assertEqual(len({s["span"] for s in tbl}), 1)

    def test_table_split_by_rows(self):
        md = "# t\n\n" + "\n".join(
            ["| A | B |", "| --- | --- |"] + [f"| {i} | x |" for i in range(9)])
        steps = sf.plan(md.splitlines(), TMP / "plan2", 6, 160)
        imgs = [s for s in steps if s["kind"] == "image"]
        self.assertGreaterEqual(len(imgs), 2)
        for p in {s["payload"] for s in imgs}:
            self.assertTrue(Path(p).is_file(), p)


    def test_file_reference(self):
        md = "正文。\n\n[文件](/home/sandbox/tmp/小主机三档.html)\n/home/sandbox/tmp/ds_mini_pc.txt\n"
        steps = sf.plan(md.splitlines(), TMP / "plan3", 6, 160)
        kinds = [s["kind"] for s in steps]
        self.assertEqual(kinds.count("file"), 2)
        for s in steps:
            if s["kind"] == "file":
                self.assertTrue(s["payload"].startswith("/"))

    def test_bare_image_path_is_image_not_file(self):
        md = "/home/sandbox/tmp/小主机_out2.png\n"
        steps = sf.plan(md.splitlines(), TMP / "plan4", 6, 160)
        self.assertEqual([s["kind"] for s in steps], ["image"])

    def test_peek_prefix_suffix(self):
        self.assertEqual(sf.peek("一二三四五"), "一二三四五")
        self.assertEqual(sf.peek("一二三四五六七八九十"), "一二三四五六七八九十")
        self.assertEqual(sf.peek("一二三四五六七八九十甲"), "一二三四五…七八九十甲")


class Trim(unittest.TestCase):
    MD = "第一行。\n\n第二行。\n\n第三行。\n"

    def test_trim_by_hash_and_bak(self):
        md = write("q.md", self.MD)
        lines = md.read_text(encoding="utf-8").splitlines()
        h = sf.block_hash([lines[2]])
        ok, why = sf.trim(md, h)
        self.assertTrue(ok, why)
        rest = md.read_text(encoding="utf-8")
        self.assertNotIn("第二行", rest)
        self.assertIn("第一行", rest)
        self.assertIn("第三行", rest)
        self.assertTrue(Path(str(md) + ".bak").is_file())

    def test_trim_missing_hash_aborts(self):
        md = write("q2.md", self.MD)
        ok, why = sf.trim(md, "deadbeef0000")
        self.assertFalse(ok)
        self.assertIn("没在这份 md 里找到", why)
        self.assertEqual(md.read_text(encoding="utf-8"), self.MD)

    def test_trim_survives_line_drift(self):
        md = write("q3.md", self.MD)
        sf.trim(md, sf.block_hash(["第一行。"]))
        # 行号已漂移，第二行现在是第 1 行——按哈希仍要能删掉
        ok, _ = sf.trim(md, sf.block_hash(["第二行。"]))
        self.assertTrue(ok)
        self.assertEqual(md.read_text(encoding="utf-8").strip(), "第三行。")


class Lock(unittest.TestCase):
    def test_second_instance_refuses(self):
        md = write("q4.md", "一条。\n")
        lp = Path(str(md) + ".lock")
        lp.write_text(str(os.getpid()), encoding="utf-8")   # 锁里＝本测试进程，活着
        try:
            r = subprocess.run(
                [sys.executable, str(HERE / "actions" / "send_flow" / "send_flow.py"),
                 "private_1000000002", str(md), "--only-plan"],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("正被另一个 send_flow 占用", r.stderr)
        finally:
            lp.unlink(missing_ok=True)

    def test_stale_lock_with_dead_pid_is_stolen(self):
        md = write("q7.md", "一条。\n")
        lp = Path(str(md) + ".lock")
        lp.write_text("2147483647", encoding="utf-8")       # 不存在的 pid
        r = subprocess.run(
            [sys.executable, str(HERE / "actions" / "send_flow" / "send_flow.py"),
             "private_1000000002", str(md), "--only-plan"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("一条", r.stdout)


class Reconcile(unittest.TestCase):
    def test_reconcile_via_subprocess(self):
        """journal 里「已发未删」且 get_msg 核到 ⇒ 启动时补删，不重发。"""
        md = write("q6.md", "已发的那条。\n没发的那条。\n")
        # 用一条真实发过的 msgid（消息库里存在），让启动对账核得到
        sf.journal_write(md, {"hash": sf.block_hash(["已发的那条。"]),
                              "msgid": "1074447037", "kind": "text",
                              "sent_at": "2026-01-01 00:00:00"})
        r = subprocess.run(
            [sys.executable, str(HERE / "actions" / "send_flow" / "send_flow.py"),
             "private_1000000002", str(md), "--only-plan"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("对账", r.stdout)
        rest = md.read_text(encoding="utf-8")
        self.assertNotIn("已发的那条", rest)
        self.assertIn("没发的那条", rest)


class Xml(unittest.TestCase):
    """xml 格式（标签集与切分口径 2026-10-19 约定）。"""

    def plan(self, src, rows=6, mx=160, sub="xplan"):
        return sf.plan_xml(src, TMP / sub, rows, mx)

    def test_element_is_the_unit_not_newline(self):
        steps = self.plan("<text>第一行\n第二行</text>")
        self.assertEqual([s["kind"] for s in steps], ["text"])
        self.assertIn("\n", steps[0]["payload"])

    def test_split_attr_splits_by_newline(self):
        steps = self.plan('<text split="true">第一行\n第二行</text>')
        self.assertEqual([s["payload"] for s in steps], ["第一行", "第二行"])

    def test_inline_image_shares_the_bubble(self):
        steps = self.plan("<text>看<image>/home/sandbox/tmp/小主机_out2.png</image>图</text>")
        self.assertEqual([s["kind"] for s in steps], ["mix"])
        self.assertEqual([t for t, _ in steps[0]["payload"]], ["t", "i", "t"])

    def test_separate_attr_makes_media_its_own_bubble(self):
        steps = self.plan('<text separate="true">看<image>'
                          '/home/sandbox/tmp/小主机_out2.png</image>图</text>')
        self.assertEqual([s["kind"] for s in steps], ["text", "image", "text"])

    def test_table_can_be_inline_in_text(self):
        src = ('<text>前<table>\n| A | B |\n| --- | --- |\n| 1 | 二 |\n</table>后</text>')
        steps = self.plan(src)
        self.assertEqual([s["kind"] for s in steps], ["mix"])
        imgs = [v for t, v in steps[0]["payload"] if t == "i"]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(Path(imgs[0]).is_file(), imgs[0])

    def test_file_and_poke_are_their_own_messages(self):
        steps = self.plan('<file>/home/sandbox/tmp/小主机三档.html</file>'
                          '<poke qq="1000000002"/>')
        self.assertEqual([s["kind"] for s in steps], ["file", "poke"])
        self.assertEqual(steps[1]["payload"], "1000000002")

    def test_file_inside_text_is_rejected(self):
        with self.assertRaises(sf.XmlError):
            self.plan('<text>正文<file>/home/sandbox/tmp/x.zip</file></text>')

    def test_unclosed_tag_is_rejected(self):
        with self.assertRaises(sf.XmlError):
            self.plan("<text>没闭合")

    def test_loose_lines_and_format_detection(self):
        steps = self.plan("裸文本一行\n第二行\n")
        self.assertEqual([s["payload"] for s in steps], ["裸文本一行", "第二行"])
        self.assertEqual(sf.detect_format("裸文本"), "md")
        self.assertEqual(sf.detect_format("<text>x</text>"), "xml")
        self.assertEqual(sf.detect_format("<poke qq=\"1\"/>"), "xml")

    def test_long_text_splits_at_bubble_boundary(self):
        long = "。".join(["句" * 40] * 6) + "。"
        steps = self.plan(f"<text>{long}<image>"
                          "/home/sandbox/tmp/小主机_out2.png</image></text>", mx=100)
        self.assertGreater(len(steps), 1)
        self.assertEqual(steps[-1]["kind"], "mix")
        for s in steps[:-1]:
            self.assertEqual(s["kind"], "text")
            self.assertLessEqual(len(s["payload"]), 100)

    def test_trim_raw_removes_the_block_once(self):
        src = ('<text>甲\n乙</text>\n<text split="true">丙\n丁</text>\n')
        md = write("q8.xml", src)
        steps = self.plan(md.read_text(encoding="utf-8"), sub="xplan2")
        self.assertEqual([s["kind"] for s in steps], ["text", "text", "text"])
        ok, why = sf.trim_raw(md, steps[0]["raw"])
        self.assertTrue(ok, why)
        rest = md.read_text(encoding="utf-8")
        self.assertNotIn("甲", rest)
        self.assertIn("丙", rest)
        self.assertIn("丁", rest)
        self.assertTrue(Path(str(md) + ".bak").is_file())

    def test_trim_raw_missing_aborts(self):
        md = write("q9.xml", "<text>甲</text>\n")
        ok, why = sf.trim_raw(md, "<text>不存在</text>")
        self.assertFalse(ok)
        self.assertIn("没在这份文件里找到", why)
        self.assertEqual(md.read_text(encoding="utf-8"), "<text>甲</text>\n")

    def test_inline_media_does_not_break_line_based_trim(self):
        """一行里挤着 text＋内联 image：删除单位是整块原文，不是整行。"""
        src = '<text>看<image>/home/sandbox/tmp/小主机_out2.png</image>图</text>\n尾巴\n'
        md = write("q10.xml", src)
        steps = self.plan(md.read_text(encoding="utf-8"), sub="xplan3")
        self.assertEqual(len(steps), 2)
        ok, _ = sf.trim_raw(md, steps[0]["raw"])
        self.assertTrue(ok)
        self.assertEqual(md.read_text(encoding="utf-8").strip(), "尾巴")




class NoMsgidReconcile(unittest.TestCase):
    """拿不到 msgid 时**不重发**，改按消息库核（2026-10-19 约定口径）。"""

    def setUp(self):
        self._api, self._load, self._save = sf._api_messages, sf._load_seq, sf._save_seq
        sf._load_seq = lambda conv: 0
        sf._save_seq = lambda conv, seq: None

    def tearDown(self):
        sf._api_messages, sf._load_seq, sf._save_seq = self._api, self._load, self._save

    def run_reconcile(self, msgs, kind, val, timeout=1.0):
        sf._api_messages = lambda conv, after, limit=300: msgs
        t0 = time.time() - 1
        return sf.reconcile("private_x", kind, val, timeout, t0)

    def test_text_matched_by_content(self):
        msgs = [{"seq": 10, "direction": "out", "remote_message_id": "111",
                 "content": "第一条 发送内容", "created_at": now()}]
        self.assertEqual(self.run_reconcile(msgs, "text", "第一条 发送内容"), "111")

    def test_text_mismatch_not_counted(self):
        msgs = [{"seq": 10, "direction": "out", "remote_message_id": "111",
                 "content": "别人从另一端发的", "created_at": now()}]
        self.assertIsNone(self.run_reconcile(msgs, "text", "我要发的正文", timeout=0.2))

    def test_image_file_poke_by_shape(self):
        now_s = now()
        self.assertEqual(self.run_reconcile(
            [{"seq": 3, "direction": "out", "remote_message_id": "555",
              "content": '<image file="base64://AAAA">', "created_at": now_s}],
            "image", "/home/sandbox/tmp/a.png"), "555")
        self.assertEqual(self.run_reconcile(
            [{"seq": 3, "direction": "out", "remote_message_id": "666",
              "content": "[file]", "created_at": now_s}],
            "file", "/home/sandbox/tmp/a.zip"), "666")
        self.assertEqual(self.run_reconcile(
            [{"seq": 3, "direction": "out", "remote_message_id": "poke_out_9_1",
              "content": "[poke]", "created_at": now_s}],
            "poke", "1000000002"), "poke_out_9_1")

    def test_ambiguous_two_hits_is_not_accepted(self):
        now_s = now()
        msgs = [{"seq": 3, "direction": "out", "remote_message_id": "1",
                 "content": "[file]", "created_at": now_s},
                {"seq": 4, "direction": "out", "remote_message_id": "2",
                 "content": "[file]", "created_at": now_s}]
        self.assertIsNone(self.run_reconcile(msgs, "file", "/x.zip", timeout=0.2))

    def test_records_before_send_are_ignored(self):
        msgs = [{"seq": 3, "direction": "out", "remote_message_id": "1",
                 "content": "[file]", "created_at": "2020-01-01 00:00:00"}]
        self.assertIsNone(self.run_reconcile(msgs, "file", "/x.zip", timeout=0.2))

    def test_system_out_ignored(self):
        msgs = [{"seq": 3, "direction": "out", "remote_message_id": "1",
                 "content": "[消息撤回] xxx", "created_at": now()}]
        self.assertIsNone(self.run_reconcile(msgs, "file", "/x.zip", timeout=0.2))

    def test_inbound_ignored(self):
        msgs = [{"seq": 3, "direction": "in", "remote_message_id": "1",
                 "content": "[file]", "created_at": now()}]
        self.assertIsNone(self.run_reconcile(msgs, "file", "/x.zip", timeout=0.2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
