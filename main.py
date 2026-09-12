import telebot
from telebot import types
import json
import os
from flask import Flask
from threading import Thread

# ================== НАСТРОЙКИ СЕЙФА ==================
API_TOKEN = os.environ.get('API_TOKEN')
VAULT_PASSWORD = os.environ.get('VAULT_PASSWORD')
# =====================================================

bot = telebot.TeleBot(API_TOKEN)
DB_FILE = "games_vault_db.json"
user_states = {}

if os.path.exists(DB_FILE):
    with open(DB_FILE, "r", encoding="utf-8") as f:
        games_database = json.load(f)
else:
    games_database = {}

def get_main_menu_keyboard():
    # Изменено избирательное скрытие: убираем селекторы для принудительного обновления интерфейса
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, row_width=2)
    btn_list = types.KeyboardButton("All Games")
    btn_search = types.KeyboardButton("Search")
    btn_lock = types.KeyboardButton("Lock")
    markup.add(btn_list, btn_search, btn_lock)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    uid = message.from_user.id
    user_states[uid] = "LOCKED"
    bot.send_message(
        message.chat.id, 
        "🔒 Password:", 
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_text(message):
    uid = message.from_user.id
    text = message.text

    # Если сейф заблокирован — проверяем пароль и удаляем сообщение
    if user_states.get(uid, "LOCKED") == "LOCKED":
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        if text == VAULT_PASSWORD:
            user_states[uid] = "UNLOCKED"
            # Принудительно шлем клавиатуру, чтобы она открылась внизу
            bot.send_message(
                message.chat.id, 
                "🟢 Open.", 
                reply_markup=get_main_menu_keyboard()
            )
        else:
            bot.send_message(message.chat.id, "🔴 Incorrect.")
        return

    # Если сейф открыт — обрабатываем команды меню
    if text == "Lock":
        user_states[uid] = "LOCKED"
        bot.send_message(
            message.chat.id, 
            "🔒 Password:", 
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    elif text == "All Games":
        if not games_database:
            bot.send_message(message.chat.id, "Empty.", reply_markup=get_main_menu_keyboard())
            return
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        for f_id, f_name in games_database.items():
            markup.add(types.InlineKeyboardButton(text=f"📦 {f_name}", callback_data=f_id))
        bot.send_message(message.chat.id, "🎮 Games:", reply_markup=markup)

    elif text == "Search":
        user_states[uid] = "AWAITING_SEARCH"
        bot.send_message(message.chat.id, "🔍 Enter name:")

    # Логика поиска (Ctrl+F)
    elif user_states.get(uid) == "AWAITING_SEARCH":
        user_states[uid] = "UNLOCKED"
        query = text.lower()
        results = {f_id: f_name for f_id, f_name in games_database.items() if query in f_name.lower()}
        
        if not results:
            bot.send_message(message.chat.id, "❌ Not found.", reply_markup=get_main_menu_keyboard())
            return
            
        markup = types.InlineKeyboardMarkup(row_width=1)
        for f_id, f_name in results.items():
            markup.add(types.InlineKeyboardButton(text=f"📦 {f_name}", callback_data=f_id))
            
        bot.send_message(message.chat.id, "Results:", reply_markup=markup)

# Сохранение игр (работает только в разблокированном состоянии)
@bot.message_handler(content_types=['document', 'video'])
def save_game_file(message):
    uid = message.from_user.id
    if user_states.get(uid, "LOCKED") != "UNLOCKED":
        return

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "Untitled"
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "Video"
    else:
        return

    games_database[file_id] = file_name
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(games_database, f, ensure_ascii=False, indent=4)

    bot.send_message(message.chat.id, f"Saved: {file_name}", reply_markup=get_main_menu_keyboard())

@bot.callback_query_handler(func=lambda call: True)
def handle_download(call):
    uid = call.from_user.id
    if user_states.get(uid, "LOCKED") != "UNLOCKED":
        bot.answer_callback_query(call.id, "Locked.", show_alert=True)
        return

    file_id = call.data
    if file_id in games_database:
        try:
            bot.answer_callback_query(call.id, "Sending...")
            bot.send_document(call.message.chat.id, file_id)
        except Exception as e:
            bot.answer_callback_query(call.id, "Error.", show_alert=True)

# === ФОНОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER.COM ===
app = Flask('')

@app.route('/')
def home():
    return "Status: OK"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.start()

if __name__ == "__main__":
    keep_alive()
    bot.infinity_polling()
