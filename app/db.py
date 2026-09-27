"""SQLite persistence and deterministic workbook recalculation service."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .constraints import evaluate_rules, validate_rules
from .errors import FormulaError
from .evaluator import evaluate_formula
from .formula import ast_from_json, ast_to_json, iter_references, parse_formula
from .model import Address, column_index, column_name

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS workbook (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    columns INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sheet (
    workbook_id INTEGER NOT NULL REFERENCES workbook(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (workbook_id, name),
    UNIQUE (workbook_id, position)
);
CREATE TABLE IF NOT EXISTS branch (
    workbook_id INTEGER NOT NULL REFERENCES workbook(id),
    name TEXT PRIMARY KEY,
    head_version_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workbook_id INTEGER NOT NULL REFERENCES workbook(id),
    version_number INTEGER NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES version(id),
    merge_source_id INTEGER REFERENCES version(id),
    branch_name TEXT NOT NULL REFERENCES branch(name),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    audit_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS cell (
    version_id INTEGER NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    column_index INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('constant', 'formula')),
    value_json TEXT NOT NULL,
    formula_json TEXT,
    PRIMARY KEY (version_id, sheet_name, column_index, row_index)
);
CREATE TABLE IF NOT EXISTS edge (
    version_id INTEGER NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    source_sheet TEXT NOT NULL,
    source_column INTEGER NOT NULL,
    source_row INTEGER NOT NULL,
    target_sheet TEXT NOT NULL,
    target_column INTEGER NOT NULL,
    target_row INTEGER NOT NULL,
    reference_kind TEXT NOT NULL CHECK (reference_kind IN ('cell', 'range')),
    ref_start INTEGER NOT NULL,
    ref_end INTEGER NOT NULL,
    PRIMARY KEY (version_id, source_sheet, source_column, source_row,
                 target_sheet, target_column, target_row, ref_start, ref_end)
);
CREATE TABLE IF NOT EXISTS constraint_set (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version_number)
);
CREATE TABLE IF NOT EXISTS constraint_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_name TEXT NOT NULL REFERENCES branch(name),
    binding_number INTEGER NOT NULL,
    constraint_set_id INTEGER NOT NULL REFERENCES constraint_set(id),
    workbook_version_number INTEGER NOT NULL,
    -- 分支绑定时钉住的分支头版本（旧库通过迁移按 workbook_version_number 回填）。
    workbook_version_id INTEGER REFERENCES version(id),
    created_at TEXT NOT NULL,
    UNIQUE (branch_name, binding_number)
);
-- 版本级钉版状态：与分支绑定（constraint_binding）完全分开。
-- 钉版只改变目标历史版本的规则关联，绝不追加分支绑定、不改变分支当前
-- 规则与绑定代数，因此预演/提交/合并门禁不受钉版影响。
-- pin_number 是“目标版本”的独立修订号（从 1 开始，未钉版等价于 0），
-- expected_version_number 校验该独立修订号；同一版本的并发/重复钉版
-- 只能成功一次，其余 409 且不写记录、不移动分支头。
CREATE TABLE IF NOT EXISTS version_pin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workbook_version_id INTEGER NOT NULL REFERENCES version(id) ON DELETE CASCADE,
    branch_name TEXT NOT NULL REFERENCES branch(name),
    pin_number INTEGER NOT NULL,
    constraint_set_id INTEGER NOT NULL REFERENCES constraint_set(id),
    workbook_version_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workbook_version_id, pin_number)
);
-- 工作簿版本与当时生效的约束集版本的直接关联（版本级状态）：
-- commit = 该版本提交时按分支生效绑定评估并随提交记录；
-- pinned = 用户显式把约束集钉在该历史版本（重复钉版以最新修订为准）。
CREATE TABLE IF NOT EXISTS version_constraint_set (
    workbook_version_id INTEGER PRIMARY KEY REFERENCES version(id) ON DELETE CASCADE,
    constraint_set_id INTEGER NOT NULL REFERENCES constraint_set(id),
    source TEXT NOT NULL CHECK (source IN ('commit', 'pinned')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cell_version ON cell(version_id);
CREATE INDEX IF NOT EXISTS idx_edge_version ON edge(version_id);
CREATE INDEX IF NOT EXISTS idx_version_branch ON version(branch_name, version_number);
CREATE INDEX IF NOT EXISTS idx_constraint_set_name ON constraint_set(name, version_number);
CREATE INDEX IF NOT EXISTS idx_binding_branch ON constraint_binding(branch_name, binding_number);
CREATE INDEX IF NOT EXISTS idx_version_pin_branch ON version_pin(branch_name, id);
CREATE INDEX IF NOT EXISTS idx_version_constraint_set ON version_constraint_set(constraint_set_id);
"""

FIXED_TIME = "2026-01-01T00:00:00Z"

# 多个互不支配的合并基线对同一单元格取值不一致时，虚拟基线使用该确定性
# 哨兵值，使三路合并必然报告真正的双边冲突，而不是按遍历顺序任选基线。
BASE_CONFLICT = "\x00__merge_base_conflict__"
# 虚拟基线递归合并的深度上限；版本 DAG 规模很小，正常路径远小于此值。
VIRTUAL_BASE_DEPTH_LIMIT = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def address_key(sheet: str, column: int, row: int) -> str:
    return f"{sheet}!{column_name(column)}{row + 1}"


def parse_a1(sheet: str, ref: str) -> tuple[str, int, int]:
    if "!" in ref:
        sheet, ref = ref.rsplit("!", 1)
        sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
    letters = "".join(ch for ch in ref if ch.isalpha())
    digits = "".join(ch for ch in ref if ch.isdigit())
    if not letters or not digits:
        raise ValueError(f"无效的单元格地址: {ref}")
    return sheet, column_index(letters), int(digits) - 1


def constant_value(content: str) -> dict | None:
    try:
        number = float(content)
        if math.isfinite(number):
            return {"kind": "number", "value": number}
    except ValueError:
        pass
    if len(content) >= 2 and content[0] == '"' and content[-1] == '"':
        return {"kind": "text", "value": content[1:-1].replace('""', '"')}
    return {"kind": "text", "value": content}


def value_to_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default=None):
    return default if value is None else json.loads(value)


