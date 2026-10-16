"""正式文件/路径工具（subagent 从0重建版）——check_path + exec + 文件工具。

设计原则（设计原则）：
  - 变量尽量写 config；本文件不硬编码路径/白名单/限额，工作区根从 session
    session.json 的 workdir 读取，exec 白名单/文件工具限额从 config 注入。
  - 安全模型：agent 只能碰自己的工作区（session workdir）。所有含 path
    参数的工具（exec/read/write/delete/edit/replace/append/glob/grep）执行前一律
    先 check_path 校验路径是否在工作区内；越界 → 触发审批（notice 通知
    用户），approve 放行执行、reject（或超时）→ 返回 403 拒绝码不执行。

check_path：给定「待检测路径 + session 名」，规范化后判断该路径是否落在
session 工作区内。路径形式：绝对 / ./ 相对 / 裸相对；统一 realpath 规范化
防 `..`/符号链接逃逸（含 ../../../、/aaa/../../.. 多重逃逸）。

文件工具（对齐 resident workspace_tools）：
  read(path, start, end, show_line_numbers)      读文件（cat -n 风格，可指定行范围）
  write(path, content)                           整文件覆盖写（原文件备份为 .old）
  delete(path)                                   安全删除（移入 trash_dir 回收站，
                                                 重名自动加时间戳后缀，不真删）
  edit(path, old_string, new_string, replace_all) 精确文本替换（差分修改）
  replace(path, start_line, end_line, new_content) 按行号替换行区间
  append(path, content)                          追加内容
  glob(pattern, path)                            按 glob 查找文件
  grep(pattern, path, glob, ...)                 按正则搜文件内容
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time as _time
from datetime import datetime as _datetime
from pathlib import Path
from typing import Any, Optional

from kernel.agent_framework import checkpoint as _checkpoint  # noqa: F401

logger = __import__("logging").getLogger("subagent.workspace_tools")

# ── 线程局部会话态（每个 agent 线程独立，参考 skill_tools 局部根机制）─
_thread = threading.local()


def set_session_state(session: str, workdir: Optional[str],
                      broker: Optional[object]) -> None:
    """在 agent 线程内绑定 session 名 + 工作区 + 审批 broker。"""
    _thread.session = session
    _thread.workdir = workdir
    _thread.broker = broker


def _current() -> tuple[Optional[str], Optional[str], Optional[object]]:
    return (getattr(_thread, "session", None),
            getattr(_thread, "workdir", None),
            getattr(_thread, "broker", None))


# ── 命令白名单 + 串行命令切分 ───────────────────────────────────────
# （切分在 parse_serial_commands 里手工扫描实现——引号内的分隔符不能切；
#   历史实现用的是正则 `\s*(?:&&|\|\||;|\|)\s*`，会把 f-string 里的 `|` 也切开。）
_URL_RE = re.compile(
    r"^(?:https?|ftp|git|ssh|rsync|file)://|@[^:]+:")
# heredoc 定界符（<<EOF / <<'EOF' / <<"EOF" / <<-EOF）。只认"独立标识符"，
# 且后面必须跟空白或行尾——避免把 C++ 里的 `cout<<x`、`cin>>y` 之流当定界符。
_HEREDOC_RE = re.compile(
    r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?(?=\s|$)")


def _command_name(segment: str) -> str:
    """取一段命令的第一个词作为命令名（参数不算命令）。"""
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    return toks[0] if toks else ""


def _extract_path_tokens(segment: str) -> list[str]:
    """提取一段命令里所有疑似路径的 token（URL / 纯选项排除）。"""
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    out: list[str] = []
    for i, t in enumerate(toks):
        if i == 0:
            continue  # 命令名由白名单把关，跳过
        if _URL_RE.match(t):
            continue
        if t.startswith("-"):
            continue
        if (t.startswith("/") or t.startswith("./") or t.startswith("../")
                or t.startswith("~") or ("/" in t)
                or (not t.startswith(("-", "=")) and "." in t)):
            out.append(t)
    return out


def parse_serial_commands(cmd: str) -> list[str]:
    """按 &&/||/;/| 切分串行命令为多个子命令（去空白，过滤空段）。

    ⚠️ 两类内容**不能当命令**（它们只是数据，却会被切片后当命令名 → 误判、
    狂弹审批/拒执行）：
    ① **引号内**（含转义）的分隔符不切：否则
       `python3 -c '…print(f"{a} | {b}")…'` 会被 f-string 里的 `|`（或字符串
       里的 `;`）切开（2026-10-14 实测踩过）；
    ② **heredoc body**（`cat > x.cpp <<'EOF' … EOF`）整段跳过：否则 body 里
       的每行（`do` / `}` / `cout<<s` / `dp[i][1]=max(...)`）都会被当成一条
       命令去查白名单（2026-10-14 23:0x 实测：一个 g++ 编译命令报出 30 多条
       "命令不在白名单"）。
    返回的每个段只用于**白名单/路径判定**（真正执行用的是原始 command），
    所以 body 直接丢弃、不参与判定——body 里的路径是代码里的字符串，不是
    这次命令要访问的路径。
    """
    segs: list[str] = []
    cur: list[str] = []
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 2
            continue
        m = _HEREDOC_RE.match(cmd, i)
        if m:
            delim = m.group(1)
            cur.append(cmd[i:m.end()])
            i = m.end()
            nl = cmd.find("\n", i)
            if nl == -1:
                i = n
            else:
                cur.append(cmd[i:nl])
                i = nl + 1
            while i < n:  # 丢弃 body，直到"单独一行 == 定界符"为止
                nl2 = cmd.find("\n", i)
                line = cmd[i:] if nl2 == -1 else cmd[i:nl2]
                i = n if nl2 == -1 else nl2 + 1
                if line.strip() == delim:
                    break
            # heredoc 结束即一段结束：后面的内容另起一段单独查白名单，
            # 否则 `cat > x <<EOF … EOF` 后面的命令会被并进 `cat` 这一段逃过检查。
            segs.append("".join(cur))
            cur = []
            continue
        if cmd[i:i + 2] in ("&&", "||"):
            segs.append("".join(cur))
            cur = []
            i += 2
            continue
        if ch in ";|":
            segs.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s and s.strip()]


# ── 路径合法性（check_path）─────────────────────────────────────────
def _read_workdir(sess_dir: Path) -> Optional[str]:
    meta_path = Path(sess_dir) / "session.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("workdir")
    except Exception:  # noqa: BLE001
        return None


def check_path(path: str, session: str, context_root: str) -> str:
    """检查给定路径是否在指定 session 的工作区内。参数:
    path: 要检测的路径（绝对路径 / 相对路径，如 ./sub/a.txt 或 sub/a.txt）；
    session: session 名（对应技能数据目录 skill_data/subagent/<session>/ 下的工作区）；
    context_root: session 数据根目录（接入层注入，含各 session 子目录 + session.json）。
    返回该路径是否位于工作区内、以及规范化后的绝对路径与相对工作区根的路径。"""
    sess_dir = Path(context_root) / session
    wd = _read_workdir(sess_dir)
    if not wd:
        return (f"无法检查：session '{session}' 没有可用的工作区"
                f"（{sess_dir}/session.json 缺失或未记录 workdir）")
    inside, real = check_path_against(path, wd)
    wd_real = os.path.realpath(os.path.abspath(wd))
    in_ws = "是" if inside else "否"
    rel_desc = ""
    if inside:
        rel = os.path.relpath(real, wd_real)
        rel_desc = f"，相对工作区路径: {rel}"
    return (f"路径 '{path}' 在工作区内(in_workspace)={inside}；"
            f"规范化绝对路径: {real}；工作区根: {wd_real}"
            f"{rel_desc}. Agent 只能访问自己的工作区，越界路径将被拒绝.")


def check_path_against(path: str, workdir: str) -> tuple[bool, str]:
    """给具体 workdir 判路径合法性（供各文件工具复用，避免重复读 session.json）。
    返回 (in_workspace, 规范化绝对路径)。"""
    wd_real = os.path.realpath(os.path.abspath(workdir))
    candidate = path if os.path.isabs(path) else os.path.join(wd_real, path)
    real = os.path.realpath(os.path.abspath(candidate))
    try:
        rel = os.path.relpath(real, wd_real)
        if rel == ".":
            return True, real
        if not rel.startswith("..") and not os.path.isabs(rel):
            return True, real
        return False, real
    except ValueError:  # 跨盘符
        return False, real


# ── 审批把关（exec + 文件工具共用）─────────────────────────────────
# 写类操作 → 记录被改动文件的真实路径，供 checkpoint 增量 add
# （背景：workdir 可能有十万级文件，全量 `git add -A` 会卡死 spawn/save；
#   约定「按增量」。）
_TOUCHED: dict[str, set[str]] = {}
_WRITE_ACTIONS = {"write", "edit", "replace", "append", "delete"}


def _mark_touched(workdir: str, real: str) -> None:
    if workdir and real:
        key = os.path.realpath(workdir)
        _TOUCHED.setdefault(key, set()).add(real)


def pop_touched(workdir: str) -> list[str]:
    """取出并清空某 workdir 记录的被改动文件（供 checkpoint.save 用）。

    key 统一用 realpath，避免 spawn 时传的 workdir 与 save 时读出的
    字符串形态（尾斜杠/软链）不一致导致取不到。
    """
    if not workdir:
        return []
    return sorted(_TOUCHED.pop(os.path.realpath(workdir), set()))


def _guard_path(path: str, workdir: str, broker: Optional[object],
                action: str, detail: str = "") -> tuple[bool, str, Any]:
    """路径门卫：校验 path 是否在工作区内。

    返回 (放行?, 规范化路径, 结果)。
    - 合法 → (True, real, None)
    - 越界无 broker → (False, real, 拒绝串)
    - 越界有 broker → 发审批（notice 说明 action+path），阻塞等 approve/reject：
        approve → (True, real, None)；reject/超时 → (False, real, 拒绝结果code403)
    """
    inside, real = check_path_against(path, workdir)
    if inside:
        if action in _WRITE_ACTIONS:
            _mark_touched(workdir, real)
        return True, real, None
    if broker is None:
        return False, real, f"路径 {path} 不在工作区内（无审批通道）"
    label = f"{action}:" + (detail or path)
    req_id = broker.request_approval(
        _current()[0] or "", f"{label}", f"路径不在工作区内: {path}")
    approved, _err = broker.wait_approval(req_id)
    if approved:
        if action in _WRITE_ACTIONS:
            _mark_touched(workdir, real)
        return True, real, None
    return False, real, f"执行被拒绝（未执行）：{req_id}"


# ── exec ────────────────────────────────────────────────────────────
def _run_shell(command: str, cwd: str, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True,
            text=True, timeout=timeout)
        out = proc.stdout
        if proc.returncode != 0 and proc.stderr:
            out = out.rstrip() + ("\n" if out else "") + proc.stderr.rstrip()
        return proc.returncode, out
    except subprocess.TimeoutExpired as exc:
        return 124, f"命令执行超时（>{timeout:g}s）"
    except Exception as exc:  # noqa: BLE001
        return 500, f"命令执行失败：{exc}"


def exec_command(command: str) -> dict | str:
    """在工作区执行命令。返回 dict 带 code；异常返回直接 str。"""
    session, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法执行：当前 session 无工作区"}

    segs = parse_serial_commands(command)
    if not segs:
        return {"code": 403, "content": "空命令"}
    bad_cmds = []
    whitelist = set(broker.exec_whitelist if broker else [])
    for seg in segs:
        cname = _command_name(seg)
        if cname and cname not in whitelist:
            bad_cmds.append(cname)
    bad_cmds = sorted(set(bad_cmds))

    bad_paths = []
    ok = True
    for seg in segs:
        for t in _extract_path_tokens(seg):
            inside, _real = check_path_against(t, workdir)
            if not inside:
                bad_paths.append(t)
                ok = False

    if ok and not bad_cmds:
        rc, out = _run_shell(command, workdir, broker.exec_timeout_s if broker
                             else 60)
        return {"code": 200 if rc == 0 else 500, "content": (
            out.rstrip() or f"(无输出) returncode={rc}")}

    reason = []
    if bad_cmds:
        reason.append(f"命令不在白名单: {', '.join(bad_cmds)}")
    if bad_paths:
        reason.append(f"路径不在工作区内: {', '.join(dict.fromkeys(bad_paths))}")
    if not broker:
        return {"code": 403, "content": f"执行被拒：{'；'.join(reason)}（无审批通道）"}
    req_id = broker.request_approval(session, command, "；".join(reason))
    approved, _err = broker.wait_approval(req_id) if req_id else (False, "no-req")
    if approved:
        rc, out = _run_shell(command, workdir, broker.exec_timeout_s)
        return {"code": 200 if rc == 0 else 500, "content": (
            out.rstrip() or f"(无输出) returncode={rc}")}
    return {"code": 403, "content": f"执行被拒绝（未执行）：{req_id or '无审批请求'}"}


# ── 文件工具（对齐 resident；限额从配置，审批走 _guard_path）────────
def _read_text(host: str) -> str:
    with open(host, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_range(host: str, start: int, end: int) -> str:
    lines = []
    with open(host, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            if end and i > end:
                break
            if i >= start:
                lines.append(line.rstrip("\n"))
    return "\n".join(lines)


def _line_numbers(text: str, start: int) -> str:
    if not text:
        return ""
    return "\n".join(
        f"{i + start:>6}\t{line}" for i, line in enumerate(text.split("\n")))


def _unified_diff_text(path: str, old_text: str, new_text: str) \
        -> tuple[str, int, int]:
    import difflib
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    removed = len(old_lines) - len(new_lines)
    added = len(new_lines) - len(old_lines)
    diff = "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=path, tofile=path, lineterm="",
        n=1))
    return diff, removed, added


def _removed_lines(old_text: str, new_text: str) -> list[tuple[int, str]]:
    import difflib
    deleted = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_text.splitlines(), new_text.splitlines()).get_opcodes():
        if tag in ("delete", "replace"):
            for i in range(i1, i2):
                deleted.append((i + 1, old_text.splitlines()[i]))
    return deleted


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（输出已被截断，共 {len(text)} 字符）"


def file_read(cfg: dict, path: str, start: int = 1, end: int = 0,
              show_line_numbers: Optional[bool] = None) -> Any:
    """读文件（cat -n 风格，可指定行范围）。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法读取：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "read", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        if start < 1:
            raise ValueError("start 必须 >= 1")
        if end and end < start:
            raise ValueError(f"end({end}) 不能小于 start({start})")
        try:
            size = os.path.getsize(real)
        except OSError:
            raise ValueError(f"文件不存在或不可读: {path}")
        full_read = start == 1 and not end
        rmb = int(cfg.get("read_max_bytes", 65535))
        rml = int(cfg.get("read_max_lines", 1000))
        if full_read:
            if size > rmb:
                raise ValueError(
                    f"文件 {path} 为 {size} 字节，超过读取上限 {rmb} 字节；"
                    "请用 start/end 指定行范围，或先用 grep 定位")
            content = _read_text(real)
            total = content.count("\n") + (1 if content else 0)
            if total > rml:
                raise ValueError(
                    f"文件 {path} 共 {total} 行，超过读取上限 {rml} 行；"
                    "请用 start/end 指定行范围")
        else:
            limit = (end - start + 1) if end else None
            if limit and limit > rml:
                raise ValueError(f"请求的 {limit} 行超过读取上限 {rml} 行")
            content = _read_range(real, start, end)
            total = None
        show = cfg.get("read_default_line_numbers", True) \
            if show_line_numbers is None else show_line_numbers
        body = _line_numbers(content, start if show else 0)
        if total is None:
            return f"{path}（第 {start} 行起，{size} 字节）\n{body}"
        rwb = int(cfg.get("read_warn_bytes", 32768))
        warn = ""
        if full_read and size > rwb:
            warn = (f"\n（文件较大，共 {size} 字节已完整读取；"
                    "如需部分内容请用 start/end 指定行范围）")
        return f"{path}（共 {total} 行，{size} 字节）\n{body}{warn}"
    except Exception as exc:  # noqa: BLE001
        return f"读取失败: {exc}"


