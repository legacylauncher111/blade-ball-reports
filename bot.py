import os
import re
import time
import hmac
import struct
import base64
import hashlib
import logging
import secrets
from html import escape
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    LinkPreviewOptions,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllChatAdministrators,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────── НАСТРОЙКИ ───────────────────────────

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")


def _chat_id(value):
    value = (value or "").strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else "@" + value


# ID группы модераторов и канала берутся из переменных окружения,
# чтобы НЕ слетали после перезапуска/сна Render.
ADMIN_CHAT_ID = _chat_id(os.environ.get("ADMIN_CHAT_ID"))
CHANNEL_ID = _chat_id(os.environ.get("CHANNEL_ID"))

SECRET = hashlib.sha256((os.environ.get("SECRET_KEY") or TOKEN).encode()).digest()
WEBHOOK_SECRET = hashlib.sha256(b"webhook:" + SECRET).hexdigest()[:48]

TITLE = "Blade Ball Anonymous"
MAX_TEXT = 800          # лимит описания (подпись к фото в Telegram — максимум 1024)
MIN_TEXT = 5
COOLDOWN = 30           # секунд между заявками от одного человека
SEP = "━━━━━━━━━━━━━━"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

tg = Application.builder().token(TOKEN).build()

sessions = {}       # uid -> состояние формы
last_sent = {}      # uid -> время последней заявки
processing = set()  # заявки, которые прямо сейчас модерируются
channel_cache = {}


# ─────────────────────── АНОНИМНОСТЬ ОТПРАВИТЕЛЯ ───────────────────────
# ID автора заявки зашифрован прямо в кнопке модерации.
# Админы его не видят, а бот после перезапуска всё равно
# сможет сообщить автору, одобрили его заявку или нет.

def seal_uid(uid: int) -> str:
    nonce = secrets.token_bytes(4)
    key = hmac.new(SECRET, nonce, hashlib.sha256).digest()[:8]
    raw = struct.pack(">q", uid)
    enc = bytes(a ^ b for a, b in zip(raw, key))
    return base64.urlsafe_b64encode(nonce + enc).decode().rstrip("=")


def open_uid(token: str):
    try:
        data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        nonce, enc = data[:4], data[4:12]
        key = hmac.new(SECRET, nonce, hashlib.sha256).digest()[:8]
        return struct.unpack(">q", bytes(a ^ b for a, b in zip(enc, key)))[0]
    except Exception:
        return None


# ─────────────────────────── КНОПКИ ───────────────────────────

def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(*rows):
    return InlineKeyboardMarkup([list(r) for r in rows])


CANCEL_ROW = [btn("❌ Отмена", "form:cancel")]


def main_menu():
    return kb(
        [btn("📝 Отправить жалобу", "form:report")],
        [btn("ℹ️ Как это работает", "form:info")],
    )


def deep_link(payload):
    return f"https://t.me/{tg.bot.username}?start={payload}"


def post_keyboard(post_id):
    # Кнопки под каждым постом в канале — сразу открывают бота с формой.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Отправить анонимно", url=deep_link("report"))],
        [InlineKeyboardButton("💬 Ответить анонимно", url=deep_link(f"reply_{post_id}"))],
    ])


# ─────────────────────────── ХЕЛПЕРЫ ───────────────────────────

async def say(chat_id, text, markup=None):
    return await tg.bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )


async def get_channel():
    if not CHANNEL_ID:
        return None
    if "chat" not in channel_cache:
        try:
            channel_cache["chat"] = await tg.bot.get_chat(CHANNEL_ID)
        except TelegramError:
            log.exception("Не удалось получить канал")
            return None
    return channel_cache["chat"]


def message_link(chat, msg_id):
    if chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    return f"https://t.me/c/{str(chat.id).removeprefix('-100')}/{msg_id}"


