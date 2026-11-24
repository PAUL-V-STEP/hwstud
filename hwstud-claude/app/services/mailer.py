from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from ..config import Settings


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, *, subject: str, body: str) -> None:
        settings = self.settings
        if not settings.smtp_configured:
            raise RuntimeError("SMTP не настроен: заполните параметры в .env")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = settings.mail_from
        message["To"] = settings.mail_to
        message.set_content(body, charset="utf-8")

        context = ssl.create_default_context()
        if settings.smtp_security == "ssl":
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=30,
                context=context,
            ) as smtp:
                smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=30
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
