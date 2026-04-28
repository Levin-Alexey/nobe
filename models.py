import asyncpg
from pgvector.asyncpg import register_vector

# Конфиг БД (потом вынесешь в .env)
DB_DSN = "postgres://botuser:botpass@localhost:5432/nobe_db"

async def get_db_pool():
    """Создает пул соединений с базой данных и регистрирует тип vector"""
    pool = await asyncpg.create_pool(DB_DSN)
    
    # Регистрируем векторный тип для каждого нового соединения в пуле
    async def init(conn):
        await register_vector(conn)
        
    # Применяем инициализацию ко всему пулу
    pool = await asyncpg.create_pool(DB_DSN, init=init)
    return pool

async def search_products_by_vector(pool: asyncpg.Pool, query_embedding: list, limit: int = 5):
    """
    Семантический поиск по вектору.
    Использует оператор косинусного расстояния (<=>)
    """
    query = """
        SELECT id, articul, name, color, type_short, price, stock
        FROM products
        ORDER BY embedding <=> $1::vector
        LIMIT $2;
    """
    # Выполняем запрос
    records = await pool.fetch(query, query_embedding, limit)
    
    # Упаковываем результат в удобный список словарей для ИИ
    results = []
    for r in records:
        results.append({
            "id": r["id"],
            "articul": r["articul"],
            "name": r["name"],
            "color": r["color"],
            "type": r["type_short"],
            "price": float(r["price"]) if r["price"] else 0.0,
            "stock": r["stock"]
        })
    return results

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