"""Notification architecture (Email / Teams / Slack / Webhook). Nothing is required for local operation.

Every channel is disabled unless configured through environment variables. `dispatch()` records each attempt in the
Notification table as SENT / SKIPPED (not configured) / FAILED, so behaviour is transparent.
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from abc import ABC, abstractmethod
from email.message import EmailMessage

from flask import current_app

from ..extensions import db
from ..models import Notification


class Notifier(ABC):
    name = "base"

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def send(self, subject: str, body: str) -> str: ...


def _post_json(url: str, payload: dict) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - URL comes from operator-controlled config
        return f"HTTP {r.status}"


class WebhookNotifier(Notifier):
    name = "WEBHOOK"

    def configured(self):
        return bool(current_app.config.get("NOTIFY_WEBHOOK_URL"))

    def send(self, subject, body):
        return _post_json(current_app.config["NOTIFY_WEBHOOK_URL"], {"subject": subject, "body": body, "source": "inventory-control-tower"})


class SlackNotifier(Notifier):
    name = "SLACK"

    def configured(self):
        return bool(current_app.config.get("NOTIFY_SLACK_URL"))

    def send(self, subject, body):
        return _post_json(current_app.config["NOTIFY_SLACK_URL"], {"text": f"*{subject}*\n{body}"})


class TeamsNotifier(Notifier):
    name = "TEAMS"

    def configured(self):
        return bool(current_app.config.get("NOTIFY_TEAMS_URL"))

    def send(self, subject, body):
        return _post_json(current_app.config["NOTIFY_TEAMS_URL"], {"title": subject, "text": body})


class EmailNotifier(Notifier):
    name = "EMAIL"

    def configured(self):
        c = current_app.config
        return bool(c.get("SMTP_HOST") and c.get("NOTIFY_EMAIL_TO"))

    def send(self, subject, body):
        c = current_app.config
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, c.get("SMTP_USER") or "ict@localhost", c["NOTIFY_EMAIL_TO"]
        msg.set_content(body)
        with smtplib.SMTP(c["SMTP_HOST"], c["SMTP_PORT"], timeout=8) as s:
            s.starttls()
            if c.get("SMTP_USER"):
                s.login(c["SMTP_USER"], c.get("SMTP_PASSWORD") or "")
            s.send_message(msg)
        return "SMTP sent"


NOTIFIERS: dict[str, Notifier] = {n.name: n for n in (EmailNotifier(), TeamsNotifier(), SlackNotifier(), WebhookNotifier())}


def dispatch(subject: str, body: str, channels: list[str] | None = None) -> list[dict]:
    out = []
    for name in (channels or list(NOTIFIERS)):
        n = NOTIFIERS[name]
        if not n.configured():
            status, detail = "SKIPPED", "channel not configured"
        else:
            try:
                status, detail = "SENT", n.send(subject, body)
            except Exception as e:  # network/SMTP errors must never break the caller
                status, detail = "FAILED", str(e)[:180]
        db.session.add(Notification(channel=name, subject=subject[:200], body=body[:4000], status=status, detail=detail))
        out.append({"channel": name, "status": status, "detail": detail})
    return out


def status() -> list[dict]:
    return [{"channel": n.name, "configured": n.configured()} for n in NOTIFIERS.values()]
