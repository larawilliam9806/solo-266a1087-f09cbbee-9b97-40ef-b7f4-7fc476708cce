"""End-to-end tests for the bulletin API."""

from __future__ import annotations

import itertools

import pytest

from app import suppression

Q = "2024Q1"

DISTRICTS = ["东城区", "西城区", "南城区", "北城区"]
INDUSTRIES = ["制造业", "建筑业", "批发零售"]

VALUES = [
    [30, 50, 40],
    [25, 35, 35],
    [10, 20, 30],
    [45, 3, 2],
]
ROW_TOTALS = [sum(r) for r in VALUES]
COL_TOTALS = [sum(VALUES[i][j] for i in range(len(VALUES)))
              for j in range(len(VALUES[0]))]


def register(client, quarter=Q, row_totals=ROW_TOTALS, col_totals=COL_TOTALS):
    body = {
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, row_totals)],
        "industries": [{"name": n, "total": t}
                       for n, t in zip(INDUSTRIES, col_totals)],
    }
    return client.put(f"/api/v1/editor/quarters/{quarter}/totals", json=body)


def submit(client, values=VALUES, must_public=(), quarter=Q, **extra):
    body = {"values": values, "must_public": [list(c) for c in must_public]}
    body.update(extra)
    return client.post(f"/api/v1/editor/quarters/{quarter}/detail", json=body)


def mask(client, cells, quarter=Q, **extra):
    body = {"cells": [list(c) for c in cells]}
    body.update(extra)
    return client.put(f"/api/v1/editor/quarters/{quarter}/mask", json=body)


def publish(client, quarter=Q, **extra):
    return client.post(f"/api/v1/editor/quarters/{quarter}/publish",
                       json=extra or {})


# --------------------------------------------------------------------- totals

