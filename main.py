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
#   OPENAI_VISION_MODEL=gpt-5
#   OPENAI_TEXT_MODEL=gpt-5
#   ADMIN_API_BASE=http://localhost:8000
#   ADMIN_API_KEY=supersecret

import os, json, base64, sqlite3, logging
from datetime import datetime, timezone, date, timedelta, time
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)
import httpx

# OpenAI-compatible client (OpenRouter supported via env)
from llm_client import get_llm_client

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
OPENAI_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
MODEL_VISION = os.getenv("OPENAI_VISION_MODEL", "gpt-5")
MODEL_TEXT   = os.getenv("OPENAI_TEXT_MODEL",   "gpt-5")


def _env_flag(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


DAILY_REPORT_ENABLED = _env_flag("DAILY_REPORT_ENABLED", default=True)

if not TELEGRAM_TOKEN or not OPENAI_KEY:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY (or OPENAI_API_KEY) in .env")

# ------------- LOGGING -------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("foodbot")

# ------------- OPENAI -------------
client = get_llm_client()


def log_llm_startup_config() -> None:
    provider = "openrouter" if os.getenv("OPENROUTER_API_KEY") else "openai"
    base_url = str(getattr(client, "base_url", "")) or "(default)"
    log.info(
        "LLM startup config: provider=%s base_url=%s vision_model=%s text_model=%s",
        provider,
        base_url,
        MODEL_VISION,
        MODEL_TEXT,
    )

# ------------- DB (SQLite) -------------
DB_PATH = os.path.join(os.path.dirname(__file__), "state_simple.db")
MSK = ZoneInfo("Europe/Moscow")


def _today_iso_msk() -> str:
    return datetime.now(MSK).date().isoformat()


def _msk_day_bounds_utc() -> tuple[datetime, datetime]:
    now_msk = datetime.now(MSK)
    start_msk = datetime.combine(now_msk.date(), datetime.min.time(), tzinfo=MSK)
    start_utc = start_msk.astimezone(timezone.utc)
    return start_utc, start_utc + timedelta(days=1)

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS interactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, original_message_id INTEGER, bot_message_id INTEGER,
                mode TEXT,                     -- 'image' or 'text'
                original_hint TEXT,            -- caption or text
                bot_output TEXT,               -- last rendered formatted text
                created_at TEXT, updated_at TEXT
            )"""
        )
        conn.commit()

def save_interaction(
    chat_id: int,
    original_message_id: int,
    bot_message_id: int,
    mode: str,
    hint: str,
    bot_output: str,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO interactions(chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output, created_at, updated_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
            (chat_id, original_message_id, bot_message_id, mode, hint, bot_output, ts, ts),
        )
        conn.commit()

def update_interaction_bot_output(bot_message_id: int, new_text: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """UPDATE interactions SET bot_output=?, updated_at=? WHERE bot_message_id=?""",
            (new_text, ts, bot_message_id),
        )
        conn.commit()

def get_interaction_by_bot_message_id(bot_message_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output
                     FROM interactions WHERE bot_message_id=?""",
            (bot_message_id,),
        )
        row = c.fetchone()
    return row

def get_last_interaction_by_chat(chat_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output
                     FROM interactions WHERE chat_id=? ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        )
        row = c.fetchone()
    return row


def get_interaction_by_id(interaction_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, chat_id, original_message_id, bot_message_id, mode, original_hint, bot_output
                     FROM interactions WHERE id=?""",
            (interaction_id,),
        )
        row = c.fetchone()
    return row

# ------------- PROMPTS -------------
FORMAT_INSTRUCTIONS_RU = """
Сформируй ответ СТРОГО этим человеком читаемым блоком (без кода, без JSON):

🍽️ Разбор блюда (оценка по {SOURCE})
{TITLE}.
Порция: ~ {PORTION} г  ·  доверие {CONF}%
Калории: {KCAL} ккал
БЖУ: белки {P} г · жиры {F} г · углеводы {C} г
Жиры подробно: всего {F_TOTAL} г; насыщенные {F_SAT} г; мононенасыщенные {F_MONO} г; полиненасыщенные {F_POLY} г; транс {F_TRANS} г
Омега: омега-6 {OMEGA6} г; омега-3 {OMEGA3} г (соотношение {OMEGA_RATIO})
Клетчатка: всего {FIBER_TOTAL} г (растворимая {FIBER_SOL} г, нерастворимая {FIBER_INSOL} г)
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
    "Перепиши блок, аккуратно исправив ТОЛЬКО ошибочные части (например, название, состав, порцию, флаги, БЖУ, жиры подробно, омега, клетчатка), остальное оставь как было.\n"
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
    content = resp.choices[0].message.content.strip()
    content = await ensure_fat_fiber_sections(content)
    return content

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
    content = await ensure_fat_fiber_sections(content)
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
) -> dict | None:
    """Parse bot block and send to Admin API (upsert by message_id)."""
    try:
        parsed = parse_formatted_block(block_text)
        effective_user = update.effective_user
        if not effective_user:
            return None
        ingest_meal({
            "telegram_user_id": effective_user.id,
            "telegram_username": effective_user.username,
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
            "extras": parsed.get("extras", {}),
            "source_type": source_type,
            "image_path": image_path,
            "message_id": message_id
        })
        missing_macros = any(parsed.get(k) is None for k in ("protein_g", "fat_g", "carbs_g"))
        return {"missing_macros": missing_macros}
    except Exception as e:
        log.exception("Failed to ingest meal", exc_info=e)
        return None

