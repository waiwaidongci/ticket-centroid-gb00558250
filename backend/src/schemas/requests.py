from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class CorrectionEntry(Request):
    ticket_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)

class CorrectionRequest(Request):
    review_no: str = Field(min_length=1, max_length=80)
    entries: list[CorrectionEntry] = Field(min_length=1, max_length=500)
    note: str = Field(default='', max_length=500)

class WithdrawalRequest(Request):
    note: str = Field(default='', max_length=500)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False
