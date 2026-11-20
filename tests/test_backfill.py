"""End-to-end tests for back-filling published copies from the old system."""

from __future__ import annotations

import copy as copy_module
import itertools

import pytest

from app import suppression

PUBLISHED = "2023-04-15T09:00:00+00:00"


def copy_for(quarter, values=None, masked=(), published_at=PUBLISHED,
             districts=None, industries=None):
    values = values or [[30, 50, 40], [25, 35, 35], [10, 20, 30]]
    masked = set(masked)
    districts = districts or [f"D{i}" for i in range(len(values))]
    industries = industries or [f"I{j}" for j in range(len(values[0]))]
    cells = [[None if (i, j) in masked else values[i][j]
              for j in range(len(industries))]
             for i in range(len(districts))]
    return {
        "quarter": quarter,
        "districts": [{"name": n, "total": sum(r)}
                      for n, r in zip(districts, values)],
        "industries": [{"name": n, "total": sum(values[i][j] for i in range(len(values)))}
                       for j, n in enumerate(industries)],
        "row_totals": [sum(r) for r in values],
        "col_totals": [sum(values[i][j] for i in range(len(values)))
                       for j in range(len(values[0]))],
        "grand_total": sum(sum(r) for r in values),
        "cells": cells,
        "published_at": published_at,
    }


def backfill(client, copies):
    return client.post("/api/v1/editor/backfill", json={"copies": copies})


# A masking that leaves every hidden cell with at least two possible values:
# hide the two tiny cells in row 3 plus two cells in row 0 so the residual
# masses can be exchanged (the suggestion endpoint finds the same set).
VALUES = [
    [30, 50, 40],
    [25, 35, 35],
    [10, 20, 30],
    [45, 3, 2],
]
MASK = {(0, 1), (0, 2), (3, 1), (3, 2)}


def test_backfill_imports_a_copy_and_makes_it_public(client):
    response = backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_count"] == 1
    assert body["unchanged_count"] == 0

    public = client.get("/api/v1/bulletins/2023Q1")
    assert public.status_code == 200
    payload = public.json()
    assert payload["published_at"] == PUBLISHED
    assert payload["row_totals"] == [120, 95, 60, 50]
    assert payload["cells"][0] == [30, None, None]
    assert payload["cells"][3] == [45, None, None]

    listed = client.get("/api/v1/bulletins").json()["bulletins"]
    assert any(item["quarter"] == "2023Q1" for item in listed)

    state = client.get("/api/v1/editor/quarters/2023Q1").json()
    assert state["from_public_copy"] is True
    assert state["source"] == "backfill"
    assert state["draft_read_only"] is True
    assert state["copy_cells"] == payload["cells"]


def test_backfill_batch_accepts_up_to_eight_copies(client):
    copies = [copy_for(f"2022Q{q}", VALUES, MASK) for q in range(1, 5)]
    copies += [copy_for(f"2023Q{q}", VALUES, MASK) for q in range(1, 5)]
    response = backfill(client, copies)
    assert response.status_code == 200, response.text
    assert response.json()["imported_count"] == 8
    assert len(client.get("/api/v1/bulletins").json()["bulletins"]) == 8


def test_backfill_rejects_more_than_eight_copies(client):
    copies = [copy_for(f"2022Q{q}", VALUES, MASK) for q in range(1, 5)]
    copies += [copy_for(f"2023Q{q}", VALUES, MASK) for q in range(1, 5)]
    copies.append(copy_for("2024Q1", VALUES, MASK))
    response = backfill(client, copies)
    assert response.status_code == 422


def test_backfill_preserves_masking_and_never_reveals_hidden_cells(client):
    backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    payload = client.get("/api/v1/bulletins/2023Q1").json()
    for i in range(4):
        for j in range(3):
            if (i, j) in MASK:
                assert payload["cells"][i][j] is None
            else:
                assert payload["cells"][i][j] == VALUES[i][j]
    # Even the editor view must not invent the hidden values.
    state = client.get("/api/v1/editor/quarters/2023Q1").json()
    assert state["current_detail_version"] is None
    assert state["copy_cells"] == payload["cells"]


def test_backfill_requires_auth(client):
    from fastapi.testclient import TestClient

    anon = TestClient(client.app)
    response = anon.post("/api/v1/editor/backfill",
                         json={"copies": [copy_for("2023Q1", VALUES, MASK)]})
    assert response.status_code == 401


# ---------------------------------------------------------------- validation

