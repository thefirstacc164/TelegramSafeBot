#!/usr/bin/env python3
"""
Vault Bot — Discrete cloud storage via Telegram file_id linking.
Single-file deployment target for Render.com.
"""

import os
import json
import threading
import logging
from pathlib import Path
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ──────────────────────────── CONFIG ────────────────────────────

API_TOKEN = os.environ.get("API_TOKEN", "")
VAULT_PASSWORD = os.environ.get("VAULT_PASSWORD", "")
PORT = int(os.environ.get("PORT", 8080))
DB_FILE = "games_vault_db.json"

# ──────────────────────────── LOGGING ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vault")

# ──────────────────────────── FLASK KEEP-ALIVE ──────────────────

app = Flask(__name__)


@app.route("/")
def health():
    return "OK", 200


@app.route("/health")
def health_check():
    return "OK", 200


def _run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


# ──────────────────────────── BOT INIT ──────────────────────────

bot = telebot.TeleBot(API_TOKEN, parse_mode=None)

# Per-user states: "LOCKED" | "UNLOCKED" | "AWAITING_SEARCH"
user_states: dict[int, str] = {}


# ──────────────────────────── DB HELPERS ────────────────────────

def _load_db() -> list[dict]:
    """Return list of {name, file_id, type} dicts."""
    path = Path(DB_FILE)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_db(records: list[dict]) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _add_record(name: str, file_id: str, file_type: str) -> None:
    records = _load_db()
    # Avoid exact duplicates by file_id
    if any(r.get("file_id") == file_id for r in records):
        return
    records.append({"name": name, "file_id": file_id, "type": file_type})
    _save_db(records)


# ──────────────────────────── UI BUILDERS ───────────────────────

def _menu_markup() -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup(row_width=2)
    mk.add(
        InlineKeyboardButton("📂 All Games", callback_data="cb_all"),
        InlineKeyboardButton("🔍 Search", callback_data="cb_search"),
    )
    mk.add(InlineKeyboardButton("🔒 Lock", callback_data="cb_lock"))
    return mk


def _files_markup(records: list[dict]) -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup(row_width=1)
    for idx, rec in enumerate(records):
        mk.add(
            InlineKeyboardButton(
                f"📦 {rec['name']}",
                callback_data=f"dl_{idx}",
            )
        )
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
    return mk


def _back_markup() -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
    return mk


# ──────────────────────────── STATE HELPERS ─────────────────────

def _state(uid: int) -> str:
    return user_states.get(uid, "LOCKED")


def _set_state(uid: int, state: str) -> None:
    user_states[uid] = state


# ──────────────────────────── /start ────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.from_user.id
    _set_state(uid, "LOCKED")
    bot.send_message(msg.chat.id, "🔒 Password:")


# ──────────────────────────── CALLBACK QUERIES ──────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    mid = call.message.message_id
    data = call.data

    if _state(uid) == "LOCKED":
        bot.answer_callback_query(call.id)
        return

    # ── All Games ───────────────────────────────────────────────
    if data == "cb_all":
        records = _load_db()
        if not records:
            bot.edit_message_text(
                "❌ Not found.",
                chat_id=cid,
                message_id=mid,
                reply_markup=_back_markup(),
            )
        else:
            bot.edit_message_text(
                "🎮 Games:",
                chat_id=cid,
                message_id=mid,
                reply_markup=_files_markup(records),
            )
        bot.answer_callback_query(call.id)
        return

    # ── Search ──────────────────────────────────────────────────
    if data == "cb_search":
        _set_state(uid, "AWAITING_SEARCH")
        bot.edit_message_text(
            "🔍 Enter name:",
            chat_id=cid,
            message_id=mid,
            reply_markup=_back_markup(),
        )
        bot.answer_callback_query(call.id)
        return

    # ── Lock ────────────────────────────────────────────────────
    if data == "cb_lock":
        _set_state(uid, "LOCKED")
        bot.edit_message_text("🔒 Password:", chat_id=cid, message_id=mid)
        bot.answer_callback_query(call.id)
        return

    # ── Back to menu ────────────────────────────────────────────
    if data == "cb_back":
        _set_state(uid, "UNLOCKED")
        bot.edit_message_text(
            "🟢 Open.",
            chat_id=cid,
            message_id=mid,
            reply_markup=_menu_markup(),
        )
        bot.answer_callback_query(call.id)
        return

    # ── Download file by index ──────────────────────────────────
    if data.startswith("dl_"):
        try:
            idx = int(data[3:])
            records = _load_db()
            rec = records[idx]
            file_id = rec["file_id"]
            file_type = rec.get("type", "document")

            if file_type == "video":
                bot.send_video(cid, file_id)
            else:
                bot.send_document(cid, file_id)
        except (IndexError, ValueError, KeyError) as exc:
            log.warning("Download callback error: %s", exc)
            bot.send_message(cid, "❌ Not found.")
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


