import json
import os
import re
from openai import AsyncOpenAI
from openai import BadRequestError
from dotenv import load_dotenv
from models import (
    add_or_update_cart_item,
    get_cart_items,
    remove_cart_item,
    search_products_by_vector,
)

load_dotenv()

# Конфиг (лучше вынести в .env)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_TOKEN", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")  # Можно переопределить через .env
EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")

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
            "name": "add_to_cart",
            "description": "Добавляет выбранные клиентом товары в корзину. Вызывай, когда клиент просит добавить/берет товар.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Список товаров для добавления в корзину",
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
    },
    {
        "type": "function",
        "function": {
            "name": "show_cart",
            "description": "Показать текущую корзину пользователя с товарами и количеством.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Удаляет одну позицию из корзины по ID товара.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "ID товара для удаления из корзины"
                    }
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_excel_order",
            "description": "Сформировать итоговое коммерческое предложение в Excel по текущей корзине пользователя.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

SYSTEM_PROMPT = """Ты — умный менеджер по продажам электроустановочных изделий (бренд Arlight и аналоги). 
Твоя задача — консультировать клиентов, подбирать им выключатели, розетки и рамки, и формировать заказы.
ПРАВИЛА:
1. Никогда не выдумывай цены, артикулы, ссылки или наличие. Всегда используй функцию search_catalog.
2. Если клиент выбрал позицию и количество (например: "добавь 3 шт"), вызывай функцию add_to_cart.
3. Если клиент просит оформить/посчитать/прислать КП по тому, что уже добавлял ранее, вызывай функцию create_excel_order без параметров.
4. Корзина накапливается между сообщениями пользователя: добавляй позиции по мере диалога.
5. Если пользователь просит показать корзину, вызывай функцию show_cart.
6. Если пользователь просит удалить товар из корзины, вызывай функцию remove_from_cart.
7. Общайся вежливо, по-деловому, но кратко.
Сокращения которые может написать пользователь:
ВЫКЛ - выключатель
РОЗ - розетка
РАМ - рамка
ЦВЕТ - цвет (например: белый, черный, серый)
ТИП - тип (например: сенсорный, с подсветкой, с usb)
Перекл - переключатель
Звонк - звонок,механизм звонкового выклюателя
ПРАВИЛА РАБОТЫ СО ССЫЛКАМИ:
1. Если пользователь просит ссылку на товар или хочет "посмотреть на сайте", дай ему ссылку из поля 'catalog_url'.
2. Если пользователь просит инструкцию, паспорт или PDF, дай ему ссылку из поля 'pdf_url'.
3. Ссылки оформляй красиво, например: <a href="ССЫЛКА">Инструкция (PDF)</a>.
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


def format_clickable_links(text: str) -> str:
    """Преобразует обычные URL в HTML-ссылки для Telegram ParseMode.HTML."""
    if not text:
        return ""

    # Не трогаем уже размеченные ссылки <a href="...">...</a>
    anchor_pattern = re.compile(r"<a\s+href=\"[^\"]+\">.*?</a>", re.IGNORECASE | re.DOTALL)
    url_pattern = re.compile(r"(?<![\"'=])(https?://[^\s<>)]+)")

    parts = []
    last_end = 0

    for anchor_match in anchor_pattern.finditer(text):
        prefix = text[last_end:anchor_match.start()]
        prefix = url_pattern.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', prefix)
        parts.append(prefix)
        parts.append(anchor_match.group(0))
        last_end = anchor_match.end()

    tail = text[last_end:]
    tail = url_pattern.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', tail)
    parts.append(tail)

    return "".join(parts)


async def get_embedding(text: str) -> list:
    """Асинхронное получение вектора для текста"""
    try:
        response = await client.embeddings.create(
            input=text,
            model=EMBEDDING_MODEL,
            encoding_format="float",
        )
        return response.data[0].embedding
    except BadRequestError as e:
        print(f"Embedding 400 BadRequest: {e}")
        if getattr(e, "response", None) is not None:
            print(f"Embedding provider response: {e.response.text}")
        raise


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
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=message_history,
            tools=TOOLS,
            tool_choice="auto"
        )
    except BadRequestError as e:
        print(f"Chat 400 BadRequest: {e}")
        if getattr(e, "response", None) is not None:
            print(f"Chat provider response: {e.response.text}")
        raise

    response_message = response.choices[0].message
    message_history.append(assistant_message_to_dict(response_message))

    # Если ИИ решил вызвать функцию (Tool Calling)
    if response_message.tool_calls:
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            raw_args = tool_call.function.arguments or "{}"
            function_args = json.loads(raw_args)

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
                try:
                    second_response = await client.chat.completions.create(
                        model=MODEL,
                        messages=message_history
                    )
                except BadRequestError as e:
                    print(f"Second chat 400 BadRequest: {e}")
                    if getattr(e, "response", None) is not None:
                        print(f"Second chat provider response: {e.response.text}")
                    raise
                final_msg = second_response.choices[0].message
                message_history.append(assistant_message_to_dict(final_msg))
                return {
                    "type": "text",
                    "content": format_clickable_links(final_msg.content or ""),
                    "history": message_history,
                }

            elif function_name == "add_to_cart":
                items_to_add = function_args.get("items", [])
                added_items = 0

                for item in items_to_add:
                    product_id = int(item["id"])
                    quantity = int(item["quantity"])
                    if quantity <= 0:
                        continue
                    await add_or_update_cart_item(db_pool, user_id, product_id, quantity)
                    added_items += 1

                message_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "added_items": added_items,
                        },
                        ensure_ascii=False,
                    )
                })

                try:
                    second_response = await client.chat.completions.create(
                        model=MODEL,
                        messages=message_history
                    )
                except BadRequestError as e:
                    print(f"Second chat 400 BadRequest: {e}")
                    if getattr(e, "response", None) is not None:
                        print(f"Second chat provider response: {e.response.text}")
                    raise

                final_msg = second_response.choices[0].message
                message_history.append(assistant_message_to_dict(final_msg))
                return {
                    "type": "text",
                    "content": format_clickable_links(final_msg.content or ""),
                    "history": message_history,
                }

            elif function_name == "show_cart":
                cart_items = await get_cart_items(db_pool, user_id)
                message_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "items": cart_items,
                            "items_count": len(cart_items),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                })

                try:
                    second_response = await client.chat.completions.create(
                        model=MODEL,
                        messages=message_history
                    )
                except BadRequestError as e:
                    print(f"Second chat 400 BadRequest: {e}")
                    if getattr(e, "response", None) is not None:
                        print(f"Second chat provider response: {e.response.text}")
                    raise

                final_msg = second_response.choices[0].message
                message_history.append(assistant_message_to_dict(final_msg))
                return {
                    "type": "text",
                    "content": format_clickable_links(final_msg.content or ""),
                    "history": message_history,
                }

            elif function_name == "remove_from_cart":
                product_id = int(function_args["product_id"])
                db_result = await remove_cart_item(db_pool, user_id, product_id)

                message_history.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "product_id": product_id,
                            "db_result": db_result,
                        },
                        ensure_ascii=False,
                    )
                })

                try:
                    second_response = await client.chat.completions.create(
                        model=MODEL,
                        messages=message_history
                    )
                except BadRequestError as e:
                    print(f"Second chat 400 BadRequest: {e}")
                    if getattr(e, "response", None) is not None:
                        print(f"Second chat provider response: {e.response.text}")
                    raise

                final_msg = second_response.choices[0].message
                message_history.append(assistant_message_to_dict(final_msg))
                return {
                    "type": "text",
                    "content": format_clickable_links(final_msg.content or ""),
                    "history": message_history,
                }

            elif function_name == "create_excel_order":
                return {
                    "type": "excel_order",
                    "history": message_history,
                    "content": "Секунду, формирую итоговое КП..."
                }

    # Если ИИ ответил просто текстом (без вызова функций)
    return {
        "type": "text",
        "content": format_clickable_links(response_message.content or ""),
        "history": message_history,
    }
