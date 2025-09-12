# bot/main.py
# Simple Telegram food recognition bot with formatted RU output + Admin API ingestion
# Requirements:
#   python-telegram-bot==21.9
#   openai>=1.40.0
#   python-dotenv>=1.0.1
#
# .env (root or bot/):
#   TELEGRAM_BOT_TOKEN=...
#   OPENAI_API_KEY=...
#   OPENAI_VISION_MODEL=gpt-4o-mini
#   OPENAI_TEXT_MODEL=gpt-4o-mini
#   ADMIN_API_BASE=http://localhost:8000
#   ADMIN_API_KEY=supersecret

import os, json, base64, sqlite3, logging
from datetime import datetime, timezone, date
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)
import httpx

# --- Local modules for Admin integration ---
from bot.parse_block import parse_formatted_block          # bot/parse_block.py
from bot.ingest_client import ingest_meal                  # bot/ingest_client.py

# ------------- ENV / CONFIG -------------
# Try loading from repo root and bot/ folder
if os.path.exists(os.path.join(os.path.dirname(__file__), "..", ".env")):
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
else:
    load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL_VISION = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
MODEL_TEXT   = os.getenv("OPENAI_TEXT_MODEL",   "gpt-4o-mini")

if not TELEGRAM_TOKEN or not OPENAI_KEY:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and OPENAI_API_KEY in .env")

# ------------- LOGGING -------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("foodbot")

# ------------- OPENAI -------------
client = OpenAI(api_key=OPENAI_KEY)

# ------------- DB (SQLite) -------------
DB_PATH = os.path.join(os.path.dirname(__file__), "state_simple.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS interactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, original_message_id INTEGER, bot_message_id INTEGER,
        mode TEXT,                     -- 'image' or 'text'
        original_hint TEXT,            -- caption or text
        bot_output TEXT,               -- last rendered formatted text
        created_at TEXT, updated_at TEXT
    )""")
    conn.commit(); conn.close()

def save_interaction(chat_id, original_message_id, bot_message_id, mode, hint, bot_output):
    ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""INSERT INTO interactions(chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output, created_at, updated_at)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (chat_id, original_message_id, bot_message_id, mode, hint, bot_output, ts, ts))
    conn.commit(); conn.close()

def update_interaction_bot_output(bot_message_id, new_text):
    ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""UPDATE interactions SET bot_output=?, updated_at=? WHERE bot_message_id=?""",
              (new_text, ts, bot_message_id))
    conn.commit(); conn.close()

def get_interaction_by_bot_message_id(bot_message_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""SELECT id, chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output
                 FROM interactions WHERE bot_message_id=?""", (bot_message_id,))
    row = c.fetchone(); conn.close(); return row

def get_last_interaction_by_chat(chat_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""SELECT id, chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output
                 FROM interactions WHERE chat_id=? ORDER BY id DESC LIMIT 1""", (chat_id,))
    row = c.fetchone(); conn.close(); return row

# ------------- PROMPTS -------------
FORMAT_INSTRUCTIONS_RU = """
Сформируй ответ СТРОГО этим человеком читаемым блоком (без кода, без JSON):

🍽️ Разбор блюда (оценка по {SOURCE})
{TITLE}.
Порция: ~ {PORTION} г  ·  доверие {CONF}%
Калории: {KCAL} ккал
БЖУ: белки {P} г · жиры {F} г · углеводы {C} г
Ключевые микроэлементы (топ-5):
• {MICRO1}
• {MICRO2}
Флаги диеты:
• vegetarian: {VEGETARIAN}  ·  vegan: {VEGAN}
• glutenfree: {GLUTENFREE}  ·  lactosefree: {LACTOSEFREE}
Допущения:
• {ASSUMP1}
• {ASSUMP2}

Правила:
- Сохраняй точный макет и порядок строк.
- Если чего-то нет, поставь реалистичную оценку, не оставляй пусто (например, «Калории: 360 ккал»).
- Название блюда {TITLE} — короткое и точное (например: «Жареный лосось с картофелем и салатом»).
- Не добавляй ничего вне блока.
"""

SYSTEM_SIMPLE = (
    "Ты — ассистент, который ПРОСТО распознаёт еду по фотографии или описанию и выдаёт аккуратный форматированный отчёт на русском.\n"
    "Не вдавайся в сложные нутри-расчёты: достаточно реалистичных оценок. Название блюда всегда обязательно.\n"
    "Строго соблюдай формат из инструкции. Никаких JSON и лишних слов."
)

REVISE_RULES = (
    "Ниже твой прошлый ответ в нужном формате. Пользователь прислал уточнение/поправку.\n"
    "Перепиши блок, аккуратно исправив ТОЛЬКО ошибочные части (например, название, состав, порцию, флаги, БЖУ), остальное оставь как было.\n"
    "Формат и макет должны остаться теми же. В конце блока ничего не добавляй."
)

