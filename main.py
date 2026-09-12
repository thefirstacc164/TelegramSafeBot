#!/usr/bin/env python3
"""
Vault Bot — Discrete cloud storage via Telegram file_id linking.
Features: Auto-harvesting, manual ID adding, persistent chat wiping, and 24h auto-delete.
"""

import os
import json
import time
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
MSG_LOG_FILE = "message_log.json"
SOURCE_GROUP_ID = os.environ.get("SOURCE_GROUP_ID", "").strip()

# ──────────────────────────── LOGGING ───────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vault")

# ──────────────────────────── FLASK KEEP-ALIVE ──────────────────

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200

def _run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ──────────────────────────── BOT INIT ──────────────────────────

bot = telebot.TeleBot(API_TOKEN, parse_mode=None)

# State management
user_states: dict[int, str] = {}
last_activity: dict[int, float] = {}
temp_add_data: dict[int, str] = {}  # Temporarily holds the filename while waiting for ID

# ──────────────────────────── DATA MANAGERS ─────────────────────

def _load_json(filepath: str, default):
    path = Path(filepath)
    if not path.exists(): return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(filepath: str, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_db() -> list[dict]:
    return _load_json(DB_FILE, [])

def _add_record(name: str, file_id: str, file_type: str) -> bool:
    records = _load_db()
    if any(r.get("file_id") == file_id for r in records):
        return False
    records.append({"name": name, "file_id": file_id, "type": file_type})
    _save_json(DB_FILE, records)
    return True

# Message Log Helpers
def _log_msg(chat_id: int, message_id: int):
    logs = _load_json(MSG_LOG_FILE, {})
    cid = str(chat_id)
    if cid not in logs: logs[cid] = []
    if message_id not in logs[cid]:
        logs[cid].append(message_id)
    _save_json(MSG_LOG_FILE, logs)

def _wipe_chat(chat_id: int) -> int:
    logs = _load_json(MSG_LOG_FILE, {})
    cid = str(chat_id)
    if cid not in logs: return 0
    
    deleted = 0
    for mid in logs[cid]:
        try:
            bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass 
    
    logs[cid] = []
    _save_json(MSG_LOG_FILE, logs)
    return deleted

def _track_send(method, chat_id, *args, **kwargs):
    try:
        m = method(chat_id, *args, **kwargs)
        if m and hasattr(m, "message_id"):
            _log_msg(chat_id, m.message_id)
        return m
    except Exception as e:
        log.warning("Send failed: %s", e)
        return None

# ──────────────────────────── STATE HELPERS ─────────────────────

def _state(uid: int) -> str:
    return user_states.get(uid, "LOCKED")

def _set_state(uid: int, state: str) -> None:
    user_states[uid] = state
    last_activity[uid] = time.time()

def _ping_activity(uid: int):
    last_activity[uid] = time.time()

def _is_private(msg) -> bool:
    return msg.chat.type == "private"

# ──────────────────────────── UI BUILDERS ───────────────────────

def _menu_markup() -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup(row_width=2)
    mk.add(
        InlineKeyboardButton("📂 All Games", callback_data="cb_all"),
        InlineKeyboardButton("🔍 Search", callback_data="cb_search"),
    )
    mk.add(
        InlineKeyboardButton("➕ Add ID", callback_data="cb_add"),
        InlineKeyboardButton("🔒 Lock & Wipe", callback_data="cb_lock")
    )
    return mk

def _files_markup(records: list[dict]) -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup(row_width=1)
    for idx, rec in enumerate(records):
        mk.add(InlineKeyboardButton(f"📦 {rec['name']}", callback_data=f"dl_{idx}"))
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
    return mk

def _back_markup() -> InlineKeyboardMarkup:
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
    return mk

# ──────────────────────────── COMMANDS ──────────────────────────

@bot.message_handler(commands=["start"], func=lambda m: _is_private(m))
def cmd_start(msg):
    uid = msg.from_user.id
    _log_msg(uid, msg.message_id)
    _set_state(uid, "LOCKED")
    _track_send(bot.send_message, uid, "🔒 Password:")

@bot.message_handler(commands=["delete", "wipe"], func=lambda m: _is_private(m))
def cmd_delete(msg):
    uid = msg.from_user.id
    _log_msg(uid, msg.message_id)
    _ping_activity(uid)
    deleted = _wipe_chat(uid)
    _track_send(bot.send_message, uid, f"🧹 {deleted}")

@bot.message_handler(commands=["chatid"])
def cmd_chatid(msg):
    bot.reply_to(msg, f"`{msg.chat.id}`", parse_mode="Markdown")

# ──────────────────────────── CALLBACK QUERIES ──────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    mid = call.message.message_id
    data = call.data

    _ping_activity(uid)

    if _state(uid) == "LOCKED":
        bot.answer_callback_query(call.id)
        return

    if data == "cb_all":
        records = _load_db()
        if not records:
            bot.edit_message_text("❌ Not found.", cid, mid, reply_markup=_back_markup())
        else:
            bot.edit_message_text(f"🎮 Games: {len(records)}", cid, mid, reply_markup=_files_markup(records))
        bot.answer_callback_query(call.id)
        return

    if data == "cb_search":
        _set_state(uid, "AWAITING_SEARCH")
        bot.edit_message_text("🔍 Enter name:", cid, mid, reply_markup=_back_markup())
        bot.answer_callback_query(call.id)
        return
    
    if data == "cb_add":
        _set_state(uid, "AWAITING_ADD_NAME")
        bot.edit_message_text("📝 Enter name:", cid, mid, reply_markup=_back_markup())
        bot.answer_callback_query(call.id)
        return

    if data == "cb_lock":
        _set_state(uid, "LOCKED")
        bot.answer_callback_query(call.id, "Locking and wiping...")
        _wipe_chat(cid)
        _track_send(bot.send_message, cid, "🔒 Password:")
        return

    if data == "cb_back":
        _set_state(uid, "UNLOCKED")
        temp_add_data.pop(uid, None) # Clear any pending add requests
        bot.edit_message_text("🟢 Open.", cid, mid, reply_markup=_menu_markup())
        bot.answer_callback_query(call.id)
        return

    if data.startswith("dl_"):
        try:
            idx = int(data[3:])
            rec = _load_db()[idx]
            file_id = rec["file_id"]
            if rec.get("type") == "video":
                _track_send(bot.send_video, cid, file_id)
            else:
                _track_send(bot.send_document, cid, file_id)
        except Exception:
            _track_send(bot.send_message, cid, "❌ Not found.")
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)