def file_write(cfg: dict, path: str, content: str) -> Any:
    """整文件覆盖写（原文件备份为 .old）。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法写入：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "write", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        host = real
        if os.path.isdir(host):
            raise ValueError(f"{path} 是目录，write 需要文件路径")
        data = str(content or "")
        wmb = int(cfg.get("write_max_bytes", 262144))
        if len(data) > wmb:
            raise ValueError(f"写入内容 {len(data)} 字节超过上限 {wmb}")
        os.makedirs(os.path.dirname(host) or ".", exist_ok=True)
        backup = ""
        if os.path.exists(host):
            backup = host + ".old"
            try:
                _write_text(backup, _read_text(host))
            except Exception:  # noqa: BLE001
                backup = ""
        _write_text(host, data)
        if backup:
            return (f"已写入 {len(data)} 字节 -> {path}；"
                    f"原文件已备份为 {path}.old")
        return f"已写入 {len(data)} 字节 -> {path}（新文件）"
    except Exception as exc:  # noqa: BLE001
        return f"写入失败: {exc}"


def _write_text(host: str, content: str) -> None:
    with open(host, "w", encoding="utf-8") as f:
        f.write(content)


def file_delete(cfg: dict, path: str) -> Any:
    """安全删除：把文件移入回收站（trash_dir，配置于 workspace_tools），
    不真正删除；原路径不再存在，文件保留在回收站可手动恢复。
    路径在工作区内直接执行；越界 → 触发审批（approve 放行、
    reject/超时 → 403 不执行）。回收站已有同名文件时自动加时间戳后缀
    （再重名加序号），绝不覆盖已有文件。只支持文件，目录拒绝。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法删除：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "delete", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        if not os.path.exists(real):
            raise ValueError(f"文件不存在: {path}")
        if os.path.isdir(real):
            raise ValueError(f"{path} 是目录；delete 只支持文件"
                             "（删目录请先用 exec 走审批）")
        trash = str(cfg.get("trash_dir") or "/home/sandbox/trash")
        os.makedirs(trash, exist_ok=True)
        base = os.path.basename(real)
        dest = os.path.join(trash, base)
        if os.path.exists(dest):
            root, ext = os.path.splitext(base)
            ts = _datetime.now().strftime("%Y%m%d_%H%M%S")
            cand = os.path.join(trash, f"{root}_{ts}{ext}")
            n = 1
            while os.path.exists(cand):
                cand = os.path.join(trash, f"{root}_{ts}_{n}{ext}")
                n += 1
            dest = cand
        shutil.move(real, dest)
        return (f"已安全删除: {path} → 已移入回收站 {dest}"
                "（原路径不再存在；文件仍在回收站，可手动恢复）")
    except Exception as exc:  # noqa: BLE001
        return f"删除失败: {exc}"