# Пост-проверка: если LLM не выдала секции жиров/омега/клетчатка — аккуратно допрашиваем и дописываем их.
async def ensure_fat_fiber_sections(block: str) -> str:
    needs_fats = ("Жиры подробно:" not in block)
    needs_omega = ("Омега:" not in block)
    needs_fiber = ("Клетчатка:" not in block)
    if not (needs_fats or needs_omega or needs_fiber):
        return block
    try:
        missing_list = ", ".join([
            s for s, cond in [("жиры подробно", needs_fats), ("омега", needs_omega), ("клетчатка", needs_fiber)] if cond
        ])
        revise_system = (
            "Ты редактор. Вставь в переданный блок отсутствующие строки для 'Жиры подробно', 'Омега' и 'Клетчатка' в соответствии с заданным форматом. "
            "Сохрани весь остальной текст без изменений. Если точных данных нет — поставь реалистичные оценки и единицы (г). "
            "Строго верни только обновлённый блок без комментариев."
        )
        user_req = f"Отсутствуют: {missing_list}. Добавь соответствующие строки ровно в те места формата после БЖУ."
        resp = client.chat.completions.create(
            model=MODEL_TEXT,
            messages=[
                {"role": "system", "content": revise_system},
                {"role": "user", "content": "Текущий блок:\n" + block},
                {"role": "user", "content": user_req},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return block

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
    processing_msg = await update.message.reply_text("📸 Фото получено, анализирую…")
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
        try:
            await processing_msg.edit_text("⚠️ Не удалось распознать блюдо по фото. Попробуйте ещё раз или пришлите другое изображение.")
        except Exception:
            await update.message.reply_text("⚠️ Не удалось распознать блюдо по фото. Попробуйте ещё раз или пришлите другое изображение.")
        return

    try:
        await processing_msg.edit_text(block, reply_markup=analysis_keyboard(processing_msg.message_id))
        sent = processing_msg
    except Exception:
        sent = await update.message.reply_text(block, reply_markup=analysis_keyboard(processing_msg.message_id))

    # --- Admin ingestion ---
    ingest_info = _send_ingest_from_block(
        block_text=block,
        update=update,
        message_id=sent.message_id,
        source_type="image",
        image_path=local_path
    )
    if ingest_info and ingest_info.get("missing_macros"):
        await update.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")

    # --- Local persistence for correction flow ---
    save_interaction(update.effective_chat.id, update.message.message_id, sent.message_id, "image", caption, block)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("handle_text: has_text=%s is_reply=%s", bool(update.message and update.message.text), bool(update.message and update.message.reply_to_message))
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # Correction flow triggered from inline "✏️ Исправить" button.
    forced_bot_msg_id = context.user_data.pop("force_edit_message_id", None)
    if forced_bot_msg_id and not (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.is_bot
    ):
        row = get_interaction_by_bot_message_id(forced_bot_msg_id)
        if row:
            _, chat_id, _, bot_msg_id, mode, _, prev_block = row
            if chat_id == update.effective_chat.id:
                try:
                    new_block = await llm_revise(prev_block, text)
                except Exception as e:
                    log.exception("LLM revise failed (forced flow)", exc_info=e)
                    new_block = prev_block
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_msg_id,
                        text=new_block,
                        reply_markup=analysis_keyboard(bot_msg_id),
                    )
                except Exception:
                    await update.message.reply_text(new_block, reply_markup=analysis_keyboard(bot_msg_id))
                update_interaction_bot_output(bot_msg_id, new_block)
                ingest_info = _send_ingest_from_block(
                    block_text=new_block,
                    update=update,
                    message_id=bot_msg_id,
                    source_type=mode or "text",
                    image_path=None,
                )
                if ingest_info and ingest_info.get("missing_macros"):
                    await update.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")
                return

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
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=bot_msg_id,
                        text=new_block,
                        reply_markup=analysis_keyboard(bot_msg_id),
                    )
                except Exception:
                    await update.message.reply_text(new_block, reply_markup=analysis_keyboard(bot_msg_id))

                update_interaction_bot_output(bot_msg_id, new_block)

                # Admin ingestion (update same message_id)
                dummy_update = update  # for user id/username
                ingest_info = _send_ingest_from_block(
                    block_text=new_block,
                    update=dummy_update,
                    message_id=bot_msg_id,
                    source_type=mode or "text",
                    image_path=None
                )
                if ingest_info and ingest_info.get("missing_macros"):
                    await update.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")
                return

    # Fresh text identification
    processing_msg = await update.message.reply_text("📝 Описание получено, анализирую…")
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
    try:
        await processing_msg.edit_text(block, reply_markup=analysis_keyboard(processing_msg.message_id))
        sent = processing_msg
    except Exception:
        sent = await update.message.reply_text(block, reply_markup=analysis_keyboard(processing_msg.message_id))

    # Admin ingestion
    ingest_info = _send_ingest_from_block(
        block_text=block,
        update=update,
        message_id=sent.message_id,
        source_type="text",
        image_path=None
    )
    if ingest_info and ingest_info.get("missing_macros"):
        await update.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")

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
        await msg.reply_to_message.edit_text(new_block, reply_markup=analysis_keyboard(bot_msg_id))
    except Exception:
        await msg.reply_text(new_block, reply_markup=analysis_keyboard(bot_msg_id))

    update_interaction_bot_output(bot_msg_id, new_block)

    # Admin ingestion (update same message_id)
    ingest_info = _send_ingest_from_block(
        block_text=new_block,
        update=update,
        message_id=bot_msg_id,
        source_type=mode or "text",
        image_path=None
    )
    if ingest_info and ingest_info.get("missing_macros"):
        await update.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")

