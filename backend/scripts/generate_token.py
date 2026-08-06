"""
Генератор Bearer-токенов для RachisQL API.

Использование:
    python scripts/generate_token.py integration
    python scripts/generate_token.py another_integration
    
"""

import hashlib
import secrets
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("Использование: python generate_token.py <label>")
        print("Пример: python generate_token.py my_app")
        return 1

    label = sys.argv[1].strip()
    if not label or ":" in label:
        print("label не может быть пустым или содержать двоеточие")
        return 1

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    print("Токен готов\n")
    print(f"1. Отдай этот токен потребителю '{label}' (он вставит его как Bearer-токен):")
    print(f"   {raw_token}\n")
    print(f"2. Добавь эту строку в backend/tokens.txt на своем сервере:")
    print(f"   {label}:{token_hash}\n")
    print("3. Перезапусти backend, чтобы токен заработал:")
    print("   docker compose restart backend\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
