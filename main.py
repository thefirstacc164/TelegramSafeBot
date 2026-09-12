#!/usr/bin/env python3
"""
Vault Bot — discrete Telegram file_id vault.
Persistent GitHub storage + aggressive chat wipe + native .zip sends.
"""

import os
import json
import time
import base64
import threading
import logging
from pathlib import Path

import requests
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ──────────────────────────── CONFIG ────────────────────────────

API_TOKEN       = os.environ.get("API_TOKEN", "")
VAULT_PASSWORD  = os.environ.get("VAULT_PASSWORD", "")
PORT            = int(os.environ.get("PORT", 8080))

GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "")          # e.g. "user/vault-db"
GITHUB_FILE     = os.environ.get("GITHUB_FILE", "games_vault_db.json")

DB_FILE         = "games_vault_db.json"
MSG_LOG_FILE    = "message_log.json"
SOURCE_GROUP_ID = os.environ.get("SOURCE_GROUP_ID", "").strip()

# ──────────────────────────── LOGGING ───────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vault")

# ──────────────────────────── FLASK ─────────────────────────────

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200

def _run_flask():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ──────────────────────────── BOT ───────────────────────────────

bot = telebot.TeleBot(API_TOKEN, parse_mode=None)

user_states: dict[int, str] = {}
last_activity: dict[int, float] = {}

# ──────────────────────────── JSON HELPERS ──────────────────────

def _load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ──────────────────────────── GITHUB PERSISTENCE ────────────────