def file_edit(cfg: dict, path: str, old_string: str,
              new_string: str = "", replace_all: bool = False) -> Any:
    """精确文本替换（差分修改）。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法编辑：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "edit", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        if not old_string:
            raise ValueError("old_string 不能为空")
        emc = int(cfg.get("edit_max_old_chars", 20000))
        if len(old_string) > emc:
            raise ValueError(f"old_string 超过上限 {emc} 字符")
        content, _size = _read_text(real), os.path.getsize(real)
        count = content.count(old_string)
        if count == 0:
            raise ValueError(f"在 {path} 中未找到 old_string，请检查内容")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string 在 {path} 中出现 {count} 处；请提供更多上下文，"
                "或显式设置 replace_all=true")
        new_content = content.replace(
            old_string, new_string, -1 if replace_all else 1)
        diff, removed, added = _unified_diff_text(path, content, new_content)
        deleted = _removed_lines(content, new_content)
        _write_text(real, new_content)
        deleted_lines = "".join(
            f"- {line_no}| {text}\n" for line_no, text in deleted)
        if diff:
            base = (f"已修改 {path}（-{removed} 行 +{added} 行）\n{diff}")
            if deleted_lines:
                base += f"\n被删除的行（行号|原文）：\n{deleted_lines}"
            return _truncate(base, int(cfg.get("edit_output_max_chars", 20000)))
        return f"{path} 内容未发生变化（old_string 与 new_string 相同）"
    except Exception as exc:  # noqa: BLE001
        return f"修改失败: {exc}"


def file_replace(cfg: dict, path: str, start_line: int, end_line: int,
                 new_content: str = "") -> Any:
    """按行号替换行区间。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法替换：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "replace", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        if start_line < 1:
            raise ValueError("start_line 必须 >= 1")
        if end_line < start_line:
            raise ValueError(f"end_line({end_line}) 不能小于 start_line({start_line})")
        content = _read_text(real)
        total = content.count("\n") + (1 if content else 0)
        if end_line > total:
            raise ValueError(f"end_line({end_line}) 超出文件行数 {total}")
        lines = content.splitlines()
        new_parts = (new_content or "").split("\n") if new_content else []
        new_lines = lines[:start_line - 1] + new_parts + lines[end_line:]
        new_text = "\n".join(new_lines)
        if new_text and content.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        diff, removed, added = _unified_diff_text(path, content, new_text)
        deleted = _removed_lines(content, new_text)
        _write_text(real, new_text)
        deleted_lines = "".join(
            f"- {line_no}| {text}\n" for line_no, text in deleted)
        base = (f"已替换 {path} 第 {start_line} 行到第 {end_line} 行"
                f"（-{removed} 行 +{added} 行）\n{diff}")
        if deleted_lines:
            base += f"\n被删除的行（行号|原文）：\n{deleted_lines}"
        return _truncate(base, int(cfg.get("edit_output_max_chars", 20000)))
    except Exception as exc:  # noqa: BLE001
        return f"替换失败: {exc}"