def test_visible_sensitive_cell_rejected(client):
    response = backfill(client, [copy_for("2023Q1", VALUES,
                                          MASK - {(3, 1), (3, 2)})])
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_copy"
    kinds = {issue["kind"] for issue in error["details"]["issues"]}
    assert "sensitive_cell_visible" in kinds
    assert client.get("/api/v1/bulletins/2023Q1").status_code == 404


def test_uniquely_determined_masked_cell_rejected(client):
    # Mask only the two 3/2 cells; the row total then forces both values.
    response = backfill(client, [copy_for("2023Q1", VALUES, {(3, 1), (3, 2)})])
    assert response.status_code == 422
    kinds = {issue["kind"]
             for issue in response.json()["error"]["details"]["issues"]}
    assert "cell_uniquely_determined" in kinds


def test_copy_with_no_feasible_table_rejected(client):
    # Every margin still covers its disclosed cells, but no non-negative
    # table can complete the disclosed 30/50/38 row while keeping rows 1-3.
    bad = copy_for(
        "2023Q1",
        [[0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]],
        {(1, 1), (1, 2), (2, 0), (2, 2), (3, 0), (3, 1)},
        districts=["D0", "D1", "D2", "D3"],
        industries=["I0", "I1", "I2"],
    )
    bad["cells"][0] = [30, 50, 38]
    bad["row_totals"] = [120, 45, 45, 0]
    bad["col_totals"] = [55, 70, 85]
    for i, total in enumerate(bad["row_totals"]):
        bad["districts"][i]["total"] = total
    for j, total in enumerate(bad["col_totals"]):
        bad["industries"][j]["total"] = total
    bad["grand_total"] = 210
    response = backfill(client, [bad])
    assert response.status_code == 422
    kinds = {issue["kind"]
             for issue in response.json()["error"]["details"]["issues"]}
    assert "no_feasible_table" in kinds


def test_inconsistent_axis_totals_rejected(client):
    bad = copy_for("2023Q1", VALUES, MASK)
    bad["industries"][0]["total"] += 1
    response = backfill(client, [bad])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_grand_total_and_row_totals_must_agree(client):
    bad = copy_for("2023Q1", VALUES, MASK)
    bad["grand_total"] += 1
    response = backfill(client, [bad])
    assert response.status_code == 422

    bad = copy_for("2023Q1", VALUES, MASK)
    bad["row_totals"][0] += 1
    response = backfill(client, [bad])
    assert response.status_code == 422


def test_batch_is_atomic_when_one_copy_is_invalid(client):
    good = copy_for("2022Q1", VALUES, MASK)
    bad = copy_for("2022Q2", VALUES, MASK)
    bad["cells"][0][0] = None   # may or may not still be valid; force a hard error
    bad["districts"] = bad["districts"][:2]
    response = backfill(client, [good, bad])
    assert response.status_code == 422
    # Nothing from the batch was written.
    assert client.get("/api/v1/bulletins/2022Q1").status_code == 404
    assert client.get("/api/v1/bulletins/2022Q2").status_code == 404
    assert client.get("/api/v1/bulletins").json()["bulletins"] == []


def test_malformed_cell_matrix_rejected(client):
    bad = copy_for("2023Q1", VALUES, MASK)
    bad["cells"][0] = bad["cells"][0][:2]
    assert backfill(client, [bad]).status_code == 422


def test_out_of_range_quarter_rejected(client):
    bad = copy_for("2023Q1", VALUES, MASK)
    bad["quarter"] = "2023Q5"
    assert backfill(client, [bad]).status_code == 422


def test_invalid_timestamp_rejected(client):
    bad = copy_for("2023Q1", VALUES, MASK, published_at="last Tuesday")
    assert backfill(client, [bad]).status_code == 422


def test_identical_duplicate_quarter_within_batch_is_imported_once(client):
    response = backfill(client, [copy_for("2023Q1", VALUES, MASK),
                                 copy_for("2023Q1", VALUES, MASK)])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_count"] == 1
    assert body["unchanged_count"] == 1
    assert len(client.get("/api/v1/bulletins").json()["bulletins"]) == 1


def test_conflicting_duplicate_quarter_within_batch_rejected(client):
    a = copy_for("2023Q1", VALUES, MASK)
    b = copy_for("2023Q1", [[29, 51, 40], [25, 35, 35],
                            [10, 20, 30], [45, 3, 2]], MASK)
    response = backfill(client, [a, b])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "bulletin_content_conflict"
    assert error["details"]["quarter"] == "2023Q1"
    assert client.get("/api/v1/bulletins/2023Q1").status_code == 404


