import requests


RACHISQL_API_URL = "https://example.com/rachisql"
RACHISQL_API_TOKEN = "example"

QUESTION = "Построй график топа 5 клиентов по сумме заказов"


def test_rachisql_api():
    url = f"{RACHISQL_API_URL}/ask/image"
    headers = {"Authorization": f"Bearer {RACHISQL_API_TOKEN}"}
    payload = {"question": QUESTION}

    print(f"Отправка запроса на: {url}")
    print(f"Тело запроса: {payload}\n")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60)

        print(f"Статус ответа: {response.status_code}")

        if response.status_code == 200:
            print("Успех! API вернуло данные.")

            with open("chart.png", "wb") as f:
                f.write(response.content)

            print(
                "График успешно сохранен в файл 'chart.png' в папке со скриптом."
            )

        else:
            print("Ошибка API!")
            try:
                print("Структура ответа (JSON):", response.json())
            except ValueError:
                print("Текст ответа:", response.text)

    except requests.exceptions.RequestException as e:
        print(f"Не удалось связаться с сервером. Ошибка: {e}")


if __name__ == "__main__":
    test_rachisql_api()