
RachisQL_API_URL = os.getenv("RachisQL_API_URL")
RachisQL_API_TOKEN = os.getenv("RachisQL_API_TOKEN")


def _extract_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            return detail.get("detail", str(detail))
        return str(detail)
    except ValueError:
        return response.text or f"HTTP {response.status_code}"


@dp.message_created(Command("ask"))
async def handle_ask(event: MessageCreated) -> None:
    message = event.message
    full_text = (message.body.text or "").strip()

    question = full_text.split(maxsplit=1)[1] if " " in full_text else ""
    if not question:
        await message.answer("Напишите ваш запрос после команды /ask, например:\n/ask топ 5 клиентов по сумме заказов")
        return

    async with message.typing():
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{RachisQL_API_URL}/ask/image",
                    json={"question": question},
                    headers={"Authorization": f"Bearer {RachisQL_API_TOKEN}"},
                )
        except httpx.HTTPError as e:
            await message.answer(f"Не удалось связаться с RachisQL API: {e}")
            return

    if response.status_code != 200:
        message_text = _extract_error_message(response)
        await message.answer(f"Не получилось построить график:\n{message_text}")
        return

    png_bytes = response.content

    await message.answer(
        text=f"Вопрос: {question}",
        attachments=[InputMediaBuffer(buffer=png_bytes, filename="chart.png")],
    )