# ------------- UTILS -------------
def encode_image_to_data_url(path: str) -> str:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

async def llm_render_from_image(image_data_url: str, hint_text: str = "") -> str:
    user_parts = [
        {"type": "text", "text": SYSTEM_SIMPLE + "\n\n" + FORMAT_INSTRUCTIONS_RU.replace("{SOURCE}", "фото")}
    ]
    user_parts.append({"type": "image_url", "image_url": {"url": image_data_url}})
    if hint_text:
        user_parts.append({"type": "text", "text": f"Подпись/подсказка пользователя: {hint_text}"})
    resp = client.chat.completions.create(
        model=MODEL_VISION,
        messages=[{"role":"user","content": user_parts}],
        temperature=1
    )
    return resp.choices[0].message.content.strip()

async def llm_render_from_text(text: str) -> str:
    prompt = SYSTEM_SIMPLE + "\n\n" + FORMAT_INSTRUCTIONS_RU.replace("{SOURCE}", "описанию") + "\nОписание: " + text
    resp = client.chat_completions.create(  # fallback for SDK variations
        model=MODEL_TEXT,
        messages=[{"role":"user","content": prompt}],
        temperature=1
    ) if hasattr(client, "chat_completions") else client.chat.completions.create(
        model=MODEL_TEXT,
        messages=[{"role":"user","content": prompt}],
        temperature=1
    )
    # normalize SDK difference
    content = (resp.choices[0].message.content if hasattr(resp.choices[0], "message") else resp.choices[0].content).strip()
    return content

async def llm_revise(previous_block: str, correction_text: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL_TEXT,
        messages=[
            {"role":"system","content": REVISE_RULES},
            {"role":"user","content": "Твой прошлый ответ:\n" + previous_block},
            {"role":"user","content": "Коррекция пользователя:\n" + correction_text}
        ],
        temperature=1
    )
    return resp.choices[0].message.content.strip()

def _send_ingest_from_block(
    block_text: str,
    update: Update,
    message_id: int,
    source_type: str,
    image_path: Optional[str] = None
) -> None:
    """Parse bot block and send to Admin API (upsert by message_id)."""
    try:
        parsed = parse_formatted_block(block_text)
        ingest_meal({
            "telegram_user_id": update.message.from_user.id,
            "telegram_username": update.message.from_user.username,
            "captured_at_iso": datetime.now(timezone.utc).isoformat(),
            "title": parsed["title"],
            "portion_g": parsed["portion_g"],
            "confidence": parsed["confidence"],
            "kcal": parsed["kcal"],
            "protein_g": parsed.get("protein_g"),
            "fat_g": parsed.get("fat_g"),
            "carbs_g": parsed.get("carbs_g"),
            "flags": parsed.get("flags", {}),
            "micronutrients": parsed.get("micronutrients", []),
            "assumptions": parsed.get("assumptions", []),
            "source_type": source_type,
            "image_path": image_path,
            "message_id": message_id
        })
    except Exception as e:
        log.exception("Failed to ingest meal", exc_info=e)