def test_register_totals_fixes_rows_and_columns(client):
    response = register(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["row_totals"] == ROW_TOTALS
    assert body["col_totals"] == COL_TOTALS
    assert body["grand_total"] == sum(ROW_TOTALS)

    again = register(client)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "totals_already_registered"


def test_register_rejects_inconsistent_grand_total(client):
    body = {
        "districts": [{"name": "A", "total": 10}, {"name": "B", "total": 5}],
        "industries": [{"name": "X", "total": 10}, {"name": "Y", "total": 4}],
    }
    response = client.put(f"/api/v1/editor/quarters/{Q}/totals", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "totals_inconsistent"


# --------------------------------------------------------------------- detail

def test_detail_reports_mismatched_row_and_column(client):
    register(client)
    bad = [row[:] for row in VALUES]
    bad[1][0] += 1          # row 1 becomes too large by 1
    bad[3][2] += 2          # column 2 becomes +2
    response = submit(client, bad)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "totals_mismatch"
    assert error["details"]["rows"] == [1, 3]
    assert error["details"]["columns"] == [0, 2]
    cells = {(m["axis"], m["index"]) for m in error["details"]["mismatches"]}
    assert cells == {("row", 1), ("row", 3), ("column", 0), ("column", 2)}


def test_failed_detail_keeps_previous_valid_version(client):
    register(client)
    assert submit(client).status_code == 200
    bad = [row[:] for row in VALUES]
    bad[0][0] += 5
    assert submit(client, bad).status_code == 422
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["current_detail_version"] == 1
    assert len(state["detail_versions"]) == 1


def test_detail_requires_non_negative_integers(client):
    register(client)
    bad = [row[:] for row in VALUES]
    bad[0][0] = -1
    bad[0][1] = 55  # keep the row sum
    assert submit(client, bad).status_code == 422

    fractional = [row[:] for row in VALUES]
    fractional[0][1] = 49.5
    fractional[0][2] = 40.5
    assert submit(client, fractional).status_code == 422


def test_validation_errors_use_the_common_envelope(client):
    register(client)
    body = {"values": [row[:] for row in VALUES]}
    body["values"][0][0] = 3.5
    response = client.post(f"/api/v1/editor/quarters/{Q}/detail", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["errors"]


def test_dimensions_are_capped_at_8x8(client):
    body = {
        "districts": [{"name": f"D{i}", "total": 1} for i in range(9)],
        "industries": [{"name": "X", "total": 9}],
    }
    response = client.put(f"/api/v1/editor/quarters/{Q}/totals", json=body)
    assert response.status_code == 422


# ----------------------------------------------------------------------- mask

def test_sensitive_cells_must_be_masked(client):
    register(client)
    submit(client)
    response = mask(client, [])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "mask_conflict"
    assert {(c["row"], c["col"]) for c in error["details"]["cells"]} == {
        (3, 1), (3, 2)
    }


def test_mask_cannot_cover_must_public_cell(client):
    register(client)
    submit(client, must_public=[(0, 0)])
    response = mask(client, [(0, 0), (3, 1), (3, 2)])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["details"]["issues"][0]["kind"] == "must_public_cell_masked"


def test_must_public_cell_cannot_be_sensitive(client):
    register(client)
    response = submit(client, must_public=[(3, 1)])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sensitive_cell_must_be_masked"


# ------------------------------------------------------------------ publishing

def test_publish_requires_protected_masking(client):
    register(client)
    submit(client)
    mask(client, [(3, 1), (3, 2)])
    response = publish(client)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "mask_conflict"
    assert error["details"]["cells"] == [{"row": 3, "col": 1},
                                         {"row": 3, "col": 2}]


def test_publish_and_public_query_hides_masked_values(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    assert advice["valid"], advice
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    assert mask(client, cells).status_code == 200
    response = publish(client)
    assert response.status_code == 200, response.text

    public = client.get(f"/api/v1/bulletins/{Q}")
    assert public.status_code == 200
    body = public.json()
    assert body["row_totals"] == ROW_TOTALS
    assert body["col_totals"] == COL_TOTALS
    for i, j in itertools.product(range(4), range(3)):
        if (i, j) in cells:
            assert body["cells"][i][j] is None
        else:
            assert body["cells"][i][j] == VALUES[i][j]
    # Internal bookkeeping never leaks into the public payload.
    for key in ("masked_cells", "detail_version", "mask_version",
                "protected", "must_public_cells"):
        assert key not in body


def test_public_payload_matches_recomputed_bounds(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    cells = {(c["row"], c["col"]) for c in advice["masked"]}
    mask(client, sorted(cells))
    assert publish(client).status_code == 200

    body = client.get(f"/api/v1/bulletins/{Q}").json()
    for i, j in cells:
        assert cell_has_two_values(body, i, j), (i, j)


def cell_has_two_values(payload, i, j):
    """Independent brute force: at least two feasible values for one cell.

    Enumerates hidden cells row by row, pruning on the remaining column sums.
    """
    rows = payload["row_totals"]
    cols = payload["col_totals"]
    n, m = len(rows), len(cols)
    hidden = [(r, c) for r in range(n) for c in range(m)
              if payload["cells"][r][c] is None]
    fixed = [[payload["cells"][r][c] for c in range(m)] for r in range(n)]
    need_r = [rows[r] - sum(v for v in fixed[r] if v is not None)
              for r in range(n)]
    need_c = [cols[c] - sum(fixed[r][c] for r in range(n)
                            if fixed[r][c] is not None) for c in range(m)]
    by_row = {r: [] for r in range(n)}
    for cell in hidden:
        by_row[cell[0]].append(cell[1])

    feasible = set()

    def assign(row, remaining_c):
        if row == n:
            if any(remaining_c):
                return
            feasible.add(current[(i, j)])
            return
        row_cells = by_row[row]
        if not row_cells:
            if need_r[row] == 0:
                assign(row + 1, remaining_c)
            return

        def spread(k, budget, state):
            if k == len(row_cells):
                if budget == 0:
                    assign(row + 1, state)
                return
            col = row_cells[k]
            for value in range(budget + 1):
                if value > state[col]:
                    break
                current[(row, col)] = value
                state[col] -= value
                spread(k + 1, budget - value, state)
                state[col] += value

        spread(0, need_r[row], remaining_c)

    current = {}
    assign(0, list(need_c))
    return len(feasible) >= 2


def test_quarter_publishes_only_once(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    mask(client, [(c["row"], c["col"]) for c in advice["masked"]])
    assert publish(client).status_code == 200
    first = client.get(f"/api/v1/bulletins/{Q}").json()

    again = publish(client)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_published"
    assert client.get(f"/api/v1/bulletins/{Q}").json() == first


def test_detail_change_rejects_old_mask_plan(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    assert mask(client, cells).status_code == 200

    alt = [row[:] for row in VALUES]
    # Cycle: same row and column margins, different cell values.
    alt[0][0], alt[0][1] = 29, 51
    alt[1][0], alt[1][1] = 26, 34
    assert submit(client, alt).status_code == 200
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["detail_versions"][-1]["version"] == 2
    assert state["mask_version"] is None

    response = publish(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_plan_superseded"


def test_publish_rechecks_mask_after_edit(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    mask(client, cells)
    # Editor loosens the mask afterwards; publication must refuse it.
    loosened = [c for c in cells if c != (3, 1)]
    response = mask(client, loosened)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_conflict"


def test_stale_mask_version_blocks_publish(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    mask(client, cells)
    response = publish(client, mask_version=99)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_mask_version"


def test_failed_publish_keeps_existing_bulletin_readable(client):
    register(client)
    submit(client)
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    mask(client, cells)
    assert publish(client).status_code == 200

    # A later detail revision cannot replace the published bulletin.
    alt = [row[:] for row in VALUES]
    alt[0][0], alt[0][1] = 29, 51
    alt[1][0], alt[1][1] = 26, 34
    submit(client, alt)
    assert publish(client).status_code == 409
    assert client.get(f"/api/v1/bulletins/{Q}").status_code == 200


def test_unknown_quarter_and_missing_bulletin(client):
    assert client.get("/api/v1/bulletins/2024Q3").status_code == 404
    assert client.get("/api/v1/bulletins/not-a-quarter").status_code == 422


def test_editor_endpoints_require_valid_token(client):
    from fastapi.testclient import TestClient

    register(client)
    no_auth = TestClient(client.app)
    assert no_auth.get(f"/api/v1/editor/quarters/{Q}").status_code == 401
    no_auth.headers.update({"Authorization": "Bearer nope"})
    assert no_auth.get(f"/api/v1/editor/quarters/{Q}").status_code == 403


# -------------------------------------------------------------- suppression

@pytest.mark.parametrize("seed", range(6))
def test_supplementary_cells_are_individually_necessary(seed):
    import random

    rng = random.Random(seed)
    values = [[rng.randint(0, 9) for _ in range(4)] for _ in range(4)]
    suggestion = suppression.suggest(values, set(), [])
    if not suggestion.valid:
        pytest.skip("no solution exists")
    chosen = set(suggestion.supplementary)
    full = set(suggestion.masked)
    mandatory = suppression.default_mask(values, [])
    if chosen:
        # Without the suggestions, protection fails.
        assert not suppression.ambiguity_ok(values, mandatory, [])
    # Publishing any single supplementary cell would break the guarantee for
    # at least one protected cell (the fast path must agree with the full
    # check).
    for cell in chosen:
        assert not suppression.ambiguity_ok(values, full - {cell}, []), (
            f"cell {cell} is redundant"
        )
        assert suppression.is_individually_necessary(values, full, [], cell)


def test_suppression_bounds_match_brute_force():
    import random

    rng = random.Random(3)
    for _ in range(60):
        n, m = rng.randint(2, 3), rng.randint(2, 3)
        values = [[rng.randint(0, 2) for _ in range(m)] for _ in range(n)]
        cells = [(i, j) for i in range(n) for j in range(m)]
        mask_set = {c for c in cells if rng.random() < 0.6} or {cells[0]}
        if len(mask_set) > 4:
            continue
        edges = sorted(mask_set)
        got = {c: suppression.cell_bounds(values, edges, c) for c in mask_set}
        assert got == brute_bounds(values, mask_set)


def brute_bounds(values, mask_set):
    """Enumerate every matrix that keeps visible cells and margins fixed."""
    n, m = len(values), len(values[0])
    rows = [sum(r) for r in values]
    cols = [sum(values[i][j] for i in range(n)) for j in range(m)]
    visible_row = [sum(values[r][c] for c in range(m) if (r, c) not in mask_set)
                   for r in range(n)]
    visible_col = [sum(values[r][c] for r in range(n) if (r, c) not in mask_set)
                   for c in range(m)]
    target_r = [rows[r] - visible_row[r] for r in range(n)]
    target_c = [cols[c] - visible_col[c] for c in range(m)]
    cells = sorted(mask_set)
    limit = max(rows + cols)
    out = {}
    for combo in itertools.product(range(limit + 1), repeat=len(cells)):
        assignment = dict(zip(cells, combo))
        if any(sum(assignment.get((r, c), 0) for c in range(m)) != target_r[r]
               for r in range(n)):
            continue
        if any(sum(assignment.get((r, c), 0) for r in range(n)) != target_c[c]
               for c in range(m)):
            continue
        for cell, value in assignment.items():
            lo, hi = out.get(cell, (value, value))
            out[cell] = (min(lo, value), max(hi, value))
    return out
