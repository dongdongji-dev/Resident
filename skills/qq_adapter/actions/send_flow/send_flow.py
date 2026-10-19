#!/usr/bin/env python3
"""send_flow — 把一个 markdown 文档当「发送队列」，逐条串行发出、发一条删一条。

口径（2026-10-19）：短小＋多串，一条一个点；表格渲染成图；
图片引用走 send_image；**不能并行发送**——每条拿到 message_id 才发下一条；
发出并核到回执后，把 md 里已发送的那块自动删掉。

用法：
  python3 send_flow.py private_1000000002 /home/sandbox/tmp/note.md
  python3 send_flow.py private_1000000002 - < /home/sandbox/tmp/note.md   # stdin 不删（无队列文件）
  python3 send_flow.py private_1000000002 /home/sandbox/tmp/note.xml      # 自动识别 xml 格式
  python3 send_flow.py private_1000000002 /home/sandbox/tmp/note.md --only-plan
  python3 send_flow.py private_1000000002 /home/sandbox/tmp/note.md --dry-run

md 里的写法：
  一行＝一条消息（太长按句子边界再切）。⚠️ **QQ 会把含换行的消息拆成多个气泡**
  （2026-10-19 实测：一条 659 字带 5 个换行的消息，对方看到 6 个气泡），
  所以每条消息内部绝不带换行——要分段就发多条，别用换行分段。`#` 标题、`- ` 列表、`**粗体**`、
  `` `代码` ``、`[文字](链接)` 都会被清成 QQ 能直接读的纯文本。
  图片：`![说明](/绝对路径.png)` 或整行只写一个图片绝对路径。
  文件：`[文件](/绝对路径.zip)`（也认 `[附件]`/`[file]`），或整行只写一个非图片的绝对路径
        ⇒ 走 send_file（QQ 上文件与图片是不同协议）。核对同图片：get_msg 核到 msgid 才算发出。
  markdown 表格：自动渲染成 PNG 发出，每张最多 --rows-per-img 行，超了切多张。

xml 格式（标签集与切分口径 2026-10-19 约定，`--format xml`，后缀 .xml 自动识别）：
  <text>…</text>   被包裹的是文本。**切分只按元素，不按换行**——一个 text 元素＝一条消息，
                   块内的换行原样交给 QQ（QQ 自己会按换行拆气泡，那是 QQ 的行为，我们不干预）。
    属性 split="true"    开＝本块按换行拆成多条消息发送（默认关＝一个元素一条消息）。
    属性 separate="true" 开＝块内的图片与表格各自单独占一条消息（默认关＝跟文字同一条气泡）。
  <table>…</table> 包裹的是 md 表格语法 ⇒ 渲染成 PNG 发出。**可以嵌进 text**（同 image）；
                   单独出现时自己占一条消息。
  <image>…</image> 内联元素，内容是图片绝对路径；可放进 text 里（走 QQ 的消息段混排）。
                   也认 <image path="…"/>。
  <file>…</file>   文件绝对路径，走 send_file；**不能放进 text**（QQ 上文件是独立消息）。
  <poke qq="…"/>   戳一戳，target 用 qq=（也认 target=/target_id=）；**不能放进 text**。
  规则：① 只认这五个标签，其余内容全当纯文本（正文里的 & 和尖括号不用转义）；
        ② 标签之间的空白忽略，标签外的裸文本按行各发一条；
        ③ 一条文本超过 --max-chars 会按句子边界再切（切点＝气泡边界），
           切完的最后一小段才带上它后面的内联图片/表格图。
  核对：文本走回执对账；混排消息、图片、文件走 get_msg 核 msgid；
        poke 无平台消息 id（适配器生成 poke_out_*），只认 ok:true，不做 get_msg。

选项：
  --delay 1.5        两条之间间隔秒（默认 1.5）
  --max-chars 160    单条文本上限
  --rows-per-img 6   一张表格图最多几行
  --tmp-dir DIR      表格图输出目录（默认 /home/sandbox/tmp/flow）
  --no-trim          发完不删 md（默认删）
  --force            跳过未读仲裁
  --no-receipt       跳过文本回执对账（**同时也不会删**——没核到就不删）
  --send-timeout 30  一次发送请求最多等多久（/hook 的 HTTP 超时）
  --msgid-timeout 20 没拿到 msgid 时，去消息库核这条的超时（**不重发**）
  --only-plan / --dry-run

撤回：发出去的消息**可撤回**——`python3 .skills/qq_adapter/actions/recall/recall.py <message_id>`
      （动作元素名＝`delete_msg`；message_id 就是本脚本每条成功时打印的那个 msgid）。
      例：`✓ 03 文「…」发送成功 msgid=1207381460` ⇒ `recall.py 1207381460`。
      撤回只撤 QQ 侧那条消息，队列文件里已删掉的内容不会回来。

输出口径（2026-10-19）：
  ① 先打**切分计划**：每条一个序号＋类型（文/图）＋前 5 字…后 5 字（短消息原样）；
     前缀够用——看前缀就知道上一条截到哪。
  ② 顺序发送，每成功一条打一行：`✓ 03 文「stdin 模…锁；只」发送成功 msgid=… 回执 hhmmss`
     （回执时间＝核对完成那一刻，只到时分秒）。
  ③ 某条发不出去＝**丢包**：打 `✗ 编号 类型「前缀…」丢包：<原因>`，随即中止，
     剩下的不发（不并行连发）。末尾打「串行发送完成 N 条」与队列剩余行数。
  ④ **拿不到 msgid 时不重发**（约定：重发＝同一条内容发两遍——请求超时
     或响应里没有 message_id，都不等于 QQ 侧没投递）。改成**超时核库**：
     在 `--msgid-timeout` 内轮询消息库，只看发送时刻之后新增的 out 记录，
     文本按正文比、图片/文件/戳按落库形态认（`<image file="base64://…"` / `[file]` / `[poke]`），
     **恰好一条**才算核到 ⇒ 打 `⚠ … 没拿到 msgid（原因）⇒ 不重发，按消息库对账补到 msgid=…` 并继续下一条；
     核不到、或窗口里同类型不止一条（歧义）⇒ 判丢包中止，队列里那块**不删**。

防坑（风险点逐条实现）：
  ① 并发：`<md>.lock` 独占锁（O_CREAT|O_EXCL 原子创建＋锁里写 pid），抢不到退出码 4。
     ⚠ 本机工作区是宿主机映射的共享文件系统，**flock 实测不生效**，别用 flock。
  ② 丢消息：文本要**回执对账通过**、图片要 **get_msg 核到 msgid** 才算「已发送」，
     核不到不删 md。
  ③ 发出后删除前崩溃：每条先在 `<md>.journal.jsonl` 记 msgid；下次启动先对账——
     核到就补删（不重发），核不到就报出来等人判。
  ④ 写坏文件：删块走「临时文件 + os.replace 原子替换」，首次删前备份 `<md>.bak`。
  ⑤ 行号漂移：删块按**内容哈希**定位（不是行号）；定位不到＝文件被别处改过 ⇒ 中止不猜。
     注：哈希会把块前后的空行一起吃掉（空行不承载内容，无害）；md 里两处完全相同的行，
     删的是靠前的那一处。
"""
from __future__ import annotations

