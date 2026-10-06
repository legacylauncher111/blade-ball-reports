import os
import logging
from html import escape
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
BOT_USERNAME = "BladeBallAnonymousBot"

logging.basicConfig(level=logging.INFO)
app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()

users = {}
report_number = 0
pending_replies = {}

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подать жалобу", callback_data="report")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="info")]
    ])

def roblox_profile(username):
    return f"https://www.roblox.com/users/profile?username={username}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Blade Ball Anonymous Reports</b>\n\n"
        "Здесь можно анонимно сообщить о нарушении другого игрока.\n\n"
        "Нажми «📝 Подать жалобу».",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Используй /setup в группе.")
        return

    member = await update.effective_chat.get_member(update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("❌ Только администратор может выполнить /setup.")
        return

    ADMIN_CHAT_ID = str(update.effective_chat.id)

    await update.message.reply_text(
        "✅ <b>Группа для жалоб настроена!</b>\n\n"
        "Теперь новые жалобы будут приходить сюда.",
        parse_mode="HTML"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Жалоба отменена.", reply_markup=main_menu())

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "report":
        users[uid] = {
            "step": "username",
            "username": None,
            "description": None,
            "photo": None
        }
        await query.message.reply_text(
            "🎯 <b>Шаг 1/3</b>\n\n"
            "Отправь точный username игрока Roblox.",
            parse_mode="HTML"
        )

    elif query.data == "info":
        await query.message.reply_text(
            "ℹ️ <b>Как работает бот</b>\n\n"
            "1. Указываешь username игрока.\n"
            "2. Описываешь нарушение.\n"
            "3. Добавляешь скриншот по желанию.\n"
            "4. Проверяешь жалобу.\n"
            "5. Она отправляется администраторам анонимно.",
            parse_mode="HTML"
        )

    elif query.data == "skip_photo":
        await show_confirmation(query.message, uid)

    elif query.data == "send_report":
        await send_report(query.message, uid)

    elif query.data == "cancel_report":
        users.pop(uid, None)
        await query.message.reply_text("❌ Жалоба отменена.", reply_markup=main_menu())

    elif query.data.startswith("reply:"):
        try:
            author_id = int(query.data.split(":")[1])
            pending_replies[uid] = author_id
            await query.message.reply_text(
                "💬 Напиши ответ. Он будет отправлен автору жалобы анонимно."
            )
        except ValueError:
            await query.message.reply_text("❌ Не удалось открыть анонимный ответ.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # Анонимный ответ администратора
    if uid in pending_replies:
        author_id = pending_replies.pop(uid)
        try:
            await telegram_app.bot.send_message(
                chat_id=author_id,
                text=(
                    "💬 <b>У тебя новое анонимное сообщение!</b>\n\n"
                    f"{escape(text)}\n\n"
                    f"✍️ <b>Написать анонимно</b> — @{BOT_USERNAME}"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "💬 Ответить анонимно",
                        callback_data=f"anonreply:{uid}"
                    )]
                ])
            )
            await update.message.reply_text("✅ Анонимный ответ отправлен.")
        except Exception:
            await update.message.reply_text("❌ Не удалось отправить ответ.")
        return

    if uid not in users:
        await update.message.reply_text(
            "Нажми «📝 Подать жалобу», чтобы начать.",
            reply_markup=main_menu()
        )
        return

    data = users[uid]

    if data["step"] == "username":
        if len(text) > 100:
            await update.message.reply_text("❌ Username слишком длинный.")
            return

        data["username"] = text
        data["step"] = "description"

        await update.message.reply_text(
            "📝 <b>Шаг 2/3</b>\n\n"
            "Опиши подробно, что произошло.",
            parse_mode="HTML"
        )

    elif data["step"] == "description":
        if len(text) < 5:
            await update.message.reply_text("❌ Опиши проблему подробнее.")
            return

        if len(text) > 4000:
            await update.message.reply_text("❌ Максимум 4000 символов.")
            return

        data["description"] = text
        data["step"] = "photo"

        await update.message.reply_text(
            "📸 <b>Шаг 3/3</b>\n\n"
            "Отправь скриншот или нажми «⏭ Пропустить».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_photo")]
            ])
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in users or users[uid]["step"] != "photo":
        return

    users[uid]["photo"] = update.message.photo[-1].file_id
    await show_confirmation(update.message, uid)

async def show_confirmation(message, uid):
    data = users[uid]
    username = escape(data["username"])
    description = escape(data["description"])

    await message.reply_text(
        "🔎 <b>Проверь жалобу</b>\n\n"
        f"🎯 <b>Игрок:</b> <a href=\"{roblox_profile(data['username'])}\">{username}</a>\n\n"
        f"📝 <b>Описание:</b>\n{description}\n\n"
        f"📸 <b>Скриншот:</b> {'Есть' if data['photo'] else 'Нет'}\n\n"
        "Всё верно?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить", callback_data="send_report"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_report")
            ]
        ])
    )

async def send_report(message, uid):
    global report_number

    if uid not in users:
        return

    if not ADMIN_CHAT_ID:
        await message.reply_text("⚠️ Группа для жалоб ещё не настроена.")
        return

    data = users[uid]
    report_number += 1
    number = report_number

    report_text = (
        f"💬 <b>У тебя новое анонимное сообщение!</b>\n\n"
        f"🚨 <b>Жалоба #{number:04d}</b>\n\n"
        f"🎯 <b>Игрок:</b> "
        f"<a href=\"{roblox_profile(data['username'])}\">{escape(data['username'])}</a>\n\n"
        f"📝 <b>Проблема:</b>\n{escape(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> {'Прикреплён' if data['photo'] else 'Нет'}\n\n"
        f"✍️ <b>Написать анонимно</b> — @{BOT_USERNAME}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💬 Ответить анонимно",
            callback_data=f"reply:{uid}"
        )]
    ])

    try:
        if data["photo"]:
            await telegram_app.bot.send_photo(
                chat_id=int(ADMIN_CHAT_ID),
                photo=data["photo"],
                caption=report_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await telegram_app.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=report_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

        users.pop(uid, None)

        await message.reply_text(
            f"✅ <b>Жалоба #{number:04d} отправлена.</b>\n\n"
            "Спасибо. Администраторы рассмотрят её.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

    except Exception:
        logging.exception("Failed to send report")
        await message.reply_text(
            "❌ Не удалось отправить жалобу. Попробуй ещё раз."
        )

async def handle_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("anonreply:"):
        return

    # Ответ на сообщение пользователя из сообщения пользователя
    # Здесь Telegram не позволяет определить исходного пользователя
    # только по кнопке без сохранённой сессии, поэтому используем
    # pending reply только для ответов администраторов.
    await query.message.reply_text(
        "ℹ️ Чтобы ответить анонимно, отправь новое сообщение через бота."
    )

async def init_bot():
    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/telegram"
        )

@app.on_event("startup")
async def startup():
    await init_bot()

@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def home():
    return {"status": "ok", "bot": BOT_USERNAME}

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("setup", setup))
telegram_app.add_handler(CommandHandler("cancel", cancel))
telegram_app.add_handler(CallbackQueryHandler(button))
telegram_app.add_handler(CallbackQueryHandler(handle_reply_button))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
