"""测试辅助：构造旧版（版本级绑定之前的）SQLite 数据库，验证迁移兼容性。"""
from __future__ import annotations

import sqlite3

# 旧版 schema：constraint_binding 没有 workbook_version_id，
# 也没有 version_constraint_set 关联表。
LEGACY_SCHEMA = """
CREATE TABLE workbook (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    columns INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE sheet (
    workbook_id INTEGER NOT NULL REFERENCES workbook(id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (workbook_id, name),
    UNIQUE (workbook_id, position)
);
CREATE TABLE branch (
    workbook_id INTEGER NOT NULL REFERENCES workbook(id),
    name TEXT PRIMARY KEY,
    head_version_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE version (
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
CREATE TABLE cell (
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
CREATE TABLE edge (
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
CREATE TABLE constraint_set (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version_number)
);
CREATE TABLE constraint_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_name TEXT NOT NULL REFERENCES branch(name),
    binding_number INTEGER NOT NULL,
    constraint_set_id INTEGER NOT NULL REFERENCES constraint_set(id),
    workbook_version_number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (branch_name, binding_number)
);
"""

FIXED_TIME = "2026-01-01T00:00:00Z"

RULES_V1 = '[{"name":"数量非负","type":"number_range","target":"Inputs!B2:B4","min":0}]'
RULES_V2 = '[{"name":"数量上限","type":"number_range","target":"Inputs!B2:B4","min":0,"max":10}]'


def build_legacy_db(path: str = ":memory:") -> sqlite3.Connection:
    """构造一个包含 main/feature 两条分支与历史绑定的旧库。

    版本链：
      main:    v1 -> v2 -> v4（v3 在 feature 上分叉）
      feature: v1 -> v3
    绑定（旧表只记录工作簿版本号）：
      main #1 -> 集合v1 @ v1，#2 -> 集合v2 @ v2
      feature #1 -> 集合v1 @ v1
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    with conn:
        conn.execute(
            "INSERT INTO workbook(id,name,columns,rows,created_at) VALUES (1,'旧库',8,12,?)",
            (FIXED_TIME,),
        )
        conn.executemany(
            "INSERT INTO sheet(workbook_id,position,name) VALUES (1,?,?)",
            [(0, "Inputs"), (1, "Ledger"), (2, "Summary")],
        )
        conn.execute(
            "INSERT INTO branch(workbook_id,name,head_version_id,created_at) VALUES (1,'main',4,?)",
            (FIXED_TIME,),
        )
        conn.execute(
            "INSERT INTO branch(workbook_id,name,head_version_id,created_at) VALUES (1,'feature',3,?)",
            (FIXED_TIME,),
        )
        versions = [
            (1, None, "main", "初始"),
            (2, 1, "main", "main 提交"),
            (3, 1, "feature", "feature 分叉提交"),
            (4, 2, "main", "main 再提交"),
        ]
        for number, parent, branch, message in versions:
            conn.execute(
                """INSERT INTO version(id,workbook_id,version_number,parent_id,merge_source_id,
                       branch_name,message,created_at,audit_json)
                   VALUES (?,1,?,?,NULL,?,?,?,'{}')""",
                (number, number, parent, branch, message, FIXED_TIME),
            )
        conn.execute(
            """INSERT INTO constraint_set(id,name,version_number,message,rules_json,created_at)
               VALUES (1,'规则集',1,'初版',?,?)""",
            (RULES_V1, FIXED_TIME),
        )
        conn.execute(
            """INSERT INTO constraint_set(id,name,version_number,message,rules_json,created_at)
               VALUES (2,'规则集',2,'收紧',?,?)""",
            (RULES_V2, FIXED_TIME),
        )
        bindings = [
            ("main", 1, 1, 1),
            ("main", 2, 2, 2),
            ("feature", 1, 1, 1),
        ]
        for branch, number, set_id, wb_number in bindings:
            conn.execute(
                """INSERT INTO constraint_binding(branch_name,binding_number,constraint_set_id,
                       workbook_version_number,created_at) VALUES (?,?,?,?,?)""",
                (branch, number, set_id, wb_number, FIXED_TIME),
            )
    return conn
