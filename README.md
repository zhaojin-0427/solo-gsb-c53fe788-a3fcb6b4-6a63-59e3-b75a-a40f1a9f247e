# 电子表格公式依赖与增量重算审查台

本项目是一个本地 Web 应用：服务端使用 Python、Litestar 与 SQLite；浏览器端不依赖框架，使用原生 JavaScript。它将公式解析为带源位置的依赖图，编辑后只重算受影响子图，并把每次成功编辑保存为不可变版本。

## 功能概览

- 多工作表网格和 A1 引用：
  - 算术：`+ - * /`、括号、一元正负号
  - 比较：`= <> < <= > >=`
  - `IF(condition, when_true, when_false)`，条件分支惰性求值
  - `SUM(...)`，支持单值和连续区域
  - 跨表引用：`Inputs!B2`
  - 区域：`A2:A4`、`Inputs!C2:C4`
- 数值使用 Python/IEEE-754 double；文本参加算术会返回 `VALUE_ERROR`。
- 错误值沿依赖边传播；循环、缺失/越界引用、类型错误不会生成新版本。
- 依赖边记录公式字符区间，可定位到具体引用或运算符。
- 编辑时：
  1. 解析变更公式；
  2. 重建依赖关系；
  3. 通过反向边求受影响子图；
  4. 对受影响公式做拓扑排序；
  5. 只重算该顺序中的公式；
  6. 输出直接变更、受影响集合、重算顺序和前后值差异。
- 所有版本是完整且不可变的 SQLite 快照。
- 可从任意历史版本创建分支；合并前输出合并基线、双方变更、单元格冲突和合并后语义校验。
- 版本历史是 `parent_id`（常规提交父版本）与 `merge_source_id`（合并来源版本）共同构成的 **版本 DAG**：
  - 合并基线按 DAG 偏序求所有极大共同祖先，重复合并时能识别“目标已合入来源某版本”这一关系，
    不会把目标分支对已合入单元格的后续修改误报为冲突；
  - 存在多个互不支配的基线（criss-cross）时，按版本 id 排序后递归做确定性三路合并合成**虚拟基线**，
    与遍历顺序无关；基线间取值不一致的单元格标记为冲突哨兵，只有双方最终取值一致才会自动合并；
  - 预览与提交使用同一基线规则；提交携带预览给出的目标头 `expected_version_number` 做原子检查，
    真正的双边不同修改仍返回冲突，失败时不移动分支头。
- 提交使用 `BEGIN IMMEDIATE` 事务、全局递增版本号和 `expected_version_number` 乐观并发检查。
- 版本化约束门禁：
  - 针对单元格或连续区域定义带名称的规则，类型支持数值范围（`number_range`）、非空（`not_empty`）、
    允许值（`allowed_values`）、唯一性（`unique`），每条规则可附带可选条件公式（条件不成立时规则不适用）；
  - 约束集本身不可变：创建与修改都生成新的版本号，历史版本可随时取回当时规则；
  - 约束集版本通过“绑定”挂到分支上，绑定记录同时通过工作簿版本号与版本 id
    （`workbook_version_number` / `workbook_version_id`）钉住当时的分支头版本；
  - 支持**版本级绑定**：可把不可变约束集版本直接钉在任意指定的历史工作簿版本
    （请求带 `version_id`，可省略 `branch`；同时提供二者时会校验该版本确实属于
    该分支）。版本与约束集版本的关联持久化在 `version_constraint_set` 表中，
    每次提交也会随事务记录当时通过门禁所用的规则版本，因此查看任意历史版本
    都能恢复当时生效的完整规则，后续换绑不会改写历史；
  - 旧版数据库（只有分支绑定、关联表缺失）在首次打开时自动迁移：
    回填 `workbook_version_id` 并把历史绑定钉住的版本恢复为版本↔约束集关联，
    迁移幂等，可重复执行；分支绑定行为与旧库完全兼容；
  - 预演、提交与分支合并时，在增量重算完成后按稳定顺序只评估受影响规则，
    失败条目包含单元格、实际值、依赖路径与约束集版本，任何失败都不会写入版本或移动分支头；
  - 约束集的创建、修改、绑定（含版本级钉住与重复请求）同样使用
    `expected_version_number` 做原子校验，冲突返回 `409` 且不写入记录、不移动分支头。