# ------------- HANDLERS -------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли фото или опиши блюдо — я распознаю и верну отчёт.\n"
        "Уточнять можно реплаем или отдельным сообщением («есть …», «добавь …», «без …»)."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("handle_photo: received update has_photo=%s", bool(update.message and update.message.photo))
    if not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    f = await photo.get_file()
    downloads_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    local_path = os.path.join(downloads_dir, f"{f.file_unique_id}.jpg")
    await f.download_to_drive(local_path)
    caption = (update.message.caption or "").strip()

    try:
        block = await llm_render_from_image(encode_image_to_data_url(local_path), caption)
    except Exception as e:
        log.exception("LLM image render failed", exc_info=e)
        block = (
            "🍽️ Разбор блюда (оценка по фото)\nБлюдо (ассорти).\nПорция: ~ 300 г  ·  доверие 60%\n"
            "Калории: 360 ккал\nБЖУ: белки 15 г · жиры 15 г · углеводы 45 г\n"
            "Ключевые микроэлементы (топ-5):\n• Клетчатка — 6 g\n• Витамин C — 30 mg\n"
            "Флаги диеты:\n• vegetarian: нет  ·  vegan: нет\n• glutenfree: нет  ·  lactosefree: нет\n"
            "Допущения:\n• Оценка по фото.\n• Ингредиенты и масса — приблизительно."
        )

    sent = await update.message.reply_text(block)

    # --- Admin ingestion ---
    _send_ingest_from_block(
        block_text=block,
        update=update,
        message_id=sent.message_id,
        source_type="image",
        image_path=local_path
    )

    # --- Local persistence for correction flow ---
    save_interaction(update.effective_chat.id, update.message.message_id, sent.message_id, "image", caption, block)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("handle_text: has_text=%s is_reply=%s", bool(update.message and update.message.text), bool(update.message and update.message.reply_to_message))
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # Not a reply but likely a correction → apply to last bot message
    if not (update.message.reply_to_message and update.message.reply_to_message.from_user and update.message.reply_to_message.from_user.is_bot):
        markers = ("есть ", "добавь", "убери", "без ", "+", "ещё ", "еще ", "поменяй", "замени")
        lc = text.lower()
        if any(lc.startswith(m) or m in lc for m in markers):
            last = get_last_interaction_by_chat(update.effective_chat.id)
            if last:
                _, chat_id, orig_id, bot_msg_id, mode, hint, prev_block = last
                try:
                    new_block = await llm_revise(prev_block, text)
                except Exception as e:
                    log.exception("LLM revise failed", exc_info=e)
                    new_block = prev_block  # fallback: keep as is

                try:
                    await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=bot_msg_id, text=new_block)
                except Exception:
                    await update.message.reply_text(new_block)

                update_interaction_bot_output(bot_msg_id, new_block)

                # Admin ingestion (update same message_id)
                dummy_update = update  # for user id/username
                _send_ingest_from_block(
                    block_text=new_block,
                    update=dummy_update,
                    message_id=bot_msg_id,
                    source_type=mode or "text",
                    image_path=None
                )
                return

    # Fresh text identification
    try:
        block = await llm_render_from_text(text)
    except Exception as e:
        log.exception("LLM text render failed", exc_info=e)
        block = (
            "🍽️ Разбор блюда (оценка по описанию)\nБлюдо (ассорти).\nПорция: ~ 300 г  ·  доверие 60%\n"
            "Калории: 360 ккал\nБЖУ: белки 15 г · жиры 15 г · углеводы 45 г\n"
            "Ключевые микроэлементы (топ-5):\n• Клетчатка — 6 g\n• Витамин C — 30 mg\n"
            "Флаги диеты:\n• vegetarian: нет  ·  vegan: нет\n• glutenfree: нет  ·  lactosefree: нет\n"
            "Допущения:\n• Оценка по описанию.\n• Ингредиенты и масса — приблизительно."
        )
    sent = await update.message.reply_text(block)

    # Admin ingestion
    _send_ingest_from_block(
        block_text=block,
        update=update,
        message_id=sent.message_id,
        source_type="text",
        image_path=None
    )

    save_interaction(update.effective_chat.id, update.message.message_id, sent.message_id, "text", text, block)

async def handle_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Reply-to-bot → correction
    msg = update.message
    if not msg or not msg.text or not msg.reply_to_message:
        return
    if not (msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot and msg.reply_to_message.from_user.id == context.bot.id):
        return

    row = get_interaction_by_bot_message_id(msg.reply_to_message.message_id)
    if not row: return
    _, chat_id, orig_id, bot_msg_id, mode, hint, prev_block = row

    try:
        new_block = await llm_revise(prev_block, msg.text.strip())
    except Exception as e:
        log.exception("LLM revise failed", exc_info=e)
        new_block = prev_block

    try:
        await msg.reply_to_message.edit_text(new_block)
    except Exception:
        await msg.reply_text(new_block)

    update_interaction_bot_output(bot_msg_id, new_block)

    # Admin ingestion (update same message_id)
    _send_ingest_from_block(
        block_text=new_block,
        update=update,
        message_id=bot_msg_id,
        source_type=mode or "text",
        image_path=None
    )

async def finalize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок — просто ответьте реплаем, если нужно исправить детали. Команда финализации не требуется 😊")

# ------------- MENU / STATS -------------
MENU_CB_HELP = "MENU_HELP"
MENU_CB_ABOUT = "MENU_ABOUT"
MENU_CB_DAILY = "MENU_DAILY"
MENU_CB_WEEKLY = "MENU_WEEKLY"

def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Инструкция", callback_data=MENU_CB_HELP), InlineKeyboardButton("ℹ️ О боте", callback_data=MENU_CB_ABOUT)],
        [InlineKeyboardButton("📊 За сегодня", callback_data=MENU_CB_DAILY), InlineKeyboardButton("📆 За неделю", callback_data=MENU_CB_WEEKLY)]
    ])

INSTRUCTION_TEXT = (
    "📖 Инструкция\n"
    "1. Пришлите фото блюда — бот вернёт разбор с калориями и БЖУ.\n"
    "2. Можно описать блюдо текстом.\n"
    "3. Уточнения: сообщение со словами ‘добавь’, ‘убери’, ‘без’, ‘ещё/еще’, ‘поменяй’, ‘замени’, или ответ реплаем.\n"
    "4. /menu — показать меню.\n"
    "5. Сводки: кнопки ‘За сегодня’ и ‘За неделю’."
)

