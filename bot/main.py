# bot/main.py
# Simple Telegram food recognition bot with formatted RU output + Admin API ingestion
# Added features:
#   /menu — интерактивное меню с кнопками:
#       📖 Инструкция — как пользоваться
#       ℹ️ О боте — краткое описание
#       📊 За сегодня — суммарные калории и БЖУ за текущий день
#       📆 За неделю — суммарные калории и БЖУ за последнюю неделю (агрегация)
#   Использует эндпоинты Admin API (/clients, /summary/daily, /summary/weekly)
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

import os, json, base64, sqlite3, logging, re
from datetime import datetime, timezone, date, timedelta
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
)
import httpx

# --- Local modules for Admin integration ---
from parse_block import parse_formatted_block          # bot/parse_block.py
from ingest_client import (
    ingest_meal,
    upsert_daily_metrics_for_user,
    post_event_for_user,
)                  # bot/ingest_client.py

# ------------- ENV / CONFIG -------------
# Try loading from repo root and bot/ folder
if os.path.exists(os.path.join(os.path.dirname(__file__), "..", ".env")):
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
else:
    load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL_VISION = os.getenv("OPENAI_VISION_MODEL", "gpt-5")
MODEL_TEXT   = os.getenv("OPENAI_TEXT_MODEL",   "gpt-5")

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
        messages=[{"role":"user","content": user_parts}]
    )
    content = resp.choices[0].message.content.strip()
    content = await ensure_fat_fiber_sections(content)
    return content

async def llm_render_from_text(text: str) -> str:
    prompt = SYSTEM_SIMPLE + "\n\n" + FORMAT_INSTRUCTIONS_RU.replace("{SOURCE}", "описанию") + "\nОписание: " + text
    resp = client.chat_completions.create(  # fallback for SDK variations
        model=MODEL_TEXT,
        messages=[{"role":"user","content": prompt}]
    ) if hasattr(client, "chat_completions") else client.chat.completions.create(
        model=MODEL_TEXT,
        messages=[{"role":"user","content": prompt}]
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
        ]
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
            "extras": parsed.get("extras", {}),
            "source_type": source_type,
            "image_path": image_path,
            "message_id": message_id
        })
    except Exception as e:
        log.exception("Failed to ingest meal", exc_info=e)

# Ensure sections for detailed fats, omega, fiber are present; if missing, ask LLM to revise-insert them.
async def ensure_fat_fiber_sections(block: str) -> str:
    needs_fats = ("Жиры подробно:" not in block)
    needs_omega = ("Омега:" not in block)
    needs_fiber = ("Клетчатка:" not in block)
    if not (needs_fats or needs_omega or needs_fiber):
        return block
    try:
        missing_list = ", ".join([
            s for s, cond in [("жиры подробно", needs_fats),("омега", needs_omega),("клетчатка", needs_fiber)] if cond
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
                {"role":"system","content": revise_system},
                {"role":"user","content": "Текущий блок:\n" + block},
                {"role":"user","content": user_req},
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


async def water_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    ok = upsert_daily_metrics_for_user(update.effective_user.id, {"water_goal_met": True})
    if ok:
        await msg.reply_text("Отмечено: дневная норма воды выполнена ✅")
    else:
        await msg.reply_text("Не удалось обновить отметку по воде. Попробуйте чуть позже.")


async def steps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    steps_value: Optional[int] = None
    if context and getattr(context, "args", None):
        try:
            steps_value = int(context.args[0]) if context.args else None
        except (TypeError, ValueError):
            steps_value = None
    if steps_value is None:
        text = msg.text or ""
        matches = re.findall(r"\d+", text)
        if matches:
            try:
                steps_value = int(matches[0])
            except ValueError:
                steps_value = None
    if steps_value is None or steps_value < 0:
        await msg.reply_text("Укажите количество шагов, например: /steps 8500")
        return
    ok = upsert_daily_metrics_for_user(update.effective_user.id, {"steps": steps_value})
    if ok:
        await msg.reply_text(f"Записано: {steps_value} шагов за сегодня.")
    else:
        await msg.reply_text("Не удалось сохранить шаги. Попробуйте позже.")


async def dinner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    ok = upsert_daily_metrics_for_user(update.effective_user.id, {"dinner_logged": True})
    if ok:
        await msg.reply_text("Ужин отмечен ✅")
    else:
        await msg.reply_text("Не удалось отметить ужин. Попробуйте позже.")


async def new_recipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    ok = upsert_daily_metrics_for_user(update.effective_user.id, {"new_recipe_logged": True})
    if ok:
        await msg.reply_text("Отлично! Новый рецепт добавлен в дневные отметки.")
    else:
        await msg.reply_text("Не удалось сохранить отметку. Попробуйте позже.")

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
        await update.message.reply_text("Не удалось распознать блюдо по фото. Попробуйте ещё раз или пришлите другое изображение.")
        return

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
                try:
                    post_event_for_user(
                        update.effective_user.id,
                        {
                            "type": "portion_adjusted",
                            "payload": {
                                "message_id": bot_msg_id,
                                "note": text,
                            },
                        },
                    )
                except Exception:
                    pass
                return

    # Fresh text identification
    try:
        block = await llm_render_from_text(text)
    except Exception as e:
        log.exception("LLM text render failed", exc_info=e)
        await update.message.reply_text("Не удалось распознать блюдо по описанию. Попробуйте переформулировать.")
        return
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
    try:
        user_id = msg.from_user.id if msg.from_user else update.effective_user.id
        post_event_for_user(
            user_id,
            {
                "type": "portion_adjusted",
                "payload": {
                    "message_id": bot_msg_id,
                    "note": msg.text.strip(),
                },
            },
        )
    except Exception:
        pass

async def finalize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок — просто ответьте реплаем, если нужно исправить детали. Команда финализации не требуется 😊")

# ------------- MENU / STATS -------------
MENU_CB_HELP = "MENU_HELP"
MENU_CB_ABOUT = "MENU_ABOUT"
MENU_CB_DAILY = "MENU_DAILY"
MENU_CB_WEEKLY = "MENU_WEEKLY"
MENU_CB_DAILY_DETAILS = "MENU_DAILY_DETAILS"

def menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Инструкция", callback_data=MENU_CB_HELP),
            InlineKeyboardButton("ℹ️ О боте", callback_data=MENU_CB_ABOUT)
        ],
        [
            InlineKeyboardButton("📊 За сегодня", callback_data=MENU_CB_DAILY),
            InlineKeyboardButton("📆 За неделю", callback_data=MENU_CB_WEEKLY)
        ],
        [
            InlineKeyboardButton("🧾 За сегодня подробно", callback_data=MENU_CB_DAILY_DETAILS)
        ]
    ])