async def is_group_admin(update: Update) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    # Анонимный админ пишет «от имени группы».
    if msg.sender_chat and msg.sender_chat.id == chat.id:
        return True
    try:
        member = await chat.get_member(update.effective_user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def roblox_user(username):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False},
                headers={"User-Agent": "BladeBallAnonymousBot/2.0"},
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
        log.exception("Roblox API error")
        return "error"


def report_body(d):
    r = d["roblox"]
    return (
        "🚨 <b>Анонимная жалоба</b>\n\n"
        f"🎯 <b>Игрок:</b> <a href=\"{r['profile']}\">{escape(r['name'])}</a>\n\n"
        f"📝 <b>Что произошло:</b>\n{escape(d['text'])}"
    )


def reply_body(d):
    body = "💬 <b>Анонимный ответ</b>"
    if d["text"]:
        body += f"\n\n{escape(d['text'])}"
    return body


def body_of(d):
    return report_body(d) if d["kind"] == "report" else reply_body(d)


# ─────────────────────────── ФОРМА ───────────────────────────

async def start_report(uid):
    sessions[uid] = {
        "kind": "report", "step": "username",
        "roblox": None, "text": None, "photo": None, "reply_to": None,
    }
    await say(
        uid,
        "🎯 <b>Шаг 1/3 — кто нарушил?</b>\n\n"
        "Отправь ник игрока в Roblox.\n"
        "<i>Например: Builderman</i>",
        kb(CANCEL_ROW),
    )


async def start_reply(uid, post_id):
    sessions[uid] = {
        "kind": "reply", "step": "text",
        "roblox": None, "text": None, "photo": None, "reply_to": post_id,
    }
    where = "на пост"
    ch = await get_channel()
    if ch:
        where = f"на <a href=\"{message_link(ch, post_id)}\">этот пост</a>"
    await say(
        uid,
        f"💬 <b>Анонимный ответ {where}</b>\n\n"
        "Напиши ответ (можно прикрепить фото с подписью).\n"
        "Перед публикацией его проверит модератор.",
        kb(CANCEL_ROW),
    )


async def ask_photo(uid):
    sessions[uid]["step"] = "photo"
    await say(
        uid,
        "📸 <b>Шаг 3/3 — доказательства</b>\n\n"
        "Отправь скриншот или нажми «Пропустить».",
        kb([btn("⏭ Пропустить", "form:skip_photo")], CANCEL_ROW),
    )


async def show_confirm(uid):
    d = sessions[uid]
    d["step"] = "confirm"
    extra = "\n\n📸 <i>+ скриншот</i>" if d["photo"] else ""
    await say(
        uid,
        "👀 <b>Так это будет выглядеть:</b>\n\n"
        f"{body_of(d)}{extra}\n\n"
        "Всё верно?",
        kb(
            [btn("✅ Отправить", "form:send")],
            [btn("✏️ Заново", "form:restart"), btn("❌ Отмена", "form:cancel")],
        ),
    )


