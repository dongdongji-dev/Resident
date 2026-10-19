#!/usr/bin/env python3
"""table_render — 把 markdown 表格渲染成「论文三线表」PNG（QQ 不渲染 markdown）。

被 send_flow 调用（xml 里写 `<table>` 即自动走这里），也可单独用：
  python3 table_render.py /home/sandbox/tmp/tbl.md /home/sandbox/tmp/out
  （第一个参数＝含 markdown 表格的文件；第二个参数＝输出前缀，生成 <前缀>_01.png …）

版式口径（2026-10-19 定稿）：
  · 风格五套（style 属性 / CLI --table-style）：
      three   默认论文三线表——顶线粗、表头下一条细线、底线粗，不画竖线，浅灰表头；
      compact 同三线表，字号小一档、行更紧（列多时用）；
      dark    黑表头白字＋列间白线＋整片灰底（不交错）；
      blue    蓝表头白字＋白/浅蓝交替行＋数据间细黑线；
      green   绿表头白字＋偶数行浅绿＋数据间细黑线。
  · 字体只有两种：中文仿宋_GB2312 ＋ 拉丁 Times New Roman，文件在本 action 的 `fonts/`，
    按文件名引用；字体文件因版权不进版本库，缺文件时打 warning 并回退系统字体（不抛异常）。
  · 画布固定 12 英寸 @150dpi（1800px），图宽不随内容变；列宽按字形实测（TextPath）分配，不靠字符数估。
  · 排布顺序：① 调列宽让每格单行 → ② 无解逐档缩字号（13→9pt，9pt 是仿宋可渲染下限）
    → ③ 表头折两行再试 → ④ 仍无解不折行直接输出。列间距硬性下限按列数分摊画布（48–90px）。
  · 默认整列居中；分隔行 :--- 左对齐、---: 右对齐。
  · 单元格内 **…** 局部加粗；整行全加粗＝合计行（上方补一条细线）。
  · 多行表头：上层某行只在一组第一列填字、其余留空＝跨列合并；上层单列且下层对应列空＝跨行合并。

密度口径（避免一张图信息量过大）：
  每张图最多 --rows-per-img 行（默认 6），超了自动切成多张，表头在每张上重复。
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.textpath import TextPath  # noqa: E402
from matplotlib import patheffects as pe  # noqa: E402

# 字体：**只用两种**——拉丁＝Times New Roman，
# 中文＝仿宋_GB2312。字体文件放在本目录 `fonts/` 下按文件名引用；因版权**不进版本库**
# （分发版默认不带），缺文件时打 warning 并回退系统字体，不抛异常。
HERE = Path(__file__).resolve().parent
FONT_DIR = HERE / "fonts"
LATIN_KEYS = ("times", "timesnewroman", "newroman")
CJK_KEYS = ("fangsong", "仿宋")


INK = "#1a1a1a"
HEAD_BG = "#f5f5f4"
ROW_SEP = "#efefef"
FIG_W, DPI = 12.0, 150      # 画布固定：图宽不随内容变（12 英寸＝1800px）
AX_L, AX_R = 0.012, 0.988   # 绘图区在图里的左右边界（列宽按这段算）
FS_MAX, FS_MIN = 13.0, 9.0  # 字号范围：优先缩字号让每格单行，到下限才折行
#   下限 9.0 是硬底线——仿宋_GB2312 在 8.5pt 及以下**整段不渲染**（实测 0 像素）。
LINE_H_IN = 0.34          # 一行文字占的图高（英寸）
PAD_PX = 72               # 相邻列文字之间的最小间距（像素，硬性下限）

STYLES = {
    # 默认：论文三线表（顶线粗＋表头下线细＋底线粗，无竖线，浅灰表头）
    "three":  dict(head_bg=HEAD_BG, head_fg=INK, top_lw=1.8, head_lw=0.9,
                   bottom_lw=1.8, row_sep=ROW_SEP, cell_edge=None, fs_delta=0,
                   pad_delta=0, head_center=True, zebra=None),
    # 紧凑三线：同三线表，字号小一档、行更紧（列多时用）
    "compact": dict(head_bg=HEAD_BG, head_fg=INK, top_lw=1.6, head_lw=0.8,
                    bottom_lw=1.6, row_sep=ROW_SEP, cell_edge=None, fs_delta=-1.5,
                    pad_delta=10, head_center=True, zebra=None),
    # 暗夜黑：表头黑底白字，列间白线，正文黑字**整片灰底**（不交错）
    "dark":   dict(head_bg="#111111", head_fg="#ffffff", top_lw=0, head_lw=0,
                   bottom_lw=1.2, row_sep=None, cell_edge="#ffffff",
                   fs_delta=0, pad_delta=0, head_center=True,
                   zebra=("#e6e6e6", "#e6e6e6")),
    # 专业蓝：蓝表头白字，正文白/浅蓝交替，数据间细黑线
    "blue":   dict(head_bg="#1f6ed4", head_fg="#ffffff", top_lw=0, head_lw=0,
                   bottom_lw=0.8, row_sep=None, cell_edge="#000000", edge_lw=0.6,
                   fs_delta=0, pad_delta=0, head_center=True,
                   zebra=("#ffffff", "#dbe9f7")),
    # 商务绿：绿表头白字，偶数行浅绿底，数据间细黑线
    "green":  dict(head_bg="#2e8b3d", head_fg="#ffffff", top_lw=0, head_lw=0,
                   bottom_lw=0.8, row_sep=None, cell_edge="#000000", edge_lw=0.6,
                   fs_delta=0, pad_delta=0, head_center=True,
                   zebra=("#ffffff", "#e3f3e3")),
}
DEFAULT_STYLE = "three"
NUM_RE = re.compile(r"^[0-9\s.,~\-–%×x*/]+(元|块|GB|TB|W|核|槽|人|条|页|起|万)?")


CJK_FALLBACKS = ("Noto Sans CJK SC", "Noto Serif CJK SC", "WenQuanYi Zen Hei",
                 "WenQuanYi Micro Hei", "AR PL UMing CN", "AR PL UKai CN",
                 "Droid Sans Fallback", "SimHei", "Microsoft YaHei")
LATIN_FALLBACKS = ("Liberation Serif", "DejaVu Serif", "Times New Roman", "DejaVu Sans")


def _sys_font(names: tuple) -> str | None:
    """在系统已注册字体里按优先级挑一个，返回字体名（都没有则 None）。"""
    have = {f.name for f in font_manager.fontManager.ttflist}
    for n in names:
        if n in have:
            return n
    return None


def setup_font() -> None:
    """引用字体：拉丁 Times New Roman ＋ 中文仿宋_GB2312（`fonts/` 里有文件时优先用）。

    字体文件因版权不进版本库，分发版默认不带——
    缺文件时**打 warning 并回退系统字体**（中文按 CJK_FALLBACKS 优先级挑，
    拉丁按 LATIN_FALLBACKS），不抛异常，渲染照常出图。
    """
    files = [f for f in sorted(FONT_DIR.iterdir())
             if f.suffix.lower() in (".ttf", ".otf", ".ttc")] if FONT_DIR.is_dir() else []
    latin = cjk = None
    for f in files:
        n = f.name.lower()
        if any(k in n for k in CJK_KEYS):
            cjk = f
        elif any(k in n for k in LATIN_KEYS):
            latin = f
    global _LATIN, _CJK, _LATIN_NAME, _CJK_NAME, FONT_USED
    _LATIN, _CJK = latin, cjk
    _LATIN_NAME = _CJK_NAME = None
    chain: list[str] = []
    for kind, path, fb, is_cjk in (("拉丁 Times New Roman", latin, LATIN_FALLBACKS, False),
                                   ("中文 仿宋_GB2312", cjk, CJK_FALLBACKS, True)):
        if path is not None:
            font_manager.fontManager.addfont(str(path))
            name = font_manager.FontProperties(fname=str(path)).get_name()
        else:
            name = _sys_font(fb)
            print(f"[table_render] warning: 缺 {kind} 字体文件，回退系统字体 "
                  f"{name or '（系统也没有，用 matplotlib 默认）'}", file=sys.stderr)
            if name:
                if is_cjk:
                    _CJK_NAME = name
                else:
                    _LATIN_NAME = name
        if name:
            chain.append(name)
    if chain:
        plt.rcParams["font.family"] = chain
        plt.rcParams["font.sans-serif"] = chain
        plt.rcParams["font.serif"] = chain
    plt.rcParams["axes.unicode_minus"] = False
    FONT_USED = "+".join(chain) or "matplotlib-default"


_LATIN = None
_CJK = None
_LATIN_NAME = None
_CJK_NAME = None


def _fp(cjk: bool, weight: str = "normal") -> FontProperties:
    """取 FontProperties：有自带字体文件走 fname，回退时走 family 名。"""
    p = _CJK if cjk else _LATIN
    if p is not None:
        return FontProperties(fname=str(p), weight=weight)
    nm = _CJK_NAME if cjk else _LATIN_NAME
    if nm:
        return FontProperties(family=[nm], weight=weight)
    return FontProperties(weight=weight)


_MEASURE_CACHE: dict = {}


def _segs(text: str) -> list[tuple[str, bool]]:
    """按 `**…**` 切成 (文字, 是否粗体) 段——支持单元格内局部加粗。"""
    text = text or ""
    out: list[tuple[str, bool]] = []
    bold, buf, i = False, "", 0
    while i < len(text):
        if text.startswith("**", i):
            if buf:
                out.append((buf, bold))
                buf = ""
            bold = not bold
            i += 2
            continue
        buf += text[i]
        i += 1
    if buf:
        out.append((buf, bold))
    return out


def measure_cell(raw: str, fs: float) -> float:
    """整格宽度：按 **粗** 分段分别量再相加。"""
    return sum(measure(t, fs, b) for t, b in _segs(raw))


def _runs(s: str) -> list[tuple[str, bool]]:
    """按中日韩／非中日韩切段（量宽度时每段用各自的字体）。"""
    out: list[tuple[str, bool]] = []
    buf, cur = "", False
    for ch in s:
        cjk = ord(ch) > 0x2E80
        if buf and cjk != cur:
            out.append((buf, cur))
            buf = ""
        cur = cjk
        buf += ch
    if buf:
        out.append((buf, cur))
    return out


def measure(s: str, fs: float, bold: bool = False) -> float:
    """按字形实测文本宽度（像素，DPI 下）。取 10pt 基准后按 fs/10 线性缩放即可。"""
    s = re.sub(r"\s+", " ", s or "").strip()
    if not s:
        return 0.0
    key = (s, round(fs, 2), bold)
    hit = _MEASURE_CACHE.get(key)
    if hit is not None:
        return hit
    total = 0.0
    for chunk, cjk in _runs(s):
        if not chunk.strip():                    # 纯空格：TextPath 会给出空路径
            total += len(chunk) * fs * 0.30
            continue
        fp = _fp(cjk, weight="bold" if bold else "normal")
        try:
            total += TextPath((0, 0), chunk, size=fs, prop=fp).get_extents().width
        except Exception:                        # 字形取不到时退回按字符数估
            total += units(chunk) * fs * 0.52
    val = total * DPI / 72.0
    _MEASURE_CACHE[key] = val
    return val


FONT_USED = "?"


def units(s: str) -> int:
    """显示宽度：中日韩字符算 2 个单位，其余算 1。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def tokens(s: str) -> list[str]:
    """切成不可拆的最小单元：拉丁词/数字串整体，CJK 单字，标点跟随前词。"""
    out: list[str] = []
    buf = ""
    for ch in s:
        if ch == " ":
            if buf:
                out.append(buf + " ")
                buf = ""
            continue
        if ord(ch) > 0x2E80:                 # CJK：单字成单元
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        elif ch.isdigit() or ch.isalpha() or ch in ".%,-~×x*/":
            if buf and not (buf[-1].isdigit() or buf[-1].isalpha()
                            or buf[-1] in ".%,-~×x*/"):
                out.append(buf)
                buf = ""
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
    if buf:
        out.append(buf)
    return out


