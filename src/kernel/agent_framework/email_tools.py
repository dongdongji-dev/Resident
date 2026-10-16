"""Kernel-bound email channel: SMTP send + IMAP receive (stdlib only).

Not an external adapter — these are Agent tools wired into the kernel, the
escape channel: even when the docker container / workspace is unusable, as
long as the kernel is alive the Agent can send and read mail.

Config (``email`` YAML section, all values from config, no hidden
defaults):
- smtp.host/port/ssl/username/password   outbound
- imap.host/port/ssl/username/password   inbound (read + mark \\Seen)
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage, Message
from typing import Any, Optional

from kernel.agent_framework.config import require
from kernel.agent_framework.logging_setup import get_logger

logger = get_logger("kernel.agent_framework.email_tools")

_MAIL_MAX_CHARS = 8000


@dataclass(frozen=True, slots=True)
class EmailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    # SMTP 与 IMAP 凭据分开存储：两者密码可能不同（各自的 username/password
    # 段必须独立读取，不能拿 SMTP 的凭据去登 IMAP）
    smtp_username: str
    smtp_password: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    imap_username: str
    imap_password: str


def load_email_config(config: dict) -> Optional[EmailConfig]:
    """Build EmailConfig from the ``email`` YAML section. Returns None when
    ``email.enabled`` is false (tools are not registered)."""
    section = config.get("email") or {}
    if not bool(section.get("enabled", False)):
        return None
    smtp = section["smtp"]
    imap = section["imap"]
    return EmailConfig(
        enabled=True,
        smtp_host=str(require({"smtp": smtp}, "smtp.host")),
        smtp_port=int(require({"smtp": smtp}, "smtp.port")),
        smtp_ssl=bool(require({"smtp": smtp}, "smtp.ssl")),
        imap_host=str(require({"imap": imap}, "imap.host")),
        imap_port=int(require({"imap": imap}, "imap.port")),
        imap_ssl=bool(require({"imap": imap}, "imap.ssl")),
        smtp_username=str(require({"smtp": smtp}, "smtp.username")),
        smtp_password=str(require({"smtp": smtp}, "smtp.password")),
        imap_username=str(require({"imap": imap}, "imap.username")),
        imap_password=str(require({"imap": imap}, "imap.password")),
    )


# ── transport (separable for tests) ────────────────────────────────────

def _smtp_send(cfg: EmailConfig, to: str, subject: str, body: str) -> None:
    """Send one mail via SMTP (raise on failure)."""
    message = EmailMessage()
    message["From"] = cfg.smtp_username
    message["To"] = to
    message["Subject"] = (subject or "").strip() or "(无主题)"
    message.set_content(body or "")
    if cfg.smtp_ssl:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                              timeout=60) as smtp:
            smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(message)


def _parse_address(raw: Any) -> tuple[str, str]:
    """Return (name, addr) from an RFC 5322 address string (RFC2047)."""
    if raw is None:
        return "", ""
    try:
        text = str(email.header.make_header(
            email.header.decode_header(raw)))
    except Exception:  # noqa: BLE001
        text = str(raw)
    name, addr = email.utils.parseaddr(text)
    return str(name or ""), str(addr or "").lower()


def _header_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(email.header.make_header(
            email.header.decode_header(value))).strip()
    except Exception:  # noqa: BLE001
        return str(value or "").strip()


def _extract_body(message: Message) -> str:
    """Plain-text body first; falls back to de-tagged HTML."""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        # HTML fallback
        for part in message.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
                import re
                return re.sub(r"<[^>]+>", "", html)
        return ""
    try:
        payload = message.get_payload(decode=True)
        if payload is None:
            return str(message.get_payload() or "")
        return payload.decode(message.get_content_charset() or "utf-8",
                              "replace")
    except Exception:  # noqa: BLE001
        return str(message.get_payload() or "")


def _imap_fetch(cfg: EmailConfig, limit: int) -> list[dict[str, Any]]:
    """Fetch the latest unseen mails from INBOX and mark them \\Seen."""
    if cfg.imap_ssl:
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=60)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
    try:
        conn.login(cfg.imap_username, cfg.imap_password)
        conn.select("INBOX")
        _status, data = conn.search(None, "UNSEEN")
        ids = (data[0] or b"").split()
        picked = ids[-int(limit):] if ids else []
        items: list[dict[str, Any]] = []
        for mid in picked:
            _status, msg_data = conn.fetch(mid, "(RFC822)")
            if not msg_data or msg_data[0] is None:
                continue
            message = email.message_from_bytes(msg_data[0][1])
            name, addr = _parse_address(message.get("From"))
            body = _extract_body(message)
            if len(body) > _MAIL_MAX_CHARS:
                body = body[:_MAIL_MAX_CHARS] + "…（内容过长已截断）"
            items.append({
                "from_name": name,
                "from_addr": addr,
                "subject": _header_text(message.get("Subject")),
                "date": str(message.get("Date") or ""),
                "message_id": _header_text(message.get("Message-ID")),
                "body": body,
            })
            try:
                conn.store(mid, "+FLAGS", "\\Seen")
            except Exception:  # noqa: BLE001
                pass
        return items
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


# ── tools ──────────────────────────────────────────────────────────────

def make_email_send(cfg: EmailConfig) -> Any:
    def email_send(to: str, subject: str = "", body: str = "") -> dict:
        """通过 SMTP 发送一封邮件。

        参数:
          to: 收件人邮箱地址（必填）。
          subject: 主题（缺省为"(无主题)"）。
          body: 正文内容。

        用途: kernel 内置邮件通道——即使容器/docker 不可用，只要 kernel
        存活就能对外通信（紧急逃生通道）。"""
        target = (to or "").strip()
        if not target:
            return {"ok": False, "errors": ["收件人为空"]}
        try:
            _smtp_send(cfg, target, str(subject or ""), str(body or ""))
        except Exception as exc:  # noqa: BLE001
            logger.error("email_send failed to=%s: %s", target, exc)
            return {"ok": False, "errors": [str(exc)]}
        logger.info("email sent to=%s subject=%r", target, subject)
        return {"ok": True, "to": target,
                "subject": str(subject or "").strip() or "(无主题)"}

    return email_send


def make_email_receive(cfg: EmailConfig) -> Any:
    def email_receive(limit: int = 10) -> dict:
        """通过 IMAP 收取收件箱最新未读邮件（收取后标记已读）。

        参数:
          limit: 最多返回的未读邮件数（默认 10，上限 50）。

        用途: kernel 内置邮件通道——主动查看邮箱新邮件（紧急逃生通道）。
        """
        limit = max(1, min(int(limit or 10), 50))
        try:
            items = _imap_fetch(cfg, limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("email_receive failed: %s", exc)
            return {"ok": False, "errors": [str(exc)]}
        return {"ok": True, "count": len(items), "mails": items}

    return email_receive