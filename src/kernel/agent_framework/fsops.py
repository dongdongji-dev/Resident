"""fsops — workspace file operations executed INSIDE the runtime.

The kernel drives this module with ONE JSON job on stdin
(``{"op": "...", "args": {...}}``) and reads ONE JSON result from stdout
(``{"ok": true, "text": "..."}`` or ``{"ok": false, "error": "..."}``).
It is shipped into the runtime (sandbox: ``<workspace>/.resident/fsops.py``
via the bind mount, executed with the container's python3; host mode: run
directly with the kernel interpreter), so every file operation happens where
the workspace actually lives:

- all paths are RUNTIME paths, handed to the runtime verbatim — no
  host/container mapping and no path checks; isolation comes from the
  runtime boundary (sandbox container) itself;
- the same code serves both modes (sandbox python 3.12 / host python 3.10):
  stdlib only, no kernel imports.

Payload conventions: every text is UTF-8; results are formatted for the
agent (Chinese messages); ``rm_list`` additionally returns structured
``targets``/``lines`` for the kernel-side two-step confirmation store.
"""

from __future__ import annotations

import difflib
import fnmatch
import glob as globmod
import json
import os
import re
import shutil
import sys
import time
import uuid


# edit/replace 读取全文的内部安全网（1GB）：编辑工具按返回 diff 防撑，
# 不按文件大小拒绝——该值仅防极端超限文件，不暴露为配置。
_EDIT_READ_SAFETY_BYTES = 1 << 30


# ── small helpers ──────────────────────────────────────────────────────────

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _read_range(path: str, start: int, end: int) -> str:
    """Read rows [start, end] (1-based; end=0 means to EOF) lazily."""
    lines: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            if lineno < start:
                continue
            lines.append(line)
            if end and lineno >= end:
                break
    return "".join(lines)


def _read_full(path: str, max_bytes: int) -> tuple[str, int]:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ValueError(f"文件不存在或不可读: {path}") from exc
    if size > max_bytes:
        raise ValueError(
            f"文件 {path} 为 {size} 字节，超过读取上限 {max_bytes} 字节；"
            "请用 workspace_grep 定位后按范围读取，或确认是否需要大文件处理")
    return _read_text(path), size


def _write_text(path: str, content: str) -> None:
    """Create/overwrite a file, keeping any existing permissions."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(path).st_mode
    except OSError:
        mode = None
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    if mode is not None:
        os.chmod(path, mode)


def _count_lines(content: str) -> int:
    """Visual row count (cat -n semantics: a trailing newline does not add
    an extra row)."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def _line_numbers(text: str, start: int) -> str:
    """cat -n style: 6-digit right-aligned number + arrow (``     1→内容``);
    wider numbers fall back to ``N→``. ``start`` is the 1-based number of
    the first line (0 or negative = off)."""
    if not start or start <= 0:
        return text
    lines = text.splitlines() if text else []
    width = max(len(str(start + len(lines) - 1)), 6)
    numbered = []
    for offset, line in enumerate(lines):
        number = start + offset
        prefix = f"{number:>{width}}→"
        numbered.append(f"{prefix}{line}")
    return "\n".join(numbered)


def _unified_diff_text(path: str, old_text: str,
                       new_text: str) -> tuple[str, int, int]:
    """Unified diff hunks (context 3) plus removed/added line counts."""
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    hunks = list(difflib.unified_diff(
        old_lines, new_lines, fromfile=path, tofile=path, lineterm=""))
    removed = sum(1 for line in hunks if line.startswith("-")
                  and not line.startswith("---"))
    added = sum(1 for line in hunks if line.startswith("+")
                and not line.startswith("+++"))
    return "\n".join(hunks), removed, added


def _removed_lines(old_text: str, new_text: str) -> list[tuple[int, str]]:
    """(original line number, original text) for every deleted line."""
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    removed: list[tuple[int, str]] = []
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            for index in range(i1, i2):
                removed.append((index + 1, old_lines[index]))
    return removed


def _readonly(path: str) -> bool:
    """True when the file has no write bit (read-only mode)."""
    try:
        mode = os.stat(path).st_mode
        return (mode & 0o222) == 0
    except OSError:
        return False


def _truncate_output(text: str, max_chars: int) -> str:
    """Edit/replace 返回值防撑：超过 max_chars 截断并附省略提示。"""
    if len(text) <= max_chars:
        return text
    kept = text[:max_chars]
    omitted = len(text) - max_chars
    return kept + f"\n\n（后面的 {omitted} 字节已省略）"