def wrap(s: str, max_u: int) -> list[str]:
    """按显示宽度折行：只在空格/分隔符后断，绝不拆拉丁词与数字串。"""
    if max_u < 2:
        max_u = 2
    out: list[str] = []
    cur = ""
    for t in tokens(s):
        if units(cur) + units(t) <= max_u:
            cur += t
            continue
        if cur.strip():
            out.append(cur.rstrip())
        cur = t.lstrip()
        while units(cur) > max_u:            # 单 token 本身超宽（超长英文词）
            acc = ""
            for ch in cur:
                if units(acc) + units(ch) > max_u:
                    break
                acc += ch
            if not acc:
                acc = cur[0]
            out.append(acc)
            cur = cur[len(acc):]
    if cur.strip():
        out.append(cur.rstrip())
    return out or [""]


def _ha(a: str) -> str:
    return {"left": "left", "right": "right"}.get(a, "center")


def _x(x0: float, w: float, a: str) -> float:
    if a == "left":
        return x0 + 0.014
    if a == "right":
        return x0 + w - 0.014
    return x0 + w / 2


def is_num_col(header: list[str], body: list[list[str]], i: int) -> bool:
    """该列是否数字/价格列（右对齐）。"""
    vals = [r[i] for r in body if r[i].strip()]
    if not vals:
        return False
    hit = sum(1 for v in vals if NUM_RE.match(v.strip()) and any(c.isdigit() for c in v))
    return hit >= max(1, int(len(vals) * 0.6))


