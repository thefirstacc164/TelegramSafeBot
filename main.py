import telebot
from telebot import types
import json
import os
from flask import Flask
from threading import Thread

# ================== НАСТРОЙКИ СЕЙФА ==================
API_TOKEN = os.environ.get('API_TOKEN')  # Убедитесь, что вы перевыпустили его в BotFather!
VAULT_PASSWORD = "qwer4321QWER$#@!qwer4321QWER$#@!"      # СЮДА НАПИШИТЕ ВАШ ПАРОЛЬ
# =====================================================

bot = telebot.TeleBot(API_TOKEN)
DB_FILE = "games_vault_db.json"
user_states = {}

# Загрузка базы данных файлов игр
if os.path.exists(DB_FILE):
    with open(DB_FILE, "r", encoding="utf-8") as f:
        games_database = json.load(f)
else:
    games_database = {}

def get_main_menu_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_list = types.KeyboardButton("📂 Список всех игр")
    btn_search = types.KeyboardButton("🔍 Поиск по названию")
    btn_lock = types.KeyboardButton("🔒 Заблокировать сейф")
    markup.add(btn_list, btn_search, btn_lock)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    uid = message.from_user.id
    user_states[uid] = "LOCKED"
    bot.send_message(
        message.chat.id, 
        "🔒 **Сейф заблокирован.**\nПожалуйста, введите пароль для доступа к играм:", 
        parse_mode='Markdown',
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_text(message):
    uid = message.from_user.id
    text = message.text

    if user_states.get(uid, "LOCKED") == "LOCKED":
        if text == VAULT_PASSWORD:
            user_states[uid] = "UNLOCKED"
            bot.send_message(
                message.chat.id, 
                "✅ **Доступ разрешен!** Сейф открыт.", 
                parse_mode='Markdown', 
                reply_markup=get_main_menu_keyboard()
            )
        else:
            bot.send_message(message.chat.id, "❌ Неверный пароль. Попробуйте еще раз:")
        return

    if text == "🔒 Заблокировать сейф":
        user_states[uid] = "LOCKED"
        bot.send_message(
            message.chat.id, 
            "🔒 Сейф успешно заблокирован. Кнопки скрыты.", 
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    elif text == "📂 Список всех игр":
        if not games_database:
            bot.send_message(message.chat.id, "📭 В сейфе пока нет игр. Просто перешлите мне файлы игр прямо сюда.")
            return
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        for f_id, f_name in games_database.items():
            markup.add(types.InlineKeyboardButton(text=f"🎮 {f_name}", callback_data=f_id))
        bot.send_message(message.chat.id, "👇 Выберите игру для скачивания:", reply_markup=markup)

    elif text == "🔍 Поиск по названию":
        user_states[uid] = "AWAITING_SEARCH"
        bot.send_message(message.chat.id, "⌨️ Введите название игры или его часть (как Ctrl+F):")

    elif user_states.get(uid) == "AWAITING_SEARCH":
        user_states[uid] = "UNLOCKED"
        query = text.lower()
        results = {f_id: f_name for f_id, f_name in games_database.items() if query in f_name.lower()}
        
        if not results:
            bot.send_message(message.chat.id, "🤷‍♂️ Ничего не найдено по этому запросу.", reply_markup=get_main_menu_keyboard())
            return
            
        markup = types.InlineKeyboardMarkup(row_width=1)
        for f_id, f_name in results.items():
            markup.add(types.InlineKeyboardButton(text=f"🎮 {f_name}", callback_data=f_id))
            
        bot.send_message(message.chat.id, f"🔍 Найдено совпадений: {len(results)}", reply_markup=markup)
        bot.send_message(message.chat.id, "Вы можете продолжить управление:", reply_markup=get_main_menu_keyboard())

@bot.message_handler(content_types=['document', 'video'])
def save_game_file(message):
    uid = message.from_user.id
    if user_states.get(uid, "LOCKED") != "UNLOCKED":
        bot.send_message(message.chat.id, "🔒 Сначала разблокируйте сейф паролем.")
        return

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "Без названия"
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "Видео-файл"
    else:
        return

    games_database[file_id] = file_name
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(games_database, f, ensure_ascii=False, indent=4)

    bot.send_message(message.chat.id, f"✅ Игра «{file_name}» добавлена в вашу базу данных!")

@bot.callback_query_handler(func=lambda call: True)
def handle_download(call):
    uid = call.from_user.id
    if user_states.get(uid, "LOCKED") != "UNLOCKED":
        bot.answer_callback_query(call.id, "🔒 Сейф заблокирован! Введите пароль.", show_alert=True)
        return

    file_id = call.data
    if file_id in games_database:
        try:
            bot.answer_callback_query(call.id, "Началась отправка файла...")
            bot.send_document(call.message.chat.id, file_id)
        except Exception as e:
            bot.answer_callback_query(call.id, "Ошибка отправки.", show_alert=True)

# === ЗАГЛУШКА ДЛЯ RENDER.COM СЛУШАЕТ ПОРТ ===
app = Flask('')

@app.route('/')
def home():
    return "Vault Bot is running 24/7!"

def run_web_server():
    # Render автоматически передает нужный порт в переменную среды PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.start()

if __name__ == "__main__":
    keep_alive()  # Запуск веб-сервера в фоновом потоке для Render
    print("Бот успешно запущен и слушает запросы...")
    bot.infinity_polling()
