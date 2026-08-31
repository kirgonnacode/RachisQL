"""
Тестовый терминальный клиент к RachisQL API.

Использование:
    python cli_test.py "сколько заказов за последний месяц по дням"
    python cli_test.py "топ 5 клиентов по сумме заказов" --host http://localhost:8000
    python cli_test.py "..." --no-image   # только JSON, без похода в /ask/image

Быстрый способ проверить пайплайн вручную перед интеграцией
"""

import argparse
import datetime
import os
import sys
from pathlib import Path
import requests


def _print_error(resp: requests.Response) -> None:
    """
    API оборачивает ошибки в {"detail": {"detail": "...", "generated_sql": "..."}}
    (см. main.py) - specifically чтобы было видно, на каком SQL всё упало.
    """
    try:
        body = resp.json()
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            print(f"  Причина: {detail.get('detail', '(нет описания)')}")
            sql = detail.get("generated_sql")
            if sql:
                print(f"  SQL запрос вызвавший ошибку: {sql}")
            return
        print(f"  {detail}")
    except (ValueError, AttributeError):
        print(f"  {resp.text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Тестовый клиент RachisQL API")
    parser.add_argument("question", help="Вопрос на естественном языке")
    parser.add_argument("--host", default="http://localhost:8000", help="Базовый URL API")
    parser.add_argument(
        "--token",
        default=os.getenv("RachisQL_API_TOKEN"),
        help="Bearer-токен (по умолчанию берётся из RachisQL_API_TOKEN)",
    )
    parser.add_argument("--no-image", action="store_true", help="Не запрашивать PNG-график")
    parser.add_argument("--out", default=None, help="Куда сохранить PNG (по умолчанию cli_test_output/chart_<время>.png)")
    args = parser.parse_args()

    if not args.token:
        print("✗ Нет токена. Передай --token или задай RachisQL_API_TOKEN.")
        return 1

    headers = {"Authorization": f"Bearer {args.token}"}

    print(f"→ Вопрос: {args.question}")
    print(f"→ Хост:   {args.host}\n")

    # 1. Текстовый эндпоинт - смотрим SQL и сырые данные
    try:
        resp = requests.post(f"{args.host}/ask", json={"question": args.question}, headers=headers, timeout=60)
    except requests.RequestException as e:
        print(f"✗ Не удалось достучаться до {args.host}/ask: {e}")
        return 1

    if resp.status_code != 200:
        print(f"✗ /ask вернул {resp.status_code}")
        _print_error(resp)
        return 1

    try:
        data = resp.json()
    except ValueError:
        print(f"✗ /ask вернул 200, но тело не парсится как JSON: {resp.text[:200]}")
        return 1

    print("✓ Сгенерированный SQL:")
    print(f"  {data.get('generated_sql', '(нет в ответе)')}\n")

    rows = data.get("rows", [])
    row_count = data.get("row_count", len(rows))
    print(f"✓ Строк получено: {row_count}")
    for row in rows[:10]:
        print(f"  {row}")
    if row_count > 10:
        print(f"  ... ещё {row_count - 10} строк")

    if args.no_image:
        return 0

    # 2. PNG эндпоинт
    print("\n→ Запрашиваю график (/ask/image)...")
    try:
        img_resp = requests.post(f"{args.host}/ask/image", json={"question": args.question}, headers=headers, timeout=60)
    except requests.RequestException as e:
        print(f"✗ Не удалось достучаться до {args.host}/ask/image: {e}")
        return 1

    if img_resp.status_code != 200:
        print(f"✗ /ask/image вернул {img_resp.status_code}")
        _print_error(img_resp)
        return 1

    if args.out:
        out_path = Path(args.out)
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("cli_test_output") / f"chart_{timestamp}.png"    

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(img_resp.content)
    print(f"✓ График сохранён: {out_path.resolve()} ({len(img_resp.content)} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