def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _gh_get_sha_and_content():
    """Return (sha, content_dict) or (None, None)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return data["sha"], json.loads(content)
    except Exception as e:
        log.warning("GitHub pull failed: %s", e)
        return None, None

def _gh_push(records: list):
    """Create or update the DB file on GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    sha, _ = _gh_get_sha_and_content()
    payload = {
        "message": f"vault sync {int(time.time())}",
        "content": base64.b64encode(
            json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=20)
        if r.status_code in (200, 201):
            log.info("GitHub sync OK (%d games)", len(records))
        else:
            log.warning("GitHub push %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("GitHub push failed: %s", e)

def _pull_db_from_github():
    """On startup: if GitHub has data, use it."""
    sha, content = _gh_get_sha_and_content()
    if content and isinstance(content, list):
        _save_json(DB_FILE, content)
        log.info("Restored %d games from GitHub", len(content))
        return True
    return False

# ──────────────────────────── DB API ────────────────────────────

def _load_db() -> list:
    return _load_json(DB_FILE, [])

def _save_db(records: list):
    _save_json(DB_FILE, records)
    # Push to GitHub in background so replies stay fast
    threading.Thread(target=_gh_push, args=(records,), daemon=True).start()

def _add_record(name: str, file_id: str, file_type: str) -> bool:
    records = _load_db()
    if any(r.get("file_id") == file_id for r in records):
        return False
    records.append({"name": name, "file_id": file_id, "type": file_type})
    _save_db(records)
    return True

# ──────────────────────────── MESSAGE LOG + WIPE ────────────────

def _log_msg(chat_id: int, message_id: int):
    logs = _load_json(MSG_LOG_FILE, {})
    cid = str(chat_id)
    logs.setdefault(cid, [])
    if message_id not in logs[cid]:
        logs[cid].append(message_id)
        # keep log from growing forever
        if len(logs[cid]) > 500:
            logs[cid] = logs[cid][-400:]
    _save_json(MSG_LOG_FILE, logs)

def _track_send(method, chat_id, *args, **kwargs):
    # Never generate link previews
    if method == bot.send_message:
        kwargs.setdefault("disable_web_page_preview", True)
    try:
        m = method(chat_id, *args, **kwargs)
        if m and hasattr(m, "message_id"):
            _log_msg(chat_id, m.message_id)
        return m
    except Exception as e:
        log.warning("Send failed: %s", e)
        return None

def _aggressive_wipe(chat_id: int) -> int:
    """
    Delete everything possible:
    1. All tracked IDs
    2. Walk backward from the latest message ID until Telegram refuses
       (48-hour limit / already gone / rate limit).
    """
    deleted = 0

    # 1) tracked messages
    logs = _load_json(MSG_LOG_FILE, {})
    cid = str(chat_id)
    tracked = list(logs.get(cid, []))
    for mid in tracked:
        try:
            bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    logs[cid] = []
    _save_json(MSG_LOG_FILE, logs)

    # 2) probe current high-water mark
    try:
        probe = bot.send_message(chat_id, "·")
        max_id = probe.message_id
        try:
            bot.delete_message(chat_id, max_id)
            deleted += 1
        except Exception:
            pass
    except Exception:
        return deleted

    # 3) walk backwards (practical window ~300 msgs)
    consecutive_fail = 0
    for mid in range(max_id - 1, max(max_id - 350, 0), -1):
        try:
            bot.delete_message(chat_id, mid)
            deleted += 1
            consecutive_fail = 0
            time.sleep(0.03)  # gentle on rate limits
        except Exception:
            consecutive_fail += 1
            if consecutive_fail >= 20:
                break  # older than 48h or empty history
    return deleted

# ──────────────────────────── STATE ─────────────────────────────

def _state(uid: int) -> str:
    return user_states.get(uid, "LOCKED")

def _set_state(uid: int, state: str):
    user_states[uid] = state
    last_activity[uid] = time.time()

def _ping(uid: int):
    last_activity[uid] = time.time()

def _is_private(msg) -> bool:
    return msg.chat.type == "private"

# ──────────────────────────── UI ────────────────────────────────

def _menu_markup():
    mk = InlineKeyboardMarkup(row_width=2)
    mk.add(
        InlineKeyboardButton("📂 All Games", callback_data="cb_all"),
        InlineKeyboardButton("🔍 Search", callback_data="cb_search"),
    )
    mk.add(InlineKeyboardButton("🔒 Lock & Wipe", callback_data="cb_lock"))
    return mk

def _files_markup(records: list):
    mk = InlineKeyboardMarkup(row_width=1)
    for idx, rec in enumerate(records):
        mk.add(InlineKeyboardButton(f"📦 {rec['name']}", callback_data=f"dl_{idx}"))
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
    return mk

def _back_markup():
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
    _ping(uid)
    n = _aggressive_wipe(uid)
    # After wipe there is no history; send a fresh locked prompt
    _set_state(uid, "LOCKED")
    _track_send(bot.send_message, uid, f"🧹 {n}\n🔒 Password:")

@bot.message_handler(commands=["backup"], func=lambda m: _is_private(m))
def cmd_backup(msg):
    uid = msg.from_user.id
    if _state(uid) != "UNLOCKED":
        return
    _log_msg(uid, msg.message_id)
    if Path(DB_FILE).exists():
        with open(DB_FILE, "rb") as f:
            _track_send(bot.send_document, uid, f, caption="💾")

@bot.message_handler(commands=["chatid"])
def cmd_chatid(msg):
    bot.reply_to(msg, f"`{msg.chat.id}`", parse_mode="Markdown",
                 disable_web_page_preview=True)

# ──────────────────────────── CALLBACKS ─────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    mid = call.message.message_id
    data = call.data
    _ping(uid)

    if _state(uid) == "LOCKED":
        bot.answer_callback_query(call.id)
        return

    if data == "cb_all":
        records = _load_db()
        if not records:
            bot.edit_message_text("❌ Not found.", cid, mid, reply_markup=_back_markup())
        else:
            bot.edit_message_text(f"🎮 Games: {len(records)}", cid, mid,
                                  reply_markup=_files_markup(records))
        bot.answer_callback_query(call.id)
        return

    if data == "cb_search":
        _set_state(uid, "AWAITING_SEARCH")
        bot.edit_message_text("🔍 Enter name:", cid, mid, reply_markup=_back_markup())
        bot.answer_callback_query(call.id)
        return

    if data == "cb_lock":
        bot.answer_callback_query(call.id, "Wiping…")
        _set_state(uid, "LOCKED")
        n = _aggressive_wipe(cid)
        _track_send(bot.send_message, cid, f"🧹 {n}\n🔒 Password:")
        return

    if data == "cb_back":
        _set_state(uid, "UNLOCKED")
        bot.edit_message_text("🟢 Open.", cid, mid, reply_markup=_menu_markup())
        bot.answer_callback_query(call.id)
        return

    if data.startswith("dl_"):
        bot.answer_callback_query(call.id, "📥")
        try:
            idx = int(data[3:])
            rec = _load_db()[idx]
            # Native file bubble — no caption, no links
            if rec.get("type") == "video":
                _track_send(bot.send_video, cid, rec["file_id"])
            else:
                _track_send(bot.send_document, cid, rec["file_id"])
        except Exception:
            _track_send(bot.send_message, cid, "❌")
        return

    bot.answer_callback_query(call.id)

# ──────────────────────────── GROUP HARVEST ─────────────────────

@bot.message_handler(
    content_types=["document", "video"],
    func=lambda m: m.chat.type in ("group", "supergroup")
                   and (not SOURCE_GROUP_ID or str(m.chat.id) == SOURCE_GROUP_ID)
)
def on_group_file(msg):
    f = msg.document if msg.content_type == "document" else msg.video
    ftype = "document" if msg.content_type == "document" else "video"
    name = getattr(f, "file_name", None) or f"{ftype}_{f.file_id[:8]}"
    if _add_record(name, f.file_id, ftype):
        log.info("Harvested: %s", name)

# ──────────────────────────── DM FILES / FORWARDS ───────────────

@bot.message_handler(
    content_types=["document", "video"],
    func=lambda m: _is_private(m) and _state(m.from_user.id) == "UNLOCKED"
)
def on_dm_file(msg):
    uid = msg.from_user.id
    _log_msg(uid, msg.message_id)
    _ping(uid)

    f = msg.document if msg.content_type == "document" else msg.video
    ftype = "document" if msg.content_type == "document" else "video"
    name = getattr(f, "file_name", None) or f"{ftype}_{f.file_id[:8]}"

    # Restore from backup JSON
    if name.endswith(".json") and "vault" in name.lower():
        try:
            info = bot.get_file(f.file_id)
            raw = bot.download_file(info.file_path)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                _save_db(data)
                _track_send(bot.send_message, uid, f"✅ Restored {len(data)}",
                            reply_markup=_menu_markup())
                return
        except Exception as e:
            _track_send(bot.send_message, uid, "❌ Restore failed")
            return

    if _add_record(name, f.file_id, ftype):
        _track_send(bot.send_message, uid, f"✅ {name}")
    else:
        _track_send(bot.send_message, uid, f"⚠️ {name}")

# ──────────────────────────── DM TEXT ───────────────────────────

@bot.message_handler(content_types=["text"], func=lambda m: _is_private(m))
def on_text(msg):
    uid = msg.from_user.id
    cid = msg.chat.id
    state = _state(uid)

    if state == "LOCKED":
        try:
            bot.delete_message(cid, msg.message_id)
        except Exception:
            pass
        if msg.text and msg.text.strip() == VAULT_PASSWORD:
            _set_state(uid, "UNLOCKED")
            _track_send(bot.send_message, cid, "🟢 Open.", reply_markup=_menu_markup())
        else:
            _track_send(bot.send_message, cid, "🔴 Incorrect.")
        return

    _log_msg(cid, msg.message_id)
    _ping(uid)

    if state == "AWAITING_SEARCH":
        query = (msg.text or "").strip().lower()
        records = _load_db()
        matches = [(i, r) for i, r in enumerate(records)
                   if query in r.get("name", "").lower()]
        _set_state(uid, "UNLOCKED")
        if not matches:
            _track_send(bot.send_message, cid, "❌ Not found.", reply_markup=_menu_markup())
        else:
            mk = InlineKeyboardMarkup(row_width=1)
            for idx, rec in matches:
                mk.add(InlineKeyboardButton(f"📦 {rec['name']}", callback_data=f"dl_{idx}"))
            mk.add(InlineKeyboardButton("⬅️ Back", callback_data="cb_back"))
            _track_send(bot.send_message, cid, f"🎮 {len(matches)}", reply_markup=mk)

# ──────────────────────────── 24h AUTO-WIPE ─────────────────────

def _auto_delete_worker():
    while True:
        time.sleep(60)
        now = time.time()
        for uid, ts in list(last_activity.items()):
            if now - ts > 86400:
                if _state(uid) == "LOCKED":
                    log.info("24h wipe %s", uid)
                    _aggressive_wipe(uid)
                    last_activity.pop(uid, None)
                else:
                    last_activity[uid] = ts + 3600  # +1h grace if session open

# ──────────────────────────── MAIN ──────────────────────────────

def main():
    if not API_TOKEN or not VAULT_PASSWORD:
        log.error("API_TOKEN / VAULT_PASSWORD missing")
        raise SystemExit(1)

    # Boot: prefer GitHub copy if it exists
    if not _pull_db_from_github():
        if not Path(DB_FILE).exists():
            _save_json(DB_FILE, [])
    if not Path(MSG_LOG_FILE).exists():
        _save_json(MSG_LOG_FILE, {})

    threading.Thread(target=_run_flask, daemon=True).start()
    threading.Thread(target=_auto_delete_worker, daemon=True).start()

    log.info("Polling… GitHub repo=%s", GITHUB_REPO or "OFF")
    bot.infinity_polling(timeout=60, long_polling_timeout=60,
                         allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