def file_append(cfg: dict, path: str, content: str) -> Any:
    """追加内容。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法追加：当前 session 无工作区"}
    ok, real, denied = _guard_path(path, workdir, broker, "append", path)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        data = str(content or "")
        wmb = int(cfg.get("write_max_bytes", 262144))
        if len(data) > wmb:
            raise ValueError(f"追加内容 {len(data)} 字节超过上限 {wmb}")
        os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
        with open(real, "a", encoding="utf-8") as f:
            f.write(data)
        return f"已追加 {len(data)} 字节 -> {path}"
    except Exception as exc:  # noqa: BLE001
        return f"追加失败: {exc}"


def file_glob(cfg: dict, pattern: str, path: str = "") -> Any:
    """按 glob 找文件（支持 ** 递归）。path 缺省=工作区根。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法查找：当前 session 无工作区"}
    if not pattern:
        return {"code": 400, "content": "pattern 不能为空"}
    base_w = path or workdir
    ok, real, denied = _guard_path(base_w, workdir, broker, "glob", base_w)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        if not os.path.isdir(real):
            return f"{base_w} 不是目录（glob 需要目录作为搜索起点）"
        import glob as globmod
        hits = [c for c in globmod.glob(
            os.path.join(real, pattern), recursive=True)
            if ".git" not in c.split(os.sep)]
        hits.sort()
        total = len(hits)
        limit = int(cfg.get("glob_max_results", 100))
        truncated = total > limit
        shown = hits[:limit]
        body = "\n".join(os.path.relpath(p, real) for p in shown)
        note = (f"\n（共 {total} 个结果，已截断至 {limit} 个）"
                if truncated else f"\n（共 {total} 个结果）")
        return f"匹配文件：\n{body}{note}" if body else f"无匹配（{total}）"
    except Exception as exc:  # noqa: BLE001
        return f"查找失败: {exc}"