- 首次启动自动写入固定示例数据（含一个绑定到 `main` 的示例约束集）。

示例工作簿包含三张表：`Inputs`、`Ledger`、`Summary`，其中有数量、单价、跨表乘法、`SUM` 连续区域、`IF` 和跨表汇总。

## Docker Compose 一键启动

要求安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后访问：

- Web 控制台：<http://localhost:8000/>
- Swagger API：<http://localhost:8000/schema/swagger>
- ReDoc API：<http://localhost:8000/schema/redoc>
- OpenAPI JSON：<http://localhost:8000/schema/openapi.json>
- 健康检查：<http://localhost:8000/api/health>

SQLite 数据库保存在 Docker 命名卷 `spreadsheet-data` 中。停止后数据仍保留；若要重新生成示例数据库，可执行：

```bash
docker compose down -v
docker compose up --build
```

## 本地直接运行

建议使用 Python 3.11+。

```bash
python3 -m pip install -r requirements.txt
SPREADSHEET_DB=./data/spreadsheet.db \
  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后访问 <http://127.0.0.1:8000/>。

删除 `./data/spreadsheet.db*` 后重启会重新生成固定示例数据。

## 运行测试

后端测试：

```bash
python3 -m unittest discover -s tests -v
```

页面切换回归测试（需要 Node.js，依赖仅用于测试，不进入运行镜像）：

```bash
npm install
npm test
```

测试覆盖增量拓扑顺序、源位置类型错误、错误传播路径、循环最短诊断、缺失引用、分支合并冲突、成功合并、版本号冲突，以及重复合并的 DAG 基线选择、预览/提交同一基线的原子版本检查和 criss-cross 多基线虚拟基线。约束门禁部分覆盖：约束集创建/修改/绑定的 `expected_version_number` 原子校验与重复请求、四类规则与条件公式、受影响规则的稳定评估顺序、失败时的依赖路径、失败不写入版本也不移动分支头、跨分支规则冲突、合并被目标分支门禁拦截，以及历史版本可复现当时约束集版本。

版本级绑定部分额外覆盖：

- 旧版数据库迁移（新增列与关联表的回填）与迁移幂等、新库结构与兼容旧分支绑定；
- 并发钉住/并发创建约束集的原子性（只有一个请求成功，其余 `409`，不写脏记录）；
- HTTP API：按分支绑定、按 `version_id` 钉住、版本归属校验（400/404）、
  缺少目标（400）、重复请求（409）、历史版本详情与
  `GET /api/versions/{id}/constraint-set` 审计查询、冲突不移动分支头；
- 历史查询：后续换绑不改写已提交版本的规则，版本级钉住只影响指定版本；
- 页面切换（jsdom 回归）：查看历史版本时恢复该版本当时规则、钉住控件只在
  历史版本可用、钉住请求只带 `version_id` 且 `expected_version_number`
  取归属分支绑定代数、历史版本下编辑被阻止。


## 页面使用方式

1. 在顶部选择分支；默认为 `main`。
2. 点击某个单元格，在网格上方公式栏输入：
   - 数字：`20`
   - 文本：直接输入 `铅笔`，或用双引号输入 `"铅笔"`
   - 公式：以 `=` 开头，例如 `=Inputs!B2*Inputs!C2`
3. 点击“增量预演”只做校验和审计，不写入版本。
4. 点击“原子提交版本”生成新的不可变版本；失败时旧版本和分支头都不会改变。
5. 选择历史版本可只读查看该版本的网格与完整依赖边；可从任意版本创建分支。
6. 右侧“版本化约束门禁”面板可查看当前分支绑定与生效规则、浏览约束集各历史版本、
   编辑并保存约束集新版本、把所选版本绑定到当前分支，并查看绑定历史。
   切换到某个历史版本时，面板顶部显示该版本**当时生效（或被钉住）的规则**而非
   分支头规则；在历史版本勾选“钉到当前查看的历史版本”再点绑定按钮，可把所选
   约束集版本直接钉在该版本（请求带 `version_id`，后端校验版本归属），不移动
   分支头；不勾选则保持绑定到当前分支头的行为。
7. “约束门禁结果”面板在预演、提交或合并后列出每条失败规则的单元格、实际值、
   依赖路径与约束集版本；查看历史版本时也会显示该版本提交时的门禁审计。
8. 右侧面板还可查看：
   - 入边/出边和跨单元格依赖路径；
   - 受影响子图；
   - 拓扑重算顺序；
   - 每个单元格的编辑前/后内容与值；
   - 最短循环或首个可定位类型错误，以及错误传播路径；
   - 分支合并冲突清单与跨分支规则冲突。

## 主要 HTTP API

### 查询

```http
GET /api/workbook
GET /api/branches
GET /api/versions?branch=main
GET /api/versions/1
GET /api/health
```

`GET /api/versions/{id}` 返回：

- `version`：版本号、父版本、合并来源、分支、说明、审计信息，以及
  `effective_constraint_set`（该版本当时生效的约束集版本与完整规则）；
- `cells`：该版本完整单元格快照和公式 AST；
- `edges`：依赖边，源单元格依赖目标单元格，并带公式字符位置。

也可以单独查询某历史版本当时生效的规则（审计查询）：

```http
GET /api/versions/1/constraint-set
```

```json
{
  "version_id": 1,
  "constraint_set": {
    "constraint_set": "示例约束集",
    "constraint_set_id": 1,
    "constraint_set_version": 1,
    "source": "pinned",
    "pinned_at": "2026-01-01T00:00:00Z",
    "rules": []
  }
}
```

`source` 为 `pinned` 表示该版本被显式钉住（分支头绑定或版本级钉住），
为 `commit` 表示该版本提交时按当时分支生效的绑定通过门禁。该版本提交时
分支未绑定约束集时 `constraint_set` 为 `null`。

### 预演，不写库

```bash
curl -s http://localhost:8000/api/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "branch": "main",
    "expected_version_number": 1,
    "edits": [
      {"sheet": "Inputs", "address": "B2", "content": "20"}
    ]
  }'
