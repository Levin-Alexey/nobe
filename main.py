import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from models import get_db_pool
from ai_logic import process_user_message
from excel_export import generate_order_excel

load_dotenv()

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Глобальная переменная для пула БД
db_pool = None

# Временное хранилище истории диалогов (для MVP храним в памяти)
# Ключ - ID пользователя, значение - список сообщений [{"role": "...", "content": "..."}, ...]
user_histories = {}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Приветственное сообщение"""
    # Очищаем историю при старте
    user_histories[message.from_user.id] = []
    await message.answer(
        "👋 Привет! Я AI-менеджер по электроустановочным изделиям.\n\n"
        "Спроси меня про выключатели, розетки или рамки. Если нужно сформировать счет — просто скажи, какие позиции и сколько штук тебе нужно."
    )

@dp.message()
async def handle_user_message(message: types.Message):
    """Обработчик всех текстовых сообщений"""
    user_id = message.from_user.id
    user_text = message.text

    # Получаем или создаем историю для пользователя
    if user_id not in user_histories:
        user_histories[user_id] = []
        
    history = user_histories[user_id]

    # Показываем статус "печатает..." пока ИИ думает
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        # 1. Отдаем запрос в наш мозг (ai_logic.py)
        result = await process_user_message(user_id, user_text, history, db_pool)
        
        # Обновляем историю диалога
        user_histories[user_id] = result["history"]

        # 2. Обрабатываем ответ
        if result["type"] == "text":
            # Просто отправляем текст
            await message.answer(result["content"])
            
        elif result["type"] == "excel_order":
            # Сначала пишем, что файл формируется
            processing_msg = await message.answer("Секунду, формирую файл сметы... 📊")
            
            # Генерируем Excel
            filepath = await generate_order_excel(db_pool, result["items"])
            
            if filepath:
                # Отправляем файл пользователю
                doc = FSInputFile(filepath)
                await message.answer_document(document=doc, caption="Ваш заказ готов!")
                
                # Удаляем временный файл с сервера, чтобы не копился мусор
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"Не удалось удалить файл {filepath}: {e}")
            else:
                await message.answer("Не удалось сформировать смету (возможно, не найдены товары в БД).")
                
            # Удаляем сообщение "Секунду, формирую..."
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)

    except Exception as e:
        print(f"Ошибка при обработке сообщения: {e}")
        await message.answer("Произошла ошибка при обращении к базе или ИИ. Попробуйте еще раз.")

async def main():
    global db_pool
    # Создаем подключение к БД
    print("Подключение к базе данных...")
    db_pool = await get_db_pool()
    
    print("Бот запущен и готов к работе!")
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())