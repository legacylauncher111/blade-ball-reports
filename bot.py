import os
import logging
import httpx
from html import escape

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
BOT_USERNAME = "BladeBallAnonymousBot"

logging.basicConfig(level=logging.INFO)
app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()

ADMIN_CHAT_ID = None
users = {}
reports = {}
reply_sessions = {}
report_number = 0


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подать жалобу", callback_data="report")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="info")],
    ])


async def roblox_user(username):
    username = username.strip().lstrip("@")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://users.roblox.com/v1/usernames/users",
                json={
                    "usernames": [username],
                    "excludeBannedUsers": False,
                },
                headers={"User-Agent": "BladeBallAnonymousBot/1.0"},
            )
            r.raise_for_status()
            data = r.json().get("data", [])

        if not data:
            return None

        u = data[0]
        return {
            "id": int(u["id"]),
            "name": u["name"],
            "profile": f"https://www.roblox.com/users/{u['id']}/profile",
        }
    except Exception:
        logging.exception("Roblox API error")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Ссылка «Ответить анонимно» из сообщения жалобы.
    if context.args and context.args[0].startswith("reply_"):
        try:
            rid = int(context.args[0].split("_", 1)[1])
        except ValueError:
            await update.message.reply_text("❌ Неверная ссылка.")
            return

        if rid not in reports:
            await update.message.reply_text(
                "❌ Эта жалоба больше недоступна."
            )
            return

        reply_sessions[uid] = rid

        await update.message.reply_text(
            f"💬 <b>Ответ на жалобу #{rid:04d}</b>\n\n"
            "Просто напиши здесь свой ответ.\n"
            "Он будет отправлен в группу анонимно как ответ на эту жалобу.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "👋 <b>Blade Ball Anonymous Reports</b>\n\n"
        "Здесь можно анонимно сообщить о нарушении игрока.",
        parse_mode="HTML",
        reply_markup=menu(),
    )


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("❌ Используй /setup в группе.")
        return

    member = await update.effective_chat.get_member(update.effective_user.id)
    if member.status not in ("administrator", "creator"):
        await update.message.reply_text(
            "❌ Только администратор может выполнить /setup."
        )
        return

    ADMIN_CHAT_ID = str(update.effective_chat.id)

    await update.message.reply_text(
        "✅ Группа для жалоб настроена."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.pop(update.effective_user.id, None)
    reply_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "❌ Отменено.",
        reply_markup=menu(),
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "report":
        users[uid] = {
            "step": "username",
            "roblox": None,
            "description": None,
            "photo": None,
        }
        await q.message.reply_text(
            "🎯 <b>Шаг 1/3</b>\n\n"
            "Отправь точный username игрока Roblox.",
            parse_mode="HTML",
        )

    elif q.data == "info":
        await q.message.reply_text(
            "ℹ️ <b>Как работает бот</b>\n\n"
            "1. Указываешь username.\n"
            "2. Описываешь нарушение.\n"
            "3. Прикрепляешь скриншот по желанию.\n"
            "4. Подтверждаешь жалобу.\n"
            "5. Жалоба появляется в админской группе анонимно.",
            parse_mode="HTML",
        )

    elif q.data == "skip_photo":
        await confirmation(q.message, uid)

    elif q.data == "send_report":
        await send_report(q.message, uid)

    elif q.data == "cancel_report":
        users.pop(uid, None)
        await q.message.reply_text(
            "❌ Жалоба отменена.",
            reply_markup=menu(),
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # Ответ на существующую жалобу.
    # Ответ отправляется В ГРУППУ reply_to_message_id,
    # поэтому Telegram показывает его именно как ответ на жалобу.
    if uid in reply_sessions:
        rid = reply_sessions.pop(uid)
        report = reports.get(rid)

        if not report or not ADMIN_CHAT_ID:
            await update.message.reply_text(
                "❌ Не удалось найти эту жалобу."
            )
            return

        try:
            await telegram_app.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=(
                    "💬 <b>Анонимный ответ на жалобу</b>\n\n"
                    f"{escape(text)}"
                ),
                parse_mode="HTML",
                reply_to_message_id=report["message_id"],
                allow_sending_without_reply=True,
            )

            await update.message.reply_text(
                "✅ Ответ отправлен анонимно."
            )
        except Exception:
            logging.exception("Failed to send anonymous reply")
            await update.message.reply_text(
                "❌ Не удалось отправить ответ."
            )
        return

    if uid not in users:
        await update.message.reply_text(
            "Нажми «📝 Подать жалобу», чтобы начать.",
            reply_markup=menu(),
        )
        return

    data = users[uid]

    if data["step"] == "username":
        username = text.lstrip("@")

        if len(username) > 20:
            await update.message.reply_text(
                "❌ Username слишком длинный."
            )
            return

        await update.message.reply_text("🔎 Проверяю username Roblox...")

        roblox = await roblox_user(username)

        if not roblox:
            await update.message.reply_text(
                "❌ Такой Roblox username не найден.\n\n"
                "Проверь написание и отправь ещё раз."
            )
            return

        data["roblox"] = roblox
        data["step"] = "description"

        await update.message.reply_text(
            "✅ Игрок найден:\n"
            f"🎯 <a href=\"{roblox['profile']}\">{escape(roblox['name'])}</a>\n\n"
            "📝 <b>Шаг 2/3</b>\n\n"
            "Опиши подробно, что произошло.",
            parse_mode="HTML",
        )

    elif data["step"] == "description":
        if len(text) < 5:
            await update.message.reply_text(
                "❌ Опиши проблему подробнее."
            )
            return

        if len(text) > 4000:
            await update.message.reply_text(
                "❌ Максимум 4000 символов."
            )
            return

        data["description"] = text
        data["step"] = "photo"

        await update.message.reply_text(
            "📸 <b>Шаг 3/3</b>\n\n"
            "Отправь скриншот или нажми «⏭ Пропустить».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "⏭ Пропустить",
                    callback_data="skip_photo"
                )]
            ]),
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in reply_sessions:
        rid = reply_sessions.pop(uid)
        report = reports.get(rid)

        if report and ADMIN_CHAT_ID:
            try:
                await telegram_app.bot.send_photo(
                    chat_id=int(ADMIN_CHAT_ID),
                    photo=update.message.photo[-1].file_id,
                    caption="💬 <b>Анонимный ответ на жалобу</b>",
                    parse_mode="HTML",
                    reply_to_message_id=report["message_id"],
                    allow_sending_without_reply=True,
                )
                await update.message.reply_text(
                    "✅ Ответ отправлен анонимно."
                )
            except Exception:
                logging.exception("Failed to send photo reply")
                await update.message.reply_text(
                    "❌ Не удалось отправить ответ."
                )
        return

    if uid not in users or users[uid]["step"] != "photo":
        return

    users[uid]["photo"] = update.message.photo[-1].file_id
    await confirmation(update.message, uid)


