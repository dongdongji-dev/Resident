#!/usr/bin/env python3
"""_generate.py — 按 `_spec.yaml` 生成 actions/<dir>/<dir>.py 与 README.md 索引。

用法：
  python3 _generate.py                 # 生成缺失的脚本（已存在的不动）
  python3 _generate.py --force         # 覆盖已存在的脚本（会改手写内容，慎用）
  python3 _generate.py --only kick ban # 只处理指定动作
  python3 _generate.py --list          # 只看会生成哪些，不落盘
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE = '''#!/usr/bin/env python3
"""{dir} — {brief}

用法：
{usage}
{note}
属性从哪来：位置参数按顺序给、--xxx 可选、其余走服务端默认。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _hook import call, element  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="{brief}")
{args}
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印 payload，不真的发")
    ns = parser.parse_args()

{attrs}
{flags}
    call([element("{tag}", attributes)], dry_run=ns.dry_run)


if __name__ == "__main__":
    main()
'''


LIST_KEYS = ("pos", "opt", "flag", "const")


def parse_yaml(path: str) -> list[dict]:
    """只认本文件用到的极简 YAML 子集。

    形状：顶层 `- dir: xxx` 起一条；`  key: value`（缩进两格）是它的字段；
    字段值可以是行内 `[{...}, {...}]`，也可以是下面缩进更深的 `- {...}` 列表行。
    """
    entries: list[dict] = []
    cur: dict | None = None
    sub_key: str | None = None
    for raw in open(path, encoding="utf-8").read().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            if not line.startswith("- "):
                continue
            cur = {}
            entries.append(cur)
            sub_key = None
            line = line[2:]
        if line.startswith("- "):          # 列表项：归属上一个 list 字段
            assert cur is not None and sub_key, f"孤立的列表项：{raw}"
            cur[sub_key].append(parse_item(line[2:]))
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        assert cur is not None, f"缩进行出现在条目之前：{raw}"
        if key in LIST_KEYS:
            cur[key] = inline_list(value) if value.startswith("[") else []
            sub_key = key
        else:
            cur[key] = value
            sub_key = None
    return entries


def parse_item(text: str) -> dict:
    """解析 `{name: x, attr: y}` 或 `name: x, attr: y`。"""
    record = {}
    for part in text.strip().strip("{}").split(","):
        key, _, value = part.partition(":")
        record[key.strip()] = value.strip().strip('"\'')
    return record


def inline_list(text: str) -> list[dict]:
    """把 `[{...}, {...}]` 这种行内列表解析成 dict 列表。"""
    items, buf, depth = [], "", 0
    for ch in text.strip().strip("[]"):
        if ch == "{" :
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf)
    out = []
    for item in items:
        if not item.strip():
            continue
        record = {}
        for part in item.strip().strip("{}").split(","):
            key, _, value = part.partition(":")
            record[key.strip()] = value.strip().strip('"\'')
        out.append(record)
    return out


def field_list(entry: dict, name: str) -> list[dict]:
    return entry.get(name) or []


def render(entry: dict) -> str:
    pos = field_list(entry, "pos")
    opt = field_list(entry, "opt")
    flag = field_list(entry, "flag")
    const = field_list(entry, "const")

    arg_lines = []
    for item in pos:
        extra = ', nargs="?", default=""' if item.get("optional") == "true" else ""
        arg_lines.append(f'    parser.add_argument("{item["name"]}"{extra}, '
                         f'help="{item.get("help", "")}")')
    for item in opt:
        arg_lines.append(
            f'    parser.add_argument("--{item["name"]}", '
            f'default="{item.get("default", "")}", '
            f'help="{item.get("help", "")}")')
    for item in flag:
        arg_lines.append(f'    parser.add_argument("--{item["name"]}", '
                         f'action="store_true", '
                         f'help="{item.get("help", "")}")')

    attrs = [(item["attr"], f'ns.{item["name"]}') for item in pos]
    attrs += [(item["attr"], f'ns.{item["name"]}') for item in opt]
    attrs += [(item["attr"], f'"{item["value"]}"') for item in const]
    attr_text = ", ".join(f'"{key}": {expr}' for key, expr in attrs)
    attr_block = f"    attributes = {{{attr_text}}}" if attr_text \
        else "    attributes: dict = {}"

    flag_lines = []
    for item in flag:
        flag_lines.append(
            f'    if ns.{item["name"]}:\n'
            f'        attributes["{item["attr"]}"] = "{item["value"]}"')
    flag_block = "\n".join(flag_lines) if flag_lines else ""

    usage_pos = " ".join(f'<{item["name"]}>' for item in pos)
    usage = f"  python3 {entry['dir']}.py {usage_pos}".rstrip()
    for item in opt:
        default = item.get("default", "")
        usage += f' [--{item["name"]} {default}]' if default \
            else f' [--{item["name"]} …]'
    for item in flag:
        usage += f' [--{item["name"]}]'
    usage += ' [--dry-run]'
    note = ""
    if entry.get("note"):
        note = f"\n{entry['note']}\n"

    return TEMPLATE.format(dir=entry["dir"], tag=entry["tag"],
                           brief=entry["brief"], usage=usage, note=note,
                           args="\n".join(arg_lines), attrs=attr_block,
                           flags=flag_block)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--spec", default=os.path.join(HERE, "_spec.yaml"))
    args = parser.parse_args()

    entries = parse_yaml(args.spec)
    made, skipped = [], []
    for entry in entries:
        if args.only and entry["dir"] not in args.only:
            continue
        if args.list:
            print(f'{entry["dir"]:<28} {entry["tag"]:<32} {entry["brief"]}')
            continue
        target_dir = os.path.join(HERE, entry["dir"])
        target = os.path.join(target_dir, f'{entry["dir"]}.py')
        if os.path.exists(target) and not args.force:
            skipped.append(entry["dir"])
            continue
        os.makedirs(target_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(render(entry))
        os.chmod(target, 0o755)
        made.append(entry["dir"])
    if args.list:
        return
    print(f"generated: {len(made)} -> {' '.join(made)}")
    if skipped:
        print(f"skipped(existing): {len(skipped)} -> {' '.join(skipped)}")

    write_readme(entries)


def write_readme(entries: list[dict]) -> None:
    """索引表：spec 里的动作 + 手写动作（没进 spec 的目录）都列上。"""
    spec_dirs = {entry["dir"] for entry in entries}
    rows = [(entry["dir"], entry["brief"], f'`{entry["tag"]}`')
            for entry in entries]
    for name in sorted(os.listdir(HERE)):
        path = os.path.join(HERE, name, f"{name}.py")
        if name in spec_dirs or not os.path.isfile(path):
            continue
        brief = ""
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if "—" in line and line.startswith('"""'):
                    brief = line.split("—", 1)[1].strip().rstrip('"').strip()
                    break
        rows.append((name, brief or "(手写)", "(手写/组合动作)"))

    lines = ["# actions — 一个动作一件事", "",
             "目录约定：`actions/<动作名>/<动作名>.py`，一个脚本只干一件事，"
             "用法写在脚本首部 docstring。共用逻辑在 `actions/_hook.py`。",
             "",
             "服务地址/端口/token 由 `_hook.py` 自己找（`skill_data/qq_adapter/port` → "
             "`QQ_ADAPTER_PORT` → 19671；token 走 `.secrets/`）。",
             "",
             "| 动作 | 干什么 | 对应服务端 tag |", "| --- | --- | --- |"]
    for name, brief, tag in rows:
        lines.append(f"| `{name}` | {brief} | {tag} |")
    lines += ["", "`python3 _generate.py` 可按 `_spec.yaml` 补生成缺失脚本"
                  "（`--list` 只看、`--force` 覆盖、`--only a b` 指定）；"
                  "手写的动作不放进 `_spec.yaml` 就不会被覆盖。", ""]
    with open(os.path.join(HERE, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


if __name__ == "__main__":
    main()
