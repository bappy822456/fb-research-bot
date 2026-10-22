#!/usr/bin/env python3
"""FB Research Helper — Telegram webhook app for a Render Free Web Service.

Important: Facebook numeric IDs do NOT provide a reliable account creation date.
This bot never converts an ID range into an account age or trust/risk score.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import html
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
API_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
HOOK_SECRET = hashlib.sha256((TOKEN + "|fb-research-webhook-v1").encode()).hexdigest() if TOKEN else ""
HOOK_PATH = f"/telegram/{HOOK_SECRET}" if HOOK_SECRET else "/telegram/not-configured"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("fb-research-helper")
app = Flask(__name__)

# One thread-local HTTP session per request thread; avoids shared-session races.
_thread_state = threading.local()
_webhook_lock = threading.Lock()
_webhook_registered = False
_last_webhook_error = "not attempted"
_seen_update_ids: set[int] = set()
_seen_lock = threading.Lock()
MAX_SEEN_UPDATES = 3000
_pending_dates: dict[int, tuple[str, float]] = {}
_pending_dates_lock = threading.Lock()
DATE_INPUT_TTL_SECONDS = 600

ALLOWED_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com", "fb.com", "www.fb.com"}
RESERVED_PATHS = {
    "about", "ads", "business", "events", "gaming", "groups", "help", "login", "marketplace",
    "messages", "notifications", "people", "photo", "photos", "policies", "privacy", "recover",
    "share", "sharer", "story.php", "watch", "reel", "hashtag", "settings"
}


def http_session() -> requests.Session:
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "FBResearchHelper/1.0 (+public profile research)"})
        _thread_state.session = session
    return session


def telegram_call(method: str, payload: dict | None = None, timeout: tuple[int, int] = (4, 8)) -> dict | None:
    """Call Telegram without ever logging the bot token or full API URL."""
    if not TOKEN:
        return None
    try:
        response = http_session().post(f"{API_URL}/{method}", json=payload or {}, timeout=timeout)
        data = response.json()
        if response.ok and data.get("ok"):
            return data
        description = str(data.get("description", "Telegram API rejected request"))[:180]
        log.warning("Telegram API %s failed: HTTP %s; %s", method, response.status_code, description)
    except (requests.RequestException, ValueError) as exc:
        log.warning("Telegram API %s connection/response failure: %s", method, type(exc).__name__)
    return None


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return telegram_call("sendMessage", payload) is not None


def extract_profile(value: str) -> tuple[str | None, str | None, str | None]:
    """Return (value, kind, canonical profile URL) for a numeric ID or FB URL."""
    value = value.strip()
    if re.fullmatch(r"\d{6,20}", value):
        return value, "numeric_id", f"https://www.facebook.com/profile.php?id={value}"

    # Allow a bare profile path pasted without a scheme, but never arbitrary hosts.
    candidate = value if re.match(r"^https?://", value, re.I) else "https://" + value
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None, None, None
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return None, None, None

    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)
    if path.lower() == "profile.php":
        ids = query.get("id", [])
        if ids and re.fullmatch(r"\d{6,20}", ids[0]):
            return ids[0], "numeric_id", f"https://www.facebook.com/profile.php?id={ids[0]}"
        return None, None, None

    parts = [part for part in path.split("/") if part]
    if not parts:
        return None, None, None
    if parts[0].lower() == "people":
        if len(parts) >= 3 and re.fullmatch(r"\d{6,20}", parts[-1]):
            return parts[-1], "numeric_id", f"https://www.facebook.com/{path}"
        if len(parts) >= 2:
            username = parts[1]
            if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", username):
                return username, "username", f"https://www.facebook.com/{path}"
        return None, None, None

    first = parts[0]
    if first.lower() in RESERVED_PATHS or first.lower().endswith(".php"):
        return None, None, None
    if re.fullmatch(r"\d{6,20}", first):
        return first, "numeric_id", f"https://www.facebook.com/profile.php?id={first}"
    if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", first):
        return first, "username", f"https://www.facebook.com/{first}"
    return None, None, None


def resolve_public_username(profile_url: str) -> str | None:
    """Best-effort lookup of a public profile URL; no Facebook login or private data."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": "https://lookup-id.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        try:
            session.get("https://lookup-id.com/", timeout=(3, 4))  # establish provider cookies
        except requests.RequestException:
            pass
        response = session.post(
            "https://lookup-id.com/",
            data={"fburl": profile_url, "check": "Lookup"},
            timeout=(4, 7),
            allow_redirects=True,
        )
        if not response.ok:
            return None
        match = re.search(r'<span\s+id=["\']code["\']\s*>(\d{6,20})</span>', response.text or "", re.I)
        return match.group(1) if match else None
    except requests.RequestException as exc:
        log.info("Public ID lookup unavailable: %s", type(exc).__name__)
        return None
    finally:
        session.close()