async def submit(uid):
    d = sessions.get(uid)
    if not d or d["step"] != "confirm":
        return

    if not ADMIN_CHAT_ID:
        await say(uid, "⚠️ Бот ещё не настроен. Попробуй позже.")
        return

    wait = COOLDOWN - (time.time() - last_sent.get(uid, 0))
    if wait > 0:
        await say(
            uid,
            f"⏳ Слишком часто. Подожди {int(wait) + 1} сек. и нажми «✅ Отправить» ещё раз.",
        )
        return

    header = "🛡 <b>Новая заявка на модерацию</b>"
    if d["kind"] == "reply":
        ch = await get_channel()
        if ch:
            header += f"\n↩️ Ответ на <a href=\"{message_link(ch, d['reply_to'])}\">пост</a>"
        else:
            header += f"\n↩️ Ответ на пост #{d['reply_to']}"

    full = f"{header}\n{SEP}\n\n{body_of(d)}"
    token = seal_uid(uid)
    rt = d["reply_to"] or 0
    markup = kb([
        btn("✅ Опубликовать", f"mod:ok:{rt}:{token}"),
        btn("❌ Отклонить", f"mod:no:{rt}:{token}"),
    ])

    try:
        if d["photo"]:
            await tg.bot.send_photo(
                ADMIN_CHAT_ID, d["photo"], caption=full,
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
        else:
            await tg.bot.send_message(
                ADMIN_CHAT_ID, full, parse_mode=ParseMode.HTML,
                reply_markup=markup, link_preview_options=NO_PREVIEW,
            )
    except TelegramError:
        log.exception("Не удалось отправить заявку модераторам")
        await say(uid, "❌ Не удалось отправить. Попробуй ещё раз чуть позже.")
        return

    sessions.pop(uid, None)
    last_sent[uid] = time.time()
    await say(
        uid,
        "✅ <b>Отправлено на проверку!</b>\n\n"
        "Как только модератор примет решение — я напишу тебе здесь.",
        main_menu(),
    )


# ─────────────────────────── МОДЕРАЦИЯ ───────────────────────────

async def publish(src, reply_to):
    html = src.caption_html if src.photo else src.text_html
    body = html.split(f"{SEP}\n\n", 1)[-1]
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None

    if src.photo:
        post = await tg.bot.send_photo(
            CHANNEL_ID, src.photo[-1].file_id, caption=body,
            parse_mode=ParseMode.HTML, reply_parameters=rp,
        )
    else:
        post = await tg.bot.send_message(
            CHANNEL_ID, body, parse_mode=ParseMode.HTML, reply_parameters=rp,
        )

    await tg.bot.edit_message_reply_markup(
        chat_id=post.chat_id,
        message_id=post.message_id,
        reply_markup=post_keyboard(post.message_id),
    )
    return post


async def notify(uid, text, markup=None):
    try:
        await say(uid, text, markup)
    except TelegramError:
        pass  # человек мог заблокировать бота


async def moderate(q, action, reply_to, token):
    msg = q.message
    if ADMIN_CHAT_ID is None or msg.chat.id != ADMIN_CHAT_ID:
        await q.answer("Нет доступа.", show_alert=True)
        return

    key = (msg.chat.id, msg.message_id)
    if key in processing:
        await q.answer("Уже обрабатывается…")
        return
    processing.add(key)

    try:
        who = q.from_user.first_name or "модератор"
        uid = open_uid(token)

        if action == "ok":
            if not CHANNEL_ID:
                await q.answer("Канал не настроен: /setchannel @канал", show_alert=True)
                return
            try:
                post = await publish(msg, reply_to)
            except TelegramError as e:
                log.exception("Ошибка публикации")
                await q.answer(f"Ошибка публикации: {e.message}", show_alert=True)
                return

            link = message_link(post.chat, post.message_id)
            await msg.edit_reply_markup(InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Опубликовал(а) {who}", url=link)]
            ]))
            await q.answer("Опубликовано ✅")
            if uid:
                await notify(
                    uid,
                    "✅ <b>Твоя заявка опубликована!</b>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("👀 Посмотреть", url=link)]]),
                )

        elif action == "no":
            await msg.edit_reply_markup(kb([btn(f"❌ Отклонил(а) {who}", "noop")]))
            await q.answer("Отклонено")
            if uid:
                await notify(
                    uid,
                    "❌ <b>Заявка не прошла модерацию.</b>\n\n"
                    "Проверь, что указан правильный ник и подробно описано, что случилось, "
                    "и попробуй ещё раз.",
                    main_menu(),
                )
        else:
            await q.answer()
    finally:
        processing.discard(key)