# ──────────────────────────── GROUP AUTO-HARVEST ────────────────

@bot.message_handler(
    content_types=["document", "video"],
    func=lambda m: m.chat.type in ("group", "supergroup") and (not SOURCE_GROUP_ID or str(m.chat.id) == SOURCE_GROUP_ID)
)
def on_group_file(msg):
    f = msg.document if msg.content_type == "document" else msg.video
    ftype = "document" if msg.content_type == "document" else "video"
    name = getattr(f, "file_name", None) or f"{ftype}_{f.file_id[:8]}"
    if _add_record(name, f.file_id, ftype):
        log.info("Harvested: %s", name)

# ──────────────────────────── DM FILE UPLOADS ───────────────────

@bot.message_handler(
    content_types=["document", "video"],
    func=lambda m: _is_private(m) and _state(m.from_user.id) == "UNLOCKED"
)
def on_dm_file(msg):
    uid = msg.from_user.id
    _log_msg(uid, msg.message_id)
    _ping_activity(uid)
    
    f = msg.document if msg.content_type == "document" else msg.video
    ftype = "document" if msg.content_type == "document" else "video"
    name = getattr(f, "file_name", None) or f"{ftype}_{f.file_id[:8]}"
    
    _add_record(name, f.file_id, ftype)
    _track_send(bot.send_message, uid, f"Saved: {name}", reply_markup=_menu_markup())

# ──────────────────────────── DM TEXT HANDLER ───────────────────

@bot.message_handler(content_types=["text"], func=lambda m: _is_private(m))
def on_text(msg):
    uid = msg.from_user.id
    cid = msg.chat.id
    state = _state(uid)

    if state == "LOCKED":
        try: bot.delete_message(cid, msg.message_id)
        except Exception: pass

        if msg.text and msg.text.strip() == VAULT_PASSWORD:
            _set_state(uid, "UNLOCKED")
            _track_send(bot.send_message, cid, "🟢 Open.", reply_markup=_menu_markup())
        else:
            _track_send(bot.send_message, cid, "🔴 Incorrect.")
        return

    _log_msg(cid, msg.message_id)
    _ping_activity(uid)

    if state == "AWAITING_SEARCH":
        query = (msg.text or "").strip().lower()
        records = _load_db()
        matches = [(i, r) for i, r in enumerate(records) if query in r.get("name", "").lower()]
        _set_state(uid, "UNLOCKED")

        if not matches:
            _track_send(bot.send_message, cid, "❌ Not found.", reply_markup=_menu_markup())
        else:
            mk = InlineKeyboardMarkup(row_width=1)
            for original_idx, rec in matches:
                mk.add(InlineKeyboardButton(f"📦 {rec['name']}", callback_data=f"dl_{original_idx}"))
            mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
            _track_send(bot.send_message, cid, f"🎮 Games: {len(matches)}", reply_markup=mk)
        return

    # ── AWAITING MANUAL FILE NAME ───────────────────────────────
    if state == "AWAITING_ADD_NAME":
        filename = (msg.text or "").strip()
        temp_add_data[uid] = filename
        _set_state(uid, "AWAITING_ADD_ID")
        _track_send(bot.send_message, cid, "🆔 Enter ID:", reply_markup=_back_markup())
        return

    # ── AWAITING MANUAL FILE ID ─────────────────────────────────
    if state == "AWAITING_ADD_ID":
        file_id = (msg.text or "").strip()
        filename = temp_add_data.pop(uid, "Unknown_File")
        
        _add_record(filename, file_id, "document")
        _set_state(uid, "UNLOCKED")
        _track_send(bot.send_message, cid, f"Saved: {filename}", reply_markup=_menu_markup())
        return

# ──────────────────────────── 24H AUTO-WIPE THREAD ──────────────

def _auto_delete_worker():
    while True:
        time.sleep(60)
        now = time.time()
        for uid, last_time in list(last_activity.items()):
            if now - last_time > 86400: # 24 hours
                if _state(uid) == "LOCKED":
                    log.info("24H inactivity reached. Wiping chat %s", uid)
                    _wipe_chat(uid)
                    del last_activity[uid] 
                else:
                    log.info("24H reached but %s is UNLOCKED. Extending timer 1 hour.", uid)
                    last_activity[uid] += 3600

# ──────────────────────────── ENTRY POINT ───────────────────────

def main():
    if not API_TOKEN or not VAULT_PASSWORD:
        log.error("API_TOKEN or VAULT_PASSWORD missing.")
        raise SystemExit(1)

    if not Path(DB_FILE).exists(): _save_json(DB_FILE, [])
    if not Path(MSG_LOG_FILE).exists(): _save_json(MSG_LOG_FILE, {})

    threading.Thread(target=_run_flask, daemon=True).start()
    threading.Thread(target=_auto_delete_worker, daemon=True).start()
    
    log.info("Bot polling started.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60, allowed_updates=[
        "message", "callback_query"
    ])

if __name__ == "__main__":
    main()
