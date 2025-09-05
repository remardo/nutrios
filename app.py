import os
import io
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image
import google.generativeai as genai

# --- НАСТРОЙКИ ---
# Вставьте сюда ваши API ключи
TELEGRAM_BOT_TOKEN = "ВАШ_ТЕЛЕГРАМ_ТОКЕН"
GEMINI_API_KEY = "ВАШ_GEMINI_API_КЛЮЧ"

# Настройка логирования для отладки
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройка Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
# Используем две модели: одну для текста, другую для изображений (мультимодальную)
text_model = genai.GenerativeModel('gemini-pro')
vision_model = genai.GenerativeModel('gemini-pro-vision')

# Словарь для хранения ID сообщений, которые можно исправить
# Ключ: message_id сообщения бота, Значение: chat_id
# Это нужно, чтобы бот знал, какое сообщение редактировать
correction_data = {}


# --- ФУНКЦИИ-ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Привет, {user_name}!\n\n"
        "Я бот для распознавания еды. Отправь мне картинку или описание блюда, и я попробую его угадать.\n\n"
        "Если я ошибусь, просто ответьте на моё сообщение с правильным названием, и я исправлюсь!"
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик изображений"""
    message = update.message
    chat_id = message.chat_id
    
    # Получаем самое качественное изображение
    photo_file = await message.photo[-1].get_file()
    
    # Скачиваем изображение в виде байтов
    photo_bytes = await photo_file.download_as_bytearray()
    
    # Конвертируем байты в объект изображения PIL
    img = Image.open(io.BytesIO(photo_bytes))

    # Отправляем "заглушку" о том, что идет обработка
    processing_message = await message.reply_text("🧠 Думаю над вашим фото...")

    try:
        # Промпт для AI
        prompt = "Ты — эксперт-кулинар. Определи, какая еда изображена на картинке. Дай короткий и ясный ответ. Если не уверен, напиши предположение."
        
        # Отправляем запрос в Gemini Vision
        response = vision_model.generate_content([prompt, img])
        
        # Редактируем сообщение с результатом
        sent_message = await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message.message_id,
            text=f"🤖 Думаю, это: **{response.text.strip()}**\n\n_Если я неправ, ответьте на это сообщение с правильным названием._",
            parse_mode='Markdown'
        )
        # Сохраняем ID сообщения для возможного исправления
        correction_data[sent_message.message_id] = chat_id

    except Exception as e:
        logger.error(f"Ошибка при обработке изображения: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message.message_id,
            text="😕 Не удалось обработать изображение. Попробуйте еще раз."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик текстовых сообщений (кроме команд)"""
    message = update.message
    user_text = message.text

    # --- ЛОГИКА ИСПРАВЛЕНИЯ ---
    # Проверяем, является ли это сообщение ответом на другое сообщение
    if message.reply_to_message:
        replied_message_id = message.reply_to_message.message_id
        
        # Проверяем, было ли это сообщение отправлено нашим ботом и есть ли оно в словаре исправлений
        if message.reply_to_message.from_user.is_bot and replied_message_id in correction_data:
            await handle_correction(update, context)
            return # Прекращаем дальнейшую обработку

    # --- ЛОГИКА РАСПОЗНАВАНИЯ ЕДЫ ПО ТЕКСТУ ---
    processing_message = await message.reply_text("🤔 Анализирую описание...")
    try:
        # Промпт для AI
        prompt = f"Ты — бот-кулинар. Определи блюдо по следующему описанию. Дай короткий и ясный ответ. Описание от пользователя: '{user_text}'"
        
        response = text_model.generate_content(prompt)
        
        sent_message = await context.bot.edit_message_text(
            chat_id=message.chat_id,
            message_id=processing_message.message_id,
            text=f"🤖 По вашему описанию, это может быть: **{response.text.strip()}**\n\n_Если я неправ, ответьте на это сообщение с правильным названием._",
            parse_mode='Markdown'
        )
        # Сохраняем ID сообщения для возможного исправления
        correction_data[sent_message.message_id] = message.chat_id

    except Exception as e:
        logger.error(f"Ошибка при обработке текста: {e}")
        await context.bot.edit_message_text(
            chat_id=message.chat_id,
            message_id=processing_message.message_id,
            text="😕 Не удалось обработать ваше описание. Попробуйте еще раз."
        )

async def handle_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает исправление от пользователя"""
    replied_message = update.message.reply_to_message
    correction_text = update.message.text
    
    chat_id = replied_message.chat_id
    message_id_to_edit = replied_message.message_id
    
    try:
        # Формируем новый текст для исправленного сообщения
        new_text = f"✅ **Исправлено пользователем:**\n\n`{correction_text.strip()}`"
        
        await context.bot.edit_message_text(
            text=new_text,
            chat_id=chat_id,
            message_id=message_id_to_edit,
            parse_mode='Markdown'
        )
        
        # Удаляем сообщение из словаря, чтобы его нельзя было исправить повторно
        if message_id_to_edit in correction_data:
            del correction_data[message_id_to_edit]
            
        # Можно добавить реакцию на сообщение с исправлением
        await update.message.reply_text("Спасибо за исправление! Я запомню. 👍")

    except Exception as e:
        logger.error(f"Ошибка при исправлении сообщения: {e}")
        await update.message.reply_text("Не удалось внести исправление.")


def main() -> None:
    """Основная функция запуска бота"""
    print("Бот запускается...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_image))
    # Обработчик текста должен идти после обработчика фото
    # Исключаем команды, чтобы они не попадали в распознавание
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запускаем бота
    application.run_polling()


if __name__ == "__main__":
    main()