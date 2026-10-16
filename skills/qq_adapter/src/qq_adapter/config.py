"""Adapter configuration: OneBot11 connection + core HTTP endpoints.

Deployment-agnostic by design: the adapter only needs a forward WebSocket
URL + token (napcat can be a container, a native binary or a remote VPS).
Every value comes from the adapter's own config file / environment - no
hidden defaults, no host assumptions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

# 通用 skill：状态目录可被环境变量覆盖（任何 agent 指到自己的位置，
# 不必改代码）
DEFAULT_STATE_DIR = os.environ.get("QQ_ADAPTER_STATE_DIR",
                                   "skill_data/qq_adapter")


@dataclass(frozen=True, slots=True)
class WhitelistConfig:
    """会话白名单（V2 语义：enabled=false 不限制，true 时仅列表内放行）"""
    private_enabled: bool = False
    private_ids: frozenset = frozenset()
    group_enabled: bool = False
    group_ids: frozenset = frozenset()


@dataclass(frozen=True, slots=True)
class RawEventsConfig:
    """原始事件落盘（排障用：抓"空内容"事件的真实 payload，
    2026-09-06 约定指出文件下载通知被 Napcat 当 message 推但内容丢失）。
    mode=all 全量；anomalous 只落无文本内容的 message 事件（文件下载通知
    之类，含偶发误伤的表情/纯语音消息——量小，可接受）。"""
    enabled: bool = True
    mode: str = "anomalous"   # all | anomalous
    path: str = "run/raw_events.jsonl"


@dataclass(frozen=True, slots=True)
class TransferConfig:
    """core → adapter 文件传输：纯 HTTP 流式字节转发（napcat 缓存自管，
    adapter 不落盘、内存 O(1)），仅限单次传输大小上限。"""
    max_bytes: int = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OneBotConfig:
    # OneBot11 正向 WebSocket（napcat）
    ws_url: str            # ws://127.0.0.1:3001
    access_token: str      # napcat WebUI 里配置的 token（可为空）
    # 平台实例标识：platform[instance]（如 qq[10001]，10001=bot QQ 号）
    platform: str
    # core HTTP
    core_url: str          # http://127.0.0.1:18086
    core_api_key: str
    # 行为参数
    api_timeout_seconds: float = 15.0   # napcat api() 调用超时（V2 痛点：大文件按动作放宽，后续）
    reconnect_backoff: tuple = (1, 2, 4, 8, 16, 30, 60)
    heartbeat_seconds: float = 30.0
    wakeup_on_mention: bool = True      # 群聊 @/回复触发 wakeup（其余仅入库）
    whitelist: WhitelistConfig = WhitelistConfig()
    media_files_root: str = "data/files"       # 宿主媒体根（发送媒体读文件用）
    media_base64_max_bytes: int = 5242880      # 小文件 base64 上限（大文件走 URL 直传）
    unread_threshold: int = 5                  # 主动发言仲裁阈值（未读超过则拒绝，
                                               #   除非 message force_exec="true"）
    actions: dict = field(default_factory=dict) # action 级 enable 开关（缺省：
                                               #   高风险 false，其余 true）
    # 本地 HTTP 动作端点（agent curl 调用 /hook 执行 52 动作）
    callback_host: str = "127.0.0.1"
    callback_port: int = 19671
    transfer: TransferConfig = field(
        default_factory=TransferConfig)        # 文件传输（临时目录/过期/上限）
    raw_events: RawEventsConfig = field(
        default_factory=RawEventsConfig)       # 原始事件落盘（排障）
    state_dir: str = DEFAULT_STATE_DIR         # 运行时状态目录（pid/port/
                                               #   attention/raw_events 都在这）
    media_serve_url: str = ""                   # 大文件 URL 前缀（如 http://host:18991，
                                                #   留空则大文件也 base64（不推荐））
    media_download_timeouts: dict = field(default_factory=dict)
    # 入站媒体下载"按段类型"的总超时（秒），覆盖 media.DEFAULT_TIMEOUTS。
    # 默认 {} = 用 media.py 内置值（image 30 / record 120 / video 180 /
    # file 600）。2026-09-13 前是写死的 12s，大文件必超时。
    media_download_max_mb: float = 50.0
    # 自动下载大小上限（MB，2026-10-16 设计约定）：入站媒体声明大小超过
    # 它就不下载——消息里改给**真实直链**，并由适配器发一条 notice 提醒
    # （直链有时效）。配置键 media_download.max_mb，<=0 表示不限制。


def _parse_actions(overrides: dict) -> dict:
    """action 级开关：未列出的用默认（高风险 false，其余 true）。"""
    from qq_adapter.actions_definition import NAP_ACTIONS, \
        HIGH_RISK_ACTIONS
    enabled = {}
    for item in NAP_ACTIONS:
        action = item["type"]
        default = action not in HIGH_RISK_ACTIONS
        raw = overrides.get(action, "true" if default else "false")
        enabled[action] = str(raw).lower() in ("true", "1", "yes", "on")
    return enabled


def load_config(paths) -> OneBotConfig:
    """Load YAML config. ``paths`` = 单个文件或**多个文件**（后者覆盖前者，
    支持 ``--config config/default.yaml --config config/local.yaml``
    的"通用默认 + 本地覆盖"分层；env 展开 ${VAR} 依然生效）。

    分层约定：``config/default.yaml`` 只能放通用默认值（不含任何身份/
    密钥/个人 ID），``config/local.yaml`` 放本机差异（白名单、端口等）；
    密钥一律来自环境变量（由 start.sh 从 ``.secrets/`` 注入）。"""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]

    def _merge(base: dict, extra: dict) -> dict:
        out = dict(base)
        for key, value in (extra or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _merge(out[key], value)
            else:
                out[key] = value
        return out

    raw: dict = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            raw = _merge(raw, yaml.safe_load(handle) or {})

    def expand(value: str) -> str:
        import re

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"missing required env var: {name}")
            return os.environ[name]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, str(value))

    section = raw.get("napcat", {})
    state_dir = str(raw.get("state_dir", DEFAULT_STATE_DIR))
    raw_events_raw = raw.get("raw_events", {}) or {}
    raw_events_path = str(raw_events_raw.get(
        "path") or os.path.join(state_dir, "raw_events.jsonl"))
    return OneBotConfig(
        ws_url=expand(section["ws_url"]),
        access_token=expand(section.get("access_token", "")),
        platform=expand(section["platform"]),
        core_url=expand(raw.get("core", {}).get("url", "http://127.0.0.1:18086")),
        core_api_key=expand(raw.get("core", {}).get("api_key", "")),
        api_timeout_seconds=float(
            section.get("api_timeout_seconds", 15.0)),
        reconnect_backoff=tuple(
            section.get("reconnect_backoff", [1, 2, 4, 8, 16, 30, 60])),
        heartbeat_seconds=float(section.get("heartbeat_seconds", 30.0)),
        wakeup_on_mention=bool(section.get("wakeup_on_mention", True)),
        whitelist=WhitelistConfig(
            private_enabled=bool(section.get("whitelist", {})
                                 .get("private_enabled", False)),
            private_ids=frozenset(str(x) for x in section
                                  .get("whitelist", {})
                                  .get("private", [])),
            group_enabled=bool(section.get("whitelist", {})
                               .get("group_enabled", False)),
            group_ids=frozenset(str(x) for x in section
                                .get("whitelist", {})
                                .get("group", [])),
        ),
        media_files_root=str(section.get("media_files_root",
                                         "data/files")),
        media_base64_max_bytes=int(section.get("media_base64_max_bytes",
                                               5242880)),
        media_serve_url=str(section.get("media_serve_url", "")),
        media_download_timeouts=dict(
            section.get("media_download", {}).get("timeouts", {}) or {}),
        media_download_max_mb=float(
            section.get("media_download", {}).get("max_mb", 50) or 0),
        unread_threshold=int(section.get("unread_threshold", 5)),
        callback_host=str(raw.get("callback", {})
                          .get("host", "127.0.0.1")),
        callback_port=int(raw.get("callback", {}).get("port", 19671)),
        actions=_parse_actions(raw.get("actions", {})),
        transfer=TransferConfig(
            max_bytes=int(raw.get("transfer", {}).get(
                "max_bytes", 2 * 1024 * 1024 * 1024)),
        ),
        raw_events=RawEventsConfig(
            enabled=bool(raw_events_raw.get("enabled", True)),
            mode=str(raw_events_raw.get("mode", "anomalous")),
            path=raw_events_path,
        ),
        state_dir=state_dir,
    )