def parse_md_table(lines: list[str]) -> tuple[list, list[list[str]]]:
    """从 markdown 表格行里取表头与数据行（保持原签名，供 send_flow 调用）。"""
    h, b, _a = parse_table(lines)
    return h, b


def parse_table(lines: list[str]) -> tuple[list, list[list[str]], list[str]]:
    """取表头（可多行，支持跨列/跨行合并）／数据行／每列对齐。

    多行表头写法：分隔行（`|---|`）**之前**最后一行是叶子表头，它上面的行是上层表头，
    分隔行之后全是数据行。表头里的空单元格＝与左侧单元格合并（跨列）；叶子表头某列
    为空、正上方又有文字＝那个文字纵向合并占两行（跨行）。
    单元格用 `**粗**` 包裹＝粗体；整行都是粗体＝合计行（上方加一条细线）。
    """
    def align_of(c: str) -> str:
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            return "center"
        if c.endswith(":"):
            return "right"
        if c.startswith(":"):
            return "left"
        return "center"

    pre: list[list[str]] = []
    post: list[list[str]] = []
    aligns: list[str] | None = None
    for ln in lines:
        t = ln.strip()
        if not t.startswith("|"):
            continue
        cells = [c.strip() for c in t.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            if aligns is None:
                aligns = [align_of(c) for c in cells]
            continue
        (post if aligns is not None else pre).append(cells)
    if not pre and not post:
        return [], [], []
    if aligns is None:                      # 没有分隔行：首行当表头
        header_rows, body = pre[:1], pre[1:]
    else:
        header_rows, body = pre[:-1] + [pre[-1]], post
    ncol = max(len(r) for r in header_rows) if header_rows else 0
    if not ncol:
        return [], [], []
    header_rows = [r + [""] * (ncol - len(r)) for r in header_rows]
    body = [r + [""] * (ncol - len(r)) for r in body]
    aligns = (aligns or []) + ["center"] * ncol
    return header_rows, body, aligns[:ncol]


def _bold(s: str) -> tuple[str, bool]:
    s = (s or "").strip()
    if len(s) > 4 and s.startswith("**") and s.endswith("**"):
        return s[2:-2].strip(), True
    return s, False


def _is_total_row(row: list[str]) -> bool:
    """整行都是粗体＝合计行。"""
    vals = [c for c in row if c.strip()]
    if not vals:
        return False
    return all(_bold(c)[1] for c in vals)


def _header_groups(header_rows: list[list[str]]) -> list[dict]:
    """多行表头 → 合并块列表 {row,col,span,rowspan,text,bold}。"""
    nrow = len(header_rows)
    ncol = len(header_rows[-1])
    grid: list[list] = [[None] * ncol for _ in range(nrow)]
    groups: list[dict] = []
    for r, row in enumerate(header_rows):
        c = 0
        while c < ncol:
            txt, bold = _bold(row[c])
            span, c2 = 1, c + 1
            while c2 < ncol and not row[c2].strip():
                span += 1
                c2 += 1
            g = dict(row=r, col=c, span=span, rowspan=1, text=txt, bold=bold)
            groups.append(g)
            grid[r][c] = g
            for k in range(c + 1, c + span):
                grid[r][k] = "covered"
            c += span
    leaf = nrow - 1
    if leaf >= 1:
        for c in range(ncol):
            cell = grid[leaf][c]
            if cell == "covered" or (isinstance(cell, dict) and cell["text"]):
                continue
            above = grid[leaf - 1][c]
            if (isinstance(above, dict) and above["span"] == 1
                    and above["rowspan"] == 1 and above["text"]):
                above["rowspan"] = 2
                groups.remove(cell)
                grid[leaf][c] = "covered"
    return groups


def render_table(header, body: list[list[str]], out_prefix: str,
                 title: str | None = None, note: str | None = None,
                 rows_per_img: int = 6, style: str | None = None,
                 align: list[str] | None = None) -> list[str]:
    """渲染成 1~N 张 PNG（默认三线表），返回路径列表。

    header 可以是单行表头（list[str]）或多行表头（list[list[str]]，支持跨列/跨行合并）。
    style：three（默认）/ compact / dark / blue / green，见 STYLES；xml 里写
    `<table style="compact">` 由 send_flow 透传到这里。
    列宽按内容自适应，图宽随内容加宽（上限 MAX_W），**绝不把单词拆成竖排**。
    """
    setup_font()
    st = STYLES.get(style or DEFAULT_STYLE, STYLES[DEFAULT_STYLE])
    header_rows = [header] if header and isinstance(header[0], str) else list(header or [])
    if not header_rows:
        return []
    ncol = len(header_rows[-1])
    nrow = len(header_rows)
    out: list[str] = []
    aligns = ((align or []) + ["center"] * ncol)[:ncol]
    groups = _header_groups(header_rows)
    pad_px = PAD_PX + st["pad_delta"]

    chunks = [body[i:i + rows_per_img] for i in range(0, len(body), rows_per_img)]
    for ci, chunk in enumerate(chunks):
        req, tok = [], []
        for i in range(ncol):
            vals = [units(r[i]) for r in chunk]
            vals += [units(g["text"]) for g in groups
                     if g["row"] == nrow - 1 and g["col"] == i and g["span"] == 1]
            req.append(max(vals) if vals else 0)
            cells = [r[i] for r in chunk]
            cells += [g["text"] for g in groups if g["col"] <= i < g["col"] + g["span"]]
            tok.append(max([units(t) for c in cells for t in tokens(c)] + [2]))
        # 画布固定（FIG_W 不变）。口径：
        # ① 优先**调列宽**让每格一行放下；② 无解 → 字号小一号逐档试到 FS_MIN；
        # ③ 到下限仍无解 → 不折行，直接输出。
        # 硬性限制：每列文字之间至少留 GAP_PX 的间距（列宽＝文字真实宽度＋间距）。
        # 宽度用 TextPath 按字形实测（10pt 基准，随字号线性缩放），不靠字符数估算。
        canvas = FIG_W * DPI * (AX_R - AX_L)
        # 列间距：按列数分摊画布（列越多越窄），下限 48px、上限 90px——硬性最小间距
        gap = min(90.0, max(48.0, 0.34 * canvas / ncol)) + st["pad_delta"]
        body10 = [max([measure_cell(r[i], 10.0) for r in chunk] + [0.0])
                  for i in range(ncol)]
        head10 = [0.0] * ncol
        for g in groups:
            if g["row"] == nrow - 1 and g["span"] == 1:
                head10[g["col"]] = measure(g["text"], 10.0, True)
        grp10 = [(g, measure(g["text"], 10.0, True)) for g in groups if g["span"] > 1]
        fs_hi = FS_MAX + st["fs_delta"]

        def best2(text: str) -> tuple[float, list[str]]:
            """表头按词切成两行时最窄的摆法（切不了就是单行）。"""
            toks = [t for t in (text or "").split(" ") if t]
            full = measure(text, 10.0, True)
            if len(toks) < 2:
                return full, [text]
            best_w, best_lines = full, [text]
            for k in range(1, len(toks)):
                a, b = " ".join(toks[:k]), " ".join(toks[k:])
                w = max(measure(a, 10.0, True), measure(b, 10.0, True))
                if w < best_w:
                    best_w, best_lines = w, [a, b]
            return best_w, best_lines

        head2: dict[int, tuple[float, list[str]]] = {}
        for g in groups:
            if g["row"] == nrow - 1 and g["span"] == 1:
                head2[g["col"]] = best2(g["text"])
        grp2 = {id(g): best2(g["text"]) for g, _ in grp10}

        def plan(wrap_head: bool):
            fs, head_fs, base = FS_MIN, FS_MIN + 1.0, None
            cand = fs_hi
            while cand >= FS_MIN and base is None:
                for hoff in (1.0, 0.0, -0.5):    # 表头字号：先大一号，挤不下就同字号/小半号
                    hf = cand + hoff
                    if hf < FS_MIN:
                        continue
                    hw = [head2[i][0] if (wrap_head and i in head2) else head10[i]
                          for i in range(ncol)]
                    trial = [max(body10[i] * cand, hw[i] * hf) / 10.0 + gap
                             for i in range(ncol)]
                    for g, w in grp10:           # 分组表头要跨列放得下
                        ww = grp2[id(g)][0] if wrap_head else w
                        need = ww * hf / 10.0 + gap
                        cur = sum(trial[g["col"]:g["col"] + g["span"]])
                        if need > cur:
                            add = (need - cur) / g["span"]
                            for k in range(g["col"], g["col"] + g["span"]):
                                trial[k] += add
                    if sum(trial) <= canvas:
                        fs, head_fs, base = cand, hf, trial
                        break
                cand -= 0.5
            return fs, head_fs, base

        fs, head_fs, base = plan(False)
        if base is None:                        # 表头切成两行再试一轮
            fs, head_fs, base = plan(True)
            if base is not None:
                for g in groups:
                    if g["row"] == nrow - 1 and g["span"] == 1 and g["col"] in head2:
                        g["forced"] = head2[g["col"]][1]
                for g, _ in grp10:
                    g["forced"] = grp2[id(g)][1]
        if base is None:                       # 字号到下限也没解：直接输出，不折行
            base = [max(body10[i], head10[i] * 1.12) * FS_MIN / 10.0 + gap
                    for i in range(ncol)]
            base = [b * canvas / sum(base) for b in base]
        extra = canvas - sum(base)
        slack = [max(0.0, body10[i] - tok[i] * 0.72) for i in range(ncol)]
        s = sum(slack) or 1.0
        colpx = [base[i] + extra * slack[i] / s for i in range(ncol)]
        per_unit = fs * DPI / 140.0
        max_u = [10 ** 6] * ncol               # 一行直接输出
        fig_w = FIG_W

        totals = [_is_total_row(r) for r in chunk]
        row_h = [1] * len(chunk)               # 不折行：每行一行高

        for g in groups:                            # 表头折行与行高
            if g.get("forced"):
                g["lines"] = g["forced"]
                continue
            wu = sum(max_u[g["col"]:g["col"] + g["span"]]) - 2 * (g["span"] - 1)
            g["lines"] = wrap(g["text"], max(2, wu))
        hh = [1] * nrow
        for r in range(nrow):
            ls = [len(g["lines"]) for g in groups if g["row"] == r]
            if ls:
                # 上层表头折了行就多留一行空隙，避免和叶子表头挤在一起
                hh[r] = max(ls) + (1 if r < nrow - 1 and max(ls) > 1 else 0)
        for g in groups:                            # 跨行块要装得下
            if g["rowspan"] > 1:
                need_h = len(g["lines"])
                have = sum(hh[g["row"]:g["row"] + g["rowspan"]])
                if need_h > have:
                    add = int(math.ceil((need_h - have) / g["rowspan"]))
                    for k in range(g["row"], g["row"] + g["rowspan"]):
                        hh[k] += add

        title_h = 1 if title else 0
        note_h = 2 if note else 0
        lines_total = title_h + sum(hh) + sum(row_h) + note_h
        fig_h = LINE_H_IN * lines_total + 0.7
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=DPI)
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, lines_total + 0.5)
        fig.subplots_adjust(left=0.012, right=0.988, top=0.985, bottom=0.015)
        colw = [colpx[i] / canvas for i in range(ncol)]

        y = lines_total
        if title:
            t = title if ci == 0 else f"{title}（续 {ci + 1}）"
            ax.text(0.0, y - 0.45, t, ha="left", va="center",
                    fontsize=fs + 5, weight="bold", color=INK,
                    path_effects=[pe.withStroke(linewidth=0.6, foreground=INK)])
            y -= title_h

        if st["top_lw"]:
            ax.plot([0, 1], [y, y], color=INK, lw=st["top_lw"], solid_capstyle="butt")
        head_top = y
        ax.add_patch(plt.Rectangle((0, y - sum(hh)), 1, sum(hh),
                                   color=st["head_bg"], lw=0))
        for g in groups:                            # 表头（含合并块）
            top = head_top - sum(hh[:g["row"]])
            hgt = sum(hh[g["row"]:g["row"] + g["rowspan"]])
            x0 = sum(colw[:g["col"]])
            w = sum(colw[g["col"]:g["col"] + g["span"]])
            if len(g["lines"]) == 1:
                ax.text(_x(x0, w, aligns[g["col"]] if g["span"] == 1 else "center"),
                        top - hgt / 2, g["lines"][0],
                        ha=_ha(aligns[g["col"]] if g["span"] == 1 else "center"),
                        va="center", fontsize=head_fs, weight="bold",
                        color=st["head_fg"],
                        path_effects=[pe.withStroke(linewidth=0.6,
                                                    foreground=st["head_fg"])])
            else:
                yc = top - hgt / 2
                k0 = len(g["lines"]) // 2
                for k, ln in enumerate(g["lines"]):
                    ax.text(_x(x0, w, "center"), yc + (k0 - k) * 0.95, ln,
                            ha="center", va="center", fontsize=head_fs,
                            weight="bold", color=st["head_fg"],
                            path_effects=[pe.withStroke(linewidth=0.6,
                                                        foreground=st["head_fg"])])
        if any(g["span"] > 1 for g in groups):      # 分组表头画短竖线
            for g in groups:
                if g["span"] > 1:
                    xb = sum(colw[:g["col"] + g["span"]])
                    ax.plot([xb, xb], [head_top, head_top - 0.55],
                            color="#9a9a9a", lw=0.7, solid_capstyle="butt")
        if st["cell_edge"]:                      # 网格风格：表头逐格描边（列间线）
            elw = st.get("edge_lw", 0.8)
            for g in groups:
                top = head_top - sum(hh[:g["row"]])
                hgt = sum(hh[g["row"]:g["row"] + g["rowspan"]])
                gx = sum(colw[:g["col"]])
                gw = sum(colw[g["col"]:g["col"] + g["span"]])
                ax.add_patch(plt.Rectangle((gx, top - hgt), gw, hgt, facecolor="none",
                                           edgecolor=st["cell_edge"], lw=elw))
        y = head_top - sum(hh)
        if st["head_lw"]:
            ax.plot([0, 1], [y, y], color=INK, lw=st["head_lw"], solid_capstyle="butt")

        def draw_cell(x0: float, w: float, raw: str, size: float, a: str,
                      color: str, yy: float) -> None:
            """一格：按 **粗** 分段依次画（局部加粗），整格按对齐方式摆。"""
            segs = [(t, b) for t, b in _segs(raw) if t]
            if not segs:
                return
            widths = [measure(t, size, b) for t, b in segs]
            frac = sum(widths) / canvas
            pad = gap / 2.0 / canvas
            if a == "left":
                x = x0 + pad
            elif a == "right":
                x = x0 + w - pad - frac
            else:
                x = x0 + (w - frac) / 2
            for (t, b), wd in zip(segs, widths):
                ax.text(x, yy, t, ha="left", va="center", fontsize=size,
                        weight="bold" if b else "normal", color=color,
                        path_effects=[pe.withStroke(linewidth=0.7, foreground=color)]
                        if b else None)
                x += wd / canvas

        for r, row in enumerate(chunk):
            h = row_h[r]
            if totals[r] and st["head_lw"]:
                ax.plot([0, 1], [y, y], color=INK, lw=0.8, solid_capstyle="butt")
            x0 = 0.0
            for i in range(ncol):
                if st["cell_edge"]:
                    z = st.get("zebra") or ("#ffffff", "#f4f6fa")
                    ax.add_patch(plt.Rectangle((x0, y - h), colw[i], h, facecolor=(
                        z[0] if r % 2 == 0 else z[1]),
                        edgecolor=st["cell_edge"], lw=st.get("edge_lw", 0.8)))
                draw_cell(x0, colw[i], row[i], fs, aligns[i], INK, y - 0.55)
                x0 += colw[i]
            y -= h
            if st["row_sep"] and not totals[r]:
                ax.plot([0, 1], [y, y], color=st["row_sep"], lw=0.5,
                        solid_capstyle="butt")

        if st["bottom_lw"]:
            ax.plot([0, 1], [y, y], color=INK, lw=st["bottom_lw"],
                    solid_capstyle="butt")

        if note:
            for k, ln in enumerate(wrap(note, int((fig_w * DPI - PAD_PX) / per_unit))):
                ax.text(0.0, y - 0.6 - k, ln, ha="left", va="top",
                        fontsize=fs - 1, color="#444444")

        path = f"{out_prefix}_{ci + 1:02d}.png"
        plt.savefig(path)
        plt.close(fig)
        out.append(path)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    md = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    prefix = sys.argv[2]
    header, body, aligns = parse_table(md)
    if not header:
        print("✗ 文件里没有找到 markdown 表格", file=sys.stderr)
        return 2
    style = None
    for a in sys.argv[3:]:
        if a.startswith("--style="):
            style = a.split("=", 1)[1]
    paths = render_table(header, body, prefix, title="表格", style=style,
                         align=aligns)
    print("\n".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