def _grep_file(fp: str, rel: str, rx, output_mode: str,
               context_lines: int, max_cols: int) -> list[str]:
    with open(fp, "rb") as f:
        head = f.read(1024)
    if b"\x00" in head:
        return []
    out: list[str] = []
    try:
        lines = open(fp, "r", encoding="utf-8", errors="replace").readlines()
    except OSError:
        return []
    text_lines = [l.rstrip("\n") for l in lines]
    matched_idx = [i for i, l in enumerate(text_lines) if rx.search(l)]
    if output_mode == "count":
        if matched_idx:
            out.append(f"{rel}:{len(matched_idx)}")
    elif output_mode == "files_with_matches":
        if matched_idx:
            out.append(rel)
    else:
        shown_idx = set()
        for i in matched_idx:
            for j in range(max(0, i - context_lines),
                           min(len(text_lines), i + context_lines + 1)):
                shown_idx.add(j)
        for i in sorted(shown_idx):
            text = text_lines[i]
            shown = text if len(text) <= max_cols else text[:max_cols] + "…"
            out.append(f"{rel}:{i + 1}:{shown}")
    return out


def file_grep(cfg: dict, pattern: str, path: str = "", glob: str = "",
              output_mode: str = "content", ignore_case: bool = False,
              context_lines: int = 0, hidden: bool = False,
              head_limit: Optional[int] = None) -> Any:
    """按正则搜文件内容。"""
    _s, workdir, broker = _current()
    if not workdir:
        return {"code": 403, "content": "无法搜索：当前 session 无工作区"}
    if not pattern:
        return {"code": 400, "content": "pattern 不能为空"}
    base_w = path or workdir
    ok, real, denied = _guard_path(base_w, workdir, broker, "grep", base_w)
    if not ok:
        return {"code": 403, "content": denied}
    try:
        flags = re.IGNORECASE if ignore_case else 0
        rx = re.compile(pattern, flags)
        limit = int(cfg.get("grep_head_limit", 250)) \
            if head_limit is None else head_limit
        if output_mode not in ("content", "files_with_matches", "count"):
            raise ValueError(
                f"未知 output_mode: {output_mode}"
                "（可选 content / files_with_matches / count）")
        if context_lines < 0:
            raise ValueError("context_lines 不能为负")
        max_cols = int(cfg.get("grep_max_columns", 500))
        output: list[str] = []
        if os.path.isfile(real):
            output = _grep_file(real, os.path.basename(real), rx,
                                output_mode, context_lines, max_cols)
        elif os.path.isdir(real):
            for dirpath, dirnames, filenames in os.walk(real):
                if not hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for filename in sorted(filenames):
                    if not hidden and filename.startswith("."):
                        continue
                    rel_dir = os.path.relpath(dirpath, real)
                    rel = os.path.join(rel_dir, filename) \
                        if rel_dir != "." else filename
                    if ".git" in rel.split(os.sep):
                        continue
                    if glob and not (fnmatch.fnmatch(rel, glob)
                                     or fnmatch.fnmatch(filename, glob)):
                        continue
                    fp = os.path.join(dirpath, filename)
                    output.extend(_grep_file(fp, rel, rx, output_mode,
                                             context_lines, max_cols))
        else:
            return f"{base_w} 不存在（grep 需要目录或文件路径）"
        output.sort()
        total = len(output)
        truncated = total > limit
        shown = output[:limit]
        note = (f"（共 {total} 条匹配，已截断至 {limit} 条）"
                if truncated else f"（共 {total} 条匹配）")
        return f"{note}\n" + "\n".join(shown)
    except Exception as exc:  # noqa: BLE001
        return f"搜索失败: {exc}"