# ─────────────────────────── ОБРАБОТЧИКИ ───────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    arg = context.args[0] if context.args else ""

    # Кнопки из канала ведут сюда — форма открывается сразу.
    if arg == "report":
        await start_report(uid)
        return
    if arg.startswith("reply_") and arg[6:].isdigit():
        await start_reply(uid, int(arg[6:]))
        return

    sessions.pop(uid, None)
    await say(
        uid,
        f"👋 <b>{TITLE}</b>\n\n"
        "Здесь можно анонимно пожаловаться на игрока.\n"
        "Никто — ни модераторы, ни канал — не увидит, кто отправил.",
        main_menu(),
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_report(update.effective_user.id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    await say(update.effective_user.id, "❌ Отменено.", main_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    if data == "noop":
        await q.answer()
        return

    if data.startswith("mod:"):
        parts = data.split(":")
        if len(parts) != 4:
            await q.answer()
            return
        _, action, rt, token = parts
        await moderate(q, action, int(rt) if rt.isdigit() else 0, token)
        return

    await q.answer()
    uid = q.from_user.id

    # Убираем кнопки с нажатого сообщения, чтобы не жали по старым.
    if data in ("form:skip_photo", "form:send", "form:restart", "form:cancel"):
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass

    if data == "form:report":
        await start_report(uid)

    elif data == "form:info":
        await say(
            uid,
            "ℹ️ <b>Как это работает</b>\n\n"
            "1. Указываешь ник игрока в Roblox\n"
            "2. Описываешь, что произошло\n"
            "3. По желанию — скриншот\n"
            "4. Заявку проверяет модератор\n"
            "5. Если одобрят — она появится в канале\n\n"
            "🕶 Твоё имя никто не увидит.\n"
            "Под каждым постом в канале есть кнопки — "
            "по ним бот сразу открывается с формой.",
            main_menu(),
        )

    elif data == "form:skip_photo":
        d = sessions.get(uid)
        if d and d["step"] == "photo":
            await show_confirm(uid)

    elif data == "form:send":
        await submit(uid)

    elif data == "form:restart":
        d = sessions.get(uid)
        if d and d["kind"] == "reply":
            await start_reply(uid, d["reply_to"])
        else:
            await start_report(uid)

    elif data == "form:cancel":
        sessions.pop(uid, None)
        await say(uid, "❌ Отменено.", main_menu())


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    d = sessions.get(uid)

    # Человек просто написал боту — сразу показываем меню, без /start.
    if not d:
        await say(uid, "Хочешь отправить анонимную жалобу? 👇", main_menu())
        return

    step = d["step"]

    if step == "username":
        name = text.lstrip("@").strip()
        if not USERNAME_RE.match(name):
            await say(
                uid,
                "❌ Ник в Roblox — это 3–20 символов: латиница, цифры и «_».\n"
                "Отправь ещё раз.",
                kb(CANCEL_ROW),
            )
            return

        wait_msg = await say(uid, "🔎 Ищу игрока...")
        roblox = await roblox_user(name)
        try:
            await wait_msg.delete()
        except TelegramError:
            pass

        if roblox == "error":
            await say(uid, "⚠️ Roblox сейчас не отвечает. Отправь ник ещё раз через минуту.", kb(CANCEL_ROW))
            return
        if not roblox:
            await say(uid, "❌ Такой игрок не найден. Проверь написание и отправь ещё раз.", kb(CANCEL_ROW))
            return

        d["roblox"] = roblox
        d["step"] = "description"
        await say(
            uid,
            f"✅ Нашёл: <a href=\"{roblox['profile']}\">{escape(roblox['name'])}</a>\n\n"
            "📝 <b>Шаг 2/3 — что произошло?</b>\n\n"
            "Опиши подробно: когда, где и что сделал игрок.\n"
            "<i>Можно сразу отправить скриншот с подписью.</i>",
            kb(CANCEL_ROW),
        )

    elif step in ("description", "text"):
        min_len = MIN_TEXT if step == "description" else 1
        if len(text) < min_len:
            await say(uid, "❌ Опиши подробнее.", kb(CANCEL_ROW))
            return
        if len(text) > MAX_TEXT:
            await say(uid, f"❌ Слишком длинно: {len(text)}/{MAX_TEXT} символов. Сократи.", kb(CANCEL_ROW))
            return

        d["text"] = text
        if d["kind"] == "report" and not d["photo"]:
            await ask_photo(uid)
        else:
            await show_confirm(uid)

    elif step == "photo":
        await say(
            uid,
            "📸 Жду скриншот. Если его нет — нажми «Пропустить».",
            kb([btn("⏭ Пропустить", "form:skip_photo")], CANCEL_ROW),
        )

    elif step == "confirm":
        await say(
            uid,
            "Нажми «✅ Отправить» или «✏️ Заново».",
            kb(
                [btn("✅ Отправить", "form:send")],
                [btn("✏️ Заново", "form:restart"), btn("❌ Отмена", "form:cancel")],
            ),
        )


async def on_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    photo = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()
    d = sessions.get(uid)

    if not d:
        await say(uid, "Чтобы отправить скриншот, сначала начни жалобу 👇", main_menu())
        return

    if caption and len(caption) > MAX_TEXT:
        await say(uid, f"❌ Подпись слишком длинная: {len(caption)}/{MAX_TEXT}.", kb(CANCEL_ROW))
        return

    if d["kind"] == "reply":
        d["photo"] = photo
        if caption:
            d["text"] = caption
        await show_confirm(uid)
        return

    if d["step"] == "username":
        await say(uid, "Сначала отправь ник игрока текстом 🙂", kb(CANCEL_ROW))
        return

    d["photo"] = photo

    if d["step"] == "description":
        if len(caption) >= MIN_TEXT:
            d["text"] = caption
            await show_confirm(uid)
        else:
            await say(uid, "📸 Скриншот сохранил! Теперь опиши текстом, что произошло.", kb(CANCEL_ROW))
        return

    await show_confirm(uid)


async def on_private_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await say(
        update.effective_user.id,
        "Я понимаю только текст и фото 🙂",
        main_menu() if update.effective_user.id not in sessions else kb(CANCEL_ROW),
    )


# ─────────────────────── КОМАНДЫ ДЛЯ АДМИНОВ ───────────────────────

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID
    if not await is_group_admin(update):
        await update.message.reply_text("❌ Только администратор группы может это сделать.")
        return

    ADMIN_CHAT_ID = update.effective_chat.id
    await update.message.reply_text(
        "✅ <b>Эта группа теперь для модерации.</b>\n\n"
        f"ID: <code>{ADMIN_CHAT_ID}</code>\n\n"
        "⚠️ Чтобы настройка не слетала после перезапуска Render, "
        f"добавь в Environment переменную:\n<code>ADMIN_CHAT_ID = {ADMIN_CHAT_ID}</code>\n\n"
        "Дальше: <code>/setchannel @твой_канал</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHANNEL_ID
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Сначала сделай /setup в этой группе.")
        return
    if not await is_group_admin(update):
        await update.message.reply_text("❌ Только для администраторов.")
        return
    if not context.args:
        await update.message.reply_text(
            "Пример: /setchannel @PodBladeBall2  (канал или публичная группа)"
        )
        return

    target = _chat_id(context.args[0].replace("https://", "").replace("t.me/", ""))
    try:
        chat = await tg.bot.get_chat(target)
        me = await chat.get_member(tg.bot.id)
    except TelegramError:
        await update.message.reply_text(
            "❌ Не вижу такой канал/группу. Сначала добавь туда бота "
            "(лучше админом), потом повтори команду."
        )
        return

    if chat.type not in ("channel", "group", "supergroup"):
        await update.message.reply_text("❌ Это не канал и не группа.")
        return

    if chat.type == "channel":
        if me.status != "administrator" or not getattr(me, "can_post_messages", False):
            await update.message.reply_text(
                "❌ Дай боту в канале права администратора на публикацию."
            )
            return
    else:
        cant_write = me.status in ("left", "kicked") or (
            me.status == "restricted" and not getattr(me, "can_send_messages", True)
        )
        if cant_write:
            await update.message.reply_text(
                "❌ Бот не может писать в эту группу. Добавь его в группу (лучше админом)."
            )
            return

    CHANNEL_ID = chat.id
    channel_cache["chat"] = chat
    kind = "Канал" if chat.type == "channel" else "Группа"
    await update.message.reply_text(
        f"✅ {kind} <b>{escape(chat.title)}</b> подключён(а) для публикаций.\n\n"
        "⚠️ Добавь в Environment на Render:\n"
        f"<code>CHANNEL_ID = {chat.id}</code>\n\n"
        "Команда /post опубликует туда пост с кнопкой «Отправить анонимно».",
        parse_mode=ParseMode.HTML,
    )


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID or not await is_group_admin(update):
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❌ Сначала /setchannel @канал")
        return

    try:
        post = await tg.bot.send_message(
            CHANNEL_ID,
            "📮 <b>Анонимные жалобы Blade Ball</b>\n\n"
            "Видел читера, токсика или скамера?\n"
            "Нажми кнопку ниже — бот сразу откроет форму.\n\n"
            "🕶 Никто не узнает, кто отправил.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Отправить анонимно", url=deep_link("report"))]
            ]),
        )
        try:
            await tg.bot.pin_chat_message(CHANNEL_ID, post.message_id, disable_notification=True)
        except TelegramError:
            pass
        await update.message.reply_text("✅ Пост опубликован (и закреплён, если есть права).")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Ошибка: {e.message}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update):
        return
    ch = await get_channel()
    await update.message.reply_text(
        "⚙️ <b>Статус</b>\n\n"
        f"Группа модерации: <code>{ADMIN_CHAT_ID or 'не задана'}</code>\n"
        f"Канал: {escape(ch.title) if ch else 'не задан'}\n"
        f"Ссылка на форму: {deep_link('report')}",
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка при обработке апдейта", exc_info=context.error)


# ─────────────────────────── ЗАПУСК ───────────────────────────

PRIVATE = filters.ChatType.PRIVATE
GROUPS = filters.ChatType.GROUPS

tg.add_handler(CommandHandler("start", cmd_start, filters=PRIVATE))
tg.add_handler(CommandHandler("report", cmd_report, filters=PRIVATE))
tg.add_handler(CommandHandler("cancel", cmd_cancel, filters=PRIVATE))
tg.add_handler(CommandHandler("setup", cmd_setup, filters=GROUPS))
tg.add_handler(CommandHandler("setchannel", cmd_setchannel, filters=GROUPS))
tg.add_handler(CommandHandler("post", cmd_post, filters=GROUPS))
tg.add_handler(CommandHandler("status", cmd_status, filters=GROUPS))
tg.add_handler(CallbackQueryHandler(on_button))
tg.add_handler(MessageHandler(PRIVATE & filters.PHOTO, on_private_photo))
tg.add_handler(MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text))
tg.add_handler(MessageHandler(PRIVATE & ~filters.COMMAND, on_private_other))
tg.add_error_handler(on_error)


