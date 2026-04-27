import pandas as pd
import psycopg2
from pgvector.psycopg2 import register_vector
from openai import OpenAI
import math

# --- Настройки ---
DB_DSN = "dbname=nobe_db user=botuser password=botpass host=localhost"
OPENROUTER_API_KEY = "ТВОЙ_КЛЮЧ_OPENROUTER"
CSV_FILE = "base.csv"

# Инициализируем клиент OpenAI, перенаправив его на OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

def get_embedding(text):
    # Используем недорогую и быструю модель для векторов
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

def clean_val(val):
    """Вспомогательная функция для обработки пустых значений (NaN) из pandas"""
    if pd.isna(val):
        return ""
    return str(val).strip()

def main():
    print("Подключение к БД...")
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    register_vector(conn)

    print(f"Чтение файла {CSV_FILE}...")
    # Читаем CSV. Если разделитель запятая - оставляем так.
    df = pd.read_csv(CSV_FILE)
    
    # Убираем лишние пробелы из названий колонок
    df.columns = df.columns.str.strip()

    print(f"Найдено {len(df)} товаров. Начинаем векторизацию...")

    for index, row in df.iterrows():
        try:
            # Парсим данные из колонок
            articul = clean_val(row['Наш Артикул Arlight'])
            name = clean_val(row['Наименование Arlight'])
            color = clean_val(row['Цвет Arlight'])
            type_short = clean_val(row['Тип мехнизама коротко'])
            
            # Обработка цены (может быть пустой)
            price_raw = row['Цена Рубли']
            price = float(price_raw) if not pd.isna(price_raw) else 0.0
            
            # Обработка остатков
            stock_raw = row['Наличие на складе']
            stock = int(stock_raw) if not pd.isna(stock_raw) else 0
            
            catalog_url = clean_val(row['Страница каталога'])
            pdf_url = clean_val(row['Инструкция'])
            comp_donel = clean_val(row['Артикул нашего конкурента Donel'])
            comp_voltum = clean_val(row['Артикул нашего конкурента Voltum'])

            # Пропускаем пустые строки, если вдруг затесались
            if not articul:
                continue

            # Склеиваем текст для ИИ: чем больше важных деталей, тем лучше он будет искать
            text_for_embedding = f"Название: {name}. Цвет: {color}. Тип: {type_short}."
            
            # Получаем вектор
            embedding = get_embedding(text_for_embedding)

            # Сохраняем в базу
            cur.execute("""
                INSERT INTO products (
                    articul, name, color, type_short, price, stock, 
                    catalog_url, pdf_url, competitor_donel, competitor_voltum, embedding
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (articul) DO UPDATE 
                SET price = EXCLUDED.price, 
                    stock = EXCLUDED.stock,
                    competitor_donel = EXCLUDED.competitor_donel,
                    competitor_voltum = EXCLUDED.competitor_voltum;
            """, (
                articul, name, color, type_short, price, stock, 
                catalog_url, pdf_url, comp_donel, comp_voltum, embedding
            ))
            
            print(f"[{index+1}/{len(df)}] Успешно добавлен/обновлен: {articul} ({type_short} {color})")
            
        except Exception as e:
            print(f"Ошибка на строке {index+1} (артикул {row.get('Наш Артикул Arlight')}): {e}")

    conn.commit()
    cur.close()
    conn.close()
    print("Импорт завершен! Данные и векторы в базе.")

if __name__ == "__main__":
    main()