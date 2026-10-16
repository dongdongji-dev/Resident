"""「配置」视图后端：config/ 下文件的安全读写 + YAML 结构化选项模型。

- 路径安全：resolve_config_file（realpath 防越界 + 扩展名白名单）。
- 文本读写：list/read/save（YAML 语法校验、备份、原子写）。
- 结构化编辑（前端「配置」视图的 YAML 表单）：
  build_config_schema 把 YAML 解析成「分区 → 选项」树（带注释说明、
  按当前值推断控件类型）；update_config_file 按行定位叶子键、**只替换
  值部分**（行内注释保留、文件其余部分逐字节不动），改后重新解析校验，
  备份后原子写入。仅允许修改已存在的键，不新增/删除结构。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

_CONFIG_EXTENSIONS = (".yaml", ".yml", ".md", ".txt", ".json")

_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(\s*)(.*)$")

# 块状标量指示符（``key: |`` / ``key: >-`` 等）：值是后续多行内容，
# 结构化编辑只替换值行会破坏块，故标记为不可编辑。
_BLOCK_SCALAR_RE = re.compile(r"^[|>](?:[1-9]?[+-]?|[+-]?[1-9]?)$")


# ── 路径与文本读写 ─────────────────────────────────────────────────────

def resolve_config_file(config_dir: str, rel: str) -> Optional[Path]:
    """把「配置」视图的相对路径解析为 config_dir 内的真实文件路径。

    防越界：realpath 后必须仍在 config_dir 内（覆盖 ../ 与符号链接
    逃逸），扩展名必须在白名单内。非法返回 None。"""
    base = Path(str(config_dir or "")).resolve()
    raw = str(rel or "").strip()
    if not base or not raw or "\x00" in raw:
        return None
    target = (base / raw).resolve()
    if target != base and base not in target.parents:
        return None
    if target.suffix.lower() not in _CONFIG_EXTENSIONS:
        return None
    return target


def list_config_files(config_dir: str) -> list[dict[str, Any]]:
    """列出 config_dir 下可编辑的文件（相对路径/大小/更新时间）。"""
    base = Path(str(config_dir or ""))
    files: list[dict[str, Any]] = []
    if not base.is_dir():
        return files
    for path in sorted(base.rglob("*")):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() not in _CONFIG_EXTENSIONS:
                continue
            rel = path.relative_to(base).as_posix()
            stat = path.stat()
        except OSError:
            continue
        files.append({
            "path": rel,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(
                stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return files


def read_config_file(config_dir: str, rel: str,
                     max_bytes: int) -> tuple[int, dict[str, Any]]:
    """读取一个配置文件的文本内容（返回 (http_code, payload)）。"""
    path = resolve_config_file(config_dir, rel)
    if path is None:
        return 400, {"ok": False,
                     "error": "路径不在 config 目录内或扩展名不允许"}
    if not path.is_file():
        return 404, {"ok": False, "error": f"文件不存在: {rel}"}
    try:
        size = path.stat().st_size
        if max_bytes > 0 and size > max_bytes:
            return 413, {"ok": False,
                         "error": f"文件 {size} 字节超过上限 {max_bytes}"}
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "path": rel, "size": size, "content": content}


def _write_with_backup(path: Path, rel: str, content: str, backup_dir: str,
                       max_bytes: int) -> tuple[int, dict[str, Any]]:
    """旧文件备份 → 原子写新内容（供 save/update 共用）。"""
    data = content.encode("utf-8")
    if max_bytes > 0 and len(data) > max_bytes:
        return 413, {"ok": False,
                     "error": f"内容 {len(data)} 字节超过上限 {max_bytes}"}
    backup = ""
    if path.is_file():
        try:
            directory = Path(str(backup_dir or ""))
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_rel = str(rel).strip("/").replace("/", "__")
            backup_path = directory / f"{safe_rel}-{stamp}.bak"
            shutil.copy2(path, backup_path)
            backup = str(backup_path)
        except OSError as exc:
            return 500, {"ok": False, "error": f"备份失败，未写入: {exc}"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return 500, {"ok": False, "error": f"写入失败: {exc}"}
    return 200, {"ok": True, "path": rel, "bytes": len(data), "backup": backup}


def save_config_file(config_dir: str, backup_dir: str, rel: str,
                     content: str,
                     max_bytes: int) -> tuple[int, dict[str, Any]]:
    """保存配置文件（整文件文本）：YAML 语法校验 → 备份 → 原子写。"""
    if not isinstance(content, str):
        return 400, {"ok": False, "error": "content 必须是字符串"}
    data = content.encode("utf-8")
    if max_bytes > 0 and len(data) > max_bytes:
        return 413, {"ok": False,
                     "error": f"内容 {len(data)} 字节超过上限 {max_bytes}"}
    path = resolve_config_file(config_dir, rel)
    if path is None:
        return 400, {"ok": False,
                     "error": "路径不在 config 目录内或扩展名不允许"}
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return 400, {"ok": False, "error": f"YAML 语法错误: {exc}"}
    return _write_with_backup(path, rel, content, backup_dir, max_bytes)


# ── YAML 行扫描（定位叶子键 + 收集注释） ───────────────────────────────

def _split_inline_comment(text: str) -> tuple[str, str]:
    """把「值 # 行内注释」拆成 (值, 注释)，引号内的 # 不算。"""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and index > 0 and text[index - 1] in " \t":
            return text[:index].rstrip(), text[index:]
        index += 1
    return text.rstrip(), ""