# ------------------------------------------------------------- idempotency

def test_identical_resend_succeeds_without_duplicating(client):
    first = backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    assert first.status_code == 200
    second = backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    assert second.status_code == 200
    body = second.json()
    assert body["imported_count"] == 0
    assert body["unchanged_count"] == 1
    assert len(client.get("/api/v1/bulletins").json()["bulletins"]) == 1


def test_conflicting_resend_names_the_quarter(client):
    backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    # Same margins and totals, different disclosed pattern (still safe).
    changed = copy_for("2023Q1", [[29, 51, 40], [25, 35, 35],
                                  [10, 20, 30], [45, 3, 2]], MASK)
    response = backfill(client, [changed])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "bulletin_content_conflict"
    assert error["details"]["quarter"] == "2023Q1"


def test_identical_resend_to_a_locally_published_quarter(client):
    # Publish normally, then re-send the identical content as a copy: it must
    # succeed, because the content does not conflict.
    from tests.test_api import (COL_TOTALS, DISTRICTS, INDUSTRIES,
                                ROW_TOTALS, VALUES as V, mask, publish,
                                register, submit)

    register(client, quarter="2024Q1")
    submit(client, quarter="2024Q1")
    advice = client.post(
        "/api/v1/editor/quarters/2024Q1/suppression-advice", json={}
    ).json()
    cells = {(c["row"], c["col"]) for c in advice["masked"]}
    mask(client, sorted(cells), quarter="2024Q1")
    assert publish(client, quarter="2024Q1").status_code == 200
    published = client.get("/api/v1/bulletins/2024Q1").json()

    resend = {
        "quarter": "2024Q1",
        "districts": published["districts"],
        "industries": published["industries"],
        "row_totals": ROW_TOTALS,
        "col_totals": COL_TOTALS,
        "cells": published["cells"],
        "published_at": published["published_at"],
    }
    response = backfill(client, [resend])
    assert response.status_code == 200, response.text
    assert response.json()["unchanged_count"] == 1
    assert client.get("/api/v1/bulletins/2024Q1").json() == published


def test_conflicting_resend_to_a_locally_published_quarter(client):
    from tests.test_api import mask, publish, register, submit

    register(client, quarter="2024Q1")
    submit(client, quarter="2024Q1")
    advice = client.post(
        "/api/v1/editor/quarters/2024Q1/suppression-advice", json={}
    ).json()
    mask(client, [(c["row"], c["col"]) for c in advice["masked"]],
         quarter="2024Q1")
    assert publish(client, quarter="2024Q1").status_code == 200

    other = copy_for("2024Q1", [[30, 50, 41], [25, 35, 35],
                                [10, 20, 30], [45, 3, 2]],
                     {(0, 1), (0, 2), (3, 1), (3, 2)})
    response = backfill(client, [other])
    assert response.status_code == 409
    assert response.json()["error"]["details"]["quarter"] == "2024Q1"
    # The original bulletin is untouched.
    assert client.get("/api/v1/bulletins/2024Q1").status_code == 200


# ----------------------------------------------- interaction with publishing

def test_registered_totals_must_match_the_copy(client):
    from tests.test_api import COL_TOTALS, DISTRICTS, INDUSTRIES, ROW_TOTALS
    from tests.test_api import register

    register(client, quarter="2023Q1")
    wrong = {
        "quarter": "2023Q1",
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, ROW_TOTALS)],
        "industries": [{"name": n, "total": t + 1}
                       for n, t in zip(INDUSTRIES, COL_TOTALS)],
        "cells": [[None] * 3 for _ in DISTRICTS],
        "published_at": PUBLISHED,
    }
    response = backfill(client, [wrong])
    assert response.status_code == 422   # axis sums disagree
    kinds = {issue["kind"]
             for issue in response.json()["error"]["details"]["issues"]}
    assert "totals_inconsistent" in kinds

    # Now a self-consistent copy whose registered names differ.
    renamed = copy_for(
        "2023Q1", VALUES, {(0, 1), (0, 2), (3, 1), (3, 2)},
        districts=[f"X{i}" for i in range(len(ROW_TOTALS))],
    )
    response = backfill(client, [renamed])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "registered_totals_mismatch"
    assert "districts" in error["details"]["differences"]