# ──────────────────────────── FILE UPLOADS ──────────────────────

@bot.message_handler(
    content_types=["document"],
    func=lambda m: _state(m.from_user.id) == "UNLOCKED",
)
def on_document(msg):
    doc = msg.document
    name = doc.file_name or f"file_{doc.file_id[:8]}"
    _add_record(name, doc.file_id, "document")
    bot.reply_to(msg, f"Saved: {name}", reply_markup=_menu_markup())


@bot.message_handler(
    content_types=["video"],
    func=lambda m: _state(m.from_user.id) == "UNLOCKED",
)
def on_video(msg):
    vid = msg.video
    name = vid.file_name or f"video_{vid.file_id[:8]}"
    _add_record(name, vid.file_id, "video")
    bot.reply_to(msg, f"Saved: {name}", reply_markup=_menu_markup())


# ──────────────────────────── TEXT HANDLER ──────────────────────

@bot.message_handler(
    content_types=["text"],
    func=lambda m: True,
)
def on_text(msg):
    uid = msg.from_user.id
    cid = msg.chat.id
    state = _state(uid)

    # ── LOCKED — password attempt ───────────────────────────────
    if state == "LOCKED":
        # Immediately remove the password message from chat
        try:
            bot.delete_message(cid, msg.message_id)
        except Exception:
            pass  # May lack permission in groups; silently continue

        if msg.text and msg.text.strip() == VAULT_PASSWORD:
            _set_state(uid, "UNLOCKED")
            bot.send_message(cid, "🟢 Open.", reply_markup=_menu_markup())
        else:
            bot.send_message(cid, "🔴 Incorrect.")
        return

    # ── AWAITING_SEARCH — keyword lookup ────────────────────────
    if state == "AWAITING_SEARCH":
        query = (msg.text or "").strip().lower()
        records = _load_db()
        matches = [
            r for r in records if query in r.get("name", "").lower()
        ]
        _set_state(uid, "UNLOCKED")

        if not matches:
            bot.send_message(
                cid, "❌ Not found.", reply_markup=_menu_markup()
            )
        else:
            # Build markup with original indices so download works
            mk = InlineKeyboardMarkup(row_width=1)
            for rec in matches:
                original_idx = records.index(rec)
                mk.add(
                    InlineKeyboardButton(
                        f"📦 {rec['name']}",
                        callback_data=f"dl_{original_idx}",
                    )
                )
            mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
            bot.send_message(cid, "🎮 Games:", reply_markup=mk)
        return

    # ── UNLOCKED but random text — silently ignore ──────────────


# ──────────────────────────── ENTRY POINT ───────────────────────

def main():
    # Validate critical env vars
    if not API_TOKEN:
        log.error("API_TOKEN environment variable is missing.")
        raise SystemExit(1)
    if not VAULT_PASSWORD:
        log.error("VAULT_PASSWORD environment variable is missing.")
        raise SystemExit(1)

    # Ensure DB file exists
    if not Path(DB_FILE).exists():
        _save_db([])

    # Start Flask in a daemon thread so Render sees an open port
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()
    log.info("Flask keep-alive started on port %s", PORT)

    # Start long-polling (auto-reconnect on failure)
    log.info("Bot polling started.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)


if __name__ == "__main__":
    main()
