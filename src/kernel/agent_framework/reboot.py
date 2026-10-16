"""Self-restart support: the ``system_reboot`` tool + restart signal.

The tool sets a process-level signal and returns immediately; the Agent's
run loop exits, main() tears down the HTTP server / beat / database and
``os.execv`` re-launches the same runtime (config / skills / prompts
changes take effect). Startup recovery injects a follow-up message so the
cognition round closes cleanly.
"""

from __future__ import annotations

import threading

REBOOT_TOOL_NAME = "system_reboot"
REBOOT_OK_MESSAGE = "项目重启成功。"
REBOOT_NOTICE_MESSAGE = "系统已重新启动，请继续。"

_restart = threading.Event()


def request_restart() -> None:
    _restart.set()


def restart_pending() -> bool:
    return _restart.is_set()


def reboot_tool() -> str:
    """重启 Agent Core，使新的运行环境重新建立。

    推荐场景：
    1. 你刚刚创建或修改了 Skill、系统级运行提示或其他需要在新一轮
       Core 初始化时生效的内容；
    2. 当前运行环境出现无法通过普通工具恢复的异常、组件阻塞或其他
       运行问题，可以尝试重新启动 Core。

    注意：
    - 重启不会改变你的长期身份；
    - 持久化的上下文、工作空间和其他长期状态会继续存在；
    - 当前运行中的工作可能被中断，请在需要时提前保存状态。"""
    request_restart()
    return "已请求重启：系统即将重启，完成后自动继续。"