async def finalize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок — просто ответьте реплаем, если нужно исправить детали. Команда финализации не требуется 😊")

# ------------- MENU / STATS -------------
MENU_CB_HELP = "MENU_HELP"
MENU_CB_ABOUT = "MENU_ABOUT"
MENU_CB_DAILY = "MENU_DAILY"
MENU_CB_WEEKLY = "MENU_WEEKLY"
MENU_CB_DAILY_DETAILS = "MENU_DAILY_DETAILS"
MENU_CB_EDIT_PREFIX = "EDIT:"
MENU_CB_DAILY_EDIT_PREFIX = "DDEDIT:"
MENU_CB_DAILY_REGEN_PREFIX = "DDREGEN:"
MENU_CB_DAILY_DELETE_PREFIX = "DDDEL:"
MENU_CB_DAILY_DELETE_CONFIRM_PREFIX = "DDDELC:"


def analysis_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Исправить", callback_data=f"{MENU_CB_EDIT_PREFIX}{message_id}")]
    ])

def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Инструкция", callback_data=MENU_CB_HELP), InlineKeyboardButton("ℹ️ О боте", callback_data=MENU_CB_ABOUT)],
        [InlineKeyboardButton("📊 За сегодня", callback_data=MENU_CB_DAILY), InlineKeyboardButton("📆 За неделю", callback_data=MENU_CB_WEEKLY)],
        [InlineKeyboardButton("🧾 За сегодня подробно", callback_data=MENU_CB_DAILY_DETAILS)],
    ])