ABOUT_TEXT = (
    "ℹ️ О боте\n"
    "Nutrios — бот для приблизительной оценки блюд (калории, БЖУ, микроэлементы) по фото или описанию."
)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/menu invoked chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.message.reply_text("Меню:", reply_markup=menu_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Alias to /menu for convenience
    await menu_command(update, context)

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_daily_text(update.effective_user.id)
    await update.message.reply_text(text)

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_weekly_text(update.effective_user.id)
    await update.message.reply_text(text)

async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Avoid dumping huge structures; trim
        d = update.to_dict()
        keys = list(d.keys())
        log.info("DEBUG update keys=%s has_callback=%s has_message=%s", keys, 'callback_query' in d, 'message' in d)
    except Exception:
        pass

async def _fetch_client_id(telegram_user_id: int) -> int | None:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client_http:
            r = await client_http.get(f"{os.getenv('ADMIN_API_BASE', 'http://localhost:8000')}/clients")
            if r.status_code != 200:
                return None
            for row in r.json():
                if row.get("telegram_user_id") == telegram_user_id:
                    return row.get("id")
    except Exception:
        return None
    return None

async def _fetch_summary(client_id: int, kind: str):
    base = os.getenv('ADMIN_API_BASE', 'http://localhost:8000')
    url = f"{base}/clients/{client_id}/summary/{'daily' if kind=='daily' else 'weekly'}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client_http:
            r = await client_http.get(url)
            if r.status_code != 200:
                return []
            return r.json() or []
    except Exception:
        return []

def _fmt_macros(kcal, p, f, c):
    def _n(v):
        try:
            if v is None: return 0
            return int(round(float(v)))
        except Exception:
            return 0
    return f"Калории: {_n(kcal)} ккал\nБелки: {_n(p)} г · Жиры: {_n(f)} г · Углеводы: {_n(c)} г"

async def _build_daily_text(telegram_user_id: int) -> str:
    client_id = await _fetch_client_id(telegram_user_id)
    if not client_id:
        return "Нет данных за сегодня (ещё не распознано ни одного блюда)."
    data = await _fetch_summary(client_id, 'daily')
    if not data:
        return "Нет данных за сегодня."
    today_iso = date.today().isoformat()
    row_today = None
    for r in data:
        if r.get("period_start", "").startswith(today_iso):
            row_today = r; break
    if not row_today:
        row_today = data[-1]
    return "📊 Сводка за сегодня (" + row_today.get("period_start", '')[:10] + ")\n" + _fmt_macros(row_today.get("kcal"), row_today.get("protein_g"), row_today.get("fat_g"), row_today.get("carbs_g"))

async def _build_weekly_text(telegram_user_id: int) -> str:
    client_id = await _fetch_client_id(telegram_user_id)
    if not client_id:
        return "Нет данных за неделю (ещё не распознано ни одного блюда)."
    data = await _fetch_summary(client_id, 'weekly')
    if not data:
        return "Нет данных за неделю."
    row = data[-1]
    return "📆 Сводка за неделю (начало " + row.get("period_start", '')[:10] + ")\n" + _fmt_macros(row.get("kcal"), row.get("protein_g"), row.get("fat_g"), row.get("carbs_g"))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    log.info("callback received data=%s chat_id=%s", data, query.message.chat_id if query.message else None)
    try:
        await query.answer("Обновляю…", show_alert=False)
    except Exception:
        pass
    try:
        if data == MENU_CB_HELP:
            text = INSTRUCTION_TEXT
        elif data == MENU_CB_ABOUT:
            text = ABOUT_TEXT
        elif data == MENU_CB_DAILY:
            text = await _build_daily_text(query.from_user.id)
        elif data == MENU_CB_WEEKLY:
            text = await _build_weekly_text(query.from_user.id)
        else:
            text = "Неизвестный пункт меню."
    except Exception as e:
        log.exception("error building callback response", exc_info=e)
        text = "Ошибка при получении данных. Попробуйте позже."
    try:
        await query.edit_message_text(text, reply_markup=menu_keyboard())
    except Exception as e:
        log.warning("edit_message_text failed: %s", e)
        try:
            await query.message.reply_text(text, reply_markup=menu_keyboard())
        except Exception:
            pass

# ------------- ERROR HANDLER -------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception", exc_info=context.error)

# ------------- MAIN -------------
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("finalize", finalize_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    # Debug handler (last) to log raw updates if callbacks still not coming
    app.add_handler(MessageHandler(filters.ALL, debug_all, block=False), group=100)
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_correction))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("Bot started.")
    from telegram import Update as TgUpdate
    app.run_polling(allowed_updates=TgUpdate.ALL_TYPES, close_loop=False, drop_pending_updates=False)

if __name__ == "__main__":
    main()