import argparse
import atexit
import bisect
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hook import call, element, segment, text_segment  # noqa: E402
from _receipt import (_api_messages, _is_system_out, _load_seq, _save_seq,  # noqa: E402
                      norm as norm_db, verify)
from table_render import parse_md_table, render_table  # noqa: E402

IMG_EXT = ("png", "jpg", "jpeg", "gif", "webp")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
BARE_IMG_RE = re.compile(r"^\s*([^\s]+\.(?:%s))\s*$" % "|".join(IMG_EXT))
BARE_ANY_RE = re.compile(r"^\s*(/[^\s]+\.[A-Za-z0-9]{1,6})\s*$")
FILE_RE = re.compile(r"^\[(?:文件|附件|file|FILE)\]\(([^)]+)\)\s*$")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
HEAD_RE = re.compile(r"^#{1,6}\s*")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")


# ---------------------------------------------------------------- 文本清洗
def peek(s: str, n: int = 5) -> str:
    """给一条消息取「前 5 字…后 5 字」——前缀就够判断上一条截到哪。"""
    s = s.strip()
    if len(s) <= 2 * n:
        return s
    return f"{s[:n]}…{s[-n:]}"


def clean_line(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^>\s*", "", s)
    s = HEAD_RE.sub("", s)
    s = LIST_RE.sub("", s)
    s = LINK_RE.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", s)
    s = s.replace("**", "").replace("__", "").replace("`", "")
    s = re.sub(r"^\s*[-:|]+\s*$", "", s)
    s = re.sub(r"[\s]+", " ", s).strip()   # 内部换行也压掉：QQ 会按换行拆气泡
    return s


def split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in SENT_SPLIT_RE.split(text) if p]
    out, cur = [], ""
    for p in parts:
        if len(cur) + len(p) <= max_chars:
            cur += p
        else:
            if cur:
                out.append(cur)
            while len(p) > max_chars:
                out.append(p[:max_chars])
                p = p[max_chars:]
            cur = p
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- XML 格式
XML_TAG_RE = re.compile(r"<(/?)(text|table|image|file|poke)((?:\s+[^<>]*?)?)(/?)>", re.S)
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