def id_note(fbid: str) -> str:
    if fbid.startswith("615"):
        return "This ID has the 615 prefix. That prefix is not a reliable timestamp and does not establish when the account was created."
    if fbid.startswith("1000"):
        return "This ID has the 1000… format. This format is not a reliable timestamp and does not establish when the account was created."
    return "A numeric Facebook ID alone does not reliably reveal the account's creation date."


def analysis_message(fbid: str, username: str | None = None) -> str:
    user_line = f"👤 <b>Username:</b> <code>@{html.escape(username)}</code>\n" if username else ""
    return (
        f"{user_line}📊 <b>PUBLIC PROFILE ID REPORT</b>\n\n"
        f"🆔 <b>Numeric ID:</b> <code>{html.escape(fbid)}</code>\n"
        f"🧭 <b>ID pattern note:</b> {html.escape(id_note(fbid))}\n\n"
        "📅 <b>Account creation date/age:</b> Cannot be reliably determined from this numeric ID. "
        "If Facebook publicly displays <i>Joined Facebook</i> on the profile, use that date; it may not be visible for every profile.\n\n"
        "🛡️ <b>Authenticity/trust:</b> Cannot be assessed from the ID.\n"
        "⚠️ <b>Account risk:</b> Cannot be assessed from the ID; review public profile context independently.\n\n"
        "⚖️ <i>Educational research only. This bot does not access private account data and does not verify identity.</i>"
    )


def profile_keyboard(profile_url: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "Open Facebook profile", "url": profile_url}],
        [{"text": "Calculate from public Joined date", "callback_data": "age_joined"}],
        [{"text": "Use oldest public post (minimum age)", "callback_data": "age_oldest"}],
        [{"text": "Why ID alone is insufficient", "callback_data": "id_limits"}],
    ]}


def dhaka_today() -> date:
    # Bangladesh has a fixed UTC+6 offset (no daylight-saving transition).
    return datetime.now(timezone(timedelta(hours=6))).date()


def parse_public_date(text: str):
    """Parse YYYY-MM-DD (exact date) or YYYY-MM (month-only date)."""
    value = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
            return parsed, parsed, False
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        try:
            year, month = map(int, value.split("-"))
            first = date(year, month, 1)
            last = date(year, month, calendar.monthrange(year, month)[1])
            return first, last, True
        except ValueError:
            return None
    return None


def format_duration(days: int) -> str:
    days = max(0, days)
    if days == 0:
        return "today (0 days)"
    if days < 7:
        return f"about {days} day{'s' if days != 1 else ''}"
    if days < 30:
        weeks = max(1, round(days / 7))
        return f"about {weeks} week{'s' if weeks != 1 else ''}"
    if days < 60:
        return "about 1 month"
    if days < 730:
        total_months = max(1, round(days / 30.44))
        years, months = divmod(total_months, 12)
        if years:
            return f"about {years} year{'s' if years != 1 else ''}" + (f" {months} month{'s' if months != 1 else ''}" if months else "")
        return f"about {months} months"
    return f"about {days / 365.2425:.1f} years"