async def confirmation(message, uid):
    data = users[uid]
    roblox = data["roblox"]

    await message.reply_text(
        "🔎 <b>Проверь жалобу</b>\n\n"
        f"🎯 <b>Игрок:</b> "
        f"<a href=\"{roblox['profile']}\">{escape(roblox['name'])}</a>\n\n"
        f"📝 <b>Проблема:</b>\n{escape(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> "
        f"{'Прикреплён' if data['photo'] else 'Нет'}\n\n"
        "Всё верно?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить", callback_data="send_report"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_report"),
            ]
        ]),
    )


async def send_report(message, uid):
    global report_number

    if uid not in users:
        return

    if not ADMIN_CHAT_ID:
        await message.reply_text(
            "⚠️ Группа для жалоб ещё не настроена."
        )
        return

    data = users[uid]
    report_number += 1
    rid = report_number
    roblox = data["roblox"]

    report_text = (
        "💬 <b>У тебя новое анонимное сообщение!</b>\n\n"
        f"🚨 <b>Жалоба #{rid:04d}</b>\n\n"
        f"🎯 <b>Игрок:</b> "
        f"<a href=\"{roblox['profile']}\">{escape(roblox['name'])}</a>\n\n"
        f"📝 <b>Проблема:</b>\n{escape(data['description'])}\n\n"
        f"📸 <b>Скриншот:</b> "
        f"{'Прикреплён' if data['photo'] else 'Нет'}"
    )

    # ВАЖНО: URL-кнопка. Нажатие открывает личку бота,
    # а в группу ничего не отправляется.
    reply_url = f"https://t.me/{BOT_USERNAME}?start=reply_{rid}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💬 Ответить анонимно",
            url=reply_url
        )]
    ])

    try:
        if data["photo"]:
            sent = await telegram_app.bot.send_photo(
                chat_id=int(ADMIN_CHAT_ID),
                photo=data["photo"],
                caption=report_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            sent = await telegram_app.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=report_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        # Сохраняем ID именно сообщения жалобы.
        # Поэтому будущий ответ можно отправить Telegram Reply на него.
        reports[rid] = {
            "reporter_id": uid,
            "message_id": sent.message_id,
        }

        users.pop(uid, None)

        await message.reply_text(
            f"✅ <b>Жалоба #{rid:04d} отправлена.</b>",
            parse_mode="HTML",
            reply_markup=menu(),
        )
    except Exception:
        logging.exception("Failed to send report")
        await message.reply_text(
            "❌ Не удалось отправить жалобу."
        )


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/telegram"
        )


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
telegram_app.add_handler(CallbackQueryHandler(buttons))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
)
