"""kernel.agent_framework —— 运行期别名包。

把 ``kernel.agent_framework`` 映射到本 skill 的 ``src/agent_framework``，
等价于原分发中的符号链接 ``src/kernel/agent_framework -> ../agent_framework``
（符号链接经 zip 分发 / Windows 解压会丢失，故改为 ``__path__`` 重定向，
跨平台行为一致；``kernel`` 只是运行期的别名包）。
"""

import pathlib

__path__ = [
    str(pathlib.Path(__file__).resolve().parent.parent.parent / "agent_framework")
]
