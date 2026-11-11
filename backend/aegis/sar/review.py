"""The operator's decision on a heat-source candidate (no heavy dependencies)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class ReviewRequest(BaseModel):
    decision: ReviewStatus
    note: str = Field(default="", max_length=500)