def menu_with_daily_actions_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Обновить список", callback_data=MENU_CB_DAILY_DETAILS)]]
    for e in entries:
        idx = e["idx"]
        interaction_id = e["interaction_id"]
        rows.append(
            [
                InlineKeyboardButton(f"✏️ #{idx}", callback_data=f"{MENU_CB_DAILY_EDIT_PREFIX}{interaction_id}"),
                InlineKeyboardButton(f"🔁 #{idx}", callback_data=f"{MENU_CB_DAILY_REGEN_PREFIX}{interaction_id}"),
                InlineKeyboardButton(f"🗑️ #{idx}", callback_data=f"{MENU_CB_DAILY_DELETE_PREFIX}{interaction_id}"),
            ]
        )
    rows.extend(menu_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(rows)


def menu_daily_delete_confirm_keyboard(interaction_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"{MENU_CB_DAILY_DELETE_CONFIRM_PREFIX}{interaction_id}"),
            InlineKeyboardButton("↩️ Отмена", callback_data=MENU_CB_DAILY_DETAILS),
        ]
    ])

INSTRUCTION_TEXT = (
    "📖 Инструкция\n"
    "1. Пришлите фото блюда — бот вернёт разбор с калориями и БЖУ.\n"
    "2. Можно описать блюдо текстом.\n"
    "3. Уточнения: сообщение со словами ‘добавь’, ‘убери’, ‘без’, ‘ещё/еще’, ‘поменяй’, ‘замени’, или ответ реплаем.\n"
    "4. /menu — показать меню.\n"
    "5. Сводки: кнопки ‘За сегодня’, ‘За неделю’ и ‘За сегодня подробно’.\n"
    "6. В ‘За сегодня подробно’ можно редактировать, перегенерировать и удалять конкретное блюдо по кнопкам."
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

async def _fetch_meals(client_id: int):
    base = os.getenv('ADMIN_API_BASE', 'http://localhost:8000')
    url = f"{base}/clients/{client_id}/meals"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client_http:
            r = await client_http.get(url)
            if r.status_code != 200:
                return []
            return r.json() or []
    except Exception:
        return []

def _fmt_macros(kcal, p, f, c):
    def _k(v):
        try:
            if v is None:
                return "0"
            x = float(v)
            return str(int(round(x)))
        except Exception:
            return "0"

    def _m(v):
        try:
            if v is None:
                return "0"
            x = float(v)
            s = f"{x:.1f}"
            return s[:-2] if s.endswith(".0") else s
        except Exception:
            return "0"
    return f"Калории: {_k(kcal)} ккал\nБелки: {_m(p)} г · Жиры: {_m(f)} г · Углеводы: {_m(c)} г"

async def _build_daily_text(telegram_user_id: int) -> str:
    client_id = await _fetch_client_id(telegram_user_id)
    if not client_id:
        # Fallback to local interactions DB aggregation
        txt = _daily_local_summary_text(telegram_user_id)
        if txt:
            return txt
        return "Нет данных за сегодня (ещё не распознано ни одного блюда)."
    data = await _fetch_summary(client_id, 'daily')
    if not data:
        txt = _daily_local_summary_text(telegram_user_id)
        if txt:
            return txt
        return "Нет данных за сегодня."
    today_iso = _today_iso_msk()
    # Ищем только запись за сегодня; не подставляем "последнюю",
    # иначе получаются некорректные итоги "за сегодня".
    row_today = None
    for r in data:
        if r.get("period_start", "").startswith(today_iso):
            row_today = r; break
    if not row_today:
        return "Нет данных за сегодня."
    meals = await _fetch_meals(client_id)
    meals_count = 0
    unknown_macros_count = 0
    for m in meals:
        ts = m.get("captured_at")
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts).astimezone(MSK).date().isoformat() == today_iso:
                meals_count += 1
                if any(m.get(k) is None for k in ("protein_g", "fat_g", "carbs_g")):
                    unknown_macros_count += 1
        except Exception:
            continue
    tail = f"\nУчтено блюд: {meals_count}" if meals_count > 0 else ""
    if unknown_macros_count > 0:
        tail += f"\nБЖУ не определено для {unknown_macros_count} блюд(а): в сумме учтены только известные значения."
    return (
        "📊 Сводка за сегодня (" + row_today.get("period_start", '')[:10] + ")\n"
        + _fmt_macros(row_today.get("kcal"), row_today.get("protein_g"), row_today.get("fat_g"), row_today.get("carbs_g"))
        + tail
    )

async def _build_weekly_text(telegram_user_id: int) -> str:
    client_id = await _fetch_client_id(telegram_user_id)
    if not client_id:
        txt = _weekly_local_summary_text(telegram_user_id)
        if txt:
            return txt
        return "Нет данных за неделю (ещё не распознано ни одного блюда)."
    data = await _fetch_summary(client_id, 'weekly')
    if not data:
        txt = _weekly_local_summary_text(telegram_user_id)
        if txt:
            return txt
        return "Нет данных за неделю."
    row = data[-1]
    return "📆 Сводка за неделю (начало " + row.get("period_start", '')[:10] + ")\n" + _fmt_macros(row.get("kcal"), row.get("protein_g"), row.get("fat_g"), row.get("carbs_g"))

