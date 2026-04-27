import psycopg2
from pgvector.psycopg2 import register_vector
from openai import OpenAI
import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


DB_HOST = get_required_env("DB_HOST")
DB_PORT = get_required_env("DB_PORT")
DB_NAME = get_required_env("DB_NAME")
DB_USER = get_required_env("DB_USER")
DB_PASSWORD = get_required_env("DB_PASSWORD")
OPENROUTER_API_KEY = get_required_env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL")
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME")

DEFAULT_HEADERS = {}
if OPENROUTER_SITE_URL:
    DEFAULT_HEADERS["HTTP-Referer"] = OPENROUTER_SITE_URL
if OPENROUTER_SITE_NAME:
    DEFAULT_HEADERS["X-OpenRouter-Title"] = OPENROUTER_SITE_NAME

client = OpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
    default_headers=DEFAULT_HEADERS,
)

def get_embedding(text):
    response = client.embeddings.create(
        input=text,
        model="openai/text-embedding-3-small",
        encoding_format="float",
    )
    return response.data[0].embedding

def main():
    print("Подключение к БД...")
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    cur = conn.cursor()
    register_vector(conn)

    # Ищем все товары, у которых еще нет вектора
    cur.execute("SELECT id, name, color, type_short FROM products WHERE embedding IS NULL;")
    rows = cur.fetchall()

    if not rows:
        print("Векторы генерировать не для чего. У всех товаров уже есть embedding.")
        return

    print(f"Найдено {len(rows)} товаров без вектора. Начинаем генерацию...")

    for row in rows:
        product_id = row[0]
        name = row[1] or ""
        color = row[2] or ""
        type_short = row[3] or ""

        # Склеиваем текст для ИИ
        text_for_embedding = f"Название: {name}. Цвет: {color}. Тип: {type_short}."
        
        try:
            # Получаем вектор
            embedding = get_embedding(text_for_embedding)

            # Обновляем строку в базе, добавляя только вектор
            cur.execute("""
                UPDATE products 
                SET embedding = %s 
                WHERE id = %s;
            """, (embedding, product_id))
            
            print(f"ID {product_id}: Вектор успешно сгенерирован и добавлен.")
            
        except Exception as e:
            print(f"Ошибка при обработке ID {product_id}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print("Готово! Все векторы на месте.")

if __name__ == "__main__":
    main()