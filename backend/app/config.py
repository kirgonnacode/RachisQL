import os

from dotenv import load_dotenv

load_dotenv()

# --- Postgres ---
POSTGRES_HOST = os.getenv("POSTGRES_HOST")
POSTGRES_PORT = os.getenv("POSTGRES_PORT")
POSTGRES_DB = os.getenv("POSTGRES_DB")
POSTGRES_USER = os.getenv("POSTGRES_USER")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD")
POSTGRES_POOL_MIN_SIZE = os.getenv("POSTGRES_POOL_MIN_SIZE", "2")   # готовые соединения в памяти
POSTGRES_POOL_MAX_SIZE = os.getenv("POSTGRES_POOL_MAX_SIZE", "10")  # пиковый лимит соединений под нагрузкой

# --- Ollama ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_TIMEOUT_SECONDS = os.getenv("OLLAMA_TIMEOUT_SECONDS", "60")
OLLAMA_MAX_TOKENS = os.getenv("OLLAMA_MAX_TOKENS", "500")

# --- Guardrails ---
MAX_ROWS = os.getenv("MAX_ROWS", "500")
QUERY_TIMEOUT_SECONDS = os.getenv("QUERY_TIMEOUT_SECONDS", "15")

# --- Chart rendering ---
CHART_RENDERER_URL = os.getenv("CHART_RENDERER_URL", "http://chart_renderer:3000")

# --- Wren (опционален - если не настроен, main.py сам уходит в fallback на Postgres) ---
WREN_PROJECT_DIR = os.getenv("WREN_PROJECT_DIR", "/app/wren")
WREN_TIMEOUT_SECONDS = os.getenv("WREN_TIMEOUT_SECONDS", "20")
WREN_CONNECTION_INFO = os.getenv("WREN_CONNECTION_INFO")    # не обязателен, см. wren_client.py

# --- Аутентификация ---
TOKENS_FILE = os.getenv("TOKENS_FILE", "/app/tokens.txt")
RATE_LIMIT_PER_MINUTE = os.getenv("RATE_LIMIT_PER_MINUTE", "20")


def _validate() -> None:
    errors: list[str] = []

    # Проверка что обязательные строки не пустые
    required_strings = {
        "POSTGRES_HOST": POSTGRES_HOST,
        "POSTGRES_DB": POSTGRES_DB,
        "POSTGRES_USER": POSTGRES_USER,
        "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    }
    for name, value in required_strings.items():
        if not value or not value.strip():
            errors.append(f"{name} не задан или пустой")

    # Проверка что числа это действительно числа
    required_ints = {
        "POSTGRES_PORT": POSTGRES_PORT,
        "POSTGRES_POOL_MIN_SIZE": POSTGRES_POOL_MIN_SIZE,
        "POSTGRES_POOL_MAX_SIZE": POSTGRES_POOL_MAX_SIZE,
        "OLLAMA_TIMEOUT_SECONDS": OLLAMA_TIMEOUT_SECONDS,
        "OLLAMA_MAX_TOKENS": OLLAMA_MAX_TOKENS,
        "MAX_ROWS": MAX_ROWS,
        "QUERY_TIMEOUT_SECONDS": QUERY_TIMEOUT_SECONDS,
        "WREN_TIMEOUT_SECONDS": WREN_TIMEOUT_SECONDS,
        "RATE_LIMIT_PER_MINUTE": RATE_LIMIT_PER_MINUTE,
    }
    for name, value in required_ints.items():
        if not value or not str(value).strip():
            errors.append(f"{name} не задан или пустой")
            continue
        if not str(value).strip().isdigit():
            errors.append(f"{name} должен быть числом, получили: '{value}'")

    # Проверка формата URL-полей
    if not OLLAMA_URL.startswith("http"):
        errors.append(f"OLLAMA_URL должен начинаться с http, получили: '{OLLAMA_URL}'")
    if not CHART_RENDERER_URL.startswith("http"):
        errors.append(f"CHART_RENDERER_URL должен начинаться с http, получили: '{CHART_RENDERER_URL}'")

    if errors:
        validate_msg = "\n".join(f"  - {e}" for e in errors)
        raise EnvironmentError(
            f"\n\nОшибка конфигурации, backend не может запуститься!\n"
            f"Проверь файл .env:\n\n{validate_msg}\n"
        )


_validate()


POSTGRES_PORT = int(POSTGRES_PORT)
POSTGRES_POOL_MIN_SIZE = int(POSTGRES_POOL_MIN_SIZE)
POSTGRES_POOL_MAX_SIZE = int(POSTGRES_POOL_MAX_SIZE)
OLLAMA_TIMEOUT_SECONDS = int(OLLAMA_TIMEOUT_SECONDS)
OLLAMA_MAX_TOKENS = int(OLLAMA_MAX_TOKENS)
MAX_ROWS = int(MAX_ROWS)
QUERY_TIMEOUT_SECONDS = int(QUERY_TIMEOUT_SECONDS)
WREN_TIMEOUT_SECONDS = int(WREN_TIMEOUT_SECONDS)
RATE_LIMIT_PER_MINUTE = int(RATE_LIMIT_PER_MINUTE)

DSN = (
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
