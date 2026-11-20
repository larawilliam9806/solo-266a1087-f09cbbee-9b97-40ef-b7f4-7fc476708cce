"""Cell-suppression analysis.

A bulletin publishes every district total, every industry total, and the value
of every visible cell.  A masked cell must therefore not be recoverable: there
must be at least two non-negative integer matrices that agree with all visible
cells and all published totals, and that disagree in the masked cell.

Fixing the visible cells turns each published row/column total into a residual
sum over the masked cells, so the question becomes: over the transportation
polytope on the masked cells, what is the range of a single cell?

The true detail is one feasible point of that polytope.  Any other feasible
point differs from it by an integer circulation on the bipartite graph of
masked cells.  Increasing cell ``e`` by ``t`` requires a residual path from
``e``'s column back to its row, where

* moving row -> column increases another cell (unbounded),
* moving column -> row decreases another cell (at most its current value).

Maximising ``t`` is a max-flow problem, and decreasing ``e`` is the mirror
image (also capped by ``e``'s own value).  Two max-flow runs per masked cell
give its exact integer bounds, so the check is exact rather than heuristic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import Iterable, Sequence

INF = 1 << 60

Cell = tuple[int, int]


class _Dinic:
    """Tiny max-flow implementation (16 nodes, <= 64 edges in this domain)."""

    __slots__ = ("n", "graph")

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: list[list[list[int]]] = [[] for _ in range(n)]

    def add(self, u: int, v: int, cap: int) -> None:
        self.graph[u].append([v, cap, len(self.graph[v])])
        self.graph[v].append([u, 0, len(self.graph[u]) - 1])

    def maxflow(self, s: int, t: int) -> int:
        if s == t:
            return 0
        total = 0
        while True:
            level = [-1] * self.n
            level[s] = 0
            queue = deque([s])
            while queue:
                u = queue.popleft()
                for v, cap, _ in self.graph[u]:
                    if cap > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        queue.append(v)
            if level[t] < 0:
                return total
            it = [0] * self.n

            def dfs(u: int, pushed: int) -> int:
                if u == t:
                    return pushed
                while it[u] < len(self.graph[u]):
                    edge = self.graph[u][it[u]]
                    v, cap, rev = edge
                    if cap > 0 and level[v] == level[u] + 1:
                        got = dfs(v, min(pushed, cap))
                        if got:
                            edge[1] -= got
                            self.graph[v][rev][1] += got
                            return got
                    it[u] += 1
                return 0

            while True:
                pushed = dfs(s, INF)
                if not pushed:
                    break
                total += pushed


def _residual(n_rows: int, n_cols: int, edges: Iterable[Cell],
              values: Sequence[Sequence[int]], skip: Cell) -> _Dinic:
    flow = _Dinic(n_rows + n_cols)
    for i, j in edges:
        if (i, j) == skip:
            continue
        flow.add(i, n_rows + j, INF)
        if values[i][j] > 0:
            flow.add(n_rows + j, i, values[i][j])
    return flow


def cell_bounds(values: Sequence[Sequence[int]], edges: Sequence[Cell],
                cell: Cell) -> tuple[int, int]:
    """Exact (minimum, maximum) the masked cell can take."""
    n_rows, n_cols = len(values), len(values[0])
    i, j = cell
    value = values[i][j]
    up = _residual(n_rows, n_cols, edges, values, cell).maxflow(n_rows + j, i)
    down = _residual(n_rows, n_cols, edges, values, cell).maxflow(i, n_rows + j)
    return value - min(value, down), value + up


@dataclass(frozen=True)
class Issue:
    kind: str
    cell: Cell | None = None
    detail: dict = field(default_factory=dict, hash=False)

    def as_dict(self) -> dict:
        out: dict = {"kind": self.kind, "message": MESSAGES[self.kind]}
        if self.cell is not None:
            out["row"], out["col"] = self.cell
        out.update(self.detail)
        return out


MESSAGES = {
    "must_public_cell_masked": "a cell marked as must-public is masked",
    "sensitive_cell_visible": "cells with 1-4 households must be masked",
    "cell_uniquely_determined": "masked cell has only one possible value",
}


@dataclass
class Analysis:
    issues: list[Issue]
    bounds: dict[Cell, tuple[int, int]]

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def unique_cells(self) -> list[Cell]:
        return [c for c, (lo, hi) in self.bounds.items() if lo == hi]

    @property
    def conflict_cells(self) -> list[Cell]:
        return sorted({issue.cell for issue in self.issues if issue.cell})

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "issues": [issue.as_dict() for issue in self.issues],
            "conflict_cells": [
                {"row": r, "col": c} for r, c in self.conflict_cells
            ],
            "unique_cells": [
                {"row": r, "col": c} for r, c in self.unique_cells
            ],
            "protected": {
                f"{r},{c}": {"min": lo, "max": hi}
                for (r, c), (lo, hi) in sorted(self.bounds.items())
            },
        }


def sensitive_cells(values: Sequence[Sequence[int]]) -> set[Cell]:
    return {
        (i, j)
        for i, row in enumerate(values)
        for j, value in enumerate(row)
        if 1 <= value <= 4
    }


def analyze(values: Sequence[Sequence[int]], masked: Iterable[Cell],
            must_public: Iterable[Cell]) -> Analysis:
    """Full validity check of a suppression pattern."""
    masked_set = set(masked)
    public_set = set(must_public)
    issues: list[Issue] = []

    for cell in sorted(masked_set & public_set):
        issues.append(Issue("must_public_cell_masked", cell,
                            {"value": values[cell[0]][cell[1]]}))
    for i, row in enumerate(values):
        for j, value in enumerate(row):
            if 1 <= value <= 4 and (i, j) not in masked_set:
                issues.append(Issue("sensitive_cell_visible", (i, j),
                                    {"value": value}))

    edges = sorted(masked_set - public_set)
    bounds = {cell: cell_bounds(values, edges, cell) for cell in edges}
    for cell in edges:
        lo, hi = bounds[cell]
        if lo == hi:
            issues.append(Issue("cell_uniquely_determined", cell,
                                {"value": values[cell[0]][cell[1]],
                                 "possible_value": lo}))

    issues.sort(key=lambda item: (item.cell or (-1, -1), item.kind))
    return Analysis(issues=issues, bounds=bounds)


def ambiguity_ok(values: Sequence[Sequence[int]], masked: Iterable[Cell],
                 must_public: Iterable[Cell]) -> bool:
    """True when every masked cell has at least two possible values."""
    masked_set = set(masked) - set(must_public)
    edges = sorted(masked_set)
    return all(
        (lambda b: b[0] < b[1])(cell_bounds(values, edges, cell))
        for cell in edges
    )


def is_individually_necessary(values: Sequence[Sequence[int]], full: set[Cell],
                              must_public: Iterable[Cell],
                              cell: Cell) -> bool:
    """True when making ``cell`` public would expose at least one cell.

    Releasing the cell can only change the analysis through the component it
    belongs to, so only that component is recomputed.  Has the same answer as
    ``not ambiguity_ok(values, full - {cell}, must_public)``.
    """
    public_set = set(must_public)
    remaining = (full - {cell}) - public_set
    if not remaining:
        return False
    # Connected component of ``cell`` in the bipartite masked-cell graph.
    rows = {cell[0]}
    cols = {cell[1]}
    changed = True
    while changed:
        changed = False
        for r, c in remaining:
            if (r in rows or c in cols) and (r not in rows or c not in cols):
                rows.add(r)
                cols.add(c)
                changed = True
    component = sorted((r, c) for r, c in remaining
                       if r in rows and c in cols)
    return not all(
        (lambda b: b[0] < b[1])(cell_bounds(values, component, other))
        for other in component
    )


def default_mask(values: Sequence[Sequence[int]],
                 must_public: Iterable[Cell]) -> set[Cell]:
    """All cells that the rules make mandatory to mask."""
    return sensitive_cells(values) - set(must_public)


@dataclass
class Suggestion:
    valid: bool
    masked: list[Cell]
    required: list[Cell]
    supplementary: list[Cell]
    irredundant: bool
    unresolved: list[dict]
    analysis: Analysis | None

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "masked": [{"row": r, "col": c} for r, c in self.masked],
            "required_mask": [{"row": r, "col": c} for r, c in self.required],
            "supplementary_mask": [
                {"row": r, "col": c} for r, c in self.supplementary
            ],
            "irredundant": self.irredundant,
            "unresolved": self.unresolved,
            "analysis": self.analysis.as_dict() if self.analysis else None,
        }


def _candidates(mask: set[Cell], must_public: set[Cell],
                n_rows: int, n_cols: int) -> list[Cell]:
    """Cells that could possibly influence ambiguity.

    A cell in a fresh row *and* a fresh column would be its own disconnected
    component: its row and column sums force its value, so it cannot make any
    existing cell ambiguous.  Anything else is fair game.
    """
    busy_rows = {i for i, _ in mask}
    busy_cols = {j for _, j in mask}
    out = []
    for i, j in product(range(n_rows), range(n_cols)):
        if (i, j) in mask or (i, j) in must_public:
            continue
        if i in busy_rows or j in busy_cols:
            out.append((i, j))
    return out


def suggest(values: Sequence[Sequence[int]], base_mask: Iterable[Cell],
            must_public: Iterable[Cell]) -> Suggestion:
    """Suggest extra cells to mask, keeping only individually necessary ones.

    The working mask always contains the rules-mandatory cells (values 1-4).
    Those are reported separately as ``required_mask``; they are not part of
    the irredundancy claim, which concerns ambiguity only.
    """
    n_rows, n_cols = len(values), len(values[0])
    public_set = set(must_public)
    base = set(base_mask)
    required = default_mask(values, public_set) - base
    # Keep must-public cells that the caller wrongly masked in the working set
    # so ``analyze`` reports the conflict instead of silently dropping it.
    mask = base | required
    required_sorted = sorted(required - public_set)

    analysis = analyze(values, mask, public_set)
    if analysis.valid:
        return Suggestion(True, sorted(mask), required_sorted, [], True, [],
                          analysis)

    if base & public_set:
        return Suggestion(
            False, sorted(mask), required_sorted, [], False,
            [issue.as_dict() for issue in analysis.issues
             if issue.kind == "must_public_cell_masked"],
            analysis,
        )

    chosen: list[Cell] = []
    while True:
        analysis = analyze(values, mask, public_set)
        if analysis.valid:
            break
        candidates = _candidates(mask, public_set, n_rows, n_cols)
        if not candidates:
            unresolved = [
                issue.as_dict()
                for issue in analysis.issues
                if issue.kind != "must_public_cell_masked"
            ]
            return Suggestion(False, sorted(mask), required_sorted, [], False,
                              unresolved, analysis)
        best_cell, best_score = None, None
        for candidate in candidates:
            trial = analyze(values, mask | {candidate}, public_set)
            score = (len(trial.issues), candidate)
            if best_score is None or score < best_score:
                best_cell, best_score = candidate, score
        assert best_cell is not None
        chosen.append(best_cell)
        mask.add(best_cell)

    # Keep only suggestions that are individually necessary: un-masking any
    # one of them must break the two-value guarantee for some cell.
    changed = True
    while changed:
        changed = False
        for candidate in list(chosen):
            trial_mask = mask - {candidate}
            if is_individually_necessary(values, trial_mask, public_set,
                                         candidate):
                continue
            mask = trial_mask
            chosen.remove(candidate)
            changed = True
            break

    final = analyze(values, mask, public_set)
    return Suggestion(True, sorted(mask), required_sorted, sorted(chosen), True,
                      [], final)
