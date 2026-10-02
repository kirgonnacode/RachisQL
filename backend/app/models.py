
from typing import Any, Literal
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
    query_id: str


class ErrorResponse(BaseModel):
    detail: str
    generated_sql: str | None = None
    query_id: str | None = None
    

class FeedbackRequest(BaseModel):
    query_id: str = Field(..., min_length=1, max_length=64)
    rating: Literal["up", "down"]    