def process_public_date(chat_id: int, text: str, mode: str) -> bool:
    parsed = parse_public_date(text)
    if not parsed:
        send_message(chat_id, "I couldn't read that date. Send it as <code>YYYY-MM-DD</code> (for example <code>2024-06-15</code>) or month-only as <code>YYYY-MM</code>.")
        return False
    first_day, last_day, month_only = parsed
    today = dhaka_today()
    if first_day < date(2004, 2, 4):
        send_message(chat_id, "That date is before Facebook launched. Please check the public date and send it again, or use /cancel.")
        return False
    if first_day > today:
        send_message(chat_id, "That date is in the future. Please check it and send a past public date, or use /cancel.")
        return False
    last_day = min(last_day, today)
    days_min = max(0, (today - last_day).days)
    days_max = max(0, (today - first_day).days)

    if mode == "joined":
        if month_only and days_min != days_max:
            result = f"approximately between {format_duration(days_min)} and {format_duration(days_max)}"
            source = f"the month you entered (<code>{first_day:%Y-%m}</code>)"
        else:
            result = f"approximately {format_duration(days_min)} ({days_min:,} days)"
            source = f"the date you entered (<code>{first_day:%Y-%m-%d}</code>)"
        message = (
            f"📅 <b>Elapsed time from the public Joined date:</b> {result}.\n"
            f"🧾 <b>Source:</b> {source}; supplied by you and not independently verified.\n\n"
            "This is only as accurate as the date shown on Facebook."
        )
    else:
        # The earliest visible post proves the profile existed by then, at most; it does not prove creation then.
        message = (
            f"🗓️ <b>Minimum age clue:</b> The profile existed by the public post date you supplied, so it is at least {format_duration(days_min)} old.\n"
            f"🧾 <b>Source:</b> oldest public post date <code>{first_day:%Y-%m-%d}</code>" + (f" (month-only entry: <code>{first_day:%Y-%m}</code>; conservative lower bound)" if month_only else "") + ".\n\n"
            "The account may be older; a post date does not reveal its exact creation date."
        )
    send_message(chat_id, message)
    return True


def handle_callback_query(callback: dict) -> None:
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
    if not chat_id:
        return
    if data == "age_joined":
        with _pending_dates_lock:
            _pending_dates[chat_id] = ("joined", time.time())
        send_message(chat_id,
            "Send the public <b>Joined Facebook</b> date shown on the profile as <code>YYYY-MM-DD</code> or <code>YYYY-MM</code>.\n\n"
            "Example: <code>2024-06-15</code>. I will calculate elapsed time from the date you provide; I cannot verify the date myself. Send /cancel to stop.")
    elif data == "age_oldest":
        with _pending_dates_lock:
            _pending_dates[chat_id] = ("oldest", time.time())
        send_message(chat_id,
            "Send the date of the <b>oldest public post</b> you can see as <code>YYYY-MM-DD</code> or <code>YYYY-MM</code>.\n\n"
            "I can calculate a minimum-age clue (the account existed by that date), not its exact creation date. Send /cancel to stop.")
    elif data == "id_limits":
        send_message(chat_id,
            "A numeric Facebook ID is not a reliable timestamp. To estimate from public evidence, use the Joined Facebook date (if visible) or the oldest public post date. A post provides only a minimum-age clue.")


def handle_message(message: dict) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return

    with _pending_dates_lock:
        pending = _pending_dates.get(chat_id)
        if pending and time.time() - pending[1] > DATE_INPUT_TTL_SECONDS:
            _pending_dates.pop(chat_id, None)
            pending = None
    if text.strip().lower() == "/cancel" and pending:
        with _pending_dates_lock:
            _pending_dates.pop(chat_id, None)
        send_message(chat_id, "Date entry cancelled. Send a profile link or ID whenever you are ready.")
        return
    if pending and not text.startswith("/"):
        if process_public_date(chat_id, text, pending[0]):
            with _pending_dates_lock:
                _pending_dates.pop(chat_id, None)
        return

    if text.startswith("/start"):
        send_message(chat_id,
            "👋 <b>FB Research Helper</b>\n\n"
            "Send a public Facebook profile link or numeric ID. I can try to resolve a username link to an ID.\n\n"
            "<b>Important:</b> A numeric ID cannot reliably reveal an account's age or authenticity. "
            "After the report, use the buttons to enter a public <i>Joined Facebook</i> date or oldest public post date.\n\n"
            "Commands: /start · /help · /demo")
        return
    if text.startswith("/help"):
        send_message(chat_id,
            "<b>How to use</b>\n"
            "1. Send a Facebook profile URL or a numeric profile ID.\n"
            "2. Username URLs are best-effort resolved using a third-party public lookup.\n"
            "3. The ID is not a creation timestamp; age, trust, and identity cannot be verified from it.\n\n"
            "For research and education only. No phone-number search or private data access.")
        return
    if text.startswith("/demo"):
        send_message(chat_id,
            "<b>Demo</b>\nSend <code>61550000000000</code> or a Facebook profile URL.\n"
            "The report will explain that the ID alone cannot establish an account's creation date.")
        return
    if text.startswith("/"):
        return

    value, kind, profile_url = extract_profile(text)
    if not value:
        send_message(chat_id, "Please send a valid Facebook profile URL or a numeric ID (6–20 digits).")
        return

    if kind == "numeric_id":
        send_message(chat_id, analysis_message(value), profile_keyboard(profile_url or f"https://www.facebook.com/profile.php?id={value}"))
        return

    telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    send_message(chat_id, f"⏳ Trying to resolve <code>@{html.escape(value)}</code> to a public numeric ID…")
    numeric_id = resolve_public_username(profile_url or text)
    if numeric_id:
        send_message(chat_id, analysis_message(numeric_id, value), profile_keyboard(profile_url or text))
    else:
        send_message(chat_id,
            f"⚠️ I couldn't resolve <code>@{html.escape(value)}</code> right now. The lookup service may be unavailable or may not recognize this profile.\n\n"
            "You can send the numeric ID if you already have it. Even with an ID, account age cannot be reliably inferred.",
            {"inline_keyboard": [[{"text": "Open lookup-id.com", "url": "https://lookup-id.com/"}],
                                  [{"text": "Open Facebook profile", "url": profile_url or text}]]})


