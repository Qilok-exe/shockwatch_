"""Optional network adapters. Acceptance is not confirmed phone delivery."""
from __future__ import annotations
from email.message import EmailMessage
import json
import os
import smtplib
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import pandas as pd
from .common import iso, utc

def telegram(body):
    token, chat = os.environ["SHOCKWATCH_TELEGRAM_TOKEN"], os.environ["SHOCKWATCH_TELEGRAM_CHAT_ID"]
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=urlencode({"chat_id": chat, "text": body[:4000]}).encode(), method="POST")
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected request")
    return "accepted_telegram"

def email(body):
    message = EmailMessage()
    message["From"], message["To"] = os.environ["SHOCKWATCH_EMAIL_FROM"], os.environ["SHOCKWATCH_EMAIL_TO"]
    message["Subject"] = "SHOCKWATCH monitor"
    message.set_content(body)
    with smtplib.SMTP(os.environ["SHOCKWATCH_SMTP_HOST"], int(os.getenv("SHOCKWATCH_SMTP_PORT", "587")), timeout=10) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(os.environ["SHOCKWATCH_SMTP_USER"], os.environ["SHOCKWATCH_SMTP_PASSWORD"])
        if client.send_message(message):
            raise RuntimeError("SMTP refused recipient")
    return "accepted_email"

def dispatch(store, cfg, now, senders=None):
    senders = senders or {"telegram": telegram, "email": email}
    out = []
    pending = store.db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY next_attempt,id LIMIT 20", (iso(now),)).fetchall()
    for row in pending:
        attempts = row["attempts"]+1
        status, error = "pending", None
        if not cfg["enabled"]:
            status = "dry_run"
        else:
            try:
                if row["channel"] == "email" or not cfg["telegram"]:
                    status = senders["email"](row["body"])
                else:
                    try:
                        status = senders["telegram"](row["body"])
                    except Exception:
                        if not cfg["email_fallback"]:
                            raise
                        senders["email"](row["body"])
                        status = "accepted_email_fallback"
            except Exception as exc:
                error = type(exc).__name__+(f":HTTP_{exc.code}" if isinstance(exc, HTTPError) else "")
                if attempts >= 8:
                    status = "failed"
        retry = iso(utc(now)+pd.Timedelta(seconds=min(3600, 30*2**attempts)))
        store.db.execute("UPDATE outbox SET status=?,attempts=?,next_attempt=?,last_error=? WHERE id=?", (status, attempts, retry, error, row["id"]))
        out.append({"id": row["id"], "status": status, "error": error})
    return out