def ask_command(questions_json: str) -> Any:
    """向用户提问 N 个问题，阻塞等一次性答复。questions_json: JSON 数组，
    每题 {question, answers:[选项], options:起草回复}。返回:
    - 用户答复里的 answers（{q1:'A', q2:['B','C',...], q3:'自定义串'}）→ 200
    - 用户 cancel/超时 → {"code":403} 拒绝，本轮 reAct 结束。"""
    session, workdir, broker = _current()
    if broker is None:
        return {"code": 403, "content": "ask 不可用：无用户通道"}
    try:
        questions = json.loads(questions_json)
        if not isinstance(questions, list) or not questions:
            return {"code": 400, "content": "questions 需为非空 JSON 数组"}
    except (json.JSONDecodeError, TypeError):
        return {"code": 400, "content": "questions 不是合法 JSON"}
    ask_id = broker.request_ask(session or "", questions)
    if not ask_id:
        return {"code": 500, "content": "ask 请求登记失败"}
    ok, answers, note = broker.wait_ask(ask_id)
    if not ok:
        return {"code": 403, "content": f"ask 被取消/超时（未收到答复）：{ask_id}. {note}"}
    if not isinstance(answers, dict):
        return {"code": 500, "content": f"ask 答复无效（非对象）: {ask_id}"}
    # 返回 answers dict（附 ask_id 便于对照）作为工具结果
    return {"code": 200,
            "content": "用户答复：\n" + json.dumps(
                answers, ensure_ascii=False, indent=2)}


