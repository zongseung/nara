from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.legacy_matching import ComparisonItem, norm_company
from core.matching.normalization import material_profile


def now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class JobRequest(Model):
    company: str = Field(default='', max_length=120)
    keyword: str = Field(default='', max_length=200)
    limit: int = Field(default=0, ge=0, le=10000, strict=True)
    top: int = Field(default=3, ge=1, le=10, strict=True)
    exact_price: bool = False
    same_direction: bool = False
    fallback_band: float = Field(default=0, ge=0, le=1)

    @model_validator(mode='after')
    def valid_price_mode(self) -> 'JobRequest':
        if self.company and not norm_company(self.company):
            raise ValueError('업체명을 입력하세요')
        if self.fallback_band and not self.exact_price:
            raise ValueError('가격대 재검색은 동일단가에서만 사용합니다')
        return self


class Revision(Model):
    revision: int = Field(ge=0, strict=True)


class SupportInput(Revision):
    count: int = Field(ge=0, le=100, strict=True)
    note: str = Field(min_length=3, max_length=1000)


class SelectionInput(Revision):
    excluded: list[Annotated[int, Field(strict=True, ge=0)]] = Field(max_length=10)


class ResearchConditions(Model):
    material: str = Field(default='', max_length=120)

    @model_validator(mode='after')
    def valid_material(self) -> 'ResearchConditions':
        if self.material and not any(material_profile(self.material).families):
            raise ValueError('재질 명칭을 입력하세요')
        return self


class ResearchInput(Revision):
    replace_item_id: str = Field(pattern=r'^\d{8}$')
    conditions: ResearchConditions = Field(default_factory=ResearchConditions)


class SupportReview(Model):
    count: int
    note: str
    recorded_at: str = Field(default_factory=now)


class ReviewItem(Model):
    index: int
    company: str
    reference: ComparisonItem
    candidates: list[ComparisonItem] = Field(default_factory=list)
    excluded: list[int] = Field(default_factory=list)
    support_review: SupportReview | None = None
    rejected_ids: list[str] = Field(default_factory=list)
    research_operation_id: str | None = None


class Job(Model):
    id: str = Field(default_factory=lambda: str(uuid4()))
    request: JobRequest
    status: Literal['queued', 'running', 'review', 'exported', 'failed', 'cancelled'] = 'queued'
    operation: Literal['collect', 'rematch', 'research', 'export'] = 'collect'
    research: ResearchInput | None = None
    operation_id: str = Field(default_factory=lambda: str(uuid4()))
    item_index: int | None = None
    revision: int = 0
    total: int = 0
    completed: int = 0
    stage: str = 'queued'
    message: str = '작업 대기 중'
    error: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    uploaded: bool = False
    partial: bool = False
    cancel_requested: bool = False
    workbook: str | None = None
    flow_run_id: str | None = None


class Event(Model):
    at: str = Field(default_factory=now)
    stage: str
    message: str
    item_index: int | None = None