def test_backfilled_quarter_blocks_a_later_regular_publish(client):
    backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    from tests.test_api import register

    response = register(client, quarter="2023Q1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "totals_already_registered"


def test_regular_publish_after_backfill_is_refused(client):
    from tests.test_api import (COL_TOTALS, DISTRICTS, INDUSTRIES, ROW_TOTALS,
                                publish, register, submit)

    # Register first (same axes/totals as the copy) and submit a detail, then
    # back-fill; publishing the local draft must be refused because the
    # quarter already has its one official bulletin.
    register(client, quarter="2023Q1")
    copy = {
        "quarter": "2023Q1",
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, ROW_TOTALS)],
        "industries": [{"name": n, "total": t}
                       for n, t in zip(INDUSTRIES, COL_TOTALS)],
        "cells": [[None] * 3 for _ in DISTRICTS],
        "published_at": PUBLISHED,
    }
    assert backfill(client, [copy]).status_code == 200
    assert client.get("/api/v1/bulletins/2023Q1").status_code == 200

    # Existing local drafts are preserved but become read-only: submitting,
    # masking and publishing are refused against the authoritative copy.
    response = submit(client, quarter="2023Q1", values=VALUES)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quarter_read_only"
    response = client.put("/api/v1/editor/quarters/2023Q1/mask",
                          json={"cells": []})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quarter_read_only"
    response = publish(client, quarter="2023Q1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "quarter_read_only"
    state = client.get("/api/v1/editor/quarters/2023Q1").json()
    assert state["draft_read_only"] is True
    assert state["from_public_copy"] is True
    assert state["source"] == "backfill"
    assert client.get("/api/v1/bulletins/2023Q1").json()["published_at"] == \
        PUBLISHED


def test_backfill_preserves_an_existing_local_draft(client):
    from tests.test_api import DISTRICTS, INDUSTRIES, register, submit

    # A draft exists before the copy arrives: it must survive unchanged and
    # become read-only, not be deleted or published.
    register(client, quarter="2023Q1")
    assert submit(client, quarter="2023Q1").status_code == 200
    before = client.get("/api/v1/editor/quarters/2023Q1").json()

    copy = copy_for("2023Q1", VALUES, MASK, districts=DISTRICTS,
                    industries=INDUSTRIES)
    response = backfill(client, [copy])
    assert response.status_code == 200, response.text

    after = client.get("/api/v1/editor/quarters/2023Q1").json()
    assert after["detail_versions"] == before["detail_versions"]
    assert after["current_detail_version"] == before["current_detail_version"]
    assert after["draft_read_only"] is True
    assert after["from_public_copy"] is True
    assert client.get("/api/v1/bulletins/2023Q1").json()["cells"] == \
        copy["cells"]


def test_public_copy_round_trips_through_the_public_endpoint(client):
    backfill(client, [copy_for("2023Q1", VALUES, MASK)])
    public = client.get("/api/v1/bulletins/2023Q1").json()
    resend = dict(public)          # exactly what GET returned
    response = backfill(client, [resend])
    assert response.status_code == 200, response.text
    assert response.json()["unchanged_count"] == 1
    assert client.get("/api/v1/bulletins/2023Q1").json() == public


def test_batch_is_atomic_when_a_later_copy_conflicts(client):
    backfill(client, [copy_for("2022Q1", VALUES, MASK)])
    good = copy_for("2022Q2", VALUES, MASK)
    clash = copy_for("2022Q1", [[29, 51, 40], [25, 35, 35],
                                [10, 20, 30], [45, 3, 2]], MASK)
    response = backfill(client, [good, clash])
    assert response.status_code == 409
    assert response.json()["error"]["details"]["quarter"] == "2022Q1"
    # The good copy in the same batch was rolled back.
    assert client.get("/api/v1/bulletins/2022Q2").status_code == 404


def test_publish_and_backfill_cannot_both_win_for_one_quarter(client):
    from tests.test_api import mask, publish, register, submit

    register(client, quarter="2023Q1")
    submit(client, quarter="2023Q1")
    advice = client.post(
        "/api/v1/editor/quarters/2023Q1/suppression-advice", json={}
    ).json()
    mask(client, [(c["row"], c["col"]) for c in advice["masked"]],
         quarter="2023Q1")
    assert publish(client, quarter="2023Q1").status_code == 200
    # Back-filling a different copy cannot produce a second bulletin.
    other = copy_for("2023Q1", [[29, 51, 40], [25, 35, 35],
                                [10, 20, 30], [45, 3, 2]], MASK)
    response = backfill(client, [other])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "bulletin_content_conflict"


# ------------------------------------------------------- feasibility helper

def brute_feasible(row_sums, col_sums, fixed):
    n, m = len(row_sums), len(col_sums)
    free = [(i, j) for i in range(n) for j in range(m) if (i, j) not in fixed]
    limit = max(row_sums + col_sums)
    for combo in itertools.product(range(limit + 1), repeat=len(free)):
        table = [[0] * m for _ in range(n)]
        for (i, j), value in fixed.items():
            table[i][j] = value
        for (i, j), value in zip(free, combo):
            table[i][j] = value
        if [sum(r) for r in table] != list(row_sums):
            continue
        if [sum(table[i][j] for i in range(n)) for j in range(m)] != \
                list(col_sums):
            continue
        return True
    return False


@pytest.mark.parametrize("seed", range(40))
def test_complete_table_matches_brute_force(seed):
    import random

    rng = random.Random(seed)
    n, m = rng.randint(2, 3), rng.randint(2, 3)
    values = [[rng.randint(0, 3) for _ in range(m)] for _ in range(n)]
    rows = [sum(r) for r in values]
    cols = [sum(values[i][j] for i in range(n)) for j in range(m)]
    fixed = {(i, j): values[i][j]
             for i in range(n) for j in range(m) if rng.random() < 0.5}
    table = suppression.complete_table(rows, cols, fixed)
    expected = brute_feasible(rows, cols, fixed)
    assert (table is not None) is expected


def test_complete_table_handles_zero_margins():
    # A zero row is legitimate; a margin smaller than a fixed cell is not.
    assert suppression.complete_table([0, 3], [0, 3], {}) == [[0, 0], [0, 3]]
    assert suppression.complete_table([1], [0], {(0, 0): 1}) is None
    assert suppression.complete_table([2], [1], {(0, 0): 1}) is None


def test_complete_table_respects_fixed_cells_and_margins():
    rows, cols = [10, 6], [9, 7]
    fixed = {(0, 0): 4, (1, 1): 1}
    table = suppression.complete_table(rows, cols, fixed)
    assert table is not None
    assert table[0][0] == 4 and table[1][1] == 1
    assert [sum(r) for r in table] == rows
    assert [sum(table[i][j] for i in range(2)) for j in range(2)] == cols


def test_copy_validation_agrees_with_brute_force():
    """A copy passes exactly when the disclosed pattern is safe."""
    import random

    from app.models import PublishedCopy
    from app.service import copy_problems

    for seed in range(12):
        rng = random.Random(seed)
        # Small margins keep the brute-force enumeration cheap.
        values = [[rng.randint(0, 2) for _ in range(2)] for _ in range(2)]
        masked = {(i, j) for i in range(2) for j in range(2)
                  if rng.random() < 0.5}
        copy = PublishedCopy.model_validate(
            copy_for(f"20{20 + seed}Q1", values, masked))
        problems = copy_problems(copy)

        rows = [sum(r) for r in values]
        cols = [sum(values[i][j] for i in range(2)) for j in range(2)]
        fixed = {(i, j): values[i][j]
                 for i in range(2) for j in range(2) if (i, j) not in masked}
        tables = _all_tables(rows, cols, fixed)
        sensitive_visible = any(
            1 <= values[i][j] <= 4 and (i, j) not in masked
            for i in range(2) for j in range(2))
        two_values = all(
            len({table[i][j] for table in tables}) > 1 for i, j in masked
        ) if masked else True
        safe = bool(tables) and not sensitive_visible and two_values
        assert (problems == []) is safe


def _all_tables(row_sums, col_sums, fixed):
    n, m = len(row_sums), len(col_sums)
    free = [(i, j) for i in range(n) for j in range(m) if (i, j) not in fixed]
    limit = max(row_sums + col_sums)
    out = []
    for combo in itertools.product(range(limit + 1), repeat=len(free)):
        table = [[0] * m for _ in range(n)]
        for (i, j), value in fixed.items():
            table[i][j] = value
        for (i, j), value in zip(free, combo):
            table[i][j] = value
        if [sum(r) for r in table] != list(row_sums):
            continue
        if [sum(table[i][j] for i in range(n)) for j in range(m)] != \
                list(col_sums):
            continue
        out.append(table)
    return out