```

返回中重点查看：

```json
{
  "ok": true,
  "audit": {
    "changed": ["Inputs!B2"],
    "affected": ["Inputs!B2", "Ledger!A2", "Ledger!B6", "Ledger!B8", "Summary!B1"],
    "recalculation_order": ["Ledger!A2", "Ledger!B6", "Ledger!B8", "Summary!B1"],
    "diffs": []
  }
}
```

### 原子提交

```bash
curl -s http://localhost:8000/api/commits \
  -H 'Content-Type: application/json' \
  -d '{
    "branch": "main",
    "expected_version_number": 1,
    "message": "提高铅笔数量",
    "edits": [
      {"sheet": "Inputs", "address": "B2", "content": "20"}
    ]
  }'
```

如果分支头已经不是预期版本，返回 `409 VERSION_CONFLICT`。如果语义校验失败，返回 `422` 且不会插入版本。若分支绑定了约束集，提交还会在增量重算后经过约束门禁：任一规则失败返回 `422`，响应 `detail.constraint_violations` 列出失败明细，同样不会写入版本或移动分支头。

### 从任意版本创建分支

```bash
curl -s http://localhost:8000/api/branches \
  -H 'Content-Type: application/json' \
  -d '{"name": "feature-tax", "from_version_id": 1}'