class XmlError(Exception):
    pass


def _attrs(s: str) -> dict:
    return dict(ATTR_RE.findall(s or ""))


def _path_of(attrs: dict, inner: str) -> str:
    for k in ("path", "file", "src", "url"):
        if attrs.get(k):
            return attrs[k].strip()
    return (inner or "").strip()


def _inline_parts(inner: str) -> list[tuple[str, str]]:
    """text 块内容按内联 <image>/<table> 切成
    [("t", 文本) / ("i", 图片路径) / ("T", md 表格行)]，顺序保留。"""
    if re.search(r"</?(?:file|poke)\b", inner):
        raise XmlError("file/poke 不能放进 text 里（QQ 上它们是独立消息）")
    parts: list[tuple[str, str]] = []
    p = 0
    pat = re.compile(r"<(/?)(image|table)((?:\s+[^<>]*?)?)(/?)>", re.S)
    for m in pat.finditer(inner):
        if m.start() < p:
            continue
        if m.group(1):
            continue
        if m.start() > p:
            parts.append(("t", inner[p:m.start()]))
        attrs = _attrs(m.group(3))
        if m.group(4):
            parts.append(("i" if m.group(2) == "image" else "T", _path_of(attrs, "")))
            p = m.end()
        else:
            c = re.search(r"</%s>" % m.group(2), inner[m.end():], re.S)
            if not c:
                raise XmlError(f"<{m.group(2)}> 没有闭合")
            body = inner[m.end():m.end() + c.start()]
            parts.append(("i" if m.group(2) == "image" else "T",
                          _path_of(attrs, body) if m.group(2) == "image" else body))
            p = m.end() + c.end()
    if len(inner) > p:
        parts.append(("t", inner[p:]))
    return parts


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def _clean_keep_nl(s: str) -> str:
    """清洗一个 text 块的内容，**保留换行**（元素不拆时，换行原样交给 QQ）。"""
    out = [clean_line(l) for l in s.splitlines()]
    return "\n".join([l for l in out if l])


def _text_messages(inner: str, max_chars: int, split_nl: bool, separate: bool,
                   render) -> list[list[tuple[str, str]]]:
    """一个 <text> 块 → 若干条消息（每条 = 段列表，text/image 混排，顺序保留）。

    split_nl（属性 split=）：开＝块内换行拆成多条消息；关（默认）＝一个元素一条消息。
    separate（属性 separate=）：开＝块内图片/表格各自单独一条消息；
                              关（默认）＝图片与表格渲染出的图跟文字同一条气泡。
    超长文本按句子边界再切（切点＝气泡边界），切完的最后一小段才带上后面的内联媒体。
    """
    msgs: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []

    def close():
        if cur:
            msgs.append(list(cur))
            cur.clear()

    for kind, val in _inline_parts(inner):
        if kind in ("i", "T"):
            if kind == "i":
                segs = [("i", val.strip())]
            else:
                header, body = parse_md_table([l for l in val.splitlines() if l.strip()])
                if not header:
                    raise XmlError("<table> 里没有可用的 md 表格")
                segs = [("i", p) for p in render(header, body)]
            if separate:
                close()
                msgs.extend([[s] for s in segs])
            else:
                cur += segs
            continue
        if split_nl:
            for raw in val.splitlines():
                t = clean_line(raw)
                if not t:
                    continue
                close()
                for piece in split_long(t, max_chars):
                    cur.append(("t", piece))
            continue
        t = _clean_keep_nl(val)
        if not t:
            continue
        pieces = split_long(t, max_chars)
        for i, piece in enumerate(pieces):
            cur.append(("t", piece))
            if i < len(pieces) - 1:
                close()
    close()
    return msgs


