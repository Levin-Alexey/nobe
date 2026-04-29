import asyncpg
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv
from pgvector.asyncpg import register_vector

load_dotenv()


def _build_db_dsn_from_env() -> str:
    """Строит DSN из переменных окружения, если DB_DSN не задан."""
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")

    required = {
        "DB_HOST": host,
        "DB_NAME": db_name,
        "DB_USER": db_user,
        "DB_PASSWORD": db_password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Не заданы обязательные переменные окружения для БД: {', '.join(missing)}")

    # После проверки missing эти значения гарантированно не None.
    safe_user = str(db_user)
    safe_password = str(db_password)
    safe_host = str(host)
    safe_db_name = str(db_name)

    return (
        f"postgres://{safe_user}:{quote_plus(safe_password)}"
        f"@{safe_host}:{port}/{safe_db_name}"
    )


# Сначала пробуем готовый DSN, иначе собираем его из DB_* переменных
DB_DSN = os.getenv("DB_DSN") or _build_db_dsn_from_env()

async def get_db_pool():
    """Создает пул соединений с базой данных и регистрирует тип vector"""
    # Регистрируем векторный тип для каждого нового соединения в пуле
    async def init(conn):
        await register_vector(conn)

    # Создаем пул сразу с инициализацией vector-типа
    pool = await asyncpg.create_pool(DB_DSN, init=init)
    return pool

async def get_products_by_ids(pool: asyncpg.Pool, product_ids: list):
    """
    Получение точных данных для формирования Excel (чтобы избежать галлюцинаций LLM)
    """
    query = """
        SELECT id, articul, name, price 
        FROM products 
        WHERE id = ANY($1::int[]);
    """
    records = await pool.fetch(query, product_ids)
    return {r["id"]: dict(r) for r in records}

async def search_products_by_vector(pool: asyncpg.Pool, query_embedding: list, limit: int = 5):
    query = """
        SELECT id, articul, name, color, type_short, price, stock, catalog_url, pdf_url
        FROM products
        ORDER BY embedding <=> $1::vector
        LIMIT $2;
    """
    records = await pool.fetch(query, query_embedding, limit)
    
    results = []
    for r in records:
        results.append({
            "id": r["id"],
            "articul": r["articul"],
            "name": r["name"],
            "color": r["color"],
            "type": r["type_short"],
            "price": float(r["price"]) if r["price"] else 0.0,
            "stock": r["stock"],
            "catalog_url": r["catalog_url"],  # Добавили
            "pdf_url": r["pdf_url"]           # Добавили
        })
    return results


async def add_or_update_cart_item(
    pool: asyncpg.Pool, user_id: int, product_id: int, quantity: int
):
    """Добавляет товар в корзину или увеличивает его количество."""
    query = """
        INSERT INTO cart_items (user_id, product_id, quantity)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, product_id)
        DO UPDATE SET
            quantity = cart_items.quantity + EXCLUDED.quantity,
            added_at = CURRENT_TIMESTAMP
        RETURNING user_id, product_id, quantity, added_at;
    """
    return await pool.fetchrow(query, user_id, product_id, quantity)


async def set_cart_item_quantity(
    pool: asyncpg.Pool, user_id: int, product_id: int, quantity: int
):
    """Устанавливает точное количество товара в корзине."""
    query = """
        UPDATE cart_items
        SET quantity = $3,
            added_at = CURRENT_TIMESTAMP
        WHERE user_id = $1 AND product_id = $2
        RETURNING user_id, product_id, quantity, added_at;
    """
    return await pool.fetchrow(query, user_id, product_id, quantity)


async def get_cart_items(pool: asyncpg.Pool, user_id: int):
    """Возвращает позиции корзины пользователя вместе с данными товара."""
    query = """
        SELECT
            c.user_id,
            c.product_id,
            c.quantity,
            c.added_at,
            p.articul,
            p.name,
            p.price,
            p.stock,
            p.catalog_url,
            p.pdf_url
        FROM cart_items c
        JOIN products p ON p.id = c.product_id
        WHERE c.user_id = $1
        ORDER BY c.added_at DESC;
    """
    records = await pool.fetch(query, user_id)

    results = []
    for r in records:
        results.append(
            {
                "user_id": r["user_id"],
                "product_id": r["product_id"],
                "quantity": r["quantity"],
                "added_at": r["added_at"],
                "articul": r["articul"],
                "name": r["name"],
                "price": float(r["price"]) if r["price"] else 0.0,
                "stock": r["stock"],
                "catalog_url": r["catalog_url"],
                "pdf_url": r["pdf_url"],
            }
        )
    return results


async def remove_cart_item(pool: asyncpg.Pool, user_id: int, product_id: int):
    """Удаляет одну позицию из корзины пользователя."""
    query = """
        DELETE FROM cart_items
        WHERE user_id = $1 AND product_id = $2;
    """
    result = await pool.execute(query, user_id, product_id)
    return result


async def clear_cart(pool: asyncpg.Pool, user_id: int):
    """Очищает корзину пользователя целиком."""
    query = """
        DELETE FROM cart_items
        WHERE user_id = $1;
    """
    result = await pool.execute(query, user_id)
    return result