def remember_update(update_id: int) -> bool:
    """Prevent Telegram retries from creating duplicate replies within this process."""
    with _seen_lock:
        if update_id in _seen_update_ids:
            return False
        _seen_update_ids.add(update_id)
        if len(_seen_update_ids) > MAX_SEEN_UPDATES:
            _seen_update_ids.clear()
            _seen_update_ids.add(update_id)
        return True


def register_webhook() -> bool:
    global _webhook_registered, _last_webhook_error
    if not TOKEN:
        _last_webhook_error = "BOT_TOKEN is missing"
        log.error("BOT_TOKEN environment variable is missing")
        return False
    if not RENDER_URL:
        _last_webhook_error = "RENDER_EXTERNAL_URL is missing"
        log.error("RENDER_EXTERNAL_URL is missing; cannot register Telegram webhook")
        return False

    with _webhook_lock:
        try:
            response = requests.post(
                f"{API_URL}/setWebhook",
                json={
                    "url": RENDER_URL + HOOK_PATH,
                    "secret_token": HOOK_SECRET,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": False,
                },
                timeout=(5, 12),
            )
            try:
                result = response.json()
            except ValueError:
                result = {}
            if response.ok and result.get("ok"):
                _webhook_registered = True
                _last_webhook_error = ""
                log.info("Telegram webhook registered successfully")
                return True
            description = str(result.get("description", f"HTTP {response.status_code}"))[:180]
            _last_webhook_error = description
            _webhook_registered = False
            log.error("Telegram rejected webhook setup: %s", description)
        except requests.RequestException as exc:
            _last_webhook_error = f"Network error ({type(exc).__name__})"
            _webhook_registered = False
            log.error("Could not contact Telegram to register webhook: %s", type(exc).__name__)
    return False


@app.get("/")
def home():
    return "FB Research Helper is online. Telegram webhook mode.\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/healthz")
def health():
    # Re-attempt registration when an operator visits health after a transient startup network error.
    if TOKEN and RENDER_URL and not _webhook_registered:
        register_webhook()
    return jsonify({
        "ok": True,
        "token_configured": bool(TOKEN),
        "webhook_registered": _webhook_registered,
        "webhook_error": _last_webhook_error or None,
    })


@app.post(HOOK_PATH)
def receive_telegram_update():
    if not TOKEN or not HOOK_SECRET:
        return "Not configured", 503
    sent_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent_secret, HOOK_SECRET):
        return "Forbidden", 403

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")
    if isinstance(update_id, int) and not remember_update(update_id):
        return "ok", 200

    try:
        if isinstance(update.get("message"), dict):
            handle_message(update["message"])
        elif isinstance(update.get("edited_message"), dict):
            handle_message(update["edited_message"])
        elif isinstance(update.get("callback_query"), dict):
            handle_callback_query(update["callback_query"])
    except Exception:
        # Do not log message bodies, profile URLs, or secrets.
        log.exception("Unhandled error while processing a Telegram update")
    return "ok", 200


# Register once at app import/startup (Gunicorn imports this module).
register_webhook()