def plan_xml(src: str, tmp_dir: Path, rows_per_img: int,
             max_chars: int) -> list[dict]:
    """xml 切发送序列。一个标签块＝一个（或几个）消息，块带 span/hash 供删块定位。"""
    lines = src.splitlines()
    offsets, o = [], 0
    for ln in lines:
        offsets.append(o)
        o += len(ln) + 1

    def line_of(pos: int) -> int:
        return max(0, bisect.bisect_right(offsets, pos) - 1)

    steps: list[dict] = []
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tbl_no = 0

    def render(header, body) -> list[str]:
        nonlocal tbl_no
        tbl_no += 1
        prefix = str(tmp_dir / f"tbl{tbl_no:02d}")
        return render_table(header, body, prefix, rows_per_img=rows_per_img)

    def loose(a: int, b: int) -> None:
        """标签外的裸文本：按行各发一条（整行作为删除单位）。"""
        if b <= a:
            return
        for ln in range(line_of(a), line_of(max(a, b - 1)) + 1):
            ls = offsets[ln]
            piece = src[max(a, ls):min(b, ls + len(lines[ln]))]
            t = clean_line(piece)
            if not t:
                continue
            for part in split_long(t, max_chars):
                steps.append({"kind": "text", "payload": part, "raw": lines[ln],
                              "span": (ln, ln + 1), "hash": block_hash([lines[ln]])})

    pos = 0
    for m in XML_TAG_RE.finditer(src):
        if m.start() < pos:
            continue                       # 被外层块吃掉了（text 里的内联 image）
        loose(pos, m.start())
        pos = m.end()
        if m.group(1):
            continue                       # 多余的闭合标签：忽略
        tag, attrs = m.group(2), _attrs(m.group(3))
        start_line = line_of(m.start())
        if m.group(4):
            inner, end_char = "", m.end()
        else:
            c = re.search(r"</%s>" % tag, src[m.end():], re.S)
            if not c:
                raise XmlError(f"<{tag}> 没有闭合")
            inner = src[m.end():m.end() + c.start()]
            end_char = m.end() + c.end()
        end_line = line_of(max(m.start(), end_char - 1))
        span = (start_line, end_line + 1)
        h = block_hash(lines[start_line:end_line + 1])
        raw = src[m.start():end_char]

        if tag == "text":
            split_nl = _truthy(attrs.get("split") or attrs.get("split-lines")
                                or attrs.get("nl-split"))
            separate = _truthy(attrs.get("separate") or attrs.get("separate-media")
                               or attrs.get("standalone"))
            for segs in _text_messages(inner, max_chars, split_nl, separate, render):
                if len(segs) == 1:
                    k, v = segs[0]
                    steps.append({"kind": "image" if k == "i" else "text",
                                  "payload": v, "span": span, "hash": h, "raw": raw})
                else:
                    steps.append({"kind": "mix", "payload": segs,
                                  "span": span, "hash": h, "raw": raw})
        elif tag == "table":
            header, body = parse_md_table([l for l in inner.splitlines() if l.strip()])
            if not header:
                raise XmlError("<table> 里没有可用的 md 表格")
            for p in render(header, body):
                steps.append({"kind": "image", "payload": p,
                              "span": span, "hash": h, "raw": raw})
        elif tag == "image":
            steps.append({"kind": "image", "payload": _path_of(attrs, inner),
                          "span": span, "hash": h, "raw": raw})
        elif tag == "file":
            steps.append({"kind": "file", "payload": _path_of(attrs, inner),
                          "span": span, "hash": h, "raw": raw})
        else:
            tgt = (attrs.get("qq") or attrs.get("target") or attrs.get("target_id")
                   or attrs.get("user_id") or inner.strip())
            if not tgt:
                raise XmlError('<poke> 要带目标，写成 <poke qq="1000000002"/>')
            steps.append({"kind": "poke", "payload": tgt,
                          "span": span, "hash": h, "raw": raw})
        pos = end_char                       # 整块（含闭合标签）已消费
    loose(pos, len(src))
    return steps


def detect_format(src: str) -> str:
    return "xml" if re.search(r"<(?:text|table|image|file|poke)[ >/]", src) else "md"


# ---------------------------------------------------------------- 块与哈希
def norm(lines: list[str]) -> str:
    return "\n".join(l.rstrip() for l in lines).strip()


def block_hash(lines: list[str]) -> str:
    return hashlib.sha1(norm(lines).encode("utf-8")).hexdigest()[:12]


def plan(md_lines: list[str], tmp_dir: Path, rows_per_img: int,
         max_chars: int) -> list[dict]:
    """切发送序列。每块带 span（行区间）与 hash（删块时定位用）。"""
    steps: list[dict] = []
    tmp_dir.mkdir(parents=True, exist_ok=True)
    i, tbl_no = 0, 0
    while i < len(md_lines):
        st = md_lines[i].strip()

        if st.startswith("|"):
            j = i
            while j < len(md_lines) and md_lines[j].strip().startswith("|"):
                j += 1
            header, body = parse_md_table(md_lines[i:j])
            if header:
                tbl_no += 1
                prefix = str(tmp_dir / f"tbl{tbl_no:02d}")
                title = None
                for k in range(i - 1, max(i - 8, -1), -1):
                    if md_lines[k].strip().startswith("#"):
                        title = clean_line(md_lines[k])
                        break
                for p in render_table(header, body, prefix, title=title,
                                      rows_per_img=rows_per_img):
                    steps.append({"kind": "image", "payload": p,
                                  "span": (i, j), "hash": block_hash(md_lines[i:j])})
            i = j
            continue

        m = FILE_RE.match(st)
        if m:
            steps.append({"kind": "file", "payload": m.group(1).strip(),
                          "span": (i, i + 1), "hash": block_hash([md_lines[i]])})
            i += 1
            continue
        m = BARE_ANY_RE.match(st)
        if m and m.group(1).rsplit(".", 1)[-1].lower() not in IMG_EXT:
            steps.append({"kind": "file", "payload": m.group(1),
                          "span": (i, i + 1), "hash": block_hash([md_lines[i]])})
            i += 1
            continue
        m = MD_IMG_RE.search(st)
        if m and st.startswith("!"):
            steps.append({"kind": "image", "payload": m.group(1).strip(),
                          "span": (i, i + 1), "hash": block_hash([md_lines[i]])})
            i += 1
            continue
        m = BARE_IMG_RE.match(st)
        if m:
            steps.append({"kind": "image", "payload": m.group(1),
                          "span": (i, i + 1), "hash": block_hash([md_lines[i]])})
            i += 1
            continue

        text = clean_line(md_lines[i])
        if text:
            h = block_hash([md_lines[i]])
            for piece in split_long(text, max_chars):
                steps.append({"kind": "text", "payload": piece,
                              "span": (i, i + 1), "hash": h})
        i += 1
    return steps


