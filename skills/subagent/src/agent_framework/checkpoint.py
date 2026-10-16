"""checkpoint 模块：session 工作区 + 会话记录的双 git 快照与回滚。

设计（设计约定）：
- 每个 session 独立两套 git，不做跨 agent 工作区共享（简单可靠优先）：
  1. 工作区 git：分离仓库（git-dir 放 session 目录的 .workspace-git/，
     --work-tree 指工作区），只记录工作区文件，不污染工作区本身的 .git
  2. 会话 git：普通 git（git -C session_dir），跟踪 session 目录内的
     context.jsonl / session.json / 变动统计等会话记录
- checkpoint = 每轮触发 notice 前调用 save()：工作区 commit + 会话 commit
  + 写"工作区所有文件变动行数"统计文件（git --numstat）
- restore scope 三档：workspace=只回滚工作区 / conv=只回滚会话记录 /
  all=两者都回滚
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("subagent.checkpoint")

# 工作区 git 目录名（位于 session 目录内；会话 git 的 .gitignore 要排除它）
WS_GIT_DIR = ".workspace-git"


def _run(args: list[str], cwd: Optional[Path] = None,
         timeout: int = 120) -> subprocess.CompletedProcess:
    """跑 git（清代理避免环境变量干扰）。"""
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(
        args, capture_output=True, text=True, cwd=str(cwd) if cwd else None,
        timeout=timeout, env=env)


def init_repos(session_dir: Path, workdir: str) -> None:
    """初始化 session 的双 git。幂等：已存在的仓库不动。"""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    ws_git = session_dir / WS_GIT_DIR
    ws_git.mkdir(parents=True, exist_ok=True)

    gitignore = session_dir / ".gitignore"
    if not gitignore.exists() or WS_GIT_DIR not in gitignore.read_text(
            encoding="utf-8", errors="replace"):
        with gitignore.open("a", encoding="utf-8") as f:
            if gitignore.stat().st_size:
                f.write("\n")
            f.write(f"{WS_GIT_DIR}/\n")

    # 工作区分离 git
    if not (ws_git / "HEAD").exists():
        _run(["git", "--git-dir", str(ws_git), "init"])
        if workdir and Path(workdir).is_dir():
            _config_ws(ws_git)
            # ⚠️ 不再在这里全量 `git add -A`：workdir 可能是十万级文件的
            #    大目录（如 /home/sandbox），全量索引会把 spawn 阻塞数分钟、
            #    连 /api/status 一起卡死（2026-09-23 定位）。改为 save() 时
            #    按 agent 实际改动文件增量 add（paths 参数）。
        elif workdir:
            logger.warning("checkpoint init: workdir 不存在 %s，跳过工作区初始 add",
                           workdir)
    # 会话 git
    if not (session_dir / ".git").exists():
        _run(["git", "-C", str(session_dir), "init"])


def _config_ws(ws_git: Path) -> None:
    _run(["git", "--git-dir", str(ws_git), "config", "user.email",
          "subagent@checkpoint"])
    _run(["git", "--git-dir", str(ws_git), "config", "user.name",
          "subagent-checkpoint"])


def _config_conv(session_dir: Path) -> None:
    _run(["git", "-C", str(session_dir), "config", "user.email",
          "subagent@checkpoint"])
    _run(["git", "-C", str(session_dir), "config", "user.name",
          "subagent-checkpoint"])


def git_ws(ws_git: Path, args: list[str], workdir: str) -> list[str]:
    """工作区 git 命令前缀（分离仓库形态）。"""
    return ["git", "--git-dir", str(ws_git), "--work-tree", workdir, *args]


def save(session_dir: Path, workdir: str, label: Optional[str] = None,
         paths: Optional[list[str]] = None) -> dict:
    """每轮 checkpoint：工作区 commit + 会话 commit + 变动统计文件。

    paths: 本次改动的文件真实路径列表（增量 add；来自 workspace_tools.pop_touched）。
           为空则不做工作区 add（避免十万级文件全量扫描，约定"按增量"）。

    返回 {"ok": True/False, "work_commit", "conv_commit", "stats_file"}。
    幂等：无改动时 git commit 会失败(no changes)，视为已是最新。
    """
    session_dir = Path(session_dir)
    ws_git = session_dir / WS_GIT_DIR
    assert ws_git.exists(), f"{session_dir} 未初始化 checkpoint（先 init_repos）"
    tag = label or f"cp-{_now()}"
    result: dict = {"ok": False, "work_commit": "", "conv_commit": "",
                    "stats_file": ""}

    # 1) 工作区提交（分离 git --work-tree）——只 add 本次改动的文件（增量）
    _config_ws(ws_git)
    if paths:
        import os as _os
        rels = []
        for p in paths:
            try:
                rp = _os.path.relpath(p, workdir)
            except ValueError:  # 跨盘符等
                rp = p
            if not rp.startswith(".."):
                rels.append(rp)
        if rels:
            _run(git_ws(ws_git, ["add", "-A", "--", *rels], workdir),
                 cwd=Path(workdir))
    r = _run(git_ws(ws_git, ["commit", "-q", "-m", tag], workdir),
             cwd=Path(workdir))
    work_commit = ""
    if r.returncode == 0:
        rr = _run(git_ws(ws_git, ["rev-parse", "--short", "HEAD"], workdir))
        work_commit = rr.stdout.strip()
    result["work_commit"] = work_commit

    # 2) 变动统计文件：git diff --numstat <上一 commit>..HEAD
    #    （文本=新增 删除；二进制=`- -`；两处 commit 间未变→空）→ 落盘 session
    #    增量文件 = changes_notice.txt（每轮覆盖，反映相邻 commit 变动）
    #    总变动文件 = total_changes.txt（首个 commit..当前累计，全程产物）——
    #    约定"新增一个 changenotice（总的变动）"，避免只看
    #    末轮增量误读成全部产出。
    stats = _numstat(ws_git, workdir)
    if stats is not None:
        stats_file = session_dir / f"changes_{tag}.txt"
        stats_file.write_text(stats, encoding="utf-8")
        result["stats_file"] = str(stats_file)
    total = _numstat_total(ws_git, workdir)
    if total is not None:
        total_file = session_dir / "total_changes.txt"
        total_file.write_text(total, encoding="utf-8")
        result["total_stats_file"] = str(total_file)

    # 3) 会话提交（含 context.jsonl/session.json/变动统计）
    _config_conv(session_dir)
    _run(["git", "-C", str(session_dir), "add", "-A"])
    r = _run(["git", "-C", str(session_dir), "commit", "-q", "-m", tag])
    if r.returncode == 0:
        rr = _run(["git", "-C", str(session_dir), "rev-parse", "--short",
                   "HEAD"])
        result["conv_commit"] = rr.stdout.strip()

    # 4) checkpoint 索引：append 到 checkpoints.txt（最新在末尾）
    #    记录时间 | label | work_commit | conv_commit → 供"所有 checkpoints
    #    和创建时间"查询；该文件由会话 git 跟踪，随下一次会话 commit 入库。
    #    门槛=工作区或会话任一成功提交即可写索引（工作区空/无变化时 work
    #    记 '-'，对话 checkpoint 靠 conv_commit 仍可索引与回滚）——约定
    #    2026-09-05 指出"每轮会话 checkpoint 都应在"（元数据目录非空）。
    if work_commit or result["conv_commit"]:
        idx = session_dir / "checkpoints.txt"
        from datetime import datetime
        work_disp = work_commit or "-"
        line = (f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{tag} | work={work_disp} | conv={result['conv_commit']}\n")
        with idx.open("a", encoding="utf-8") as f:
            f.write(line)
        result["latest"] = {"time": line.split(" | ")[0],
                            "label": tag, "work_commit": work_disp,
                            "conv_commit": result["conv_commit"]}

    result["ok"] = True
    return result


def latest(session_dir: Path) -> Optional[dict]:
    """读 checkpoints.txt 返回最新一条 checkpoint；无则 None。"""
    idx = Path(session_dir) / "checkpoints.txt"
    if not idx.exists():
        return None
    lines = [l for l in idx.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return None
    try:
        time_, label, *rest = lines[-1].split(" | ")
        work = rest[0].split("=")[1] if rest and "work=" in rest[0] else ""
        return {"time": time_, "label": label, "work_commit": work}
    except Exception:  # noqa: BLE001
        return None


def _numstat(ws_git: Path, workdir: str) -> Optional[str]:
    """上一 commit..HEAD 的每文件变动行数；无上一 commit 则返回空。"""
    # 找上一 commit 的对象（HEAD 存在则取其 parents 的父）
    parent = _run(git_ws(ws_git, ["rev-parse", "HEAD^"], workdir),
                  cwd=Path(workdir))
    if parent.returncode != 0 or not parent.stdout.strip():
        # 首次 commit 无 parent：直接列全部文件
        return _linestats_all(ws_git, workdir)
    r = _run(git_ws(ws_git, ["diff", "--numstat", parent.stdout.strip(),
                             "HEAD"], workdir), cwd=Path(workdir))
    return r.stdout if r.returncode == 0 else None


def _numstat_total(ws_git: Path, workdir: str) -> Optional[str]:
    """全程累计变动清单：相对 git 空树的每文件增减（所有当前跟踪文件从
    创建到当前的总行数变化），精确反映"本次 session 的全部产物"。无 HEAD
    时返回 None。用于"总的 changenotice"（约定新增，区别于
    每轮覆盖的增量 changes_notice）。
    注：不用"首个 commit..HEAD"因为首次 commit 加入的文件后续不变就不会再
    出现在 diff 里，会漏掉首轮产物（实测 server.log+wordle.py 只显 server.log）——
    必须相对空树才算全量。"""
    head = _run(git_ws(ws_git, ["rev-parse", "HEAD"], workdir),
                cwd=Path(workdir))
    if head.returncode != 0 or not head.stdout.strip():
        return None
    r = _run(git_ws(ws_git, ["diff", "--numstat", _EMPTY_TREE, "HEAD"],
                    workdir), cwd=Path(workdir))
    return r.stdout if r.returncode == 0 else None


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git 空树常量


def _linestats_all(ws_git: Path, workdir: str) -> Optional[str]:
    """首次：对工作区所有已跟踪(Index)文件统计行数（无 parent 时）。

    用 git 空树常量作 base，`git diff --numstat <空树> HEAD` 得到
    每个文件全新增行数（文本）或二进制 `- -`。"""
    head = _run(git_ws(ws_git, ["rev-parse", "HEAD"], workdir),
                cwd=Path(workdir))
    if head.returncode != 0 or not head.stdout.strip():
        return "（工作区尚无 checkpoint 历史，本次为首个快照）\n"
    r = _run(git_ws(ws_git, ["diff", "--numstat", _EMPTY_TREE, "HEAD"],
                    workdir), cwd=Path(workdir))
    return r.stdout if r.returncode == 0 else None


def restore(session_dir: Path, workdir: str, scope: str,
            ref: Optional[str] = None) -> dict:
    """回滚。scope: workspace=只回滚工作区 / conv=只回滚会话记录 /
    all=两者。ref 缺省=回滚到上一 checkpoint（HEAD^ / HEAD~1）。

    返回 {"ok", "message", "work_restored", "conv_restored"}。
    """
    session_dir = Path(session_dir)
    ws_git = session_dir / WS_GIT_DIR
    if scope not in ("workspace", "conv", "all"):
        return {"ok": False, "message": f"未知 scope {scope}（应为 workspace/conv/all）"}

    work_restored = conv_restored = False
    msgs: list[str] = []

    if scope in ("workspace", "all"):
        r = _restore_work(ws_git, workdir, ref)
        work_restored = r["ok"]
        msgs.append(r["message"])

    if scope in ("conv", "all"):
        r = _restore_conv(session_dir, ref)
        conv_restored = r["ok"]
        msgs.append(r["message"])

    return {"ok": work_restored or conv_restored,
            "message": "；".join(msgs),
            "work_restored": work_restored,
            "conv_restored": conv_restored}


def _restore_work(ws_git: Path, workdir: str,
                  ref: Optional[str]) -> dict:
    target = ref or "HEAD~1"
    # 若 ref 是该仓库内不存在的 hash，则试着按 label 在 log 里找；找不到回滚
    # 默认上一 checkpoint。工作区与会话是独立两个 git，ref 须各自解释。
    return _do_restore(ws_git, workdir, "工作区", target, git_fn=git_ws)


def _restore_conv(session_dir: Path, ref: Optional[str]) -> dict:
    target = ref or "HEAD~1"
    return _do_restore_conv(session_dir, "会话记录", target)


def _do_restore(ws_git: Path, workdir: str, what: str,
                target: str, git_fn) -> dict:
    """工作区回滚通用：target 为 ref 或 label。

    用 `reset --hard <commit>`（而非 `checkout <commit> -- .`）——reset
    会把 worktree 整体重置到该 commit，**删除目标 commit 中不存在的**文件
    （checkout 带路径只恢复已存在文件、不会删新增文件）。"""
    resolved = _resolve_ref(git_fn, ws_git, workdir, target)
    if resolved is None:
        return {"ok": False,
                "message": f"{what}无目标 checkpoint {target}（可回滚范围不足）"}
    r = _run(git_fn(ws_git, ["reset", "--hard", resolved], workdir),
             cwd=Path(workdir))
    if r.returncode != 0:
        return {"ok": False,
                "message": f"{what}回滚失败: {r.stderr.strip()[:200]}"}
    return {"ok": True, "message": f"{what}已回滚到 {resolved}"}


def _do_restore_conv(session_dir: Path, what: str, target: str) -> dict:
    resolved = _resolve_ref_conv(session_dir, target)
    if resolved is None:
        return {"ok": False,
                "message": f"{what}无目标 checkpoint {target}（可回滚范围不足）"}
    r = _run(["git", "-C", str(session_dir), "checkout", resolved, "--", "."])
    if r.returncode != 0:
        return {"ok": False,
                "message": f"{what}回滚失败: {r.stderr.strip()[:200]}"}
    return {"ok": True, "message": f"{what}已回滚到 {resolved}"}


def _resolve_ref(git_fn, ws_git: Path, workdir: str, target: str) -> Optional[str]:
    """把 target（ref 或 label）解析为工作区 git 里存在的 commit。
    target 直接是 hash/ref 则校验；否则当 label 在 log --grep 找。"""
    try:
        exists = _run(git_fn(ws_git, ["rev-parse", "--verify",
                                      f"{target}^{{commit}}"], workdir),
                      cwd=Path(workdir))
        if exists.returncode == 0 and exists.stdout.strip():
            return exists.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    # 按 label 找（git log --grep）
    found = _run(git_fn(ws_git, ["log", "--format=%H", "--grep",
                                 target, "-1", "HEAD"], workdir),
                 cwd=Path(workdir))
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    return None


def _resolve_ref_conv(session_dir: Path, target: str) -> Optional[str]:
    try:
        exists = _run(["git", "-C", str(session_dir), "rev-parse",
                       "--verify", f"{target}^{{commit}}"])
        if exists.returncode == 0 and exists.stdout.strip():
            return exists.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    found = _run(["git", "-C", str(session_dir), "log",
                  "--format=%H", "--grep", target, "-1", "HEAD"])
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    return None


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")