async def _build_daily_details_text(telegram_user_id: int, chat_id: int | None = None) -> str:
    """Краткий список блюд за сегодня: номер, дата/время, название, калории и БЖУ, порция и доверие."""
    try:
        start, end = _msk_day_bounds_utc()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT created_at, bot_output FROM interactions
            WHERE chat_id=? AND created_at>=? AND created_at<?
            ORDER BY created_at ASC
            """,
            ((chat_id or telegram_user_id), start.isoformat(), end.isoformat()),
        )
        rows = c.fetchall()
        conn.close()
    except Exception:
        rows = []
    try:
        log.info(
            "DAILY_DETAILS: user_id=%s chat_id=%s used_id=%s start=%s end=%s rows=%s",
            telegram_user_id, chat_id, (chat_id or telegram_user_id), start.isoformat(), end.isoformat(), len(rows)
        )
    except Exception:
        pass

    if not rows:
        return "🧾 За сегодня подробно\nСегодня ещё нет блюд."

    def _num(v):
        try:
            return int(round(float(v or 0)))
        except Exception:
            return 0

    lines = []
    for idx, (created_at_iso, text) in enumerate(rows, start=1):
        try:
            parsed = parse_formatted_block(text)
        except Exception:
            continue
        title = parsed.get("title") or "Блюдо"
        portion = _num(parsed.get("portion_g"))
        kcal = _num(parsed.get("kcal"))
        p = _num(parsed.get("protein_g"))
        f = _num(parsed.get("fat_g"))
        carb = _num(parsed.get("carbs_g"))
        # Доверие убираем из краткого списка.
        try:
            dt_local = datetime.fromisoformat(created_at_iso).astimezone()
            dt_s = dt_local.strftime("%H:%M")
        except Exception:
            dt_s = created_at_iso[11:16]
        lines.append(
            f"{idx}. {dt_s} — {title} · ~{portion} г · {kcal} ккал · Б:{p} г Ж:{f} г У:{carb} г"
        )

    try:
        log.info("DAILY_DETAILS: built lines=%s for used_id=%s", len(lines), (chat_id or telegram_user_id))
    except Exception:
        pass
    return "🧾 За сегодня подробно\n" + "\n".join(lines)


async def _build_daily_details_payload(telegram_user_id: int, chat_id: int | None = None) -> tuple[str, list[dict]]:
    try:
        start, end = _msk_day_bounds_utc()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT id, created_at, bot_output FROM interactions
            WHERE chat_id=? AND created_at>=? AND created_at<?
            ORDER BY created_at ASC
            """,
            ((chat_id or telegram_user_id), start.isoformat(), end.isoformat()),
        )
        rows = c.fetchall()
        conn.close()
    except Exception:
        rows = []

    if not rows:
        return ("🧾 За сегодня подробно\nСегодня ещё нет блюд.", [])

    def _num(v):
        try:
            return int(round(float(v or 0)))
        except Exception:
            return 0

    lines = []
    entries = []
    idx = 0
    for interaction_id, created_at_iso, text in rows:
        try:
            parsed = parse_formatted_block(text)
        except Exception:
            continue
        idx += 1
        title = parsed.get("title") or "Блюдо"
        portion = _num(parsed.get("portion_g"))
        kcal = _num(parsed.get("kcal"))
        p = _num(parsed.get("protein_g"))
        f = _num(parsed.get("fat_g"))
        carb = _num(parsed.get("carbs_g"))
        try:
            dt_local = datetime.fromisoformat(created_at_iso).astimezone()
            dt_s = dt_local.strftime("%H:%M")
        except Exception:
            dt_s = created_at_iso[11:16]
        lines.append(f"{idx}. {dt_s} — {title} · ~{portion} г · {kcal} ккал · Б:{p} г Ж:{f} г У:{carb} г")
        entries.append({"idx": idx, "interaction_id": int(interaction_id)})

    if not lines:
        return ("🧾 За сегодня подробно\nСегодня ещё нет блюд.", [])
    return ("🧾 За сегодня подробно\n" + "\n".join(lines), entries)