# ── todo 任务管理（设计约定：增加 todo 机制）─────────────────────────
# 存储：每 session 独立 todo 文件，位于 session 领地
#   <context_root>/<session>/todo.json（与 compressed/ 同理，session 隔离）。
# 结构参考 resident todo：两层 {task: [todo 文案]}，每个 todo 有完成态。
# 无用户通道依赖（存本地文件即可），agent 自维护任务清单。


def _todo_path(session: Optional[str], context_root: str) -> Optional[Path]:
    """返回当前 session 的 todo 文件路径；session 缺失返回 None。"""
    if not session:
        return None
    sess_dir = Path(context_root) / session
    try:
        sess_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return sess_dir / "todo.json"


def _todo_load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _todo_save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def todo_add(task: str, todo: str, context_root: str) -> Any:
    """向 task 追加一条 todo（task 不存在则新建）。返回 {task, todo, 状态}。"""
    session, _w, _b = _current()
    path = _todo_path(session, context_root)
    if path is None:
        return {"code": 500, "content": "todo 不可用：无 session 领地"}
    task = (task or "").strip()
    todo = (todo or "").strip()
    if not task or not todo:
        return {"code": 400, "content": "task 与 todo 都必填"}
    data = _todo_load(path)
    if task not in data:
        data[task] = []
    data[task].append({"text": todo, "done": False})
    _todo_save(path, data)
    return {"code": 200, "content": f"已添加 todo [task={task}]: {todo}"}


