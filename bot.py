import os
import logging
import asyncio
from urllib.parse import quote
import httpx
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "blade_ball_anon_bot").lstrip("@")

logging.basicConfig(level=logging.INFO)
app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()

users = {}
reports = {}          # report_id -> {reporter_chat_id, username, ...}
admin_reply_mode = {} # admin_user_id -> report_id
report_number = 0

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подать жалобу", callback_data="report")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="info")]
    ])

def esc(text):
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def bot_link():
    return f"https://t.me/{BOT_USERNAME}"

async def roblox_lookup(username: str):
    """Resolve a Roblox username to a real account. Returns (id, canonical_name) or None."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                return None
            return data[0]["id"], data[0]["name"]
    except Exception:
        logging.exception("Roblox lookup failed")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    uid = update.effective_user.id

    # Admin clicked "Ответить анонимно" from a report.
    if args and args[0].startswith("reply_"):
        report_id = args[0][6:]
        report = reports.get(report_id)
        if not report:
            await update.message.reply_text("❌ Эта жалоба больше недоступна. Возможно, бот перезапускался.")
            return
        if not ADMIN_CHAT_ID:
            await update.message.reply_text("❌ Группа администраторов не настроена.")
            return
        try:
            member = await telegram_app.bot.get_chat_member(int(ADMIN_CHAT_ID), uid)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Только администраторы могут отвечать на жалобы.")
                return
        except Exception:
            await update.message.reply_text("❌ Не удалось проверить права администратора.")
            return
        admin_reply_mode[uid] = report_id
        await update.message.reply_text(
            f"💬 Ответ на анонимную жалобу #{report_id}\n\n"
            "Напиши сообщение, которое нужно отправить автору.\n"
            "Автор не увидит твой Telegram username."
        )
        return

    await update.message.reply_text(
        "👋 <b>Blade Ball Anonymous Reports</b>\n\n"
        "Здесь можно анонимно сообщить о нарушении другого игрока.\n\n"
        "Нажми «📝 Подать жалобу».",
        parse_mode="HTML", reply_markup=main_menu()
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
    await update.message.reply_text("✅ Группа для жалоб настроена!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.pop(update.effective_user.id, None)
    admin_reply_mode.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Отменено.", reply_markup=main_menu())

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "report":
        users[uid] = {"step":"username", "username":None, "roblox_id":None, "description":None, "photo":None}
        await query.message.reply_text(
            "🎯 <b>Шаг 1/3</b>\n\nОтправь точный username игрока в Roblox.",
            parse_mode="HTML"
        )
    elif query.data == "info":
        await query.message.reply_text(
            "ℹ️ 1. Username Roblox\n2. Описание нарушения\n3. Скриншот по желанию\n"
            "4. Проверка username через Roblox\n5. Жалоба уходит в закрытую админ-группу.\n\n"
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

    # Anonymous reply from an admin to the original reporter.
    if uid in admin_reply_mode:
        report_id = admin_reply_mode.pop(uid)
        report = reports.get(report_id)
        if not report:
            await update.message.reply_text("❌ Жалоба больше недоступна.")
            return
        try:
            await telegram_app.bot.send_message(
                chat_id=report["reporter_chat_id"],
                text=f"💬 <b>Анонимный ответ по жалобе #{report_id}</b>\n\n{esc(update.message.text)}",
                parse_mode="HTML"
            )
            await update.message.reply_text("✅ Анонимный ответ отправлен.")
        except Exception:
            logging.exception("Failed to send anonymous reply")
            await update.message.reply_text("❌ Не удалось отправить ответ. Возможно, пользователь заблокировал бота.")
        return

    if uid not in users:
        await update.message.reply_text("Нажми «📝 Подать жалобу», чтобы начать.", reply_markup=main_menu())
        return

    data = users[uid]
    text = update.message.text.strip()

    if data["step"] == "username":
        if len(text) > 50:
            await update.message.reply_text("❌ Username слишком длинный.")
            return
        result = await roblox_lookup(text)
        if not result:
            await update.message.reply_text(
                "❌ Такой Roblox username не найден.\n\n"
                "Проверь написание и отправь username ещё раз."
            )
            return
        roblox_id, canonical_name = result
        data["username"] = canonical_name
        data["roblox_id"] = roblox_id
        data["step"] = "description"
        await update.message.reply_text(
            f"✅ Игрок найден: <b>{esc(canonical_name)}</b>\n"
            f"🔗 https://www.roblox.com/users/{roblox_id}/profile\n\n"
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

    if uid in admin_reply_mode:
        report_id = admin_reply_mode.pop(uid)
        report = reports.get(report_id)
        if not report:
            await update.message.reply_text("❌ Жалоба больше недоступна.")
            return
        try:
            await telegram_app.bot.send_photo(
                chat_id=report["reporter_chat_id"],
                photo=update.message.photo[-1].file_id,
                caption=f"💬 Анонимный ответ по жалобе #{report_id}"
            )
            await update.message.reply_text("✅ Анонимный ответ отправлен.")
        except Exception:
            logging.exception("Failed to send anonymous photo reply")
            await update.message.reply_text("❌ Не удалось отправить ответ.")
        return

    if uid not in users or users[uid]["step"] != "photo":
        return
    users[uid]["photo"] = update.message.photo[-1].file_id
    await show_confirmation(update.message, uid)

async def show_confirmation(message, uid):
    data = users[uid]
    profile = f"https://www.roblox.com/users/{data['roblox_id']}/profile"
    await message.reply_text(
        "🔎 <b>Проверь жалобу</b>\n\n"
        f"🎯 <b>Игрок:</b> <code>{esc(data['username'])}</code>\n"
        f"🔗 <a href=\"{profile}\">Профиль Roblox</a>\n\n"
        f"📝 <b>Описание:</b>\n{esc(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> {'Есть' if data['photo'] else 'Нет'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="send_report"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_report")
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
    report_id = f"{report_number:04d}"
    reports[report_id] = {
        "reporter_chat_id": uid,
        "username": data["username"],
        "roblox_id": data["roblox_id"],
    }
    profile = f"https://www.roblox.com/users/{data['roblox_id']}/profile"

    header = (
        f"💬 <b>У тебя новое анонимное сообщение!</b>\n"
        f"✍️ <a href=\"{bot_link()}\">Написать анонимно — @{esc(BOT_USERNAME)}</a>\n\n"
        f"🚨 <b>Жалоба #{report_id}</b>\n"
        f"🎯 <b>Игрок:</b> <a href=\"{profile}\">{esc(data['username'])}</a>\n"
        f"🔗 <a href=\"{profile}\">Открыть профиль Roblox</a>\n\n"
        f"📝 <b>Описание:</b>\n{esc(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> {'Есть' if data['photo'] else 'Нет'}"
    )

    reply_url = f"{bot_link()}?start=reply_{quote(report_id)}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить анонимно", url=reply_url)]
    ])

    try:
        if data["photo"]:
            await telegram_app.bot.send_photo(
                chat_id=int(ADMIN_CHAT_ID),
                photo=data["photo"],
                caption=header,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await telegram_app.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=header,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=False
            )

        users.pop(uid, None)
        await message.reply_text(
            f"✅ <b>Жалоба #{report_id} отправлена.</b>\n\n"
            "Администратор может ответить тебе анонимно.",
            parse_mode="HTML", reply_markup=main_menu()
        )
    except Exception:
        logging.exception("Failed to send report")
        reports.pop(report_id, None)
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