async def _llm_regenerate_from_existing(previous_block: str, mode: str, original_hint: str) -> str:
    if mode == "text" and (original_hint or "").strip():
        return await llm_render_from_text(original_hint.strip())

    resp = client.chat.completions.create(
        model=MODEL_TEXT,
        messages=[
            {"role": "system", "content": SYSTEM_SIMPLE + "\n\n" + FORMAT_INSTRUCTIONS_RU.replace("{SOURCE}", "описанию")},
            {"role": "user", "content": "Перегенерируй разбор блюда с нуля в том же формате, без лишнего текста."},
            {"role": "user", "content": "Предыдущий блок:\n" + previous_block},
            {"role": "user", "content": f"Подсказка пользователя (если была): {(original_hint or '').strip()}"},
        ],
        temperature=1,
    )
    content = resp.choices[0].message.content.strip()
    return await ensure_fat_fiber_sections(content)

async def _delete_ingested_meal_by_message_id(telegram_user_id: int, message_id: int) -> bool:
    client_id = await _fetch_client_id(telegram_user_id)
    if not client_id:
        return False
    base = os.getenv('ADMIN_API_BASE', 'http://localhost:8000')
    api_key = os.getenv("ADMIN_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client_http:
            r = await client_http.delete(
                f"{base}/clients/{client_id}/meals/by_message/{message_id}",
                headers=headers,
            )
            return r.status_code == 200
    except Exception:
        return False


def _delete_interaction_by_id(interaction_id: int, chat_id: int) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM interactions WHERE id=? AND chat_id=?",
                (interaction_id, chat_id),
            )
            conn.commit()
            return c.rowcount > 0
    except Exception:
        return False