# ---------------------------------------------------------------- 发送与核对
def _send(elements, timeout: float = 60.0) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return call(elements, timeout=timeout)


def msgid_of(resp: dict) -> str | None:
    for it in (resp.get("result") or []):
        if it.get("ok") and it.get("message_id"):
            return str(it["message_id"])
    return None


def msg_exists(mid: str) -> bool:
    """get_msg 核这条消息真的存在（图片没有回执对账，只能这样核）。"""
    try:
        r = _send([element("get_msg", {"message_id": mid})])
    except SystemExit:
        return False
    s = json.dumps(r, ensure_ascii=False)
    return bool(r.get("ok")) and ('"time"' in s or '"sender"' in s)


def receipt_ok(conv: str, text: str, resp: dict) -> tuple[bool, str]:
    out = verify(conv, text, resp)
    return ("对账通过" in out), out


def _match_kind(m: dict, kind: str, want: str | None) -> bool:
    """消息库里这条 out 记录是不是我刚发的那类（用于无 msgid 时的对账）。

    文本按正文比；图片落库是 `<image file="base64://…">`（只有 base64，没有路径）；
    文件落库是 `[file]`；戳一戳落库是 `[poke]`——后三类只能按类型认，
    所以要求窗口内**只有一条**同类型的新 out，多了就是歧义。

    ⚠️ 规范化用 `_receipt.norm`（别名 norm_db）：本文件自己有个 `norm(lines)`
    （拼行算哈希用），同名会把 import 进来的那个盖掉——2026-10-19 踩过，
    传字符串进去按字符迭代，比对全假。
    """
    c = norm_db(m.get("content") or "")
    if kind == "text":
        return bool(want) and (c == want or c.startswith(want[:30]))
    if kind == "mix":
        return bool(want) and c.startswith(want[:30])
    if kind == "image":
        return c.startswith('<image file="base64://')
    if kind == "file":
        return c == "[file]"
    if kind == "poke":
        return c == "[poke]"
    return False


def reconcile(conv: str, kind: str, val, timeout: float,
              t_start: float) -> str | None:
    """没拿到 msgid 时**不重发**，改去消息库核这条到底出去了没有。

    为什么不能重发：请求超时／响应里没有 message_id，都不等于没发出去——
    QQ 侧很可能已经投递成功，重发＝同一条内容发两遍。
    判据：发送时刻之后新增的 out 记录里，按类型匹配（文本比正文，
    图片/文件/戳按落库形态认）；**恰好一条**才算核到，多条＝歧义 ⇒ 不认。
    """
    if kind == "text":
        want = re.sub(r"\s+", " ", val).strip()
    elif kind == "mix":
        want = re.sub(r"\s+", " ", " ".join(x[1] for x in val if x[0] == "t")).strip()
    else:
        want = None
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_start))
    deadline = t_start + timeout
    while True:
        try:
            msgs = _api_messages(conv, _load_seq(conv))
        except Exception:                                          # noqa: BLE001
            msgs = []
        outs = [m for m in msgs
                if m.get("direction") == "out"
                and (m.get("created_at") or "") >= since
                and not _is_system_out(m.get("content") or "")]
        hits = [m for m in outs if _match_kind(m, kind, want)]
        if len(hits) == 1:
            m = hits[0]
            try:
                _save_seq(conv, int(m.get("seq", 0)))
            except Exception:                                      # noqa: BLE001
                pass
            return str(m.get("remote_message_id") or "") or None
        if len(hits) > 1:
            return None                       # 歧义：不认、也不重发
        if time.time() >= deadline:
            return None
        time.sleep(2)