def _human_size(nbytes: int) -> str:
    value = float(max(0, int(nbytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _save_cutoff(cfg: dict, text: str, prefix: str) -> str:
    """Persist full (would-be-truncated-away) content into the runtime cache
    dir; returns its runtime path ("" on failure). Nothing is discarded
    without a trace."""
    directory = str(cfg.get("truncated_dir") or "")
    if not text or not directory:
        return ""
    try:
        os.makedirs(directory, exist_ok=True)
        filename = (f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_"
                    f"{uuid.uuid4().hex[:6]}.txt")
        path = os.path.join(directory, filename)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(text))
        return os.path.abspath(path)
    except Exception:  # noqa: BLE001 — 缓存失败不掩盖主结果
        return ""


def _grep_lines(path: str, rx: "re.Pattern[str]",
                context_lines: int) -> tuple[list[int], list[tuple[int, str]]]:
    """(matching row indices, [(row_no, text)]) with optional context rows."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return [], []
    hits = [i for i, line in enumerate(lines) if rx.search(line)]
    if not hits:
        return [], []
    if context_lines <= 0:
        return hits, [(i + 1, lines[i].rstrip("\n")) for i in hits]
    used: set[int] = set()
    out: list[tuple[int, str]] = []
    for i in hits:
        for j in range(max(0, i - context_lines),
                       min(len(lines), i + context_lines + 1)):
            if j not in used:
                used.add(j)
                out.append((j + 1, lines[j].rstrip("\n")))
    out.sort()
    return hits, out


# ── operations ─────────────────────────────────────────────────────────────

def op_read(args: dict) -> dict:
    cfg = args["cfg"]
    path = str(args["path"])
    start = int(args.get("start", 1))
    end = int(args.get("end", 0))
    show_param = args.get("show_line_numbers")
    if start < 1:
        raise ValueError("start 必须 >= 1")
    if end and end < start:
        raise ValueError(f"end({end}) 不能小于 start({start})")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ValueError(f"文件不存在或不可读: {path}") from exc
    full_read = start == 1 and not end
    if full_read:
        if size > cfg["read_max_bytes"]:
            raise ValueError(
                f"文件 {path} 为 {size} 字节，超过读取上限 "
                f"{cfg['read_max_bytes']} 字节；"
                "可先用 workspace_grep 定位内容，或调整配置后重试")
        content = _read_text(path)
        total = _count_lines(content)
        if total > cfg["read_max_lines"]:
            raise ValueError(
                f"文件 {path} 共 {total} 行，超过读取上限 "
                f"{cfg['read_max_lines']} 行（会大量占用上下文）；"
                "请用 start/end 指定行范围，或先用 workspace_grep 定位")
    else:
        limit = (end - start + 1) if end else None
        if limit and limit > cfg["read_max_lines"]:
            raise ValueError(
                f"请求的 {limit} 行超过读取上限 {cfg['read_max_lines']} 行")
        content = _read_range(path, start, end)
        total = None
    show = cfg["read_default_line_numbers"] \
        if show_param is None else bool(show_param)
    body = _line_numbers(content, start if show else 0)
    warn = ""
    if full_read and size > cfg["read_warn_bytes"]:
        warn = (f"\n（文件较大，共 {size} 字节已完整读取；"
                "如需部分内容请用 start/end 指定行范围）")
    if total is None:
        return {"ok": True,
                "text": f"{path}（第 {start} 行起，{size} 字节）\n{body}"}
    return {"ok": True,
            "text": f"{path}（共 {total} 行，{size} 字节）\n{body}{warn}"}


def op_edit(args: dict) -> dict:
    cfg = args["cfg"]
    path = str(args["path"])
    old_string = str(args["old_string"])
    new_string = str(args.get("new_string", ""))
    replace_all = bool(args.get("replace_all", False))
    if not old_string:
        raise ValueError("old_string 不能为空")
    if len(old_string) > cfg["edit_max_old_chars"]:
        raise ValueError(
            f"old_string 超过上限 {cfg['edit_max_old_chars']} 字符；"
            "请缩小匹配范围")
    content, _size = _read_full(path, _EDIT_READ_SAFETY_BYTES)
    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"在 {path} 中未找到 old_string，请检查内容")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string 在 {path} 中出现 {count} 处；"
            "请提供更多上下文，或显式设置 replace_all=true")
    new_content = content.replace(old_string, new_string,
                                  -1 if replace_all else 1)
    diff, removed, added = _unified_diff_text(path, content, new_content)
    deleted = _removed_lines(content, new_content)
    ro_note = ("（注意：原文件权限为只读，仍已直接写入）\n"
               if _readonly(path) else "")
    _write_text(path, new_content)
    if not diff:
        return {"ok": True,
                "text": f"{ro_note}{path} 内容未发生变化"
                        "（old_string 与 new_string 相同）"}
    deleted_lines = "".join(
        f"- {line_no}| {text}\n" for line_no, text in deleted)
    base = f"{ro_note}已修改 {path}（-{removed} 行 +{added} 行）\n{diff}"
    if deleted_lines:
        base += f"\n被删除的行（行号|原文）：\n{deleted_lines}"
    return {"ok": True,
            "text": _truncate_output(base, cfg["edit_output_max_chars"])}


def op_replace(args: dict) -> dict:
    cfg = args["cfg"]
    path = str(args["path"])
    start_line = int(args["start_line"])
    end_line = int(args["end_line"])
    new_content = str(args.get("new_content", ""))
    if start_line < 1:
        raise ValueError("start_line 必须 >= 1")
    if end_line < start_line:
        raise ValueError(
            f"end_line({end_line}) 不能小于 start_line({start_line})")
    content, _size = _read_full(path, _EDIT_READ_SAFETY_BYTES)
    total = _count_lines(content)
    if start_line > total:
        raise ValueError(f"start_line({start_line}) 超出文件行数 {total}")
    if end_line > total:
        raise ValueError(f"end_line({end_line}) 超出文件行数 {total}")
    lines = content.splitlines()
    new_parts = (new_content or "").split("\n")
    if not new_content:
        new_parts = []
    new_lines = lines[:start_line - 1] + new_parts + lines[end_line:]
    new_text = "\n".join(new_lines)
    if new_text and content.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    diff, removed, added = _unified_diff_text(path, content, new_text)
    deleted = _removed_lines(content, new_text)
    ro_note = ("（注意：原文件权限为只读，仍已直接写入）\n"
               if _readonly(path) else "")
    _write_text(path, new_text)
    deleted_lines = "".join(
        f"- {line_no}| {text}\n" for line_no, text in deleted)
    base = (f"{ro_note}已替换 {path} 第 {start_line} 行到 "
            f"第 {end_line} 行（-{removed} 行 +{added} 行）\n{diff}")
    if deleted_lines:
        base += f"\n被删除的行（行号|原文）：\n{deleted_lines}"
    return {"ok": True,
            "text": _truncate_output(base, cfg["edit_output_max_chars"])}


def op_append(args: dict) -> dict:
    cfg = args["cfg"]
    path = str(args["path"])
    data = str(args.get("content") or "")
    if len(data) > cfg["write_max_bytes"]:
        raise ValueError(
            f"追加内容 {len(data)} 字节超过上限 {cfg['write_max_bytes']}")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(data)
    return {"ok": True, "text": f"已追加 {len(data)} 字节 -> {path}"}


def op_write(args: dict) -> dict:
    cfg = args["cfg"]
    path = str(args["path"])
    data = str(args.get("content") or "")
    if os.path.isdir(path):
        raise ValueError(
            f"{path} 是目录，workspace_write 需要文件路径（"
            "不支持整目录写入）")
    if len(data) > cfg["write_max_bytes"]:
        raise ValueError(
            f"写入内容 {len(data)} 字节超过上限 {cfg['write_max_bytes']}")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    backup = ""
    if os.path.exists(path):
        backup = path + ".old"
        try:
            _write_text(backup, _read_full(path, _EDIT_READ_SAFETY_BYTES)[0])
        except Exception:  # noqa: BLE001 — 备份失败不阻塞写入
            backup = ""
    _write_text(path, data)
    if backup:
        return {"ok": True,
                "text": f"已写入 {len(data)} 字节 -> {path}；"
                        f"原文件已备份为 {path}.old"}
    return {"ok": True, "text": f"已写入 {len(data)} 字节 -> {path}（新文件）"}


def op_glob(args: dict) -> dict:
    cfg = args["cfg"]
    pattern = str(args.get("pattern") or "")
    path = str(args.get("path") or "").strip() or str(cfg["root"])
    if not pattern:
        raise ValueError("pattern 不能为空")
    if not os.path.isdir(path):
        raise ValueError(f"{args.get('path') or path} 不是目录"
                         "（glob 需要目录作为搜索起点）")
    hits = [candidate for candidate in globmod.glob(
        os.path.join(path, pattern), recursive=True)
        if ".git" not in candidate.split(os.sep)]
    hits.sort()
    total = len(hits)
    limit = cfg["glob_max_results"]
    truncated = total > limit
    shown = hits[:limit]
    body = "\n".join(os.path.relpath(p, path) for p in shown)
    note = (f"\n（共 {total} 个结果，已截断至 {limit} 个；"
            "请考虑更具体的路径或模式）" if truncated
            else f"\n（共 {total} 个结果）")
    if truncated:
        full = "\n".join(os.path.relpath(p, path) for p in hits)
        saved = _save_cutoff(cfg, full, "glob_full")
        if saved:
            note += (f" 完整结果列表已保存到 {saved}，"
                     "可用 workspace_read 读取")
    return {"ok": True,
            "text": f"匹配文件：\n{body}{note}" if body else f"无匹配（{total}）"}


def op_grep(args: dict) -> dict:
    cfg = args["cfg"]
    pattern = str(args.get("pattern") or "")
    path_arg = str(args.get("path") or "").strip()
    glob_filter = str(args.get("glob") or "")
    output_mode = str(args.get("output_mode") or "content")
    ignore_case = bool(args.get("ignore_case", False))
    context_lines = int(args.get("context_lines", 0))
    hidden = bool(args.get("hidden", False))
    head_limit = args.get("head_limit")
    if not pattern:
        raise ValueError("pattern 不能为空")
    try:
        flags = re.IGNORECASE if ignore_case else 0
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"正则无效: {exc}") from exc
    base = path_arg or str(cfg["root"])
    limit = int(cfg["grep_head_limit"]) if head_limit is None \
        else int(head_limit)
    if output_mode not in ("content", "files_with_matches", "count"):
        raise ValueError(
            f"未知 output_mode: {output_mode}"
            "（可选 content / files_with_matches / count）")
    if context_lines < 0:
        raise ValueError("context_lines 不能为负")

    def _search_file(fp: str, rel: str) -> list[str]:
        out: list[str] = []
        try:
            with open(fp, "rb") as handle:
                head = handle.read(1024)
        except OSError:
            return out
        if b"\x00" in head:  # 二进制：跳过，防乱码
            return out
        matches, lines = _grep_lines(fp, rx, context_lines)
        if output_mode == "count":
            if matches:
                out.append(f"{rel}:{len(matches)}")
        elif output_mode == "files_with_matches":
            if matches:
                out.append(rel)
        else:
            for lineno, text in lines:
                shown = text if len(text) <= cfg["grep_max_columns"] \
                    else text[:cfg["grep_max_columns"]] + "…"
                out.append(f"{rel}:{lineno}:{shown}")
        return out

    if os.path.isfile(base):
        output = _search_file(base, os.path.basename(base))
    elif os.path.isdir(base):
        output = []
        for dirpath, dirnames, filenames in os.walk(base):
            if not hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in sorted(filenames):
                if not hidden and filename.startswith("."):
                    continue
                rel_dir = os.path.relpath(dirpath, base)
                rel = os.path.join(rel_dir, filename) \
                    if rel_dir != "." else filename
                if ".git" in rel.split(os.sep):
                    continue
                if glob_filter and not (fnmatch.fnmatch(rel, glob_filter)
                                        or fnmatch.fnmatch(filename,
                                                           glob_filter)):
                    continue
                fp = os.path.join(dirpath, filename)
                output.extend(_search_file(fp, rel))
    else:
        raise ValueError(f"{path_arg or base} 不存在"
                         "（grep 需要目录或文件路径）")
    output.sort()
    total = len(output)
    truncated = total > limit
    shown = output[:limit]
    note = (f"（共 {total} 条匹配，已截断至 {limit} 条；"
            "请缩小 pattern 或加大 head_limit）"
            if truncated else f"（共 {total} 条匹配）")
    if truncated:
        saved = _save_cutoff(cfg, "\n".join(output), "grep_full")
        if saved:
            note += (f" 完整匹配已保存到 {saved}，"
                     "可用 workspace_read 读取")
    return {"ok": True, "text": f"{note}\n" + "\n".join(shown)}


# ── rm: two-step confirmation (step 1 lists, step 2 deletes) ──────────────

def _rm_entry_line(path: str, skipped: bool = False) -> str:
    try:
        st = os.lstat(path)
        mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(st.st_mtime))
        if os.path.islink(path):
            kind, size = "l", "-"
        elif os.path.isdir(path):
            kind, size = "d", "-"
        else:
            kind, size = "f", _human_size(st.st_size)
    except OSError:
        kind, size, mtime = "?", "-", "-"
    note = "   [目录：recursive=false 将跳过]" if skipped else ""
    return (f"  {kind}  {size:>10}  {mtime}  "
            f"{os.path.abspath(path)}{note}")


def _rm_render(cfg: dict, header: str, lines: list[str]) -> str:
    cap = int(cfg["rm_max_entries"])
    shown = lines[:cap] if cap > 0 else lines
    text = header + ("\n" + "\n".join(shown) if shown else "")
    if len(lines) > len(shown):
        full = header + "\n" + "\n".join(lines)
        saved = _save_cutoff(cfg, full, "rm_result")
        text += f"\n…（还有 {len(lines) - len(shown)} 行未显示）"
        if saved:
            text += (f"；完整结果已保存到 {saved}，"
                     "可用 workspace_read 读取")
    return text


def op_rm_list(args: dict) -> dict:
    """第一步：只列举匹配项，不删除；返回清单文本 + 结构化 targets。"""
    cfg = args["cfg"]
    raw_path = str(args.get("path") or "").strip()
    recursive = bool(args.get("recursive", False))
    if not raw_path:
        raise ValueError("第一次调用必须提供 path（如 /tmp/*）")
    matches = sorted(globmod.glob(raw_path))
    if not matches:
        raise ValueError(f"没有匹配的条目：{raw_path}")
    root = os.path.realpath(str(cfg["root"]))
    targets: list[dict] = []
    target_lines: dict[str, str] = {}
    skipped: list[str] = []
    lines: list[str] = []
    for path in matches:
        if os.path.realpath(path) == root:
            raise ValueError("不允许删除工作区根目录")
        if os.path.islink(path) or os.path.isfile(path):
            line = _rm_entry_line(path)
            targets.append({"path": os.path.abspath(path), "kind": "file"})
            target_lines[os.path.abspath(path)] = line
            lines.append(line)
        elif os.path.isdir(path):
            if recursive:
                line = _rm_entry_line(path)
                targets.append({"path": os.path.abspath(path), "kind": "dir"})
                target_lines[os.path.abspath(path)] = line
                lines.append(line)
            else:
                skipped.append(path)
                lines.append(_rm_entry_line(path, skipped=True))
        else:
            skipped.append(path)
            lines.append(_rm_entry_line(path, skipped=True))
    if not targets:
        return {"ok": True, "empty": True,
                "text": ("没有可删除的文件（匹配到的目录需要 recursive=true）：\n"
                         + "\n".join(lines))}
    header = (f"将要删除 {len(targets)} 个条目（recursive="
              f"{str(recursive).lower()}"
              + ("，目录将被跳过" if skipped and not recursive else "")
              + "）：")
    return {"ok": True, "empty": False,
            "text": _rm_render(cfg, header, lines),
            "targets": targets, "lines": target_lines,
            "skipped": len(skipped)}


def op_rm_delete(args: dict) -> dict:
    """第二步：按第一步登记的目标执行删除，返回已删除清单。"""
    cfg = args["cfg"]
    targets = list(args.get("targets") or [])
    cached = dict(args.get("lines") or {})
    recursive = bool(args.get("recursive", False))
    confirm = str(args.get("confirm") or "")
    deleted, failed = [], []
    for item in targets:
        path, kind = str(item["path"]), str(item["kind"])
        try:
            if kind == "dir":
                shutil.rmtree(path)
            else:
                os.remove(path)
            deleted.append(path)
        except OSError as exc:
            failed.append(f"  ! {path}: {exc}")
    header = (f"已删除 {len(deleted)} 个条目（确认 uuid={confirm}"
              f"，recursive={str(recursive).lower()}）：")
    body = _rm_render(cfg, header,
                      [cached.get(path) or _rm_entry_line(path)
                       for path in deleted])
    if failed:
        body += "\n以下条目删除失败：\n" + "\n".join(failed)
    return {"ok": True, "text": body}


_OPS = {
    "read": op_read,
    "edit": op_edit,
    "replace": op_replace,
    "append": op_append,
    "write": op_write,
    "glob": op_glob,
    "grep": op_grep,
    "rm_list": op_rm_list,
    "rm_delete": op_rm_delete,
}


def execute(job: dict) -> dict:
    """Run one job dict; never raises — errors come back as ``ok=False``."""
    op = str(job.get("op") or "")
    args = job.get("args") or {}
    handler = _OPS.get(op)
    if handler is None:
        return {"ok": False, "error": f"未知操作: {op}"}
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 — 一律以 error 返回
        return {"ok": False, "error": str(exc)}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        job = json.loads(raw) if raw.strip() else {}
        result = execute(job)
    except Exception as exc:  # noqa: BLE001 — 协议层错误也要以 JSON 返回
        result = {"ok": False, "error": f"fsops 协议错误: {exc}"}
    sys.stdout.buffer.write(
        json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