def _sum_local_for_period(telegram_user_id: int, start_utc: datetime, end_utc: datetime):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT bot_output FROM interactions
                WHERE chat_id=? AND created_at>=? AND created_at<?
                """,
                (telegram_user_id, start_utc.isoformat(), end_utc.isoformat()),
            )
            rows = c.fetchall()
        kcal = p = f = carb = 0.0
        for (text,) in rows:
            try:
                parsed = parse_formatted_block(text)
                kcal += float(parsed.get("kcal") or 0)
                p += float(parsed.get("protein_g") or 0)
                f += float(parsed.get("fat_g") or 0)
                carb += float(parsed.get("carbs_g") or 0)
            except Exception:
                continue
        return kcal, p, f, carb
    except Exception:
        return None

def _daily_local_summary_text(telegram_user_id: int) -> str | None:
    start, end = _msk_day_bounds_utc()
    sums = _sum_local_for_period(telegram_user_id, start, end)
    if not sums:
        return None
    kcal, p, f, carb = sums
    if kcal <= 0 and p <= 0 and f <= 0 and carb <= 0:
        return None
    return "📊 Сводка за сегодня (" + start.astimezone(MSK).date().isoformat() + ")\n" + _fmt_macros(kcal, p, f, carb)

def _weekly_local_summary_text(telegram_user_id: int) -> str | None:
    day_start_utc, _ = _msk_day_bounds_utc()
    start = day_start_utc - timedelta(days=6)
    sums = _sum_local_for_period(telegram_user_id, start, start + timedelta(days=7))
    if not sums:
        return None
    kcal, p, f, carb = sums
    if kcal <= 0 and p <= 0 and f <= 0 and carb <= 0:
        return None
    return "📆 Сводка за неделю (начало " + start.astimezone(MSK).date().isoformat() + ")\n" + _fmt_macros(kcal, p, f, carb)


def _known_chat_ids() -> list[int]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT chat_id FROM interactions WHERE chat_id IS NOT NULL")
            rows = c.fetchall()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception:
        return []


def _clip_message(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


async def send_daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    chats = _known_chat_ids()
    if not chats:
        return
    for chat_id in chats:
        try:
            summary_text = await _build_daily_text(chat_id)
            details_text, _ = await _build_daily_details_payload(chat_id, chat_id=chat_id)
            details_body = details_text.replace("🧾 За сегодня подробно\n", "", 1)
            report_text = (
                f"🌙 Итог дня ({datetime.now(MSK).date().isoformat()})\n\n"
                f"{summary_text}\n\n"
                f"🧾 Подробно:\n{details_body}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=_clip_message(report_text),
                disable_notification=True,
            )
        except Exception as e:
            log.warning("daily report send failed chat_id=%s err=%s", chat_id, e)

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    log.info("callback received data=%s chat_id=%s", data, query.message.chat_id if query.message else None)

    if data.startswith(MENU_CB_DAILY_EDIT_PREFIX):
        try:
            interaction_id = int(data.split(":", 1)[1])
        except Exception:
            interaction_id = None
        if interaction_id:
            row = get_interaction_by_id(interaction_id)
            if row and row[1] == (query.message.chat_id if query.message else None):
                bot_msg_id = row[3]
                context.user_data["force_edit_message_id"] = bot_msg_id
                try:
                    await query.answer("Режим редактирования включен")
                except Exception:
                    pass
                if query.message:
                    await query.message.reply_text(
                        "✏️ Напишите, что исправить в выбранном блюде. "
                        "Например: «это не паста, а гречка с курицей, порция 220 г»."
                    )
            else:
                try:
                    await query.answer("Блюдо не найдено", show_alert=False)
                except Exception:
                    pass
        return

    if data.startswith(MENU_CB_DAILY_REGEN_PREFIX):
        try:
            interaction_id = int(data.split(":", 1)[1])
        except Exception:
            interaction_id = None
        if not interaction_id:
            try:
                await query.answer("Некорректный запрос", show_alert=False)
            except Exception:
                pass
            return
        row = get_interaction_by_id(interaction_id)
        if not row:
            try:
                await query.answer("Блюдо не найдено", show_alert=False)
            except Exception:
                pass
            return
        _, row_chat_id, _, bot_msg_id, mode, hint, prev_block = row
        if row_chat_id != (query.message.chat_id if query.message else None):
            try:
                await query.answer("Нет доступа к этому блюду", show_alert=False)
            except Exception:
                pass
            return
        try:
            await query.answer("Перегенерирую…", show_alert=False)
        except Exception:
            pass
        try:
            new_block = await _llm_regenerate_from_existing(prev_block, mode or "text", hint or "")
        except Exception as e:
            log.exception("LLM regenerate failed", exc_info=e)
            if query.message:
                await query.message.reply_text("⚠️ Не удалось перегенерировать блюдо. Попробуйте еще раз.")
            return
        try:
            await context.bot.edit_message_text(
                chat_id=row_chat_id,
                message_id=bot_msg_id,
                text=new_block,
                reply_markup=analysis_keyboard(bot_msg_id),
            )
        except Exception:
            if query.message:
                await query.message.reply_text(new_block, reply_markup=analysis_keyboard(bot_msg_id))
        update_interaction_bot_output(bot_msg_id, new_block)
        ingest_info = _send_ingest_from_block(
            block_text=new_block,
            update=update,
            message_id=bot_msg_id,
            source_type=mode or "text",
            image_path=None,
        )
        if ingest_info and ingest_info.get("missing_macros") and query.message:
            await query.message.reply_text("⚠️ Не удалось определить БЖУ для этого блюда. Учтены только калории.")
        if query.message:
            details_text, entries = await _build_daily_details_payload(
                query.from_user.id,
                chat_id=query.message.chat_id,
            )
            await query.message.reply_text(details_text, reply_markup=menu_with_daily_actions_keyboard(entries))
        return

    if data.startswith(MENU_CB_DAILY_DELETE_PREFIX):
        try:
            interaction_id = int(data.split(":", 1)[1])
        except Exception:
            interaction_id = None
        if not interaction_id:
            try:
                await query.answer("Некорректный запрос", show_alert=False)
            except Exception:
                pass
            return
        row = get_interaction_by_id(interaction_id)
        if not row:
            try:
                await query.answer("Блюдо не найдено", show_alert=False)
            except Exception:
                pass
            return
        if row[1] != (query.message.chat_id if query.message else None):
            try:
                await query.answer("Нет доступа к этому блюду", show_alert=False)
            except Exception:
                pass
            return
        title = "Блюдо"
        try:
            parsed = parse_formatted_block(row[6] or "")
            title = parsed.get("title") or title
        except Exception:
            pass
        try:
            await query.answer("Подтвердите удаление", show_alert=False)
        except Exception:
            pass
        try:
            await query.edit_message_text(
                f"🗑️ Удалить блюдо «{title}»?\nЭто действие нельзя отменить.",
                reply_markup=menu_daily_delete_confirm_keyboard(interaction_id),
            )
        except Exception:
            if query.message:
                await query.message.reply_text(
                    f"🗑️ Удалить блюдо «{title}»?\nЭто действие нельзя отменить.",
                    reply_markup=menu_daily_delete_confirm_keyboard(interaction_id),
                )
        return

    if data.startswith(MENU_CB_DAILY_DELETE_CONFIRM_PREFIX):
        try:
            interaction_id = int(data.split(":", 1)[1])
        except Exception:
            interaction_id = None
        if not interaction_id:
            try:
                await query.answer("Некорректный запрос", show_alert=False)
            except Exception:
                pass
            return
        row = get_interaction_by_id(interaction_id)
        if not row:
            try:
                await query.answer("Блюдо уже удалено", show_alert=False)
            except Exception:
                pass
            return
        row_chat_id = row[1]
        bot_msg_id = row[3]
        if row_chat_id != (query.message.chat_id if query.message else None):
            try:
                await query.answer("Нет доступа к этому блюду", show_alert=False)
            except Exception:
                pass
            return
        deleted_local = _delete_interaction_by_id(interaction_id, row_chat_id)
        deleted_remote = await _delete_ingested_meal_by_message_id(query.from_user.id, bot_msg_id)
        try:
            if deleted_local or deleted_remote:
                await query.answer("Блюдо удалено", show_alert=False)
            else:
                await query.answer("Не удалось удалить блюдо", show_alert=False)
        except Exception:
            pass
        details_text, entries = await _build_daily_details_payload(
            query.from_user.id,
            chat_id=(query.message.chat_id if query.message else None),
        )
        if query.message:
            await query.edit_message_text(
                details_text,
                reply_markup=menu_with_daily_actions_keyboard(entries),
            )
        return

    if data.startswith(MENU_CB_EDIT_PREFIX):
        try:
            bot_msg_id = int(data.split(":", 1)[1])
        except Exception:
            bot_msg_id = None
        if bot_msg_id:
            context.user_data["force_edit_message_id"] = bot_msg_id
            try:
                await query.answer("Режим редактирования включен")
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(
                    "✏️ Напишите, что исправить в разборе. "
                    "Можно ответить реплаем на сообщение с разбором или отдельным сообщением."
                )
        return

    try:
        await query.answer("Обновляю…", show_alert=False)
    except Exception:
        pass
    try:
        post_to_chat = False
        details_markup = menu_keyboard()
        user = query.from_user
        user_label = user.full_name or str(user.id)
        if user.username:
            user_label += f" (@{user.username})"
        if data == MENU_CB_HELP:
            text = INSTRUCTION_TEXT
        elif data == MENU_CB_ABOUT:
            text = ABOUT_TEXT
        elif data == MENU_CB_DAILY:
            text = "👤 " + user_label + "\n" + await _build_daily_text(query.from_user.id)
            post_to_chat = True
        elif data == MENU_CB_WEEKLY:
            text = "👤 " + user_label + "\n" + await _build_weekly_text(query.from_user.id)
            post_to_chat = True
        elif data == MENU_CB_DAILY_DETAILS:
            text, details_entries = await _build_daily_details_payload(
                query.from_user.id,
                chat_id=(query.message.chat_id if query.message else None),
            )
            details_markup = menu_with_daily_actions_keyboard(details_entries)
        else:
            text = "Неизвестный пункт меню."
    except Exception as e:
        log.exception("error building callback response", exc_info=e)
        text = "Ошибка при получении данных. Попробуйте позже."
    try:
        if post_to_chat and query.message:
            await query.message.reply_text(text, reply_markup=menu_keyboard())
        elif data == MENU_CB_DAILY_DETAILS:
            await query.edit_message_text(text, reply_markup=details_markup)
        else:
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
    log_llm_startup_config()
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
    if app.job_queue and DAILY_REPORT_ENABLED:
        app.job_queue.run_daily(
            send_daily_report_job,
            time=time(hour=23, minute=0, tzinfo=MSK),
            name="daily_report_23_msk",
        )
        log.info("Scheduled daily report at 23:00 MSK")
    elif app.job_queue:
        log.info("Daily report schedule disabled by DAILY_REPORT_ENABLED")
    else:
        log.warning("JobQueue is unavailable; daily report schedule is disabled")
    log.info("Bot started.")
    from telegram import Update as TgUpdate
    app.run_polling(allowed_updates=TgUpdate.ALL_TYPES, close_loop=False, drop_pending_updates=False)

if __name__ == "__main__":
    main()