```

### 合并预览与提交

```bash
curl -s http://localhost:8000/api/merges/preview \
  -H 'Content-Type: application/json' \
  -d '{"target_branch": "main", "source_branch": "feature-tax"}'

curl -s http://localhost:8000/api/merges \
  -H 'Content-Type: application/json' \
  -d '{
    "target_branch": "main",
    "source_branch": "feature-tax",
    "expected_version_number": 2
  }'
```

`expected_version_number` 取自合并预览返回的 `target_version.version_number`：在预览与提交之间目标分支头若被其他操作推进，提交返回 `409 VERSION_CONFLICT`，且不会生成版本或移动分支头。

预览按三路合并比较合并基线、目标分支和来源分支：

- 合并基线沿 `parent_id` 与 `merge_source_id` 两类边在版本 DAG 上求极大共同祖先；
- 单个基线时响应中的 `ancestor_version` 指向该版本；多个互不支配的基线时 `virtual_base` 为 `true`，`merge_bases` 列出全部基线，系统用它们确定性合成虚拟基线；
- 双方都修改且结果不同：列为冲突；
- 只有来源修改：纳入自动合并编辑；
- 文本合并可通过后，还会运行一次完整增量重算；
- 增量重算完成后按目标分支当前绑定的约束集评估受影响规则；
- 存在单元格冲突、重算诊断或约束违规时，合并 API 返回 `409`，不生成合并版本。

预览响应中的约束相关字段：

- `constraint_sets`：目标/来源分支各自绑定的约束集与版本（未绑定为 `null`）；
- `constraint_conflicts`：跨分支规则冲突清单，按规则名对比双方绑定的约束集版本
  （`changed` 双方定义不同、`source_only` 仅来源有、`target_only` 仅目标有，
  以及仅一方绑定的 `source_only_binding` / `target_only_binding`）；
- `constraint_violations`：合并后数据按目标分支约束集评估出的违规；
  合并后以目标分支绑定的约束集为准，规则冲突本身不阻塞合并，数据违规才阻塞。

### 版本化约束门禁

规则是带名称的 JSON 对象：

```json
{
  "name": "数量非负",
  "type": "number_range",
  "target": "Inputs!B2:B4",
  "min": 0,
  "max": 1000,
  "condition": "=Ledger!B8=\"是\""
}
```

- `type`：`number_range`（数值范围，需 `min`/`max` 至少其一）、`not_empty`（非空）、
  `allowed_values`（允许值，需非空 `values` 列表）、`unique`（唯一性，区域内非空值不得重复）；
- `target`：`工作表!A1` 或 `工作表!A1:B2` 连续区域；
- `condition`：可选条件公式，对整个规则求值一次；结果为 `FALSE` 时规则本次不适用，
  结果不是布尔值或求值出错时记为违规；
- 空白单元格在数值范围/允许值/唯一性检查中视为不适用，非空规则专门检查空白。

约束集查询：

```http
GET /api/constraint-sets
GET /api/constraint-sets/示例约束集
GET /api/constraint-bindings
GET /api/constraint-bindings/history?branch=main
```

创建约束集（`expected_version_number` 必须为 0，即“期望尚不存在”）：

```bash
curl -s http://localhost:8000/api/constraint-sets \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "预算规则",
    "message": "初版",
    "expected_version_number": 0,
    "rules": [
      {"name": "数量非负", "type": "number_range", "target": "Inputs!B2:B4", "min": 0},
      {"name": "项目必填", "type": "not_empty", "target": "Inputs!A2:A4"},
      {"name": "项目名称唯一", "type": "unique", "target": "Inputs!A2:A4"},
      {"name": "大额标记合法", "type": "allowed_values", "target": "Ledger!B8", "values": ["是", "否"]}
    ]
  }'
```

修改约束集（生成新的不可变版本；`expected_version_number` 必须等于当前最新版本号）：

```bash
curl -s http://localhost:8000/api/constraint-sets/预算规则/versions \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "收紧数量上限",
    "expected_version_number": 1,
    "rules": [
      {"name": "数量非负", "type": "number_range", "target": "Inputs!B2:B4", "min": 0, "max": 100}
    ]
  }'
