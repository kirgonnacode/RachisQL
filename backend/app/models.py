
from typing import Any, Literal
from pydantic import BaseModel, Field
from datetime import date


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        examples=["сколько заказов за последний месяц по дням"],
    )


class AnswerPeriod(BaseModel):
    model: str
    label: str | None = None
    status: Literal["outside", "partial", "within_bounds"]
    requested_from: date
    requested_to: date
    available_from: date
    available_to: date
    message: str | None = None    


class AskResponse(BaseModel):
    question: str
    generated_sql: str
    rows: list[dict[str, Any]]
    row_count: int
    query_id: str
    period: AnswerPeriod | None = None
    reported_period: str | None = None    


class ErrorResponse(BaseModel):
    detail: str
    generated_sql: str | None = None
    query_id: str | None = None
    

class FeedbackRequest(BaseModel):
    query_id: str = Field(..., min_length=1, max_length=64)
    rating: Literal["up", "down"]    