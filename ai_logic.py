import json
import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
from models import search_products_by_vector

load_dotenv()

# Конфиг (лучше вынести в .env)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_TOKEN", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")  # Можно переопределить через .env

if not OPENROUTER_API_KEY:
    raise RuntimeError("Не задан OPENROUTER_API_KEY (или OPENROUTER_TOKEN) в .env")

# Инициализируем асинхронный клиент
client = AsyncOpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
)

# Описание инструментов для LLM
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": "Поиск товаров в базе по запросу клиента (например: 'белые выключатели', 'розетка с usb'). Возвращает список товаров с их ID, ценами и остатками.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {
                        "type": "string",
                        "description": "Смысловой запрос для поиска по каталогу"
                    }
                },
                "required": ["search_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_excel_order",
            "description": "Вызывай эту функцию, ТОЛЬКО когда клиент четко указал нужный товар и его количество, и просит сформировать заказ/смету/файл.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Список товаров для добавления в заказ",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer", "description": "ID товара из базы данных"},
                                "quantity": {"type": "integer", "description": "Количество единиц товара"}
                            },
                            "required": ["id", "quantity"]
                        }
                    }
                },
                "required": ["items"]
            }
        }
    }
]

SYSTEM_PROMPT = """Ты — умный менеджер по продажам электроустановочных изделий (бренд Arlight и аналоги). 
Твоя задача — консультировать клиентов, подбирать им выключатели, розетки и рамки, и формировать заказы.
ПРАВИЛА:
1. Никогда не выдумывай цены, артикулы или наличие. Всегда используй функцию search_catalog.
2. Если клиент готов к заказу (например: "мне нужно 5 таких"), вызывай функцию create_excel_order.
3. Общайся вежливо, по-деловому, но кратко.
Сокращения которые может написать пользователь:
ВЫКЛ - выключатель
РОЗ - розетка
РАМ - рамка
ЦВЕТ - цвет (например: белый, черный, серый)
ТИП - тип (например: сенсорный, с подсветкой, с usb)
Перекл - переключатель
Звонк - звонок,механизм звонкового выклюателя
"""


def assistant_message_to_dict(message) -> dict:
    """Готовит assistant-сообщение для безопасного повторного запроса."""
    payload = {
        "role": message.role,
        "content": message.content or "",
    }

    if message.tool_calls:
        payload["tool_calls"] = [
            tool_call.model_dump() for tool_call in message.tool_calls
        ]

    return payload


async def get_embedding(text: str) -> list:
    """Асинхронное получение вектора для текста"""
    response = await client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding


async def process_user_message(
    user_id: int, user_message: str, message_history: list, db_pool
) -> dict:
    """
    Основная логика общения.
    message_history - список предыдущих сообщений [{"role": "user", "content": "..."}, ...]
    Возвращает dict с результатом: текст для отправки юзеру ИЛИ данные для генерации Excel.
    """
    # Добавляем системный промпт, если история пустая
    if not message_history:
        message_history.append({"role": "system", "content": SYSTEM_PROMPT})

    message_history.append({"role": "user", "content": user_message})

    # Отправляем запрос в LLM
    response = await client.chat.completions.create(
        model=MODEL,
        messages=message_history,
        tools=TOOLS,
        tool_choice="auto"
    )

    response_message = response.choices[0].message
    message_history.append(assistant_message_to_dict(response_message))

    # Если ИИ решил вызвать функцию (Tool Calling)
    if response_message.tool_calls:
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)

            if function_name == "search_catalog":
                # 1. Получаем вектор из запроса
                query_vector = await get_embedding(function_args["search_query"])
                # 2. Ищем в нашей векторной БД
                db_results = await search_products_by_vector(
                    db_pool, query_vector, limit=5
                )

                # 3. Отправляем результат обратно в LLM
                message_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": json.dumps(db_results, ensure_ascii=False)
                })

                # 4. Просим LLM переварить данные из базы и ответить юзеру
                second_response = await client.chat.completions.create(
                    model=MODEL,
                    messages=message_history
                )
                final_msg = second_response.choices[0].message
                message_history.append(assistant_message_to_dict(final_msg))
                return {
                    "type": "text",
                    "content": final_msg.content or "",
                    "history": message_history,
                }

            elif function_name == "create_excel_order":
                # ИИ решил, что пора формировать заказ!
                # Мы не отправляем это обратно в ИИ, мы отдаем команду нашему Telegram-боту
                items_to_order = function_args["items"]
                return {
                    "type": "excel_order",
                    "items": items_to_order,
                    "history": message_history,
                    "content": "Секунду, формирую файл сметы..."
                }

    # Если ИИ ответил просто текстом (без вызова функций)
    return {
        "type": "text",
        "content": response_message.content or "",
        "history": message_history,
    }
