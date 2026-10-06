import os
import logging
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()
users = {}
report_number = 0

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подать жалобу", callback_data="report")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="info")]
    ])

def esc(text):
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Blade Ball Anonymous Reports</b>\n\n"
        "Здесь можно анонимно сообщить о нарушении другого игрока.\n\n"
        "Нажми «📝 Подать жалобу».",
        parse_mode="HTML", reply_markup=main_menu()
    )

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Используй /setup в группе для жалоб.")
        return
    member = await update.effective_chat.get_member(update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text("❌ Только администратор может выполнить /setup.")
        return
    ADMIN_CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Группа для жалоб настроена!\n"
        "Теперь новые жалобы будут приходить сюда."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Жалоба отменена.", reply_markup=main_menu())

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "report":
        users[uid] = {"step":"username", "username":None, "description":None, "photo":None}
        await query.message.reply_text(
            "🎯 <b>Шаг 1/3</b>\n\nОтправь точный username игрока.",
            parse_mode="HTML"
        )
    elif query.data == "info":
        await query.message.reply_text(
            "ℹ️ 1. Username игрока\n2. Описание нарушения\n3. Скриншот по желанию\n"
            "4. Подтверждение\n5. Жалоба отправляется в закрытую админ-группу.\n\n"
            "Автор жалобы не публикуется.",
            parse_mode="HTML"
        )
    elif query.data == "skip_photo":
        await show_confirmation(query.message, uid)
    elif query.data == "send_report":
        await send_report(query.message, uid)
    elif query.data == "cancel_report":
        users.pop(uid, None)
        await query.message.reply_text("❌ Жалоба отменена.", reply_markup=main_menu())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in users:
        await update.message.reply_text("Нажми «📝 Подать жалобу», чтобы начать.", reply_markup=main_menu())
        return
    data = users[uid]
    text = update.message.text.strip()

    if data["step"] == "username":
        if len(text) > 100:
            await update.message.reply_text("❌ Username слишком длинный.")
            return
        data["username"] = text
        data["step"] = "description"
        await update.message.reply_text(
            "📝 <b>Шаг 2/3</b>\n\nОпиши подробно, что произошло.",
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
            "📸 <b>Шаг 3/3</b>\n\nОтправь скриншот или нажми «⏭ Пропустить».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_photo")]])
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in users or users[uid]["step"] != "photo":
        return
    users[uid]["photo"] = update.message.photo[-1].file_id
    await show_confirmation(update.message, uid)

async def show_confirmation(message, uid):
    data = users[uid]
    await message.reply_text(
        "🔎 <b>Проверь жалобу</b>\n\n"
        f"🎯 <b>Игрок:</b> <code>{esc(data['username'])}</code>\n\n"
        f"📝 <b>Описание:</b>\n{esc(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> {'Есть' if data['photo'] else 'Нет'}\n\n"
        "Всё верно?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="send_report"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_report")
        ]])
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
    num = report_number
    text = (
        f"🚨 <b>ЖАЛОБА #{num:04d}</b>\n\n"
        f"🎯 <b>Игрок:</b> <code>{esc(data['username'])}</code>\n\n"
        f"📝 <b>Описание:</b>\n{esc(data['description'])}\n\n"
        f"📸 <b>Доказательство:</b> {'Прикреплено' if data['photo'] else 'Нет'}"
    )
    try:
        if data["photo"]:
            await telegram_app.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=data["photo"], caption=text, parse_mode="HTML")
        else:
            await telegram_app.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode="HTML")
        users.pop(uid, None)
        await message.reply_text(
            f"✅ <b>Жалоба #{num:04d} отправлена.</b>\n\nСпасибо.",
            parse_mode="HTML", reply_markup=main_menu()
        )
    except Exception:
        logging.exception("Failed to send report")
        await message.reply_text("❌ Не удалось отправить жалобу. Проверь настройку группы.")

async def init_bot():
    await telegram_app.initialize()
    await telegram_app.start()
    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram")

@app.on_event("startup")
async def startup():
    await init_bot()

@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.post("/telegram")
async def telegram_webhook(request: Request):
    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def home():
    return {"status":"ok","bot":"Blade Ball Anonymous Reports"}

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("setup", setup))
telegram_app.add_handler(CommandHandler("cancel", cancel))
telegram_app.add_handler(CallbackQueryHandler(button))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