def _scan_yaml(text: str) -> tuple[dict[str, dict[str, Any]],
                                   dict[str, str],
                                   dict[str, dict[str, Any]]]:
    """扫描 YAML 文本，返回：

    - leaves: {dotted path: {"line", "indent", "key", "comment"}}（内联值行）
    - comments: {dotted path: 键上方注释块文本}
    - keys: {dotted path: {"line", "indent", "key", "has_inline"}}（所有键行）
    仅做缩进/正则级解析（值语义交给 yaml.safe_load），列表项/多行块不会
    误判为键。注释归属启发式：「# key: value」形式的注释块（被注释掉的
    备选键值，常见于本仓库配置）紧跟在上一个叶子键之后时，归给上一个
    键；其余注释块归给下一个键。"""
    leaves: dict[str, dict[str, Any]] = {}
    comments: dict[str, str] = {}
    keys: dict[str, dict[str, Any]] = {}
    stack: list[tuple[int, str]] = []
    pending: list[tuple[int, str]] = []
    pending_prev: tuple[str, int, Optional[str]] = ("", -1, None)
    # 上一行信息（用于注释块归属）：kind ∈ {"leaf", "container", "blank", "other"}
    prev_kind, prev_indent = "", -1
    last_leaf_path: Optional[str] = None
    commented_kv = re.compile(r"^#\s*[\w.\-]+\s*:\s+\S")

    def _flush_pending(key_indent: int, path: str) -> None:
        nonlocal pending, pending_prev
        body = [text for ind, text in pending if ind == key_indent and text]
        block_indent = pending[0][0] if pending else -1
        mode, indent, leaf_path = pending_prev
        belongs_prev = (
            mode == "leaf" and indent == key_indent == block_indent
            and leaf_path and any(commented_kv.match("# " + t)
                                  for _, t in pending))
        if body:
            if belongs_prev:
                existing = comments.get(leaf_path or "", "")
                merged = "\n".join(filter(None, [existing, "\n".join(body)]))
                comments[leaf_path or ""] = merged
            else:
                comments[path] = "\n".join(body)
        pending = []
        pending_prev = ("", -1, None)

    for index, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            pending = []
            pending_prev = ("", -1, None)
            prev_kind, prev_indent = "blank", -1
            continue
        if stripped.startswith("#"):
            indent = len(line) - len(line.lstrip())
            if not pending:
                pending_prev = (prev_kind, prev_indent, last_leaf_path)
            pending.append((indent, stripped.lstrip("#").strip()))
            prev_kind, prev_indent = "comment", indent
            continue
        match = _KEY_RE.match(line)
        if not match:
            pending = []
            prev_kind, prev_indent = "other", -1
            continue
        indent = len(match.group(1))
        key = match.group(2)
        value_part, inline_comment = _split_inline_comment(match.group(4))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([name for _, name in stack] + [key])
        _flush_pending(indent, path)
        keys[path] = {"line": index, "indent": indent, "key": key,
                      "has_inline": bool(value_part)}
        if not value_part:
            stack.append((indent, key))
            prev_kind, prev_indent = "container", indent
            continue
        if _BLOCK_SCALAR_RE.match(value_part):
            stack.append((indent, key))
            prev_kind, prev_indent = "other", -1
            continue
        leaves[path] = {"line": index, "indent": indent, "key": key,
                        "comment": inline_comment}
        last_leaf_path = path
        stack.append((indent, key))
        prev_kind, prev_indent = "leaf", indent
    return leaves, comments, keys