async def setup_bot_profile():
    try:
        await tg.bot.set_my_commands(
            [
                BotCommand("report", "Отправить анонимную жалобу"),
                BotCommand("cancel", "Отменить"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await tg.bot.set_my_commands(
            [
                BotCommand("setup", "Сделать эту группу модерационной"),
                BotCommand("setchannel", "Подключить канал"),
                BotCommand("post", "Пост с кнопкой в канал"),
                BotCommand("status", "Текущие настройки"),
            ],
            scope=BotCommandScopeAllChatAdministrators(),
        )
        await tg.bot.set_my_short_description("Анонимные жалобы на игроков Blade Ball 🕶")
    except TelegramError:
        log.exception("Не удалось обновить профиль бота")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await tg.initialize()
    await setup_bot_profile()
    if WEBHOOK_URL:
        await tg.bot.set_webhook(
            url=f"{WEBHOOK_URL}/telegram",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        )
    await tg.start()
    yield
    await tg.stop()
    await tg.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/telegram")
async def telegram_webhook(request: Request):
    # Принимаем апдейты только от Telegram.
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return Response(status_code=403)
    update = Update.de_json(await request.json(), tg.bot)
    await tg.process_update(update)
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
async def home():
    return {"status": "ok", "bot": tg.bot.username if tg.bot else None}
