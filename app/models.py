"""Request schemas.

Only strict, explicitly non-negative integers are accepted for household
counts: JSON ``3.5``, ``true`` or ``"3"`` are all rejected rather than being
silently coerced.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

MAX_DIMENSION = 8
QUARTER_PATTERN = re.compile(r"^\d{4}Q[1-4]$")

Count = Annotated[StrictInt, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CellRef(StrictModel):
    """A cell, accepted either as ``[row, col]`` or ``{"row": r, "col": c}``.

    The object form is what the suppression-advice endpoint returns, so its
    output can be fed straight back into the mask endpoint.
    """

    row: StrictInt = Field(ge=0, le=MAX_DIMENSION - 1)
    col: StrictInt = Field(ge=0, le=MAX_DIMENSION - 1)

    @model_validator(mode="before")
    @classmethod
    def _accept_pair(cls, value):
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("a cell pair must have exactly two entries")
            return {"row": value[0], "col": value[1]}
        return value


class NamedTotal(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    total: Count


class TotalsRegistration(StrictModel):
    districts: list[NamedTotal] = Field(min_length=1, max_length=MAX_DIMENSION)
    industries: list[NamedTotal] = Field(min_length=1, max_length=MAX_DIMENSION)

    @field_validator("districts", "industries")
    @classmethod
    def names_unique(cls, items: list[NamedTotal]) -> list[NamedTotal]:
        names = [item.name.strip() for item in items]
        if len(set(names)) != len(names):
            raise ValueError("names must be unique within their axis")
        for item in items:
            item.name = item.name.strip()
        return items


class DetailSubmission(StrictModel):
    values: list[list[Count]]
    must_public: list[CellRef] = Field(default_factory=list, max_length=64)
    expected_version: StrictInt | None = Field(default=None, ge=1)


class MaskSubmission(StrictModel):
    cells: list[CellRef] = Field(default_factory=list, max_length=64)
    detail_version: StrictInt | None = Field(default=None, ge=1)


class SuggestionRequest(StrictModel):
    detail_version: StrictInt | None = Field(default=None, ge=1)
    cells: list[CellRef] | None = Field(default=None, max_length=64)


class PublishRequest(StrictModel):
    detail_version: StrictInt | None = Field(default=None, ge=1)
    mask_version: StrictInt | None = Field(default=None, ge=1)


class TokenRequest(StrictModel):
    label: str = Field(min_length=1, max_length=64)
