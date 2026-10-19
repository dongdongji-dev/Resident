#!/usr/bin/env python3
"""table_render — 把 markdown 表格渲染成低密度的 PNG（QQ 不渲染 markdown）。

被 send_flow 调用，也可单独用：
  python3 table_render.py /home/sandbox/tmp/tbl.md /home/sandbox/tmp/out
  （第一个参数＝含 markdown 表格的文件；第二个参数＝输出前缀，生成 <前缀>_01.png …）

密度口径（图片避免只给一张信息量过大的；2026-10-19）：
  每张图最多 --rows-per-img 行（默认 6），超了自动切成多张，表头在每张上重复。
  列宽按实测文字宽度分配，单元格过宽自动折行，字号随宽度自适应。
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
FIG_W, DPI = 8.6, 150
LINE_H_IN = 0.34          # 一行文字占的图高（英寸）
PAD_PX = 44               # 单元格左右留白合计（像素）


def use_font() -> None:
    font_manager.fontManager.addfont(FONT)
    plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


def units(s: str) -> int:
    """显示宽度：中日韩字符算 2 个单位，其余算 1。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def wrap(s: str, max_u: int) -> list[str]:
    """按显示宽度折行，优先在空格处断。"""
    if max_u < 2:
        max_u = 2
    out: list[str] = []
    cur = ""
    for w in s.split(" "):
        add = w if not cur else " " + w
        if units(cur + add) <= max_u:
            cur += add
            continue
        if cur:
            out.append(cur)
            cur = ""
        while units(w) > max_u:
            acc = ""
            for ch in w:
                if units(acc) + units(ch) > max_u:
                    break
                acc += ch
            if not acc:
                acc = w[0]
            out.append(acc)
            w = w[len(acc):]
        cur = w
    if cur:
        out.append(cur)
    return out or [""]


def parse_md_table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """从 markdown 表格行里取表头与数据行。"""
    rows = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            continue                      # |---|---| 分隔行
        rows.append(cells)
    if not rows:
        return [], []
    header, body = rows[0], rows[1:]
    ncol = len(header)
    body = [r + [""] * (ncol - len(r)) for r in body]
    return header, body


def render_table(header: list[str], body: list[list[str]], out_prefix: str,
                 title: str | None = None, note: str | None = None,
                 rows_per_img: int = 6) -> list[str]:
    """渲染成 1~N 张 PNG，返回路径列表。"""
    use_font()
    ncol = len(header)
    out: list[str] = []

    chunks = [body[i:i + rows_per_img] for i in range(0, len(body), rows_per_img)]
    for ci, chunk in enumerate(chunks):
        # 列宽：按该列最宽单元格（含表头）的显示宽度比例分配
        widths = [max(units(header[i]),
                      *(units(r[i]) for r in chunk) if chunk else [0])
                  for i in range(ncol)]
        widths = [max(w, 6) for w in widths]
        total = sum(widths)
        cols = [w / total for w in widths]

        fs = 13.0
        for _ in range(3):
            per_unit = fs * DPI / 140.0
            ok = True
            for i in range(ncol):
                avail = cols[i] * FIG_W * DPI - PAD_PX
                need = max(units(header[i]),
                           *(units(r[i]) for r in chunk) if chunk else [0]) * per_unit
                if need > avail:
                    ok = False
            if ok:
                break
            fs = max(9.5, fs * 0.88)
        per_unit = fs * DPI / 140.0
        max_u = [max(2, int((cols[i] * FIG_W * DPI - PAD_PX) / per_unit))
                 for i in range(ncol)]

        grid = [[wrap(c, max_u[i]) for i, c in enumerate(r)] for r in chunk]
        head_lines = [wrap(h, max_u[i]) for i, h in enumerate(header)]
        row_h = [max(len(c) for c in g) for g in grid]
        head_h = max(len(c) for c in head_lines)

        title_h = 1 if title else 0
        note_h = 2 if note else 0
        lines_total = title_h + head_h + sum(row_h) + note_h
        fig_h = LINE_H_IN * lines_total + 0.7
        fig, ax = plt.subplots(figsize=(FIG_W, fig_h), dpi=DPI)
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, lines_total + 0.5)

        y = lines_total
        if title:
            t = title if ci == 0 else f"{title}（续 {ci + 1}）"
            ax.text(0.0, y - 0.45, t, ha="left", va="center",
                    fontsize=fs + 5, weight="bold", color="#1b2431")
            y -= title_h

        for i, h in enumerate(head_lines):
            x0 = sum(cols[:i])
            ax.add_patch(plt.Rectangle((x0, y - head_h), cols[i], head_h,
                                       color="#2f3b52"))
        for i, h in enumerate(head_lines):
            x0 = sum(cols[:i])
            for k, ln in enumerate(h):
                ax.text(x0 + cols[i] / 2, y - 0.55 - k, ln, ha="center",
                        va="center", fontsize=fs + 1, color="white",
                        weight="bold")
        y -= head_h

        for r, g in enumerate(grid):
            h = row_h[r]
            x0 = 0.0
            for i in range(ncol):
                ax.add_patch(plt.Rectangle((x0, y - h), cols[i], h,
                                           facecolor="#f4f6fa" if r % 2 == 0 else "#e8ecf4",
                                           edgecolor="#c8cfda", lw=0.8))
                x0 += cols[i]
            x0 = 0.0
            for i in range(ncol):
                for k, ln in enumerate(g[i]):
                    ax.text(x0 + 0.014, y - 0.55 - k, ln, ha="left", va="center",
                            fontsize=fs, color="#1b2431")
                x0 += cols[i]
            y -= h

        if note:
            for k, ln in enumerate(wrap(note, int((FIG_W * DPI - PAD_PX) / per_unit))):
                ax.text(0.0, y - 0.55 - k, ln, ha="left", va="top",
                        fontsize=fs - 1, color="#33405a")

        path = f"{out_prefix}_{ci + 1:02d}.png"
        plt.tight_layout(pad=0.5)
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
    header, body = parse_md_table(md)
    if not header:
        print("✗ 文件里没有找到 markdown 表格", file=sys.stderr)
        return 2
    paths = render_table(header, body, prefix, title="表格")
    print("\n".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
