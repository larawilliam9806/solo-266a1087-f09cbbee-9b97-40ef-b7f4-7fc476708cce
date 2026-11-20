"""Business rules for registering, masking and publishing a quarter.

Lifecycle of one quarter:

1. ``register_totals`` fixes the district and industry labels and their
   published totals.  This is immutable afterwards (published numbers may not
   be quietly rewritten).
2. ``submit_detail`` stores a new immutable detail version, validating that its
   margins match the registered totals and that no 1-4 household cell is put on
   the must-public list.
3. ``put_mask`` stores the editor's masking for the *current* detail version.
   A mask is bound to a detail version, so editing the detail implicitly
   invalidates the old masking plan (it can never be reused by accident).
4. ``publish`` re-runs the full validation at the moment of publication and
   stores an immutable snapshot.  A failed publish leaves the previous
   bulletin untouched.

Only one bulletin per quarter: the first successful publish wins, everything
afterwards is a 409 conflict.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import suppression
from .db import transaction
from .errors import ApiError, conflict, not_found
from .models import (
    DetailSubmission,
    MaskSubmission,
    PublishRequest,
    TotalsRegistration,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cells(raw: Iterable[Any]) -> set[tuple[int, int]]:
    """Normalise cell references (pairs or ``{row, col}`` objects)."""
    out: set[tuple[int, int]] = set()
    for item in raw:
        if isinstance(item, (list, tuple)):
            r, c = item
        else:
            r, c = item.row, item.col
        out.add((int(r), int(c)))
    return out


def validate_dimensions(values: Sequence[Sequence[int]], n_rows: int,
                        n_cols: int) -> None:
    if len(values) != n_rows:
        raise ApiError(422, "dimension_mismatch",
                       f"detail has {len(values)} rows but {n_rows} districts "
                       f"were published", {"expected_rows": n_rows,
                                           "got_rows": len(values)})
    for index, row in enumerate(values):
        if len(row) != n_cols:
            raise ApiError(422, "dimension_mismatch",
                           f"detail row {index} has {len(row)} columns but "
                           f"{n_cols} industries were published",
                           {"row": index, "expected_cols": n_cols,
                            "got_cols": len(row)})


def validate_totals(values: Sequence[Sequence[int]], row_totals: Sequence[int],
                    col_totals: Sequence[int]) -> list[dict]:
    """Return every margin mismatch, identifying the offending row/column."""
    issues: list[dict] = []
    for i, (row, expected) in enumerate(zip(values, row_totals)):
        actual = sum(row)
        if actual != expected:
            issues.append({"axis": "row", "index": i, "expected": expected,
                           "actual": actual, "delta": actual - expected})
    for j, expected in enumerate(col_totals):
        actual = sum(values[i][j] for i in range(len(values)))
        if actual != expected:
            issues.append({"axis": "column", "index": j, "expected": expected,
                           "actual": actual, "delta": actual - expected})
    return issues


def totals_mismatch(issues: list[dict]) -> ApiError:
    bad_rows = [i["index"] for i in issues if i["axis"] == "row"]
    bad_cols = [i["index"] for i in issues if i["axis"] == "column"]
    parts = []
    if bad_rows:
        parts.append("rows " + ", ".join(str(i) for i in bad_rows))
    if bad_cols:
        parts.append("columns " + ", ".join(str(i) for i in bad_cols))
    return ApiError(
        422, "totals_mismatch",
        "detail does not add up to the published totals: " + "; ".join(parts),
        {"rows": bad_rows, "columns": bad_cols, "mismatches": issues,
         "note": "the previous valid detail version is kept unchanged"},
    )


class BulletinService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------ setup

    def ensure_editor_token(self) -> str | None:
        """Create a bootstrap token on an empty database and return it once."""
        from .db import connect

        conn = connect(self.db_path)
        try:
            with transaction(conn):
                # Only a completely empty token table triggers the bootstrap,
                # so revoking every token cannot silently mint a new one.
                count = conn.execute(
                    "SELECT COUNT(*) FROM editor_tokens"
                ).fetchone()[0]
                if count:
                    return None
                token = "ed_" + secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO editor_tokens(label, token_hash, created_at) "
                    "VALUES(?,?,?)",
                    ("bootstrap", hash_token(token), now()),
                )
                return token
        finally:
            conn.close()

    def create_token(self, label: str) -> tuple[int, str]:
        from .db import connect

        token = "ed_" + secrets.token_urlsafe(32)
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "INSERT INTO editor_tokens(label, token_hash, created_at) "
                "VALUES(?,?,?)",
                (label, hash_token(token), now()),
            )
            return int(cur.lastrowid), token
        finally:
            conn.close()

    def token_is_valid(self, token: str | None) -> bool:
        from .db import connect

        if not token:
            return False
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM editor_tokens WHERE token_hash=? AND revoked_at IS NULL",
                (hash_token(token),),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # -------------------------------------------------------------- utilities

    def _quarter_row(self, conn, quarter: str):
        row = conn.execute("SELECT * FROM quarters WHERE quarter=?",
                           (quarter,)).fetchone()
        if row is None:
            raise not_found("quarter", quarter)
        return row

    @staticmethod
    def _detail_row(conn, quarter: str, version: int):
        return conn.execute(
            "SELECT * FROM detail_versions WHERE quarter=? AND version=?",
            (quarter, version),
        ).fetchone()

    @staticmethod
    def _latest_detail(conn, quarter: str):
        return conn.execute(
            "SELECT * FROM detail_versions WHERE quarter=? "
            "ORDER BY version DESC LIMIT 1",
            (quarter,),
        ).fetchone()

    @staticmethod
    def _bulletin_row(conn, quarter: str):
        return conn.execute("SELECT * FROM bulletins WHERE quarter=?",
                            (quarter,)).fetchone()

    @staticmethod
    def _mask_row(conn, quarter: str, detail_version: int):
        return conn.execute(
            "SELECT * FROM mask_plans WHERE quarter=? AND detail_version=?",
            (quarter, detail_version),
        ).fetchone()

    @staticmethod
    def _cell_list(cells) -> list[dict]:
        return [{"row": r, "col": c} for r, c in sorted(cells)]

    # -------------------------------------------------------------- register

    def register_totals(self, quarter: str, body: TotalsRegistration,
                        token_id: int | None) -> dict[str, Any]:
        from .db import connect

        districts = body.districts
        industries = body.industries
        row_totals = [d.total for d in districts]
        col_totals = [i.total for i in industries]
        if sum(row_totals) != sum(col_totals):
            raise ApiError(
                422,
                "totals_inconsistent",
                "the sum of district totals differs from the sum of industry "
                "totals",
                {
                    "district_sum": sum(row_totals),
                    "industry_sum": sum(col_totals),
                    "delta": sum(row_totals) - sum(col_totals),
                },
            )

        registered_at = now()
        conn = connect(self.db_path)
        try:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT quarter FROM quarters WHERE quarter=?", (quarter,)
                ).fetchone()
                if existing is not None:
                    raise conflict(
                        "totals_already_registered",
                        f"totals for {quarter} are already registered and fixed",
                        {"quarter": quarter},
                    )
                conn.execute(
                    "INSERT INTO quarters(quarter, district_labels, "
                    "industry_labels, row_totals, col_totals, registered_at, "
                    "registered_by) VALUES(?,?,?,?,?,?,?)",
                    (
                        quarter,
                        json.dumps([d.name for d in districts]),
                        json.dumps([i.name for i in industries]),
                        json.dumps(row_totals),
                        json.dumps(col_totals),
                        registered_at,
                        token_id,
                    ),
                )
        finally:
            conn.close()

        try:
            bulletin = self.get_public(quarter)
        except ApiError:
            bulletin = None
        return {
            "quarter": quarter,
            "districts": [d.name for d in districts],
            "industries": [i.name for i in industries],
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": sum(row_totals),
            "registered_at": registered_at,
            "bulletin": bulletin,
            "note": "published totals are now fixed for this quarter",
        }

    # ----------------------------------------------------------------- detail

    def submit_detail(self, quarter: str, body: DetailSubmission,
                      token_id: int | None) -> dict[str, Any]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            with transaction(conn):
                q = self._quarter_row(conn, quarter)
                row_totals = json.loads(q["row_totals"])
                col_totals = json.loads(q["col_totals"])
                validate_dimensions(body.values, len(row_totals), len(col_totals))

                mismatches = validate_totals(body.values, row_totals, col_totals)
                if mismatches:
                    raise totals_mismatch(mismatches)

                latest = self._latest_detail(conn, quarter)
                current_version = latest["version"] if latest else 0
                if body.expected_version is not None and \
                        body.expected_version != current_version:
                    raise conflict(
                        "stale_detail_version",
                        "detail was modified by someone else; reload and retry",
                        {"expected_version": body.expected_version,
                         "current_version": current_version,
                         "latest_valid_detail_version": current_version},
                    )

                public = _cells(body.must_public)
                n_rows, n_cols = len(row_totals), len(col_totals)
                for r, c in public:
                    if not (0 <= r < n_rows and 0 <= c < n_cols):
                        raise ApiError(422, "cell_out_of_range",
                                       f"must_public cell ({r},{c}) is outside "
                                       f"the {n_rows}x{n_cols} table",
                                       {"row": r, "col": c})
                sensitive_public = sorted(
                    (r, c) for r, c in public if 1 <= body.values[r][c] <= 4
                )
                if sensitive_public:
                    raise conflict(
                        "sensitive_cell_must_be_masked",
                        "cells with 1-4 households cannot be required public",
                        {"cells": self._cell_list(sensitive_public)},
                    )

                new_version = current_version + 1
                conn.execute(
                    "INSERT INTO detail_versions(quarter, version, values_json, "
                    "must_public, created_at, created_by) VALUES(?,?,?,?,?,?)",
                    (
                        quarter,
                        new_version,
                        json.dumps(body.values),
                        json.dumps(sorted([r, c] for r, c in public)),
                        now(),
                        token_id,
                    ),
                )
                # The mask is bound to its detail version, so the previous
                # plan is now superseded and can never be published again. It
                # is kept so the rejection can name it explicitly.
                previous_mask = (self._mask_row(conn, quarter, current_version)
                                 if current_version else None)
        finally:
            conn.close()

        state = self.get_admin_state(quarter)
        state["detail_version"] = new_version
        state["invalidated_mask_version"] = (
            previous_mask["mask_version"] if previous_mask is not None else None
        )
        return state

    # ------------------------------------------------------------------- mask

    def put_mask(self, quarter: str, body: MaskSubmission,
                 token_id: int | None) -> dict[str, Any]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            with transaction(conn):
                self._quarter_row(conn, quarter)
                latest = self._latest_detail(conn, quarter)
                if latest is None:
                    raise ApiError(409, "detail_required",
                                   "submit a detail version before masking")
                if body.detail_version is not None and \
                        body.detail_version != latest["version"]:
                    raise conflict(
                        "stale_detail_version",
                        "the mask targets an outdated detail version",
                        {"submitted_for": body.detail_version,
                         "current_detail_version": latest["version"]},
                    )

                values = json.loads(latest["values_json"])
                must_public = _cells(json.loads(latest["must_public"]))
                n_rows, n_cols = len(values), len(values[0])
                cells = _cells(body.cells)
                for r, c in cells:
                    if not (0 <= r < n_rows and 0 <= c < n_cols):
                        raise ApiError(422, "cell_out_of_range",
                                       f"mask cell ({r},{c}) is outside the "
                                       f"{n_rows}x{n_cols} table",
                                       {"row": r, "col": c})

                issues = []
                for r, c in sorted(cells & must_public):
                    issues.append({"kind": "must_public_cell_masked",
                                   "row": r, "col": c,
                                   "message": "cell is required to be public"})
                for r, c in sorted(suppression.sensitive_cells(values) - cells):
                    if (r, c) in must_public:
                        continue
                    issues.append({
                        "kind": "sensitive_cell_visible", "row": r, "col": c,
                        "value": values[r][c],
                        "message": "cells with 1-4 households must be masked",
                    })
                if issues:
                    raise conflict(
                        "mask_conflict",
                        "the masking conflicts with the published rules",
                        {
                            "cells": self._cell_list(
                                {(i["row"], i["col"]) for i in issues}
                            ),
                            "issues": issues,
                        },
                    )

                analysis = suppression.analyze(values, cells, must_public)
                previous = self._mask_row(conn, quarter, latest["version"])
                mask_version = (previous["mask_version"] + 1) if previous else 1
                conn.execute(
                    "INSERT INTO mask_plans(quarter, detail_version, "
                    "mask_version, cells_json, updated_at, updated_by) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(quarter, detail_version) "
                    "DO UPDATE SET mask_version=excluded.mask_version, "
                    "cells_json=excluded.cells_json, "
                    "updated_at=excluded.updated_at, "
                    "updated_by=excluded.updated_by",
                    (
                        quarter, latest["version"], mask_version,
                        json.dumps(sorted([r, c] for r, c in cells)),
                        now(), token_id,
                    ),
                )
        finally:
            conn.close()

        state = self.get_admin_state(quarter)
        state["mask_version"] = mask_version
        state["analysis"] = analysis.as_dict()
        state["preview"] = [issue.as_dict() for issue in analysis.issues]
        return state

    # ------------------------------------------------------------- suggestion

    def suggest(self, quarter: str, detail_version: int | None,
                cells: list[list[int]] | None) -> dict[str, Any]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            self._quarter_row(conn, quarter)
            latest = self._latest_detail(conn, quarter)
            if latest is None:
                raise ApiError(409, "detail_required",
                               "submit a detail version before requesting "
                               "suppression advice")
            if detail_version is not None and detail_version != latest["version"]:
                raise conflict(
                    "stale_detail_version",
                    "suppression advice is only available for the current "
                    "detail version",
                    {"requested": detail_version,
                     "current_detail_version": latest["version"]},
                )
            values = json.loads(latest["values_json"])
            must_public = _cells(json.loads(latest["must_public"]))
            if cells is None:
                stored = self._mask_row(conn, quarter, latest["version"])
                base = (_cells(json.loads(stored["cells_json"]))
                        if stored else suppression.default_mask(values, must_public))
            else:
                base = _cells(cells)
                n_rows, n_cols = len(values), len(values[0])
                for r, c in base:
                    if not (0 <= r < n_rows and 0 <= c < n_cols):
                        raise ApiError(422, "cell_out_of_range",
                                       f"cell ({r},{c}) is outside the "
                                       f"{n_rows}x{n_cols} table",
                                       {"row": r, "col": c})
        finally:
            conn.close()

        suggestion = suppression.suggest(values, base, must_public)
        result = suggestion.as_dict()
        result.update({"quarter": quarter,
                       "detail_version": latest["version"]})
        return result

    # ---------------------------------------------------------------- publish

    def publish(self, quarter: str, body: PublishRequest,
                token_id: int | None) -> dict[str, Any]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            with transaction(conn):
                q = self._quarter_row(conn, quarter)
                existing = self._bulletin_row(conn, quarter)
                if existing is not None:
                    raise conflict(
                        "already_published",
                        f"{quarter} already has a published bulletin; a quarter "
                        f"is published exactly once",
                        {"quarter": quarter,
                         "published_at": existing["published_at"],
                         "detail_version": existing["detail_version"]},
                    )
                latest = self._latest_detail(conn, quarter)
                if latest is None:
                    raise ApiError(409, "detail_required",
                                   "submit a detail version before publishing")
                if body.detail_version is not None and \
                        body.detail_version != latest["version"]:
                    raise conflict(
                        "stale_detail_version",
                        "refusing to publish an outdated detail version",
                        {"submitted_for": body.detail_version,
                         "current_detail_version": latest["version"]},
                    )
                mask_row = self._mask_row(conn, quarter, latest["version"])
                if mask_row is None:
                    older = conn.execute(
                        "SELECT mask_version, detail_version FROM mask_plans "
                        "WHERE quarter=? AND detail_version<? "
                        "ORDER BY detail_version DESC LIMIT 1",
                        (quarter, latest["version"]),
                    ).fetchone()
                    if older is not None:
                        raise conflict(
                            "mask_plan_superseded",
                            "the masking plan belongs to an outdated detail "
                            "version and cannot be published",
                            {
                                "superseded_mask_version": older["mask_version"],
                                "superseded_detail_version":
                                    older["detail_version"],
                                "current_detail_version": latest["version"],
                                "hint": "review the mask again, then publish",
                            },
                        )
                    raise conflict(
                        "mask_required",
                        "no masking plan exists for the current detail version",
                        {"detail_version": latest["version"]},
                    )
                if body.mask_version is not None and \
                        body.mask_version != mask_row["mask_version"]:
                    raise conflict(
                        "stale_mask_version",
                        "the masking plan changed since it was reviewed",
                        {"submitted_for": body.mask_version,
                         "current_mask_version": mask_row["mask_version"]},
                    )

                values = json.loads(latest["values_json"])
                must_public = _cells(json.loads(latest["must_public"]))
                masked = _cells(json.loads(mask_row["cells_json"]))
                row_totals = json.loads(q["row_totals"])
                col_totals = json.loads(q["col_totals"])

                validation = suppression.analyze(values, masked, must_public)
                if not validation.valid:
                    conflict_cells = [
                        {"row": r, "col": c} for r, c in validation.conflict_cells
                    ]
                    raise conflict(
                        "mask_conflict",
                        "publication blocked: the masking does not protect every "
                        "masked cell (or conflicts with the must-public list)",
                        {
                            "cells": conflict_cells,
                            "issues": [i.as_dict() for i in validation.issues],
                            "detail_version": latest["version"],
                            "mask_version": mask_row["mask_version"],
                            "hint": "call the suppression-advice endpoint to "
                                    "obtain supplementary cells to mask",
                        },
                    )

                payload = self._build_payload(
                    quarter, q, values, row_totals, col_totals, masked,
                )
                try:
                    conn.execute(
                        "INSERT INTO bulletins(quarter, detail_version, "
                        "mask_version, payload_json, published_at, published_by) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            quarter,
                            latest["version"],
                            mask_row["mask_version"],
                            json.dumps(payload),
                            now(),
                            token_id,
                        ),
                    )
                except Exception as exc:  # UNIQUE(quarter) guard for races
                    if "UNIQUE" in str(exc).upper():
                        raise conflict(
                            "already_published",
                            f"{quarter} was published concurrently by another "
                            f"editor",
                            {"quarter": quarter},
                        ) from exc
                    raise
        finally:
            conn.close()

        return payload

    @staticmethod
    def _build_payload(quarter, q, values, row_totals, col_totals,
                       masked) -> dict[str, Any]:
        districts = json.loads(q["district_labels"])
        industries = json.loads(q["industry_labels"])
        visible = [
            [None if (i, j) in masked else values[i][j]
             for j in range(len(industries))]
            for i in range(len(districts))
        ]
        return {
            "quarter": quarter,
            "districts": [
                {"name": name, "total": row_totals[i]}
                for i, name in enumerate(districts)
            ],
            "industries": [
                {"name": name, "total": col_totals[j]}
                for j, name in enumerate(industries)
            ],
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": sum(row_totals),
            # Only published totals and disclosed cells leave the server.
            # ``None`` marks a cell that is not disclosed.
            "cells": visible,
            "published_at": now(),
        }

    # ----------------------------------------------------------------- reads

    def get_public(self, quarter: str) -> dict[str, Any]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            row = self._bulletin_row(conn, quarter)
            if row is None:
                raise not_found("bulletin", quarter)
            return json.loads(row["payload_json"])
        finally:
            conn.close()

    def list_public(self) -> list[dict[str, Any]]:
        from .db import connect

        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT quarter, payload_json FROM bulletins ORDER BY quarter"
            ).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            out.append({
                "quarter": row["quarter"],
                "grand_total": payload["grand_total"],
                "published_at": payload["published_at"],
            })
        return out

    def get_admin_state(self, quarter: str) -> dict[str, Any]:
        """Editor-only view: versions plus validation of the current mask."""
        from .db import connect

        conn = connect(self.db_path)
        try:
            q = self._quarter_row(conn, quarter)
            latest = self._latest_detail(conn, quarter)
            mask_row = (self._mask_row(conn, quarter, latest["version"])
                        if latest else None)
            bulletin = self._bulletin_row(conn, quarter)
            versions = conn.execute(
                "SELECT version, created_at FROM detail_versions "
                "WHERE quarter=? ORDER BY version",
                (quarter,),
            ).fetchall()
        finally:
            conn.close()

        state: dict[str, Any] = {
            "quarter": quarter,
            "districts": json.loads(q["district_labels"]),
            "industries": json.loads(q["industry_labels"]),
            "row_totals": json.loads(q["row_totals"]),
            "col_totals": json.loads(q["col_totals"]),
            "detail_versions": [
                {"version": row["version"], "created_at": row["created_at"]}
                for row in versions
            ],
            "current_detail_version": latest["version"] if latest else None,
            "mask_version": mask_row["mask_version"] if mask_row else None,
            "masked_cells": (
                [{"row": r, "col": c}
                 for r, c in json.loads(mask_row["cells_json"])]
                if mask_row else []
            ),
            "published": bulletin is not None,
            "published_at": bulletin["published_at"] if bulletin else None,
            "bulletin_detail_version": (
                bulletin["detail_version"] if bulletin else None
            ),
        }
        if latest and mask_row:
            values = json.loads(latest["values_json"])
            must_public = _cells(json.loads(latest["must_public"]))
            masked = _cells(json.loads(mask_row["cells_json"]))
            state["analysis"] = suppression.analyze(
                values, masked, must_public).as_dict()
        return state

    # ------------------------------------------------------------------ auth

    def auth(self, token: str | None) -> int:
        from .db import connect

        if not token:
            raise ApiError(401, "token_required",
                           "an editor token is required",
                           {"header": "Authorization: Bearer <token>"})
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM editor_tokens WHERE token_hash=? "
                "AND revoked_at IS NULL",
                (hash_token(token),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ApiError(403, "invalid_token", "the editor token is not valid")
        return int(row["id"])
