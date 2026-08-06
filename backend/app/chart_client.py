import httpx
from .config import CHART_RENDERER_URL
from .logging_config import logger


class ChartRenderError(Exception):
    pass


async def render_png(option: dict, width: int = 800, height: int = 500) -> bytes:
    payload = {"option": option, "width": width, "height": height}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post(f"{CHART_RENDERER_URL}/render", json=payload)
            response.raise_for_status()
        except TypeError as e:
            logger.error("Payload для chart_renderer не сериализуется в JSON: %s", e)
            raise ChartRenderError(f"Данные графика не удалось сериализовать: {e}")
        except httpx.HTTPError as e:
            logger.error("chart_renderer недоступен: %s", e)
            raise ChartRenderError(str(e))
        return response.content