def _dump_inline(value: Any) -> str:
    """把值序列化成单行 YAML（多行字符串转义为双引号单行）。"""
    text = yaml.safe_dump(value, default_flow_style=True,
                          allow_unicode=True, width=10 ** 9,
                          sort_keys=False).strip()
    if text.endswith("..."):
        text = text[:-3].rstrip()
    if "\n" in text:
        text = json.dumps(value, ensure_ascii=False)
    if "\n" in text:  # json 理论上不会，兜底
        raise ValueError("无法序列化为单行")
    return text


def _get_path(doc: Any, path: str) -> Any:
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def _coerce_new_value(current: Any, new: Any) -> Any:
    """按当前值的类型约束新值（类型不匹配抛 ValueError）。"""
    if isinstance(current, bool):
        if not isinstance(new, bool):
            raise ValueError("需要布尔值")
        return new
    if isinstance(current, int):
        if isinstance(new, bool):
            raise ValueError("需要整数")
        if isinstance(new, int):
            return new
        if isinstance(new, float) and new.is_integer():
            return int(new)
        raise ValueError("需要整数")
    if isinstance(current, float):
        if isinstance(new, bool):
            raise ValueError("需要数字")
        if isinstance(new, (int, float)):
            return float(new)
        raise ValueError("需要数字")
    if isinstance(current, str):
        if not isinstance(new, str):
            raise ValueError("需要字符串")
        return new
    if isinstance(current, list):
        if isinstance(new, str):
            new = yaml.safe_load(new)
        if not isinstance(new, list):
            raise ValueError("需要列表")
        if any(isinstance(item, (list, dict)) for item in new):
            raise ValueError("列表元素需为标量")
        return new
    if isinstance(current, dict):
        if isinstance(new, str):
            new = yaml.safe_load(new)
        if not isinstance(new, dict):
            raise ValueError("需要映射（YAML 文本）")
        return new
    raise ValueError(f"不支持的值类型: {type(current).__name__}")


# ── 结构化模型（「分区 → 选项」树）与按行更新 ──────────────────────────

def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "str_list"
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "other"


def _block_list_paths(doc: dict[str, Any], leaves: dict[str, dict[str, Any]],
                      keys: dict[str, dict[str, Any]]) -> set[str]:
    """找出「块状标量列表」的路径（key: 空值 + 后续 - item 行，可安全
    折叠为单行流式列表编辑）。"""
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value:
                walk(value, path)
            elif isinstance(value, list) and all(
                    isinstance(item, str) for item in value):
                info = keys.get(path)
                if (path not in leaves and info is not None
                        and not info["has_inline"]):
                    found.add(path)

    walk(doc, "")
    return found


