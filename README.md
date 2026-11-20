# 纾困户数季度公报 API

按季度发布「区县 × 行业」纾困户数交叉表。区县合计与行业合计由编辑凭令牌登记后**固定**；
明细以最多 8×8 的非负整数矩阵提交，必须与已公布的合计一致；对外只公开合计与被披露的格子。
1～4 户的格子强制遮蔽，并保证每个被遮蔽的格子都至少有两种可能的非负整数值。

技术栈：FastAPI + SQLite（标准库 `sqlite3`），零 ORM，`docker compose up` 即可启动。

---

## 快速启动

```bash
docker compose up --build -d
# 首次启动（空库）会在日志里打印一次性编辑令牌：
#   [bulletin] bootstrap editor token: ed_xxxxxxxx
docker compose logs bulletin | grep 'bootstrap editor token'
```

随后用该令牌调用编辑接口（`Authorization: Bearer ed_xxxxxxxx`）。令牌只存 SHA-256，
明文只在启动日志出现一次；可在容器内用 `POST /api/v1/editor/tokens` 另签。

数据库落在命名卷 `bulletin-data`（`/data/bulletins.sqlite3`），容器重建不丢数据。
镜像内以 `/health` 做健康检查。

本地开发：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload      # 或 BULLETIN_DB=./data/x.sqlite3 uvicorn app.main:app
pytest                             # 测试
```

接口文档：`http://localhost:8000/docs`。

---

## 业务规则

### 1. 合计登记后固定

`PUT /api/v1/editor/quarters/{quarter}/totals`

* 登记区县列表、行业列表及各自的合计。两个方向的合计之和必须相等，否则 422 `totals_inconsistent`。
* 登记成功后该季度合计不可修改：再次登记返回 409 `totals_already_registered`。
* 维度上限 8×8。

### 2. 明细提交

`POST /api/v1/editor/quarters/{quarter}/detail`

* `values` 必须是严格非负整数（`-1`、`3.5`、`true`、`"3"` 一律 422）。
* 行和必须等于区县合计，列和必须等于行业合计；不一致时返回 422 `totals_mismatch`，
  并在 `details.rows` / `details.columns` 指出具体行列及差额。
* **合计对不上时不会写入**，上一份有效明细原样保留（版本号不变）。
* 每次成功提交生成新版本（`version` 自增），版本只增不改；可用 `expected_version` 做乐观并发控制。
* `must_public` 指定必须公开的格子（`[[row, col], ...]` 或 `[{"row":r,"col":c}, ...]`）；
  1～4 户的格子不允许被要求公开（409 `sensitive_cell_must_be_masked`）。

### 3. 遮盖（遮蔽）编辑

`PUT /api/v1/editor/quarters/{quarter}/mask`

* 1～4 户的格子未遮蔽 → 409 `mask_conflict`，`details.cells` 列出所有冲突格子。
* 必须公开的格子被遮蔽 → 同样 409，`issues[].kind = must_public_cell_masked`。
* 遮盖计划绑定到明细版本：明细一变，旧计划即失效（不会被误用于新明细）。
* 遮盖可反复调整，每次 `mask_version` 自增；返回体里带当前遮盖的分析结果（可能仍有保护缺口，
  此时 `analysis.valid=false`，不阻止保存，但发布时会被拦下）。

### 4. 补遮建议

`POST /api/v1/editor/quarters/{quarter}/suppression-advice`

```json
{ "detail_version": 1, "cells": [{"row": 0, "col": 0}] }   // cells 可省略：取当前遮盖
```

返回：

```json
{
  "valid": true,
  "required_mask":  [{"row":0,"col":0}],          // 规则强制（1～4 户）
  "supplementary_mask": [{"row":0,"col":1}, ...], // 补遮建议
  "masked": [...],                                 // 建议后的完整遮盖
  "irredundant": true,
  "unresolved": [],
  "analysis": { "valid": true, "protected": { "0,0": {"min":0,"max":12} } }
}
```

**补遮建议单独公开其中任一格都会破坏「至少两种可能取值」的保证**（`irredundant: true`）：
建议集合在保持建议后方案有效的前提下，逐格做了必要性检验，去掉任何一格都会让至少一个
受保护格退化为唯一值。无法补全时 `valid=false` 并在 `unresolved` 里给出原因与格子。

