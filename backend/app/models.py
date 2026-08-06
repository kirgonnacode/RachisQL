
from typing import Any
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        examples=["сколько заказов за последний месяц по дням"],
    )


class AskResponse(BaseModel):
    question: str
    generated_sql: str
    rows: list[dict[str, Any]]
    row_count: int


class ErrorResponse(BaseModel):
    detail: str
    generated_sql: str | None = None