class Database:
    def __init__(self, path: str = ":memory:", seed: bool = True):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._write_lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self._migrate_legacy_schema()
        if seed and self.conn.execute("SELECT COUNT(*) FROM workbook").fetchone()[0] == 0:
            self.seed()

    @classmethod
    def fixed_time(cls) -> str:
        return FIXED_TIME

    def _migrate_legacy_schema(self) -> None:
        """把旧版数据库升级到“分支绑定 / 版本钉版”两类状态分离的模型。

        - 旧库的 ``constraint_binding`` 没有 ``workbook_version_id``：先按记录
          中的工作簿版本号回填该列；
        - 历史钉版串改门禁修复前，版本级钉版也会在 ``constraint_binding``
          里追加一行（绑定代数随之前进），使分支门禁被宽松规则替换。迁移时
          把这些记录拆出到独立的 ``version_pin`` 表：判定标准是该记录钉住的
          版本低于本分支历史上绑定过的最大工作簿版本号——真正的分支换绑
          只会钉住当时的分支头（单调不减），钉历史版本才会回指旧版本。
          拆出后分支绑定代数出现的空号保留不重排（绑定不可变，代数即身份），
          “当前生效绑定取代数最大者”的门禁语义因此恢复；
        - 新建关联表 ``version_constraint_set`` 后恢复历史版本↔规则关联：
          分支绑定钉住的头版本与钉版钉住的精确版本都写为 ``pinned``；未被
          精确钉住的版本由查询时沿分支回放绑定兜底。

        迁移幂等：列、表与关联行已存在时不会重复写入、重复加列或重复搬迁。
        """
        binding_cols = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(constraint_binding)")
        }
        if "workbook_version_id" not in binding_cols:
            self.conn.execute(
                "ALTER TABLE constraint_binding ADD COLUMN workbook_version_id INTEGER REFERENCES version(id)"
            )
            self.conn.execute(
                """UPDATE constraint_binding
                      SET workbook_version_id = (
                          SELECT id FROM version
                           WHERE version.version_number = constraint_binding.workbook_version_number
                      )"""
            )
        # 新列存在后再建索引（新库与旧库都在此创建，幂等）。
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_binding_version ON constraint_binding(workbook_version_id)"
        )
        self._split_legacy_pins()
        self._migrate_version_set_links()

    def _split_legacy_pins(self) -> None:
        """把历史钉版误写入 constraint_binding 的记录搬到 version_pin。

        幂等：version_pin 已有数据（含目标版本已有修订）时跳过搬迁。
        搬迁按 (版本, 原绑定 id) 稳定排序后在每个目标版本内从 1 重新编号，
        使钉版修订号只与目标版本相关，不依赖分支绑定代数。

        判定：真正的分支换绑只会钉住“当时的分支头”，沿绑定代数钉住的
        工作簿版本号单调不减；版本级钉版则把规则钉到任意历史版本，必然
        回指比此前绑定更旧的版本。因此对每条分支记录按绑定代数计算前缀
        最大值，凡钉住版本号小于该前缀最大值的，都是串入分支链的钉版。
        （分支可从历史版本建出，其绑定序列从头开始，前缀判定同样成立。）
        """
        if self.conn.execute("SELECT COUNT(*) FROM version_pin").fetchone()[0] > 0:
            return
        rows = self.conn.execute(
            """SELECT * FROM constraint_binding
               ORDER BY branch_name, binding_number, id"""
        ).fetchall()
        prefix_max: dict[str, int] = {}
        pin_rows_by_version: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            branch = row["branch_name"]
            current_max = prefix_max.get(branch)
            if current_max is not None and row["workbook_version_number"] < current_max:
                pin_rows_by_version.setdefault(row["workbook_version_id"], []).append(row)
            prefix_max[branch] = max(
                row["workbook_version_number"],
                current_max if current_max is not None else row["workbook_version_number"],
            )
        pin_rows = []
        for version_id in sorted(pin_rows_by_version):
            for pin_number, row in enumerate(pin_rows_by_version[version_id], start=1):
                pin_rows.append((
                    row["id"], row["workbook_version_id"], row["branch_name"], pin_number,
                    row["constraint_set_id"], row["workbook_version_number"], row["created_at"],
                ))
        if not pin_rows:
            return
        with self.conn:
            self.conn.executemany(
                """INSERT INTO version_pin
                       (id,workbook_version_id,branch_name,pin_number,constraint_set_id,
                        workbook_version_number,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                pin_rows,
            )
            self.conn.executemany(
                "DELETE FROM constraint_binding WHERE id=?",
                [(row[0],) for row in pin_rows],
            )

    def _migrate_version_set_links(self) -> None:
        """从分支绑定与钉版历史恢复版本↔约束集的 pinned 关联。

        同一版本被多条记录钉住时，先取创建该版本的分支的记录，再取较早的
        记录；逐版本窗口式选择保证结果确定，不依赖 INSERT...SELECT 的遍历
        顺序。幂等：version_constraint_set 已有数据时不重写。
        """
        if self.conn.execute("SELECT COUNT(*) FROM version_constraint_set").fetchone()[0] > 0:
            return
        rows = self.conn.execute(
            """SELECT v.id AS version_id, picked.constraint_set_id, picked.created_at
                 FROM version v
                 JOIN (
                        SELECT b.workbook_version_id AS version_id,
                               b.constraint_set_id, b.created_at, b.branch_name, b.id
                          FROM constraint_binding b
                         WHERE b.workbook_version_id IS NOT NULL
                         UNION ALL
                        SELECT p.workbook_version_id AS version_id,
                               p.constraint_set_id, p.created_at, p.branch_name, p.id
                          FROM version_pin p
                      ) picked
                   ON picked.version_id = v.id
                ORDER BY v.id, (v.branch_name = picked.branch_name) DESC, picked.id"""
        ).fetchall()
        chosen: dict[int, sqlite3.Row] = {}
        for row in rows:
            chosen.setdefault(row["version_id"], row)
        self.conn.executemany(
            """INSERT OR IGNORE INTO version_constraint_set
                   (workbook_version_id, constraint_set_id, source, created_at)
               VALUES (?, ?, 'pinned', ?)""",
            [(r["version_id"], r["constraint_set_id"], r["created_at"]) for r in chosen.values()],
        )

    def workbook(self) -> dict:
        row = self.conn.execute(
            """SELECT w.id, w.name, w.columns, w.rows, w.created_at,
                      json_group_array(json_object('position', s.position, 'name', s.name)) AS sheets
               FROM workbook w JOIN sheet s ON s.workbook_id = w.id
               WHERE w.id = 1 GROUP BY w.id"""
        ).fetchone()
        data = dict(row)
        data["sheets"] = sorted(json.loads(data["sheets"]), key=lambda x: x["position"])
        return data

    def _sheet_map(self) -> tuple[dict[str, int], int, int]:
        wb = self.workbook()
        return {s["name"]: s["position"] for s in wb["sheets"]}, wb["columns"], wb["rows"]

    def branch(self, name: str) -> dict:
        row = self.conn.execute("SELECT * FROM branch WHERE name = ?", (name,)).fetchone()
        if not row:
            raise KeyError(f"分支不存在: {name}")
        return dict(row)

    def branches(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT name, head_version_id, created_at FROM branch ORDER BY name")]

    def version(self, version_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM version WHERE id = ?", (version_id,)).fetchone()
        if not row:
            raise KeyError(f"版本不存在: {version_id}")
        data = dict(row)
        data["audit"] = json_loads(data.pop("audit_json"), {})
        data["effective_constraint_set"] = self._effective_version_set(data)
        return data

    def versions(self, branch_name: str | None = None) -> list[dict]:
        if branch_name:
            self.branch(branch_name)
            rows = self.conn.execute(
                """WITH RECURSIVE chain(id) AS (
                       SELECT head_version_id FROM branch WHERE name=?
                       UNION
                       SELECT parent_id FROM version JOIN chain ON version.id = chain.id
                       WHERE parent_id IS NOT NULL
                   )
                   SELECT v.* FROM version v JOIN chain c ON v.id = c.id
                   ORDER BY v.version_number""",
                (branch_name,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM version ORDER BY version_number").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["audit"] = json_loads(item.pop("audit_json"), {})
            item["effective_constraint_set"] = self._effective_version_set(item)
            result.append(item)
        return result

    _VERSION_SET_SELECT = """
        SELECT vcs.source, vcs.created_at AS pinned_at,
               s.id AS set_id, s.name AS set_name,
               s.version_number AS set_version, s.rules_json
          FROM version_constraint_set vcs
          JOIN constraint_set s ON s.id = vcs.constraint_set_id
         WHERE vcs.workbook_version_id=?
    """

    def _effective_version_set_row(self, version: dict):
        """返回某工作簿版本生效的约束集行：显式版本关联优先。

        没有关联记录时（迁移前的旧数据），沿创建该版本的分支回放绑定，
        取钉住的工作簿版本号不大于目标版本的最后一条绑定，保证历史详情
        也能恢复当时规则。
        """
        row = self.conn.execute(self._VERSION_SET_SELECT, (version["id"],)).fetchone()
        if row is not None:
            return row
        return self.conn.execute(
            """SELECT 'commit' AS source, b.created_at AS pinned_at,
                      s.id AS set_id, s.name AS set_name,
                      s.version_number AS set_version, s.rules_json
                 FROM constraint_binding b
                 JOIN constraint_set s ON s.id = b.constraint_set_id
                WHERE b.branch_name=? AND b.workbook_version_number<=?
                ORDER BY b.workbook_version_number DESC, b.binding_number DESC
                LIMIT 1""",
            (version["branch_name"], version["version_number"]),
        ).fetchone()

    def effective_version_constraint_set(self, version_id: int) -> dict | None:
        """公开接口：某历史版本当时生效的约束集版本与完整规则。"""
        return self._effective_version_set(self.version(version_id))

    def _effective_version_set(self, version: dict) -> dict | None:
        row = self._effective_version_set_row(version)
        if row is None:
            return None
        return {
            "constraint_set": row["set_name"],
            "constraint_set_id": row["set_id"],
            "constraint_set_version": row["set_version"],
            "rules": json.loads(row["rules_json"]),
            # pinned：用户显式钉在该版本（或绑定分支头时记录）；
            # commit：该版本提交时按当时分支生效绑定记录/回放。
            "source": row["source"],
            "pinned_at": row["pinned_at"],
            # 该目标版本的独立钉版修订号（未钉版为 0）；钉版请求的
            # expected_version_number 必须等于此值，成功后前进 1。
            "pin_revision": self.pin_revision(version["id"]),
        }

    def cells(self, version_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cell WHERE version_id=? ORDER BY sheet_name, row_index, column_index",
            (version_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["address"] = f"{column_name(item['column_index'])}{item['row_index'] + 1}"
            item["value"] = json.loads(item.pop("value_json"))
            raw_formula = json_loads(item.pop("formula_json"), None)
            item["formula"] = ast_from_json(raw_formula) if raw_formula else None
            result.append(item)
        return result

    def cell_map(self, version_id: int) -> dict[tuple[str, int, int], dict]:
        return {(c["sheet_name"], c["column_index"], c["row_index"]): c for c in self.cells(version_id)}

    def edges(self, version_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM edge WHERE version_id=? ORDER BY source_sheet, source_row, source_column, target_row, target_column, ref_start",
            (version_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source"] = address_key(item["source_sheet"], item["source_column"], item["source_row"])
            item["target"] = address_key(item["target_sheet"], item["target_column"], item["target_row"])
            result.append(item)
        return result

    def seed(self) -> None:
        now = FIXED_TIME
        with self.conn:
            self.conn.execute(
                "INSERT INTO workbook(id,name,columns,rows,created_at) VALUES (1,'预算与公式审查示例',8,12,?)",
                (now,),
            )
            self.conn.executemany(
                "INSERT INTO sheet(workbook_id,position,name) VALUES (1,?,?)",
                [(0, "Inputs"), (1, "Ledger"), (2, "Summary")],
            )
            self.conn.execute(
                "INSERT INTO branch(workbook_id,name,head_version_id,created_at) VALUES (1,'main',1,?)", (now,)
            )
            self.conn.execute(
                """INSERT INTO version(id,workbook_id,version_number,parent_id,merge_source_id,
                   branch_name,message,created_at,audit_json)
                   VALUES (1,1,1,NULL,NULL,'main','示例数据：常量、IF、SUM、跨表引用与连续区域',?,'{}')""",
                (now,),
            )
        seeds = {
            ("Inputs", 0, 0): ("项目", "text"), ("Inputs", 1, 0): ("数量", "text"), ("Inputs", 2, 0): ("单价", "text"),
            ("Inputs", 0, 1): ("铅笔", "text"), ("Inputs", 1, 1): ("12", "constant"), ("Inputs", 2, 1): ("2.5", "constant"),
            ("Inputs", 0, 2): ("笔记本", "text"), ("Inputs", 1, 2): ("5", "constant"), ("Inputs", 2, 2): ("8", "constant"),
            ("Inputs", 0, 3): ("胶水", "text"), ("Inputs", 1, 3): ("3", "constant"), ("Inputs", 2, 3): ("4", "constant"),
            ("Ledger", 0, 0): ("金额", "text"), ("Ledger", 0, 1): ("=Inputs!B2*Inputs!C2", "formula"),
            ("Ledger", 0, 2): ("=Inputs!B3*Inputs!C3", "formula"), ("Ledger", 0, 3): ("=Inputs!B4*Inputs!C4", "formula"),
            ("Ledger", 0, 5): ("合计", "text"), ("Ledger", 1, 5): ("=SUM(A2:A4)", "formula"),
            ("Ledger", 0, 7): ("大额订单?", "text"), ("Ledger", 1, 7): ("=IF(B6>=50,\"是\",\"否\")", "formula"),
            ("Summary", 0, 0): ("跨表总额", "text"), ("Summary", 1, 0): ("=Ledger!B6", "formula"),
            ("Summary", 0, 2): ("平均单价", "text"), ("Summary", 1, 2): ("=SUM(Inputs!C2:C4)/3", "formula"),
        }
        cells = []
        formulas = {}
        for (sheet, col, row), (content, _declared_kind) in seeds.items():
            is_formula = content.startswith("=")
            if not is_formula:
                value = constant_value(content)
                ast = None
                stored_kind = "constant"
            else:
                value = None
                ast = parse_formula(content, sheet)
                stored_kind = "formula"
                if ast is not None:
                    formulas[(sheet, col, row)] = (content, ast)
            cells.append((1, sheet, col, row, content, stored_kind, value_to_json(value), json_dumps(ast_to_json(ast)) if ast else None))
        values = self._evaluate_seed_formulas(cells, formulas)
        for i, cell in enumerate(cells):
            key = (cell[1], cell[2], cell[3])
            if key in values:
                cells[i] = (*cell[:6], value_to_json(values[key]), cell[7])
        with self.conn:
            self.conn.executemany(
                """INSERT INTO cell(version_id,sheet_name,column_index,row_index,content,kind,value_json,formula_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                cells,
            )
            self._insert_edges(1, formulas)
        self._seed_constraints()

    def _seed_constraints(self) -> None:
        """写入固定的示例约束集 v1 并绑定到 main 分支（绑定代数 1）。"""
        rules = [
            {"name": "数量非负", "type": "number_range", "target": "Inputs!B2:B4", "min": 0},
            {"name": "项目必填", "type": "not_empty", "target": "Inputs!A2:A4"},
            {"name": "项目名称唯一", "type": "unique", "target": "Inputs!A2:A4"},
            {"name": "大额标记合法", "type": "allowed_values", "target": "Ledger!B8",
             "values": ["是", "否"]},
            {"name": "大额合计非负", "type": "number_range", "target": "Summary!B1",
             "min": 0, "condition": '=Ledger!B8="是"'},
        ]
        # 固定示例数据也走同一套校验，防止种子规则与工作簿结构脱节。
        rules = validate_rules(rules, {"Inputs": 0, "Ledger": 1, "Summary": 2}, 8, 12)
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO constraint_set(name,version_number,message,rules_json,created_at)
                   VALUES ('示例约束集',1,?,?,?)""",
                ("示例规则：数量非负、项目必填且唯一、大额标记允许值、条件合计检查",
                 json_dumps(rules), FIXED_TIME),
            )
            self.conn.execute(
                """INSERT INTO constraint_binding(branch_name,binding_number,constraint_set_id,
                   workbook_version_number,workbook_version_id,created_at)
                   VALUES ('main',1,?,1,1,?)""",
                (cur.lastrowid, FIXED_TIME),
            )
            self.conn.execute(
                """INSERT INTO version_constraint_set(workbook_version_id,constraint_set_id,
                   source,created_at) VALUES (1,?,'pinned',?)""",
                (cur.lastrowid, FIXED_TIME),
            )

    def _evaluate_seed_formulas(self, cells, formulas) -> dict:
        cell_lookup = {(c[1], c[2], c[3]): c for c in cells}

        def lookup(sheet, col, row):
            key = (sheet, col, row)
            if key in result:
                return result[key]
            row_data = cell_lookup.get(key)
            if row_data is None:
                return {"kind": "blank"}
            value = json.loads(row_data[6])
            return value if value is not None else {"kind": "blank"}

        result = {}
        # Seed is known acyclic; iterating to a fixed point keeps initialization simple.
        pending = set(formulas)
        while pending:
            progressed = False
            for key in list(pending):
                content, ast = formulas[key]
                value = evaluate_formula(ast, lookup, {"sheet": key[0], "column": key[1], "row": key[2]}, content)
                if value.get("kind") != "error":
                    result[key] = value
                    pending.remove(key)
                    progressed = True
            if not progressed:
                for key in pending:
                    content, ast = formulas[key]
                    result[key] = evaluate_formula(ast, lookup, {"sheet": key[0], "column": key[1], "row": key[2]}, content)
                break
        return result

    def _insert_edges(self, version_id: int, formulas: dict) -> None:
        rows = []
        seen = set()
        for (sheet, col, row), (_content, ast) in formulas.items():
            if ast is None:
                continue
            for dep in iter_references(ast):
                if dep["kind"] == "range":
                    targets = dep["targets"]
                else:
                    targets = [{"sheet": dep["sheet"], "column": dep["column"], "row": dep["row"]}]
                for target in targets:
                    edge = (
                        version_id, sheet, col, row,
                        target["sheet"], target["column"], target["row"],
                        dep["kind"], dep["start"], dep["end"],
                    )
                    if edge not in seen:
                        seen.add(edge)
                        rows.append(edge)
        self.conn.executemany(
            """INSERT INTO edge(version_id,source_sheet,source_column,source_row,
               target_sheet,target_column,target_row,reference_kind,ref_start,ref_end)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    def validate_edits(self, branch_name: str, edits: list[dict], expected_version_number: int | None = None) -> dict:
        branch = self.branch(branch_name)
        if branch["head_version_id"] is None:
            raise KeyError("分支没有可编辑版本")
        parent = self.version(branch["head_version_id"])
        if expected_version_number is not None and parent["version_number"] != expected_version_number:
            return {
                "ok": False,
                "conflict": {
                    "code": "VERSION_CONFLICT",
                    "message": f"当前头版本为 v{parent['version_number']}，不是预期的 v{expected_version_number}",
                    "expected": expected_version_number,
                    "actual": parent["version_number"],
                },
            }
        return self.public_preview(self._recalculate(parent, edits, branch_name))

    @staticmethod
    def public_preview(preview: dict) -> dict:
        """Return the JSON-safe part of an in-progress recalculation."""
        return {key: value for key, value in preview.items() if key not in {"cells", "formulas"}}

    def commit_edits(
        self,
        branch_name: str,
        edits: list[dict],
        message: str,
        expected_version_number: int | None = None,
        merge_source_id: int | None = None,
        timestamp: str | None = None,
    ) -> dict:
        with self._write_lock:
            return self._commit_edits_impl(
                branch_name, edits, message, expected_version_number, merge_source_id, timestamp
            )

    def _commit_edits_impl(
        self,
        branch_name: str,
        edits: list[dict],
        message: str,
        expected_version_number: int | None = None,
        merge_source_id: int | None = None,
        timestamp: str | None = None,
    ) -> dict:
        branch = self.branch(branch_name)
        if branch["head_version_id"] is None:
            raise KeyError("分支没有可编辑版本")
        parent = self.version(branch["head_version_id"])
        if expected_version_number is not None and parent["version_number"] != expected_version_number:
            raise VersionConflict(expected_version_number, parent["version_number"])
        preview = self._recalculate(parent, edits, branch_name)
        if not preview["ok"]:
            raise ValidationFailed(self.public_preview(preview))

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current_head = self.conn.execute(
                "SELECT head_version_id FROM branch WHERE name=?", (branch_name,)
            ).fetchone()[0]
            if current_head != parent["id"]:
                actual = self.version(current_head)
                raise VersionConflict(expected_version_number or parent["version_number"], actual["version_number"])
            next_number = self.conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM version").fetchone()[0]
            created_at = timestamp or utc_now()
            cur = self.conn.execute(
                """INSERT INTO version(workbook_id,version_number,parent_id,merge_source_id,branch_name,
                   message,created_at,audit_json) VALUES (1,?,?,?,?,?,?,?)""",
                (next_number, parent["id"], merge_source_id, branch_name, message, created_at,
                 json_dumps(preview["audit"])),
            )
            version_id = cur.lastrowid
            self._write_snapshot(version_id, preview["cells"], preview["formulas"])
            self.conn.execute("UPDATE branch SET head_version_id=? WHERE name=?", (version_id, branch_name))
            # 新版本按分支当前生效绑定做过门禁评估（见 audit.constraints）；
            # 把版本↔约束集版本的关联随提交一起持久化，历史详情才能恢复规则。
            self._record_commit_set_version(version_id, branch_name)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"ok": True, "version": self.version(version_id), "audit": preview["audit"]}

    def _write_snapshot(self, version_id: int, cells: dict, formulas: dict) -> None:
        rows = []
        for (sheet, col, row), item in sorted(cells.items(), key=lambda x: (x[0][0], x[0][2], x[0][1])):
            rows.append((
                version_id, sheet, col, row, item["content"], item["kind"],
                value_to_json(item["value"]),
                json_dumps(ast_to_json(formulas[(sheet, col, row)])) if (sheet, col, row) in formulas else None,
            ))
        self.conn.executemany(
            """INSERT INTO cell(version_id,sheet_name,column_index,row_index,content,kind,value_json,formula_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._insert_edges(version_id, {key: (cells[key]["content"], ast) for key, ast in formulas.items()})

    def _normalize_edits(self, raw_edits: list[dict]) -> dict[tuple[str, int, int], str]:
        sheets, _, _ = self._sheet_map()
        edits = {}
        for raw in raw_edits:
            sheet = raw.get("sheet")
            if sheet not in sheets:
                raise ValueError(f"未知工作表: {sheet}")
            if "address" in raw:
                _, col, row = parse_a1(sheet, raw["address"])
            else:
                col, row = int(raw["column"]), int(raw["row"])
            content = str(raw.get("content", ""))
            edits[(sheet, col, row)] = content
        return edits

    def _recalculate(self, parent: dict, raw_edits: list[dict], branch_name: str | None = None) -> dict:
        try:
            edits = self._normalize_edits(raw_edits)
        except ValueError as exc:
            return {"ok": False, "errors": [{"code": "INVALID_ADDRESS", "message": str(exc), "severity": "fatal"}]}
        sheets, max_col, max_row = self._sheet_map()
        base = self.cell_map(parent["id"])
        changed: set[tuple[str, int, int]] = set()
        parse_errors = []
        candidate_ast: dict[tuple[str, int, int], dict | None] = {}
        formulas: dict[tuple[str, int, int], tuple[str, dict | None]] = {}

        for key, row_data in base.items():
            if row_data["kind"] == "formula":
                candidate_ast[key] = row_data["formula"]
        cells: dict[tuple[str, int, int], dict] = {}
        for key, row_data in base.items():
            cells[key] = {
                "content": row_data["content"],
                "kind": row_data["kind"],
                "value": row_data["value"],
            }
            if row_data["kind"] == "formula":
                formulas[key] = (row_data["content"], row_data["formula"])

        for key, content in edits.items():
            sheet, col, row = key
            if col < 0 or col >= max_col or row < 0 or row >= max_row:
                parse_errors.append({
                    "code": "OUT_OF_RANGE", "severity": "fatal",
                    "message": f"单元格超出工作表边界: {address_key(sheet, col, row)}",
                    "cell": address_key(sheet, col, row),
                })
                continue
            if cells.get(key, {}).get("content") != content:
                changed.add(key)
            if content == "":
                cells.pop(key, None)
                formulas.pop(key, None)
                candidate_ast.pop(key, None)
                continue
            if content.startswith("="):
                try:
                    ast = parse_formula(content, sheet)
                except FormulaError as exc:
                    parse_errors.append({
                        "code": exc.code, "severity": "fatal", "message": exc.message,
                        "cell": address_key(sheet, col, row), "formula": content,
                        "start": exc.span[0] if exc.span else None,
                        "end": exc.span[1] if exc.span else None,
                    })
                    continue
                candidate_ast[key] = ast
                formulas[key] = (content, ast)
                cells[key] = {"content": content, "kind": "formula", "value": {"kind": "blank"}}
            else:
                candidate_ast.pop(key, None)
                formulas.pop(key, None)
                cells[key] = {"content": content, "kind": "constant", "value": constant_value(content)}

        if parse_errors:
            return {"ok": False, "errors": parse_errors, "parent_version_number": parent["version_number"]}

        deps, missing = self._build_dependencies(candidate_ast, sheets, max_col, max_row)
        if missing:
            return {"ok": False, "errors": sorted(missing, key=lambda e: (e["cell"], e["start"] or -1, e["code"])),
                    "parent_version_number": parent["version_number"]}

        formula_nodes = set(candidate_ast)
        reverse: dict[tuple[str, int, int], set[tuple[str, int, int]]] = {k: set() for k in formula_nodes}
        for source, targets in deps.items():
            for target_key, _span in targets:
                reverse.setdefault(target_key, set()).add(source)
        affected: set[tuple[str, int, int]] = set(changed)
        queue = deque(sorted(changed))
        while queue:
            key = queue.popleft()
            for dependent in sorted(reverse.get(key, ())):
                if dependent not in affected:
                    affected.add(dependent)
                    queue.append(dependent)
        affected_formulas = affected & formula_nodes

        order, cycles = self._topological_order(affected_formulas, deps)
        if cycles:
            return {
                "ok": False,
                "errors": cycles,
                "affected": [address_key(*k) for k in sorted(affected)],
                "parent_version_number": parent["version_number"],
            }

        values: dict[tuple[str, int, int], dict] = {
            key: item["value"] for key, item in cells.items()
        }

        def lookup(sheet, col, row):
            return values.get((sheet, col, row), {"kind": "blank"})

        errors: list[dict] = []
        recalc_order = []
        for key in order:
            sheet, col, row = key
            content, ast = formulas[key]
            value = evaluate_formula(ast, lookup, {"sheet": sheet, "column": col, "row": key and row}, content)
            values[key] = value
            recalc_order.append(address_key(*key))
            if value.get("kind") == "error" and value["source"]["sheet"] == sheet and \
                    value["source"]["column"] == col and value["source"]["row"] == row:
                source_span = value["source"].get("span")
                errors.append({
                    "code": value["code"],
                    "severity": "error",
                    "message": value["message"],
                    "cell": address_key(sheet, col, row),
                    "formula": content,
                    "start": source_span[0] if source_span else None,
                    "end": source_span[1] if source_span else None,
                    "path": [address_key(sheet, col, row)],
                })

        propagated = []
        for key in order:
            value = values[key]
            if value.get("kind") != "error":
                continue
            source = value["source"]
            source_key = (source["sheet"], source["column"], source["row"])
            if source_key == key:
                continue
            path = self._shortest_dependency_path(source_key, key, reverse)
            propagated.append({
                "code": value["code"],
                "severity": "propagated",
                "message": f"错误由 {address_key(*source_key)} 传播",
                "cell": address_key(*key),
                "formula": formulas[key][0],
                "source": address_key(*source_key),
                "path": [address_key(*p) for p in path],
            })

        before = self.cell_map(parent["id"])
        diffs = []
        for key in sorted(affected):
            old = before.get(key)
            new_value = values.get(key, {"kind": "blank"})
            old_value = old["value"] if old else {"kind": "blank"}
            new_content = cells.get(key, {}).get("content", "")
            old_content = old["content"] if old else ""
            if old_content != new_content or old_value != new_value:
                diffs.append({
                    "cell": address_key(*key),
                    "sheet": key[0],
                    "column": key[1],
                    "row": key[2],
                    "before": {"content": old_content, "value": old_value},
                    "after": {"content": new_content, "value": new_value},
                    "recalculated": key in affected_formulas,
                    "edited": key in changed,
                })
            if key in cells:
                cells[key]["value"] = new_value
        audit = {
            "changed": [address_key(*k) for k in sorted(changed)],
            "affected": [address_key(*k) for k in sorted(affected)],
            "recalculation_order": recalc_order,
            "diffs": diffs,
        }
        if errors or propagated:
            all_errors = sorted(errors, key=lambda e: e["cell"]) + sorted(propagated, key=lambda e: e["cell"])
            return {"ok": False, "errors": all_errors, "audit": audit, "cells": cells,
                    "formulas": {k: v[1] for k, v in formulas.items()},
                    "parent_version_number": parent["version_number"]}

        # 增量重算完成后才评估约束门禁：只检查目标区域或条件依赖与受影响
        # 集合相交的规则，任何失败都会让本次预演/提交/合并整体失败。
        # 注意必须使用“正在提交的分支”而不是父版本的创建分支：分支头可能
        # 仍指向别的分支创建的版本（例如刚从某版本建分支时）。
        constraint_report = self._evaluate_constraints(
            branch_name or parent["branch_name"], values, changed, affected, deps, reverse
        )
        if constraint_report is not None:
            audit["constraints"] = constraint_report
            if constraint_report["violations"]:
                return {"ok": False,
                        "constraint_violations": constraint_report["violations"],
                        "audit": audit, "cells": cells,
                        "formulas": {k: v[1] for k, v in formulas.items()},
                        "parent_version_number": parent["version_number"]}

        return {"ok": True, "audit": audit, "cells": cells,
                "formulas": {k: v[1] for k, v in formulas.items()},
                "parent_version_number": parent["version_number"]}

    def _build_dependencies(self, asts, sheets, max_col, max_row):
        deps = {key: [] for key in asts}
        seen = {key: set() for key in asts}
        missing = []
        for (sheet, col, row), ast in asts.items():
            for dep in iter_references(ast):
                if dep["kind"] == "range":
                    targets = [Address(dep["sheet"], c, r)
                               for r in range(dep["start_row"], dep["end_row"] + 1)
                               for c in range(dep["start_column"], dep["end_column"] + 1)]
                else:
                    targets = [Address(dep["sheet"], dep["column"], dep["row"])]
                for target in targets:
                    if target.sheet not in sheets or not (0 <= target.column < max_col and 0 <= target.row < max_row):
                        missing.append({
                            "code": "MISSING_REFERENCE",
                            "severity": "fatal",
                            "message": f"缺失或越界引用: {address_key(target.sheet, target.column, target.row)}",
                            "cell": address_key(sheet, col, row),
                            "target": address_key(target.sheet, target.column, target.row),
                            "start": dep["start"],
                            "end": dep["end"],
                        })
                        continue
                    edge_key = (target.sheet, target.column, target.row, dep["start"])
                    if edge_key in seen[(sheet, col, row)]:
                        continue
                    seen[(sheet, col, row)].add(edge_key)
                    deps[(sheet, col, row)].append(((target.sheet, target.column, target.row),
                                                    (dep["start"], dep["end"])))
        return deps, missing

    def _topological_order(self, nodes, deps):
        node_set = set(nodes)
        indegree = {key: 0 for key in node_set}
        graph = {key: set() for key in node_set}
        for source in node_set:
            for target, _span in deps.get(source, []):
                if target in node_set:
                    # Edge source -> target means source depends on target.
                    graph[target].add(source)
                    indegree[source] += 1
        ready = deque(k for k in sorted(node_set) if indegree[k] == 0)
        order = []
        while ready:
            key = ready.popleft()
            order.append(key)
            for dependent in sorted(graph[key]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if len(order) == len(node_set):
            return order, []
        return order[:], [self._shortest_cycle(node_set, deps)] if node_set else []

    def _shortest_cycle(self, nodes, deps) -> dict:
        best = None
        for source in sorted(nodes):
            queue = deque([(source, [source], [])])
            visited = {source}
            while queue:
                current, path, edge_spans = queue.popleft()
                outgoing = sorted((target, span) for target, span in deps.get(current, []) if target in nodes)
                for target, span in outgoing:
                    if target == source and len(path) >= 1:
                        candidate_path = path + [source]
                        candidate = (len(candidate_path), [address_key(*x) for x in candidate_path], span, path)
                        if best is None or candidate[:2] < (best[0], best[1]):
                            best = candidate
                    elif target not in visited:
                        visited.add(target)
                        queue.append((target, path + [target], edge_spans + [span]))
        length, path, span, nodes_keys = best
        return {
            "code": "CYCLE_ERROR",
            "severity": "fatal",
            "message": "检测到循环引用，最短循环如下",
            "cell": path[0],
            "path": path,
            "start": span[0],
            "end": span[1],
        }

    def _shortest_dependency_path(self, source, target, reverse_dependencies: dict) -> list:
        if source == target:
            return [source]
        queue = deque([(source, [source])])
        visited = {source}
        while queue:
            current, path = queue.popleft()
            for next_key in sorted(reverse_dependencies.get(current, ())):
                if next_key in visited:
                    continue
                new_path = path + [next_key]
                if next_key == target:
                    return new_path
                visited.add(next_key)
                queue.append((next_key, new_path))
        return [source, target]

    def create_branch(self, name: str, from_version_id: int, timestamp: str | None = None) -> dict:
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("分支名称只能包含字母、数字、-、_")
        version = self.version(from_version_id)
        try:
            with self._write_lock, self.conn:
                self.conn.execute(
                    "INSERT INTO branch(workbook_id,name,head_version_id,created_at) VALUES (1,?,?,?)",
                    (name, version["id"], timestamp or utc_now()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"分支已存在: {name}") from exc
        return self.branch(name)

    # ------------------------------------------------------------------
    # 版本化约束门禁
    # ------------------------------------------------------------------

    def constraint_sets(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT name, MAX(version_number) AS latest_version, COUNT(*) AS versions,
                      MIN(created_at) AS created_at
               FROM constraint_set GROUP BY name ORDER BY name"""
        ).fetchall()
        return [dict(row) for row in rows]

    def constraint_set(self, name: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM constraint_set WHERE name=? ORDER BY version_number", (name,)
        ).fetchall()
        if not rows:
            raise KeyError(f"约束集不存在: {name}")
        return {"name": name, "versions": [self._constraint_set_row(row) for row in rows]}

    @staticmethod
    def _constraint_set_row(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "version_number": row["version_number"],
            "message": row["message"],
            "rules": json.loads(row["rules_json"]),
            "created_at": row["created_at"],
        }

    def _latest_constraint_set_version(self, name: str) -> int:
        return self.conn.execute(
            "SELECT COALESCE(MAX(version_number),0) FROM constraint_set WHERE name=?", (name,)
        ).fetchone()[0]

    def save_constraint_set(
        self,
        name: str,
        rules,
        message: str | None,
        expected_version_number,
        timestamp: str | None = None,
    ) -> dict:
        """创建或修改约束集，生成新的不可变版本。

        ``expected_version_number`` 必须等于该约束集当前最新版本号（新建时为 0），
        校验与插入在同一个 ``BEGIN IMMEDIATE`` 事务中完成；重复请求或并发写会
        因版本号已前进而收到 VERSION_CONFLICT，不会产生重复版本。
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("约束集名称不能为空")
        name = name.strip()
        if not isinstance(expected_version_number, int) or isinstance(expected_version_number, bool):
            raise ValueError("expected_version_number 必须是整数")
        sheets, max_col, max_row = self._sheet_map()
        normalized = validate_rules(rules, sheets, max_col, max_row)
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                latest = self._latest_constraint_set_version(name)
                if latest != expected_version_number:
                    raise VersionConflict(expected_version_number, latest)
                cur = self.conn.execute(
                    """INSERT INTO constraint_set(name,version_number,message,rules_json,created_at)
                       VALUES (?,?,?,?,?)""",
                    (name, latest + 1, message or "", json_dumps(normalized),
                     timestamp or utc_now()),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        row = self.conn.execute(
            "SELECT * FROM constraint_set WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return self._constraint_set_row(row)

    @staticmethod
    def _binding_row(row, with_rules: bool = True) -> dict:
        data = {
            "id": row["id"],
            "kind": "branch",
            "branch_name": row["branch_name"],
            "binding_number": row["binding_number"],
            "constraint_set": row["set_name"],
            "constraint_set_id": row["constraint_set_id"],
            "constraint_set_version": row["set_version"],
            "workbook_version_number": row["workbook_version_number"],
            "workbook_version_id": row["workbook_version_id"],
            "created_at": row["created_at"],
        }
        if with_rules:
            data["rules"] = json.loads(row["rules_json"])
        return data

    _BINDING_SELECT = """
        SELECT b.*, s.name AS set_name, s.version_number AS set_version, s.rules_json
        FROM constraint_binding b JOIN constraint_set s ON s.id = b.constraint_set_id
    """

    @staticmethod
    def _pin_row(row, with_rules: bool = True) -> dict:
        data = {
            "id": row["id"],
            "kind": "version_pin",
            "branch_name": row["branch_name"],
            # 目标版本的独立钉版修订号（同一版本内从 1 递增）。
            "pin_number": row["pin_number"],
            "constraint_set": row["set_name"],
            "constraint_set_id": row["constraint_set_id"],
            "constraint_set_version": row["set_version"],
            "workbook_version_number": row["workbook_version_number"],
            "workbook_version_id": row["workbook_version_id"],
            "created_at": row["created_at"],
        }
        if with_rules:
            data["rules"] = json.loads(row["rules_json"])
        return data

    _PIN_SELECT = """
        SELECT p.*, s.name AS set_name, s.version_number AS set_version, s.rules_json
        FROM version_pin p JOIN constraint_set s ON s.id = p.constraint_set_id
    """

    def active_binding(self, branch_name: str) -> dict | None:
        row = self.conn.execute(
            self._BINDING_SELECT + "WHERE b.branch_name=? ORDER BY b.binding_number DESC LIMIT 1",
            (branch_name,),
        ).fetchone()
        return self._binding_row(row) if row else None

    def active_bindings(self) -> list[dict]:
        """每个分支当前生效的分支绑定（绑定代数最大的一条）。

        只包含 constraint_binding（分支绑定状态）；版本钉版状态
        （version_pin）不参与分支当前规则，故不出现在这里。
        """
        rows = self.conn.execute(
            self._BINDING_SELECT + """
            JOIN (SELECT branch_name, MAX(binding_number) AS latest
                  FROM constraint_binding GROUP BY branch_name) cur
              ON cur.branch_name = b.branch_name AND cur.latest = b.binding_number
            ORDER BY b.branch_name"""
        ).fetchall()
        return [self._binding_row(row, with_rules=False) for row in rows]

    def binding_history(self, branch_name: str) -> list[dict]:
        self.branch(branch_name)
        rows = self.conn.execute(
            self._BINDING_SELECT + "WHERE b.branch_name=? ORDER BY b.binding_number",
            (branch_name,),
        ).fetchall()
        return [self._binding_row(row, with_rules=False) for row in rows]

    def pin_revision(self, version_id: int) -> int:
        """目标版本当前的独立钉版修订号（未钉版为 0）。"""
        return self.conn.execute(
            "SELECT COALESCE(MAX(pin_number),0) FROM version_pin WHERE workbook_version_id=?",
            (version_id,),
        ).fetchone()[0]

    def active_pin(self, version_id: int) -> dict | None:
        """指定历史版本当前生效的钉版（修订号最大的一条）。"""
        row = self.conn.execute(
            self._PIN_SELECT
            + "WHERE p.workbook_version_id=? ORDER BY p.pin_number DESC LIMIT 1",
            (version_id,),
        ).fetchone()
        return self._pin_row(row) if row else None

    def pin_history(self, branch_name: str) -> list[dict]:
        """分支归属的全部历史版本钉版记录，按版本号与修订号排列。"""
        self.branch(branch_name)
        rows = self.conn.execute(
            self._PIN_SELECT
            + "WHERE p.branch_name=? ORDER BY p.workbook_version_number, p.pin_number, p.id",
            (branch_name,),
        ).fetchall()
        return [self._pin_row(row, with_rules=False) for row in rows]

    def create_binding(
        self,
        branch_name: str | None,
        set_name: str,
        set_version: int,
        expected_version_number,
        timestamp: str | None = None,
        version_id: int | None = None,
    ) -> dict:
        """分支换绑：把约束集的某个不可变版本绑定到分支当前头版本。

        钉版（``version_id``）是另一类状态，已拆到 :meth:`create_pin`；
        这里收到 ``version_id`` 时直接转调，保持旧位置参数调用兼容。

        ``expected_version_number`` 是该分支当前的绑定代数（尚未绑定时为
        0），校验与写入在同一个 ``BEGIN IMMEDIATE`` 事务中原子完成；并发
        或重复请求只有一个能消费当前代数，其余抛 ``VersionConflict``，
        不写记录、不移动分支头。
        """
        if version_id is not None:
            return self.create_pin(
                version_id, set_name, set_version, expected_version_number,
                timestamp=timestamp, branch_name=branch_name,
            )
        if not branch_name:
            raise ValueError("必须提供 branch 或 version_id")
        if not isinstance(expected_version_number, int) or isinstance(expected_version_number, bool):
            raise ValueError("expected_version_number 必须是整数")
        set_row = self.conn.execute(
            "SELECT * FROM constraint_set WHERE name=? AND version_number=?",
            (set_name, set_version),
        ).fetchone()
        if not set_row:
            raise KeyError(f"约束集版本不存在: {set_name}@v{set_version}")
        self.branch(branch_name)  # 不存在时 KeyError
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                current = self.conn.execute(
                    "SELECT COALESCE(MAX(binding_number),0) FROM constraint_binding WHERE branch_name=?",
                    (branch_name,),
                ).fetchone()[0]
                if current != expected_version_number:
                    raise VersionConflict(expected_version_number, current)
                head = self.branch(branch_name)["head_version_id"]
                if head is None:
                    raise ValueError("分支没有可绑定版本")
                head_number = self.conn.execute(
                    "SELECT version_number FROM version WHERE id=?", (head,)
                ).fetchone()[0]
                created_at = timestamp or utc_now()
                self.conn.execute(
                    """INSERT INTO constraint_binding(branch_name,binding_number,constraint_set_id,
                       workbook_version_number,workbook_version_id,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (branch_name, current + 1, set_row["id"], head_number, head, created_at),
                )
                # 分支换绑只影响后续预演/提交/合并门禁：在头版本尚无关联时
                # 补一条 pinned（与旧库迁移回放语义一致）；已提交并记录过
                # 规则的头版本保持其 commit 关联，不被覆盖。绝不改写其他
                # 历史版本，也不影响 version_pin。
                self.conn.execute(
                    """INSERT OR IGNORE INTO version_constraint_set
                           (workbook_version_id,constraint_set_id,source,created_at)
                       VALUES (?,?, 'pinned', ?)""",
                    (head, set_row["id"], created_at),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.active_binding(branch_name)

    def create_pin(
        self,
        version_id: int,
        set_name: str,
        set_version: int,
        expected_version_number,
        timestamp: str | None = None,
        branch_name: str | None = None,
    ) -> dict:
        """钉版：把不可变规则钉到指定历史工作簿版本。

        钉版只改变目标版本的规则关联（``version_constraint_set``），并在
        ``version_pin`` 留下该版本的一条修订记录；它**不**追加分支绑定、
        **不**改变分支当前规则与绑定代数，分支头也不移动，因此随后的
        预演、提交、合并门禁完全保持不变。

        - 记录归属该版本所在分支（``version.branch_name``）；若同时给了
          ``branch_name``，二者必须一致，否则抛 ``ValueError``；
        - ``expected_version_number`` 校验的是**目标版本的独立钉版修订号**
          （该版本未钉版时为 0），与分支绑定代数互不相关；同一版本的并发
          或重复钉版只有一个成功，其余抛 ``VersionConflict``，不写记录、
          不移动分支头；同一版本再次钉版（不同修订号）以新规则为准。
        """
        if not isinstance(version_id, int) or isinstance(version_id, bool):
            raise ValueError("version_id 必须是整数")
        if not isinstance(expected_version_number, int) or isinstance(expected_version_number, bool):
            raise ValueError("expected_version_number 必须是整数")
        set_row = self.conn.execute(
            "SELECT * FROM constraint_set WHERE name=? AND version_number=?",
            (set_name, set_version),
        ).fetchone()
        if not set_row:
            raise KeyError(f"约束集版本不存在: {set_name}@v{set_version}")
        # 先解析目标并校验版本归属（失败时不开事务）。
        target_version = self.version(version_id)
        owner = target_version["branch_name"]
        if branch_name is not None and branch_name != owner:
            raise ValueError(
                f"版本 v{target_version['version_number']} 属于分支 {owner}，不能绑定到分支 {branch_name}"
            )
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # 事务内重读版本，确认归属未被任何 DDL/数据操作改变。
                fresh = self.version(version_id)
                if fresh["branch_name"] != owner:
                    raise ValueError("版本不属于该分支")
                current = self.pin_revision(version_id)
                if current != expected_version_number:
                    raise VersionConflict(expected_version_number, current)
                created_at = timestamp or utc_now()
                self.conn.execute(
                    """INSERT INTO version_pin(workbook_version_id,branch_name,pin_number,
                       constraint_set_id,workbook_version_number,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (version_id, owner, current + 1, set_row["id"],
                     target_version["version_number"], created_at),
                )
                # 只改写目标版本的规则关联：以最新钉版覆盖该版本原有
                # commit/pinned 关联，保证历史详情恢复用户钉住的规则。
                self.conn.execute(
                    """INSERT INTO version_constraint_set(workbook_version_id,constraint_set_id,
                           source,created_at)
                       VALUES (?,?, 'pinned', ?)
                       ON CONFLICT(workbook_version_id) DO UPDATE SET
                           constraint_set_id=excluded.constraint_set_id,
                           source='pinned',
                           created_at=excluded.created_at""",
                    (version_id, set_row["id"], created_at),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.active_pin(version_id)

    def _evaluate_constraints(self, branch_name, values, changed, affected, deps, reverse) -> dict | None:
        """在增量重算完成后评估分支当前绑定的约束集；未绑定时返回 None。"""
        binding = self.active_binding(branch_name)
        if binding is None:
            return None

        def path_for(cell):
            return self._constraint_path(cell, changed, deps, reverse)

        report = evaluate_rules(binding["rules"], values, changed, affected, path_for)
        report["constraint_set"] = binding["constraint_set"]
        report["constraint_set_version"] = binding["constraint_set_version"]
        report["binding_number"] = binding["binding_number"]
        for violation in report["violations"]:
            violation["constraint_set"] = binding["constraint_set"]
            violation["constraint_set_version"] = binding["constraint_set_version"]
        return report

    def _constraint_path(self, failing, changed, deps, reverse) -> list:
        """失败单元格的依赖路径：从某个直接变更单元格沿依赖边传播到它的最短链。

        找不到可达的变更单元格时退化为只含失败单元格本身。
        """
        precedents: set = set()
        stack = [failing]
        while stack:
            current = stack.pop()
            for target, _span in deps.get(current, []):
                if target not in precedents:
                    precedents.add(target)
                    stack.append(target)
        candidates = [c for c in sorted(changed) if c == failing or c in precedents]
        best = None
        for source in candidates:
            path = self._shortest_dependency_path(source, failing, reverse)
            labels = [address_key(*step) for step in path]
            if best is None or (len(labels), labels) < (len(best[1]), best[1]):
                best = (path, labels)
        return best[0] if best is not None else [failing]

    def _record_commit_set_version(self, version_id: int, branch_name: str) -> None:
        """提交事务内记录“新版本按哪个约束集版本通过门禁”。

        分支没有绑定则不写；必须取最新绑定代数指向的约束集（同一
        ``constraint_set_id`` 不会复用，绑定换集后 id 已变）。显式钉在
        该版本的关联（pinned）优先，不被提交覆盖。
        """
        row = self.conn.execute(
            """SELECT b.constraint_set_id
                 FROM constraint_binding b
                WHERE b.branch_name=?
                ORDER BY b.binding_number DESC LIMIT 1""",
            (branch_name,),
        ).fetchone()
        if row is None:
            return
        self.conn.execute(
            """INSERT OR IGNORE INTO version_constraint_set
                   (workbook_version_id,constraint_set_id,source,created_at)
               SELECT id, ?, 'commit', created_at FROM version WHERE id=?""",
            (row["constraint_set_id"], version_id),
        )

    @staticmethod
    def _binding_ref(binding: dict | None) -> dict | None:
        if binding is None:
            return None
        return {
            "kind": binding.get("kind", "branch"),
            "constraint_set": binding["constraint_set"],
            "constraint_set_version": binding["constraint_set_version"],
            "binding_number": binding["binding_number"],
            "workbook_version_number": binding["workbook_version_number"],
        }

    def _constraint_conflicts(self, target_binding, source_binding) -> list[dict]:
        """比较两个分支当前绑定的约束集，输出规则级冲突清单（确定性排序）。"""
        if target_binding is None and source_binding is None:
            return []
        if target_binding is None:
            return [{
                "kind": "source_only_binding",
                "message": f"来源分支绑定了 {source_binding['constraint_set']}"
                           f"@v{source_binding['constraint_set_version']}，目标分支未绑定约束集",
            }]
        if source_binding is None:
            return [{
                "kind": "target_only_binding",
                "message": f"目标分支绑定了 {target_binding['constraint_set']}"
                           f"@v{target_binding['constraint_set_version']}，来源分支未绑定约束集",
            }]
        if target_binding["constraint_set_id"] == source_binding["constraint_set_id"]:
            return []
        target_rules = {rule["name"]: rule for rule in target_binding["rules"]}
        source_rules = {rule["name"]: rule for rule in source_binding["rules"]}
        conflicts = []
        for name in sorted(set(target_rules) | set(source_rules)):
            in_target, in_source = name in target_rules, name in source_rules
            if in_target and not in_source:
                conflicts.append({"kind": "target_only", "rule": name,
                                  "target": target_rules[name], "source": None})
            elif in_source and not in_target:
                conflicts.append({"kind": "source_only", "rule": name,
                                  "target": None, "source": source_rules[name]})
            elif json_dumps(target_rules[name]) != json_dumps(source_rules[name]):
                conflicts.append({"kind": "changed", "rule": name,
                                  "target": target_rules[name], "source": source_rules[name]})
        return conflicts

    def merge_preview(self, target_branch: str, source_branch: str) -> dict:
        target = self.branch(target_branch)
        source = self.branch(source_branch)
        target_version = self.version(target["head_version_id"])
        source_version = self.version(source["head_version_id"])
        # 版本历史是由 parent_id 与 merge_source_id 共同构成的 DAG；
        # 共同祖先必须沿两类边回溯，否则重复合并会把目标分支的后续修改误报为冲突。
        base_ids = self._merge_bases(target_version["id"], source_version["id"])
        base_versions = [self.version(version_id) for version_id in base_ids]
        virtual = len(base_ids) != 1
        base_cells = self._virtual_base(base_ids)
        target_cells = self.cell_map(target_version["id"])
        source_cells = self.cell_map(source_version["id"])

        def fingerprint(cells, key):
            row = cells.get(key)
            return None if row is None else row["content"]

        keys = set(target_cells) | set(source_cells) | set(base_cells)
        conflicts, source_changes, target_changes = [], [], []
        for key in sorted(keys, key=lambda k: (k[0], k[2], k[1])):
            old = base_cells.get(key)
            left = fingerprint(target_cells, key)
            right = fingerprint(source_cells, key)
            label = address_key(*key)
            if old == BASE_CONFLICT:
                # 基线之间互相矛盾（criss-cross）：任一端与另一端不同即为真冲突。
                if left != right:
                    conflicts.append({
                        "cell": label, "target": left or "", "source": right or "",
                        "base": "<多个共同祖先取值不一致>",
                    })
                continue
            if left == right:
                continue
            if left == old and right != old:
                source_changes.append({"cell": label, "content": right or ""})
            elif right == old and left != old:
                target_changes.append(label)
            else:
                conflicts.append({"cell": label, "target": left or "", "source": right or "", "base": old or ""})
        edits = []
        for change in source_changes:
            sheet, ref = change["cell"].split("!", 1)
            edits.append({"sheet": sheet, "address": ref, "content": change["content"]})
        semantic = self._recalculate(target_version, edits, target_branch) if edits else {
            "ok": True, "audit": {"changed": [], "affected": [], "recalculation_order": [], "diffs": []}
        }
        # 跨分支规则冲突：两个分支绑定的约束集版本不一致时按规则名对比；
        # 合并后的数据始终以目标分支绑定的约束集为准（见 semantic 的门禁评估）。
        target_binding = self.active_binding(target_branch)
        source_binding = self.active_binding(source_branch)
        return {
            "ok": not conflicts and semantic["ok"],
            "target_branch": target_branch,
            "source_branch": source_branch,
            "target_version": self._version_ref(target_version),
            "source_version": self._version_ref(source_version),
            "ancestor_version": self._version_ref(base_versions[0]) if len(base_ids) == 1 else None,
            "merge_bases": [self._version_ref(v) for v in base_versions],
            "virtual_base": virtual,
            "conflicts": conflicts,
            "source_only_changes": source_changes,
            "target_changes": target_changes,
            "semantic_errors": [] if semantic["ok"] else semantic.get("errors", []),
            "semantic_audit": semantic.get("audit"),
            "constraint_sets": {
                "target": self._binding_ref(target_binding),
                "source": self._binding_ref(source_binding),
            },
            "constraint_conflicts": self._constraint_conflicts(target_binding, source_binding),
            "constraint_violations": [] if semantic["ok"] else semantic.get("constraint_violations", []),
        }

    def merge(
        self,
        target_branch: str,
        source_branch: str,
        message: str | None = None,
        expected_version_number: int | None = None,
    ) -> dict:
        with self._write_lock:
            preview = self.merge_preview(target_branch, source_branch)
            target_version = preview["target_version"]
            # 预览与提交使用同一基线；提交前再次原子检查目标分支头版本号，
            # 预览之后分支头若已被其他写操作推进则直接失败，且不移动分支头。
            if expected_version_number is not None and \
                    target_version["version_number"] != expected_version_number:
                raise VersionConflict(
                    expected_version_number, target_version["version_number"]
                )
            if not preview["ok"]:
                raise MergeConflict(preview)
            edits = []
            for change in preview["source_only_changes"]:
                sheet, ref = change["cell"].split("!", 1)
                edits.append({"sheet": sheet, "address": ref, "content": change["content"]})
            source_version_id = preview["source_version"]["id"]
            return self.commit_edits(
                target_branch,
                edits,
                message or f"合并分支 {source_branch} 到 {target_branch}",
                expected_version_number=target_version["version_number"],
                merge_source_id=source_version_id,
            )

    def _dag_graph(self) -> dict[int, tuple[int | None, int | None]]:
        graph: dict[int, tuple[int | None, int | None]] = {}
        for row in self.conn.execute("SELECT id, parent_id, merge_source_id FROM version"):
            graph[row["id"]] = (row["parent_id"], row["merge_source_id"])
        return graph

    @staticmethod
    def _ancestor_closure(node: int, graph: dict[int, tuple[int | None, int | None]],
                         memo: dict[int, frozenset[int]]) -> frozenset[int]:
        if node in memo:
            return memo[node]
        # 迭代后序展开 + 记忆化，避免长提交链触发递归深度限制。
        stack = [(node, 0)]
        while stack:
            current, phase = stack[-1]
            if current in memo:
                stack.pop()
                continue
            parent_id, merge_source_id = graph.get(current, (None, None))
            children = [linked for linked in (parent_id, merge_source_id)
                        if linked is not None and linked in graph and linked not in memo]
            if phase == 0 and children:
                stack[-1] = (current, 1)
                stack.extend((child, 0) for child in children)
                continue
            members = {current}
            for linked in (parent_id, merge_source_id):
                if linked is not None and linked in graph:
                    members |= memo[linked]
            memo[current] = frozenset(members)
            stack.pop()
        return memo[node]

    def _merge_bases(self, left_id: int, right_id: int) -> list[int]:
        """求版本 DAG 中所有极大共同祖先（合并基线），按 id 确定性排序。

        极大共同祖先指不被任何其他共同祖先支配（经 DAG 边可达）的共同祖先。
        """
        graph = self._dag_graph()
        memo: dict[int, frozenset[int]] = {}
        common = self._ancestor_closure(left_id, graph, memo) & self._ancestor_closure(right_id, graph, memo)
        bases = []
        for candidate in common:
            dominated = any(
                other != candidate and candidate in self._ancestor_closure(other, graph, memo)
                for other in common
            )
            if not dominated:
                bases.append(candidate)
        return sorted(bases)

    def _content_map(self, version_id: int) -> dict[tuple[str, int, int], str | None]:
        rows = self.conn.execute(
            "SELECT sheet_name, column_index, row_index, content FROM cell WHERE version_id=?",
            (version_id,),
        ).fetchall()
        return {(r["sheet_name"], r["column_index"], r["row_index"]): r["content"] for r in rows}

    def _virtual_base(self, base_ids: list[int], depth: int = 0) -> dict:
        """把一个或多个极大共同祖先确定性地合成为虚拟基线单元格映射。

        - 单个基线：直接返回其单元格内容；
        - 多个互不支配的基线：按 id 排序后两两递归做内容级三路合并
          （递归使用各自的极大共同祖先作为基点），再用“全体一致否则冲突
          哨兵”的规则归并，结果与遍历顺序无关。
        """
        if depth > VIRTUAL_BASE_DEPTH_LIMIT:
            raise RuntimeError("虚拟基线递归过深，版本 DAG 可能异常")
        if len(base_ids) == 1:
            return self._content_map(base_ids[0])
        trees = [
            self._merged_base_tree(base_ids[i], base_ids[i + 1], depth)
            for i in range(0, len(base_ids) - 1, 2)
        ]
        if len(base_ids) % 2 == 1:
            trees.append(self._content_map(base_ids[-1]))
        return self._combine_virtual_trees(trees)

    def _merged_base_tree(
        self, left_id: int, right_id: int, depth: int
    ) -> dict[tuple[str, int, int], str | None]:
        """对两个基线做内容级三路合并，用于合成虚拟基线。"""
        sub_bases = self._merge_bases(left_id, right_id)
        base = self._virtual_base(sub_bases, depth + 1)
        left = self._content_map(left_id)
        right = self._content_map(right_id)
        return self._three_way_tree(base, left, right)

    @staticmethod
    def _three_way_tree(base: dict, left: dict, right: dict) -> dict:
        keys = set(base) | set(left) | set(right)
        merged: dict = {}
        for key in keys:
            old = base.get(key)
            a = left.get(key)
            b = right.get(key)
            if old == BASE_CONFLICT:
                value = a if a == b else BASE_CONFLICT
            elif a == b:
                value = a
            elif a == old:
                value = b
            elif b == old:
                value = a
            else:
                value = BASE_CONFLICT
            if value is not None:
                merged[key] = value
        return merged

    @staticmethod
    def _combine_virtual_trees(trees: list[dict]) -> dict:
        """全体取值一致才采纳，否则写入冲突哨兵；与树的排列顺序无关。"""
        combined: dict = {}
        for key in set().union(*(tree.keys() for tree in trees)):
            values = {tree.get(key) for tree in trees}
            if len(values) == 1:
                (only,) = values
                if only is not None:
                    combined[key] = only
            else:
                combined[key] = BASE_CONFLICT
        return combined

    def _version_ref(self, version: dict) -> dict:
        return {
            "id": version["id"],
            "version_number": version["version_number"],
            "branch_name": version["branch_name"],
            "message": version["message"],
            "created_at": version["created_at"],
        }


class ServiceError(Exception):
    status_code = 400


class VersionConflict(ServiceError):
    status_code = 409

    def __init__(self, expected: int, actual: int):
        super().__init__(f"版本冲突：预期 v{expected}，实际 v{actual}")
        self.detail = {"code": "VERSION_CONFLICT", "expected": expected, "actual": actual}


class ValidationFailed(ServiceError):
    status_code = 422

    def __init__(self, preview: dict):
        super().__init__("重算校验失败")
        self.detail = preview


class MergeConflict(ServiceError):
    status_code = 409

    def __init__(self, preview: dict):
        super().__init__("合并存在冲突")
        self.detail = preview