def _build_group(key: str, path: str, value: dict[str, Any],
                 leaves: dict[str, dict[str, Any]],
                 comments: dict[str, str],
                 block_lists: set[str],
                 editable_top: Optional[list[dict[str, Any]]] = None) ->         dict[str, Any]:
    node: dict[str, Any] = {
        "key": key,
        "path": path,
        "comment": comments.get(path, ""),
        "items": [],
        "children": [],
    }
    for child_key, child_value in value.items():
        child_path = f"{path}.{child_key}"
        if isinstance(child_value, dict) and child_value:
            node["children"].append(_build_group(
                child_key, child_path, child_value, leaves, comments,
                block_lists))
        else:
            node["items"].append({
                "key": child_key,
                "path": child_path,
                "type": _value_type(child_value),
                "value": child_value,
                "comment": comments.get(child_path, ""),
                "editable": (child_path in leaves
                             or child_path in block_lists),
            })
    return node


def _build_groups(doc: dict[str, Any], leaves: dict[str, dict[str, Any]],
                  comments: dict[str, str],
                  block_lists: set[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    top_items: list[dict[str, Any]] = []
    for key, value in doc.items():
        if isinstance(value, dict) and value:
            groups.append(_build_group(key, key, value, leaves, comments,
                                       block_lists))
        else:
            top_items.append({
                "key": key,
                "path": key,
                "type": _value_type(value),
                "value": value,
                "comment": comments.get(key, ""),
                "editable": key in leaves or key in block_lists,
            })
    if top_items:
        groups.insert(0, {"key": "（顶层）", "path": "", "comment": "",
                          "items": top_items, "children": []})
    return groups


def build_config_schema(config_dir: str, rel: str,
                        max_bytes: int) -> tuple[int, dict[str, Any]]:
    """读取 YAML 并返回「分区 → 选项」结构（供前端表单渲染）。"""
    path = resolve_config_file(config_dir, rel)
    if path is None:
        return 400, {"ok": False,
                     "error": "路径不在 config 目录内或扩展名不允许"}
    if path.suffix.lower() not in (".yaml", ".yml"):
        return 400, {"ok": False, "error": "仅 YAML 文件支持结构化编辑"}
    if not path.is_file():
        return 404, {"ok": False, "error": f"文件不存在: {rel}"}
    try:
        size = path.stat().st_size
        if max_bytes > 0 and size > max_bytes:
            return 413, {"ok": False,
                         "error": f"文件 {size} 字节超过上限 {max_bytes}"}
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return 400, {"ok": False, "error": "配置文件不是 UTF-8 编码"}
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return 400, {"ok": False, "error": f"YAML 语法错误: {exc}"}
    if not isinstance(doc, dict):
        return 400, {"ok": False, "error": "配置根节点必须是映射"}
    leaves, comments, keys = _scan_yaml(text)
    block_lists = _block_list_paths(doc, leaves, keys)
    return 200, {"ok": True, "path": rel,
                 "groups": _build_groups(doc, leaves, comments, block_lists)}


def update_config_file(config_dir: str, backup_dir: str, rel: str,
                       updates: Any, max_bytes: int) -> tuple[
                           int, dict[str, Any]]:
    """按「选项路径 → 新值」更新 YAML：只替换目标行冒号后的值。

    - 仅允许修改已存在的键（结构不变）；
    - 按当前值类型约束新值（dict/list 也可用 YAML 文本传）；
    - 行内注释保留，其余行逐字节不动；
    - 改后 yaml.safe_load 复验（语法 + 每个目标路径的新值）；
    - 校验全过才备份并原子写入。"""
    if not isinstance(updates, dict) or not updates:
        return 400, {"ok": False, "error": "updates 必须是非空对象"}
    path = resolve_config_file(config_dir, rel)
    if path is None:
        return 400, {"ok": False,
                     "error": "路径不在 config 目录内或扩展名不允许"}
    if path.suffix.lower() not in (".yaml", ".yml"):
        return 400, {"ok": False, "error": "仅 YAML 文件支持结构化编辑"}
    if not path.is_file():
        return 404, {"ok": False, "error": f"文件不存在: {rel}"}
    try:
        size = path.stat().st_size
        if max_bytes > 0 and size > max_bytes:
            return 413, {"ok": False,
                         "error": f"文件 {size} 字节超过上限 {max_bytes}"}
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return 400, {"ok": False, "error": "配置文件不是 UTF-8 编码"}
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    try:
        doc = yaml.safe_load(original)
    except yaml.YAMLError as exc:
        return 400, {"ok": False, "error": f"YAML 语法错误: {exc}"}
    if not isinstance(doc, dict):
        return 400, {"ok": False, "error": "配置根节点必须是映射"}

    leaves, _comments, keys = _scan_yaml(original)
    block_lists = _block_list_paths(doc, leaves, keys)
    lines = original.splitlines()
    new_values: dict[str, Any] = {}
    edits: list[tuple[int, int, str]] = []  # (起始行, 结束行(不含), 新行内容)

    for target, raw_value in updates.items():
        target = str(target)
        if target not in leaves and target not in block_lists:
            return 400, {"ok": False,
                         "error": f"目标选项不存在或不可编辑: {target}"}
        try:
            current = _get_path(doc, target)
        except KeyError:
            return 400, {"ok": False, "error": f"目标选项不存在: {target}"}
        try:
            value = _coerce_new_value(current, raw_value)
        except (yaml.YAMLError, ValueError) as exc:
            return 400, {"ok": False,
                         "error": f"{target} 的新值无效: {exc}"}
        new_values[target] = value
        try:
            dumped = _dump_inline(value)
        except ValueError as exc:
            return 400, {"ok": False, "error": f"{target} 无法序列化: {exc}"}
        if target in leaves:
            info = leaves[target]
            line = lines[info["line"]]
            prefix = line[:line.index(":") + 1]
            tail = f" {info['comment']}" if info["comment"] else ""
            edits.append((info["line"], info["line"] + 1,
                          f"{prefix} {dumped}{tail}" if dumped else prefix))
            continue
        # 块状列表：折叠为单行流式列表，删除后续「- item / # 注释」行
        info = keys[target]
        head = info["line"]
        scan = head + 1
        while scan < len(lines):
            text = lines[scan]
            stripped = text.strip()
            if not stripped:
                break
            indent = len(text) - len(text.lstrip())
            if indent <= info["indent"]:
                break
            if not (stripped.startswith("- ") or stripped.startswith("#")):
                return 400, {"ok": False,
                             "error": f"列表 {target} 含复杂内容，"
                                      f"请手工编辑文件"}
            scan += 1
        indent_str = " " * info["indent"]
        edits.append((head, scan, f"{indent_str}{info['key']}: {dumped}"))

    for start, stop, replacement in sorted(edits, reverse=True):
        lines[start:stop] = [replacement]

    updated_text = "\n".join(lines)
    if original.endswith("\n"):
        updated_text += "\n"
    try:
        check = yaml.safe_load(updated_text)
    except yaml.YAMLError as exc:
        return 500, {"ok": False,
                     "error": f"写入前复验失败（未写入）: {exc}"}
    for target, value in new_values.items():
        try:
            written = _get_path(check, target)
        except KeyError:
            return 500, {"ok": False,
                         "error": f"写入前复验失败（未写入）: {target} 丢失"}
        if written != value:
            return 500, {"ok": False,
                         "error": f"写入前复验失败（未写入）: {target} 值不一致"}
    code, payload = _write_with_backup(path, rel, updated_text, backup_dir,
                                       max_bytes)
    if code == 200:
        payload["updated"] = sorted(new_values)
    return code, payload
