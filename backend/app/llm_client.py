import httpx
from .config import OLLAMA_MAX_TOKENS, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL, OLLAMA_NUM_CTX
from .logging_config import logger

SYSTEM_PROMPT = """Ты - генератор SQL-запросов для PostgreSQL.
Тебе дана схема базы данных и вопрос пользователя на естественном языке.
Твоя задача - вернуть ОДИН корректный SELECT-запрос, отвечающий на вопрос.

Правила:
- Возвращай ТОЛЬКО SQL, без пояснений и без markdown-разметки. Ни единого
  слова до или после запроса - только сам SQL, объяснять код и писать что-то кроме кода строго запрещено.
- Разрешены только SELECT-запросы. Запрещены команды INSERT/UPDATE/DELETE/DROP/TRUNCATE.
- Используй только таблицы и колонки из предоставленной схемы.
- КРИТИЧЕСКИ ВАЖНО про имена колонок: если имя колонки в схеме содержит
  заглавные буквы, пробелы или отличается от строчных_букв_с_подчёркиванием -
  оборачивай его в двойные кавычки ТОЧНО как дано в схеме, без изменений.
  Пример: колонка "Статус Продажи" -> пиши в SQL именно "Статус Продажи" (с кавычками,
  с большой буквы, с пробелом), а НЕ статус_продажи и НЕ Статус_Продажи.
  PostgreSQL иначе автоматически приведёт имя без кавычек к нижнему регистру,
  и колонка не будет найдена.
- Если вопрос нельзя однозначно превратить в SQL по данной схеме,
  верни: SELECT 'Не удалось найти информацию под ваш запрос' AS error;
- ПРАВИЛА ИМЕНОВАНИЯ СТОЛБЦОВ (ALIASING):
1. ВСЕГДА переводите названия возвращаемых числовых столбцов (метрик) и категорий на русский язык с помощью конструкции `AS`.
2. Запрещено использовать нижние подчеркивания (_) в алиасах столбцов, предназначенных для легенды. Заменяйте их на обычные пробелы.
3. Алиас столбца должен начинаться с ЗАГЛАВНОЙ буквы.
4. Названия должны быть короткими (1-3 слова), емкими и понятными конечному пользователю.
5. Для столбца категорий (ось X) используйте понятное имя (например, "Дата", "Категория товара", "Менеджер" вместо "dt", "cat_id", "emp_name").

ПРИМЕРЫ:
НЕПРАВИЛЬНО: SELECT date as dt, sum(amount) as total_sales_usd ...
ПРАВИЛЬНО:   SELECT date as "Дата", sum(amount) as "Общие продажи USD" ...

НЕПРАВИЛЬНО: SELECT category, count(*) as cnt ...
ПРАВИЛЬНО:   SELECT category as "Категория товара", count(*) as "Количество заказов" ...  
"""


async def generate_sql(question: str, schema_context: str) -> str:
    prompt = (
        f"Схема базы данных:\n{schema_context}\n\n"
        f"Вопрос пользователя: {question}\n\n"
        f"SQL-запрос:"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.0,
            "num_predict": OLLAMA_MAX_TOKENS,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
        logger.info("Запрос к Ollama, модель=%s", OLLAMA_MODEL)
        response = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()

    logger.debug("Ollama сырой ответ целиком: %s", data)
    raw_sql = data.get("response", "").strip()
    if not raw_sql:
        logger.warning("Ollama вернула пустой SQL. Полный ответ: %s", data)

    eval_count = data.get("eval_count", 0)
    eval_duration_ns = data.get("eval_duration", 0)
    if eval_duration_ns > 0:
        tokens_per_sec = eval_count / (eval_duration_ns / 1e9)
        logger.info(
            "Ollama сгенерировала %d токенов за %.2fс (%.1f ток/сек)",
            eval_count, eval_duration_ns / 1e9, tokens_per_sec
        )

    logger.info("Ollama вернула сырой SQL: %s", raw_sql.replace("\n", " "))

    return raw_sql