```

绑定约束集版本到分支（`expected_version_number` 是该分支当前的绑定代数，未绑定时为 0）：

```bash
curl -s http://localhost:8000/api/constraint-bindings \
  -H 'Content-Type: application/json' \
  -d '{
    "branch": "main",
    "constraint_set": "预算规则",
    "constraint_set_version": 2,
    "expected_version_number": 1
  }'
```

把不可变约束集版本**钉到指定的历史工作簿版本**（版本级绑定）。此时可省略
`branch`，后端按版本归属分支落绑定记录；若同时提供 `branch`，会校验该版本
确实属于该分支，否则返回 `400`。`expected_version_number` 仍是归属分支当前
的绑定代数：

```bash
curl -s http://localhost:8000/api/constraint-bindings \
  -H 'Content-Type: application/json' \
  -d '{
    "version_id": 3,
    "constraint_set": "预算规则",
    "constraint_set_version": 1,
    "expected_version_number": 2
  }'
```

版本级钉住不移动分支头、不产生工作簿版本；同一历史版本重复钉住时，该版本
的生效规则以最新绑定为准，但相邻其他版本的关联不受影响。`branch` 与
`version_id` 至少提供一个，否则返回 `400`；版本不存在返回 `404`。

三类写操作都在 `BEGIN IMMEDIATE` 事务内先校验 `expected_version_number` 再写入：
重复发送同一请求会因版本号/绑定代数已前进而返回 `409 VERSION_CONFLICT`，
不会产生重复版本或重复绑定，也不会移动分支头。绑定记录同时钉住当时的
工作簿版本（`workbook_version_number` 与 `workbook_version_id`），版本级
钉住或分支头绑定会把版本↔约束集版本关联写入 `version_constraint_set`；
约束集旧版本永远可通过 `GET /api/constraint-sets/{name}` 取回。

预演/提交/合并时，系统在增量重算完成后评估“受影响规则”（目标区域或条件公式依赖
与受影响集合相交的规则），按规则定义顺序与单元格位置稳定排序返回违规：

```json
{
  "ok": false,
  "constraint_violations": [
    {
      "code": "CONSTRAINT_VIOLATION",
      "rule": "数量非负",
      "rule_type": "number_range",
      "cell": "Inputs!B2",
      "actual": {"kind": "number", "value": -5.0},
      "expectation": "值 ≥ 0.0",
      "path": ["Inputs!B2"],
      "constraint_set": "示例约束集",
      "constraint_set_version": 1
    }
  ]
}
```

每个成功提交的版本在 `audit.constraints` 中记录当时评估的约束集、版本与规则清单，
因此历史版本可以随时复现当时的门禁判定。

### 重复合并场景复验

下面的序列对应“目标分支合入来源某版本 → 目标修改已合入单元格、来源继续改其他单元格 → 再次合并”：

```bash
# 1) main 与 feature 分叉，feature 改 B2，第一次合并进 main
curl -s http://localhost:8000/api/commits -H 'Content-Type: application/json' \
  -d '{"branch":"main","expected_version_number":1,"message":"n","edits":[{"sheet":"Ledger","address":"B2","content":"=1+1"}]}' >/dev/null
curl -s http://localhost:8000/api/branches -H 'Content-Type: application/json' \
  -d '{"name":"feature","from_version_id":2}' >/dev/null
curl -s http://localhost:8000/api/commits -H 'Content-Type: application/json' \
  -d '{"branch":"feature","expected_version_number":2,"message":"f","edits":[{"sheet":"Inputs","address":"B2","content":"30"}]}' >/dev/null
curl -s http://localhost:8000/api/merges -H 'Content-Type: application/json' \
  -d '{"target_branch":"main","source_branch":"feature"}' >/dev/null