def todo_list(context_root: str) -> Any:
    """列出所有 task 与其 todos（含完成态）。"""
    session, _w, _b = _current()
    path = _todo_path(session, context_root)
    if path is None or not path.exists():
        return {"code": 200, "content": "（暂无 todo）"}
    data = _todo_load(path)
    lines = []
    for task, items in data.items():
        lines.append(f"## task: {task}")
        for it in items:
            mark = "x" if it.get("done") else " "
            lines.append(f"- [{mark}] {it.get('text','')}")
    return {"code": 200, "content": "\n".join(lines) if lines else "（暂无 todo）"}


def todo_update(task: str, todo: str, status: str, context_root: str) -> Any:
    """把 task 下一条 todo 标记为 completed/pending（按文本匹配）。
    task/todo 不存在返回 404。"""
    session, _w, _b = _current()
    path = _todo_path(session, context_root)
    if path is None:
        return {"code": 500, "content": "todo 不可用：无 session 领地"}
    task = (task or "").strip()
    todo = (todo or "").strip()
    status = (status or "").strip().lower()
    if status not in ("completed", "pending"):
        return {"code": 400, "content": "status 只能为 completed / pending"}
    data = _todo_load(path)
    if task not in data:
        return {"code": 404, "content": f"task 不存在: {task}"}
    for it in data[task]:
        if it.get("text") == todo:
            it["done"] = (status == "completed")
            _todo_save(path, data)
            new_mark = "x" if it["done"] else " "
            return {"code": 200,
                    "content": f"已更新 [task={task}] [{new_mark}] {todo}"}
    return {"code": 404, "content": f"todo 不存在: {task} / {todo}"}


def register_workspace_tools(registry, context_root: str,
                             cfg: Optional[dict] = None) -> None:
    """注册正式文件工具。context_root/max_bytes/broker 用闭包注入。"""
    _cfg = dict(cfg or {})

    def _check(path: str, session: str) -> str:
        return check_path(path, session, context_root)

    async def _exec(command: str) -> Any:
        return exec_command(command)

    async def _read(path: str, start: int = 1, end: int = 0,
                    show_line_numbers: Optional[bool] = None) -> Any:
        return file_read(_cfg, path, start, end, show_line_numbers)

    async def _write(path: str, content: str) -> Any:
        return file_write(_cfg, path, content)

    async def _delete(path: str) -> Any:
        return file_delete(_cfg, path)

    async def _edit(path: str, old_string: str, new_string: str = "",
                    replace_all: bool = False) -> Any:
        return file_edit(_cfg, path, old_string, new_string, replace_all)

    async def _replace(path: str, start_line: int, end_line: int,
                       new_content: str = "") -> Any:
        return file_replace(_cfg, path, start_line, end_line, new_content)

    async def _append(path: str, content: str) -> Any:
        return file_append(_cfg, path, content)

    async def _glob(pattern: str, path: str = "") -> Any:
        return file_glob(_cfg, pattern, path)

    async def _grep(pattern: str, path: str = "", glob: str = "",
                    output_mode: str = "content",
                    ignore_case: bool = False, context_lines: int = 0,
                    hidden: bool = False,
                    head_limit: Optional[int] = None) -> Any:
        return file_grep(_cfg, pattern, path, glob, output_mode,
                         ignore_case, context_lines, hidden, head_limit)

    async def _ask(questions: str) -> Any:
        return ask_command(questions)

    async def _todo_add(task: str, todo: str) -> Any:
        return todo_add(task, todo, context_root)

    async def _todo_list() -> Any:
        return todo_list(context_root)

    async def _todo_update(task: str, todo: str, status: str) -> Any:
        return todo_update(task, todo, status, context_root)

    registry.register("check_path", _check)
    registry.register("exec", _exec)
    registry.register("read", _read)
    registry.register("write", _write)
    registry.register("delete", _delete)
    registry.register("edit", _edit)
    registry.register("replace", _replace)
    registry.register("append", _append)
    registry.register("glob", _glob)
    registry.register("grep", _grep)
    registry.register("ask", _ask)
    registry.register("todo_add", _todo_add)
    registry.register("todo_list", _todo_list)
    registry.register("todo_update", _todo_update)