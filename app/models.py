"""Request schemas.

Only strict, explicitly non-negative integers are accepted for household
counts: JSON ``3.5``, ``true`` or ``"3"`` are all rejected rather than being
silently coerced.
"""

from __future__ import annotations

import re
from datetime import datetime
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


# ------------------------------------------------------------- back-filled copies

class CopyRow(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    total: Count


class CopyColumn(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    total: Count


def _parse_timestamp(value: str) -> str:
    """Normalise an ISO-8601 timestamp, keeping the original instant."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("published_at must be an ISO-8601 timestamp") from exc
    return moment.isoformat(timespec="seconds")


class PublishedCopy(StrictModel):
    """One public bulletin copy, archived from the old system.

    The shape intentionally mirrors ``GET /api/v1/bulletins/{quarter}``: the
    archived JSON can be posted unchanged, except that ``published_at`` is
    mandatory (a back-fill must preserve the original publication time).
    ``cells`` is a full matrix where ``null`` marks a masked cell.
    """

    quarter: str = Field(pattern=QUARTER_PATTERN.pattern)
    districts: list[CopyRow] = Field(min_length=1, max_length=MAX_DIMENSION)
    industries: list[CopyColumn] = Field(min_length=1, max_length=MAX_DIMENSION)
    row_totals: list[Count] | None = None
    col_totals: list[Count] | None = None
    grand_total: Count | None = None
    cells: list[list[Count | None]]
    published_at: str

    @field_validator("published_at")
    @classmethod
    def timestamp_well_formed(cls, value: str) -> str:
        return _parse_timestamp(value)

    @model_validator(mode="after")
    def axes_and_cells_line_up(self) -> "PublishedCopy":
        n_rows, n_cols = len(self.districts), len(self.industries)
        names = [row.name.strip() for row in self.districts]
        if len(set(names)) != len(names):
            raise ValueError("district names must be unique")
        names = [col.name.strip() for col in self.industries]
        if len(set(names)) != len(names):
            raise ValueError("industry names must be unique")
        if len(self.cells) != n_rows:
            raise ValueError(f"cells has {len(self.cells)} rows but "
                             f"{n_rows} districts were given")
        for index, row in enumerate(self.cells):
            if len(row) != n_cols:
                raise ValueError(f"cells row {index} has {len(row)} entries "
                                 f"but {n_cols} industries were given")
        if self.row_totals is not None:
            if len(self.row_totals) != n_rows:
                raise ValueError("row_totals must have one entry per district")
            if self.row_totals != [row.total for row in self.districts]:
                raise ValueError("row_totals disagree with the district totals")
        if self.col_totals is not None:
            if len(self.col_totals) != n_cols:
                raise ValueError("col_totals must have one entry per industry")
            if self.col_totals != [col.total for col in self.industries]:
                raise ValueError("col_totals disagree with the industry totals")
        grand = sum(row.total for row in self.districts)
        if self.grand_total is not None and self.grand_total != grand:
            raise ValueError("grand_total disagrees with the district totals")
        return self


class BackfillBatch(StrictModel):
    # At most 8 copies per batch.  A quarter may appear twice: an identical
    # duplicate is de-duplicated on import, a conflicting one is rejected with
    # the quarter named.
    copies: list[PublishedCopy] = Field(min_length=1, max_length=8)