# ---------------------------------------------------------------- 队列文件
def side_path(md: Path, suffix: str) -> Path:
    """队列文件的伴生件：<md>.bak / <md>.journal.jsonl / <md>.lock。"""
    return Path(str(md) + suffix)


def journal_path(md: Path) -> Path:
    return side_path(md, ".journal.jsonl")


def acquire_lock(md: Path, stale_sec: int = 900) -> Path | None:
    """独占锁：<md>.lock 用 O_CREAT|O_EXCL 原子创建。

    为什么不用 flock：本机工作区是宿主机映射的共享文件系统，实测 flock 不生效
    （两个进程能同时加上锁），锁必须靠原子创建才靠得住。
    抢不到时看锁里写的 pid 是否还活着、锁是否超过 stale_sec——僵死的锁可以顶掉。
    """
    lp = side_path(md, ".lock")
    for _ in range(2):
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lp.read_text(encoding="utf-8").strip() or 0)
                age = time.time() - lp.stat().st_mtime
            except (ValueError, OSError):
                pid, age = 0, stale_sec + 1
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive and age < stale_sec:
                return None
            try:
                lp.unlink()
            except OSError:
                return None
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lp
    return None


def journal_read(md: Path) -> list[dict]:
    p = journal_path(md)
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def journal_write(md: Path, rec: dict) -> None:
    with journal_path(md).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_lines(md: Path) -> list[str]:
    return md.read_text(encoding="utf-8").splitlines()


def find_span(lines: list[str], h: str) -> tuple[int, int] | None:
    """按内容哈希在文件里定位这块（不靠行号）。"""
    n = len(lines)
    for start in range(n):
        for end in range(start + 1, min(n, start + 60) + 1):
            if block_hash(lines[start:end]) == h:
                return start, end
    return None


def trim(md: Path, h: str) -> tuple[bool, str]:
    """从 md 里删掉这一块（原子替换）。返回 (是否删成, 说明)。"""
    lines = read_lines(md)
    span = find_span(lines, h)
    if span is None:
        return False, "按哈希没在这份 md 里找到这块（文件被别处改过？）"
    start, end = span
    bak = side_path(md, ".bak")
    if not bak.exists():
        bak.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
    new = "\n".join(lines[:start] + lines[end:]).rstrip() + "\n"
    tmp = side_path(md, ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, md)
    return True, f"删 {end - start} 行（{start + 1}~{end}）"


def trim_raw(md: Path, raw: str) -> tuple[bool, str]:
    """xml 模式删块：按**原文精确切片**删掉这一块。

    为什么 xml 不用行号/行哈希：xml 里多个块能挤在同一行（内联 image、一行多标签），
    行不是删除单位——单位是「这一块原文」。定位方式与 md 同理：按内容找，不靠行号；
    找不到 ⇒ 文件被别处改过，中止不猜。同一块原文出现两次 ⇒ 删靠前的那一处。
    """
    txt = md.read_text(encoding="utf-8")
    idx = txt.find(raw)
    if idx < 0:
        return False, "按原文没在这份文件里找到这块（文件被别处改过？）"
    start = idx
    while start > 0 and txt[start - 1] != "\n":
        start -= 1
    end = idx + len(raw)
    line_end = end
    while line_end < len(txt) and txt[line_end] != "\n":
        line_end += 1
    if not txt[start:idx].strip() and not txt[end:line_end].strip():
        end = line_end + 1 if line_end < len(txt) else line_end   # 整行都是这块
    new = re.sub(r"\n{3,}", "\n\n", (txt[:start] + txt[end:])).strip() + "\n"
    bak = side_path(md, ".bak")
    if not bak.exists():
        bak.write_text(txt, encoding="utf-8")
    tmp = side_path(md, ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, md)
    return True, f"删 1 块（{len(raw)} 字）"