# 2) main 修改“已合入的” B2；feature 改另一个单元格 C2
curl -s http://localhost:8000/api/commits -H 'Content-Type: application/json' \
  -d '{"branch":"main","message":"main edits merged cell","edits":[{"sheet":"Inputs","address":"B2","content":"40"}]}' >/dev/null
curl -s http://localhost:8000/api/commits -H 'Content-Type: application/json' \
  -d '{"branch":"feature","expected_version_number":3,"message":"feature other cell","edits":[{"sheet":"Inputs","address":"C2","content":"3"}]}' >/dev/null

# 3) 再次预览：ok=true，无冲突；来源独有变更只有 Inputs!C2
curl -s http://localhost:8000/api/merges/preview -H 'Content-Type: application/json' \
  -d '{"target_branch":"main","source_branch":"feature"}'

# 4) 提交后 main 新快照同时保留 B2=40 与 C2=3
curl -s http://localhost:8000/api/merges -H 'Content-Type: application/json' \
  -d '{"target_branch":"main","source_branch":"feature"}'
```

## 诊断模型

诊断对象包含稳定错误码、单元格、公式、零基字符区间、目标引用或传播路径。

- `CYCLE_ERROR`：返回边数最少的循环路径，如 `A2 → A3 → A2`。
- `MISSING_REFERENCE`：工作表不存在或引用越过网格边界，并定位引用片段。
- `VALUE_ERROR`：例如文本参加算术，定位到运算符或函数参数。
- `DIV0_ERROR`：除数为零。
- `NAME_ERROR`：未知函数。
- `PARSE_ERROR`：公式语法错误。
- `VERSION_CONFLICT`：提交、约束集写操作或绑定时版本号/绑定代数与预期不一致。
- `CONSTRAINT_VIOLATION`：约束门禁失败，包含规则名、单元格、实际值、期望值、
  依赖路径与约束集版本。

## 数据与版本设计

每个版本保存：

- 完整 `cell` 快照，而不是仅保存差异；
- 公式 AST JSON 与当前值；
- 解析出的全部依赖边；
- 父版本、合并来源、分支名、全局版本号和审计 JSON（含约束门禁评估记录）。

这样读取任意版本是一次确定性快照查询；删除或修改后续版本不会影响历史版本。版本号在单个 SQLite 写事务中取 `MAX(version_number)+1`，配合 `BEGIN IMMEDIATE` 避免并发提交重复。

约束门禁使用三张表：

- `constraint_set`：不可变的约束集版本，`UNIQUE(name, version_number)`，只插入不更新；
- `constraint_binding`：分支绑定历史，`UNIQUE(branch_name, binding_number)`，
  每行钉住一个约束集版本与当时的工作簿版本（同时保存版本号与 `workbook_version_id`）；
  分支当前生效的绑定是绑定代数最大的一行；
- `version_constraint_set`：工作簿版本与约束集版本的直接关联，每个工作簿版本
  至多一行（`workbook_version_id` 主键）。`source='commit'` 表示随提交事务
  记录该版本通过门禁所用的规则；`source='pinned'` 表示被分支头绑定或版本级
  钉住显式指定。历史版本详情与审计查询优先读这张表，缺失时再沿创建分支回放
  绑定历史兜底。

升级旧版数据库时自动迁移：为 `constraint_binding` 增加并回填
`workbook_version_id`、建立上述关联表并把历史绑定钉住的版本恢复为 `pinned`
关联；迁移幂等，新库直接使用新结构。

版本之间的 `parent_id` 与 `merge_source_id` 共同构成有向无环图：常规提交只有 `parent_id`，合并提交同时带目标头（`parent_id`）与来源头（`merge_source_id`）。合并基线（极大共同祖先）必须沿两类边按偏序计算，只沿 `parent_id` 回溯会在重复合并时丢失“来源已被目标合入”的关系，从而把目标分支对已合入单元格的后续修改误报为双方冲突。