### 5. 发布

`POST /api/v1/editor/quarters/{quarter}/publish`

* 发布前**重新复核**：仍存在冲突（1～4 户未遮、必须公开的被遮、某遮蔽格只有唯一取值）→
  409 `mask_conflict`，`details.cells` 指出冲突格子，并提示调用补遮建议。
* 遮盖计划属于过期明细 → 409 `mask_plan_superseded`，并在 `details` 里给出旧方案与旧明细版本。
* `mask_version` 与当前不一致 → 409 `stale_mask_version`。
* **每季只发一份**：已发布后再次发布 → 409 `already_published`；
  发布失败不会影响已发布的公报，`GET /api/v1/bulletins/{quarter}` 照常可查。
* 发布是事务性的：校验、快照写入、唯一约束在同一 `BEGIN IMMEDIATE` 事务里完成，并发也不会有第二份。

### 6. 公开查询

`GET /api/v1/bulletins/{quarter}`（公开，无需令牌；不存在时 404，季度格式不合法时 422）

```json
{
  "quarter": "2024Q1",
  "districts": [{"name": "东城区", "total": 30}, ...],
  "industries": [{"name": "制造业", "total": 40}, ...],
  "row_totals": [30, 25, 10, 50],
  "col_totals": [40, 45, 30],
  "grand_total": 115,
  "cells": [[10, null, null], [null, null, 9], ...],
  "published_at": "2024-04-15T09:00:00+00:00"
}
```

`cells` 中 `null` 表示未披露。响应里**不含**明细版本、遮盖计划、`must_public`、
或任何未披露的数值。

`GET /api/v1/bulletins` 列出已发布季度及总计。

---

## 保密性判定（为什么是精确的，不是启发式）

公开区县合计、行业合计与所有可见格后，每个被遮蔽格的取值被约束在一个整点运输多面体里。
真实明细是该多面体的一个整点，任何其它可行点与它之差是一个「整圈流」。

* 让某格 `e` 增加 `t`：需要一条从 `e` 所在列回到 `e` 所在行的残量通路，
  经过其它遮蔽格时只能减少（受其当前值限制）；
* 让 `e` 减少：方向相反，另外受 `e` 自身取值限制。

求最大 `t` 就是一次最大流（本实现为 16 个节点内的 Dinic），因此每格的最小/最大可行值
是**精确整数界**。只要 `min < max`，该格就至少有两种可能取值。
测试里用暴力枚举所有可行矩阵做了交叉验证（`tests/test_api.py::test_suppression_bounds_match_brute_force`）。

补遮建议的搜索：优先加入能最大程度减少冲突的格，随后做逐格必要性剔除。

---

## 鉴权

* 空库首次启动自动生成 bootstrap 令牌，只打印一次，库里只存 SHA-256。
* 所有编辑接口要求 `Authorization: Bearer <token>`；缺失 401，无效/吊销 403。
* `POST /api/v1/editor/tokens` 可再签发令牌（`{"label": "..."}`），返回明文一次，之后不可再取。

## 目录结构

```
app/
  main.py        FastAPI 路由与依赖
  models.py      Pydantic 请求模型（严格整数、单元格两种写法）
  service.py     业务规则与版本/发布事务
  suppression.py 精确的遮蔽分析（最大流）与补遮建议
  db.py          SQLite 连接、schema、事务
tests/           pytest 端到端测试
Dockerfile / docker-compose.yml
```

## 设计取舍

* **合计不可改**：公报数字一旦登记就作为约束存在；纠正错误应改用新季度或新版本体系，而不是静默改数。
* **遮盖计划绑定明细版本**：明细变了就不再有任何「旧遮盖可复用」的歧义，发布时能明确报出
  `mask_plan_superseded`。
* **保存与发布分离**：编辑可以先保存一份尚不完整的遮盖（带 `analysis.valid=false` 提示），
  但发布必须完整合规，复核在发布瞬间进行。
* **公开载荷最小化**：公开接口只返回合计与可见格；明细版本、遮盖计划、必公开清单等内部
  状态不出现在响应里。