# ---------------------------------------------------------------- 主流程
def main() -> int:
    p = argparse.ArgumentParser(description="把 markdown 当发送队列，串行发出并删掉")
    p.add_argument("conversation", help="会话键：group_xxx / private_xxx")
    p.add_argument("file", help="markdown 文件路径；写 - 从 stdin 读（不删）")
    p.add_argument("--format", choices=("auto", "md", "xml"), default="auto",
                   help="输入格式（auto：看到 <text>/<table>/… 标签就当 xml）")
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--send-timeout", dest="send_timeout", type=float, default=30.0,
                   help="一次发送请求最多等多久（/hook 的 HTTP 超时，默认 30s）")
    p.add_argument("--msgid-timeout", dest="msgid_timeout", type=float, default=20.0,
                   help="没拿到 msgid 时去消息库核这条的超时（默认 20s；**不重发**）")
    p.add_argument("--max-chars", dest="max_chars", type=int, default=160)
    p.add_argument("--rows-per-img", dest="rows_per_img", type=int, default=6)
    p.add_argument("--tmp-dir", dest="tmp_dir", default="/home/sandbox/tmp/flow")
    p.add_argument("--no-trim", dest="no_trim", action="store_true")
    p.add_argument("--lock-stale", dest="lock_stale", type=int, default=900,
                   help="锁里的 pid 死了、或锁超过这么多秒 ⇒ 允许顶掉")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-receipt", dest="no_receipt", action="store_true")
    p.add_argument("--only-plan", dest="only_plan", action="store_true")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    a = p.parse_args()

    stdin_mode = a.file == "-"
    if stdin_mode:
        md_text = sys.stdin.read()
        md = None
    else:
        md = Path(a.file)
        if not md.is_file():
            print(f"✗ 文件不存在：{md}", file=sys.stderr)
            return 2
        md_text = md.read_text(encoding="utf-8")

    # ① 单实例锁
    if md is not None:
        lp = acquire_lock(md, a.lock_stale)
        if lp is None:
            print(f"✗ {md} 正被另一个 send_flow 占用（锁 {side_path(md, '.lock')} "
                  f"里的 pid 还活着）——本次退出", file=sys.stderr)
            return 4
        atexit.register(lambda: lp.unlink(missing_ok=True))

    fmt = detect_format(md_text) if a.format == "auto" else a.format
    lines = md_text.splitlines()

    def make_plan(txt: str) -> list[dict]:
        if fmt == "xml":
            return plan_xml(txt, Path(a.tmp_dir), a.rows_per_img, a.max_chars)
        return plan(txt.splitlines(), Path(a.tmp_dir), a.rows_per_img, a.max_chars)

    try:
        steps = make_plan(md_text)
    except XmlError as exc:
        print(f"✗ xml 写法有问题：{exc}", file=sys.stderr)
        return 2
    if not steps:
        print("✗ 文档里没有可发送的内容", file=sys.stderr)
        return 2

    # ③ 启动先对账：journal 里「已发未删」的块，核到就补删，不重发
    done_hashes: set[str] = set()
    if md is not None and not a.dry_run:
        for rec in journal_read(md):
            if rec.get("msgid") and not rec.get("removed_at"):
                if msg_exists(str(rec["msgid"])):
                    ok, why = (trim_raw(md, rec["raw"]) if rec.get("raw")
                               else trim(md, rec.get("hash", "")))
                    journal_write(md, {**rec, "removed_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S"), "note": "启动对账补删：" + why})
                    done_hashes.add(rec.get("hash", ""))
                    print(f"↻ 对账：msgid={rec['msgid']} 已发出，{why}")
                else:
                    print(f"⚠ journal 里 msgid={rec['msgid']} 核不到，"
                          f"这块（{rec.get('hash')}）不重发也不删，请人工看",
                          file=sys.stderr)
                    done_hashes.add(rec.get("hash", ""))
        if done_hashes:
            lines = read_lines(md)
            steps = make_plan("\n".join(lines))
            steps = [s for s in steps if s["hash"] not in done_hashes]
            if not steps:
                print("✅ 队列已清空（对账后没有待发内容）")
                return 0

    def label(s: dict) -> tuple[str, str]:
        k, v = s["kind"], s["payload"]
        if k == "image":
            return "图", Path(v).name
        if k == "file":
            return "件", Path(v).name
        if k == "poke":
            return "戳", str(v)
        if k == "mix":
            txt = " ".join(x[1] for x in v if x[0] == "t")
            ni = sum(1 for x in v if x[0] == "i")
            return "混", f"{peek(txt)} +{ni}图"
        return "文", peek(v)

    cnt = {k: sum(1 for s in steps if s["kind"] == k)
           for k in ("text", "mix", "image", "file", "poke")}
    print(f"切分计划（{fmt}）共 {len(steps)} 条（文 {cnt['text']} / 混 {cnt['mix']}"
          f" / 图 {cnt['image']} / 件 {cnt['file']} / 戳 {cnt['poke']}）")
    for n, s in enumerate(steps, 1):
        tag, head = label(s)
        print(f"  {n:02d} {tag}「{head}」")
    if a.only_plan:
        return 0

    sent = []
    trimmed: set[str] = set()
    for n, s in enumerate(steps, 1):
        kind, val = s["kind"], s["payload"]
        tag, head = label(s)
        if a.dry_run:
            print(f"  (dry-run) {n:02d} {tag}「{head}」")
            continue

        if kind == "text":
            val = re.sub(r"\s+", " ", val).strip()
        paths = [val] if kind in ("image", "file") else (
            [x[1] for x in val if x[0] == "i"] if kind == "mix" else [])
        for pv in paths:
            if not pv.startswith("/"):
                print(f"✗ {n:02d} {tag}「{head}」丢包：{pv} 不是绝对路径 ⇒ 中止，后面不发",
                      file=sys.stderr)
                return 3

        # 发送：一次请求。拿不到 msgid 就**核库超时**——绝不重发（重发＝同一条发两遍）
        mid, why, recovered = None, "", False
        t_start = time.time()
        try:
            if kind == "text":
                attrs = {"conversation_id": a.conversation}
                if a.force:
                    attrs["force_exec"] = "true"
                resp = _send([element("send_messages", attrs, [text_segment(val)])],
                             a.send_timeout)
            elif kind == "mix":
                segs = [text_segment(v) if t == "t" else segment("image", {"file": v})
                        for t, v in val]
                resp = _send([element("send_messages",
                                      {"conversation_id": a.conversation}, segs)],
                             a.send_timeout)
            elif kind == "poke":
                resp = _send([element("poke", {"conversation_id": a.conversation,
                                               "target_id": str(val)})], a.send_timeout)
            else:
                seg_tag = "file" if kind == "file" else "image"
                resp = _send([element("send_messages",
                                      {"conversation_id": a.conversation},
                                      [{"tag": seg_tag,
                                        "attributes": {"file": val},
                                        "content": []}])], a.send_timeout)
            mid = msgid_of(resp)
            if not mid:
                why = "适配器返回里没有 message_id"
        except SystemExit:
            why = f"适配器无响应（连不上 /hook，或请求超过 {a.send_timeout:.0f}s）"
        except Exception as exc:                                     # noqa: BLE001
            why = f"发送请求异常 {type(exc).__name__}: {exc}"

        if not mid:
            mid = reconcile(a.conversation, kind, val, a.msgid_timeout, t_start)
            recovered = bool(mid)
            if mid:
                print(f"⚠ {n:02d} {tag}「{head}」没拿到 msgid（{why}）"
                      f"⇒ 不重发，按消息库对账补到 msgid={mid}")
            else:
                print(f"✗ {n:02d} {tag}「{head}」丢包：{why}；"
                      f"{a.msgid_timeout:.0f}s 内核库也核不到这条 ⇒ 中止，后面不发",
                      file=sys.stderr)
                return 3

        # ② 核到才算发出去：文本走回执对账，图片/文件走 get_msg
        if recovered:
            stamp = time.strftime("%H%M%S")
        elif kind == "text" and not a.no_receipt:
            ok, out = receipt_ok(a.conversation, val, resp)
            stamp = time.strftime("%H%M%S")
            if not ok:
                if md is not None:
                    journal_write(md, {"hash": s["hash"], "msgid": mid,
                                       "kind": kind,
                                       "sent_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                print(f"✗ {n:02d} 文「{head}」丢包：回执对不上（msgid={mid}）"
                      f"⇒ 已记 journal、不删 md，中止\n  回执原文：{out}",
                      file=sys.stderr)
                return 5
        elif kind in ("image", "file", "mix"):
            if not msg_exists(mid):
                print(f"✗ {n:02d} {tag}「{head}」丢包：get_msg 核不到（msgid={mid}）"
                      f"⇒ 不删 md，中止", file=sys.stderr)
                return 5
            stamp = time.strftime("%H%M%S")
        else:
            stamp = time.strftime("%H%M%S")

        sent.append(mid)
        print(f"✓ {n:02d} {tag}「{head}」发送成功 msgid={mid} 回执 {stamp}")

        if md is not None and not a.no_trim:
            key = s.get("raw") or s["hash"]
            journal_write(md, {"hash": s["hash"], "raw": s.get("raw"), "msgid": mid,
                               "kind": kind,
                               "sent_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            if key in trimmed:
                print("    队列：这块已删过（同一块切成的多条消息）")
            else:
                ok, why = (trim_raw(md, s["raw"]) if fmt == "xml" and s.get("raw")
                           else trim(md, s["hash"]))
                if ok:
                    trimmed.add(key)
                    journal_write(md, {"hash": s["hash"], "raw": s.get("raw"),
                                       "msgid": mid, "kind": kind,
                                       "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "removed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                    print(f"    队列：{why}")
                else:
                    print(f"⚠ {why}——已发出（msgid={mid}），md 未改动",
                          file=sys.stderr)

        if n < len(steps):
            time.sleep(a.delay)

    print(f"✅ 串行发送完成 {len(sent)} 条")
    if sent:
        print(f"   撤回任一条：python3 .skills/qq_adapter/actions/recall/recall.py <msgid>"
              f"（动作元素 delete_msg）；本次 msgid：{' '.join(sent[:10])}")
    if md is not None and not a.no_trim:
        rest = read_lines(md)
        print(f"队列剩余 {sum(1 for l in rest if l.strip())} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
