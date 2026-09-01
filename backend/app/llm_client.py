import httpx
from .config import OLLAMA_MAX_TOKENS, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS, OLLAMA_URL
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
        "options": {
            "temperature": 0.0,
            "num_predict": OLLAMA_MAX_TOKENS,
        },
    }

    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_SECONDS) as client:
        logger.info("Запрос к Ollama, модель=%s", OLLAMA_MODEL)
        response = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        response.raise_for_status()
        data = response.json()

    raw_sql = data.get("response", "").strip()
    logger.info("Ollama вернула сырой SQL: %s", raw_sql.replace("\n", " "))
    return raw_sql