INSTRUCTION_TEXT = (
    "📖 Инструкция\n"
    "1. Пришлите фото блюда — бот вернёт разбор с калориями и БЖУ.\n"
    "2. Можно описать блюдо текстом.\n"
    "3. Уточнения: сообщение со словами ‘добавь’, ‘убери’, ‘без’, ‘ещё/еще’, ‘поменяй’, или ответ реплаем на мой блок.\n"
    "4. /menu — показать это меню.\n"
    "5. Сводки: кнопки ‘За сегодня’, ‘За неделю’ и ‘За сегодня подробно’.\n"
    "6. Быстрые отметки: /water, /steps <число>, /dinner, /newrecipe."
)

ABOUT_TEXT = (
    "ℹ️ О боте\n"
    "Nutrios — бот, который распознаёт блюда по фото или описанию и даёт приблизительную оценку калорий, БЖУ и ключевых микроэлементов."
)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/menu invoked chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await update.message.reply_text("Меню:", reply_markup=menu_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context)

async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_daily_text(update.effective_user.id)
    await update.message.reply_text(text)

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _build_weekly_text(update.effective_user.id)
    await update.message.reply_text(text)

async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        d = update.to_dict(); keys = list(d.keys())
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
    today_iso = date.today().isoformat()
    # найти запись, где period_start == today
    row_today = None
    for r in data:
        if r.get("period_start", "").startswith(today_iso):
            row_today = r; break
    if not row_today:
        # fallback — последняя
        row_today = data[-1]
    return "📊 Сводка за сегодня (" + row_today.get("period_start", '')[:10] + ")\n" + _fmt_macros(row_today.get("kcal"), row_today.get("protein_g"), row_today.get("fat_g"), row_today.get("carbs_g"))

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

async def _build_daily_details_text(telegram_user_id: int, chat_id: int | None = None) -> str:
    """Краткий список блюд за сегодня: номер, дата/время, название, калории и БЖУ, порция и доверие."""
    try:
        now = datetime.now(timezone.utc)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
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
        # доверие убираем из вывода
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

def _sum_local_for_period(telegram_user_id: int, start_utc: datetime, end_utc: datetime):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT bot_output FROM interactions
            WHERE chat_id=? AND created_at>=? AND created_at<?
            """,
            (telegram_user_id, start_utc.isoformat(), end_utc.isoformat()),
        )
        rows = c.fetchall()
        conn.close()
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
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    sums = _sum_local_for_period(telegram_user_id, start, start + timedelta(days=1))
    if not sums:
        return None
    kcal, p, f, carb = sums
    if kcal <= 0 and p <= 0 and f <= 0 and carb <= 0:
        return None
    return "📊 Сводка за сегодня (" + start.date().isoformat() + ")\n" + _fmt_macros(kcal, p, f, carb)

def _weekly_local_summary_text(telegram_user_id: int) -> str | None:
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=6)
    sums = _sum_local_for_period(telegram_user_id, start, start + timedelta(days=7))
    if not sums:
        return None
    kcal, p, f, carb = sums
    if kcal <= 0 and p <= 0 and f <= 0 and carb <= 0:
        return None
    return "📆 Сводка за неделю (начало " + start.date().isoformat() + ")\n" + _fmt_macros(kcal, p, f, carb)
    # берём последнюю (самая свежая неделя)
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
        elif data == MENU_CB_DAILY_DETAILS:
            text = await _build_daily_details_text(query.from_user.id, chat_id=(query.message.chat_id if query.message else None))
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
    app.add_handler(CommandHandler("water", water_command))
    app.add_handler(CommandHandler("steps", steps_command))
    app.add_handler(CommandHandler("dinner", dinner_command))
    app.add_handler(CommandHandler("newrecipe", new_recipe_command))
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
