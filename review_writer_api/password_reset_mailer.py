"""SMTP delivery for short-lived password reset links."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal


SmtpSecurity = Literal["starttls", "tls", "none"]


class SmtpPasswordResetMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
        security: SmtpSecurity = "starttls",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port)
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.from_email = str(from_email or "").strip()
        self.security = security
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        if not self.host or not self.from_email:
            raise ValueError("SMTP host and sender address are required.")

    def send(
        self,
        recipient: str,
        reset_url: str,
        expires_minutes: int,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = "重置 Review Writer 登录密码"
        message["From"] = self.from_email
        message["To"] = recipient
        message.set_content(
            "\n".join(
                (
                    "你申请了重置 Review Writer 登录密码。",
                    "",
                    f"请在 {int(expires_minutes)} 分钟内打开以下一次性链接：",
                    reset_url,
                    "",
                    "如果这不是你的操作，请忽略本邮件；原密码不会被修改。",
                    "This one-time link resets your Review Writer password. "
                    "Ignore this message if you did not request it.",
                )
            )
        )

        if self.security == "tls":
            with smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            ) as client:
                self._authenticate_and_send(client, message)
            return

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
            client.ehlo()
            if self.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            self._authenticate_and_send(client, message)

    def _authenticate_and_send(
        self, client: smtplib.SMTP, message: EmailMessage
    ) -> None:
        if self.username:
            client.login(self.username, self.password)
        client.send_message(message)
