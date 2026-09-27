"""旧版数据库迁移到版本级约束绑定模型的回归测试。"""
import tempfile
import unittest
from pathlib import Path

from app.db import Database, SCHEMA, VersionConflict
from tests.legacy_db import build_legacy_db, build_tampered_legacy_db


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        # 手工构造旧库，再用 Database 打开触发迁移（seed=False：已有数据不重灌）。
        self.conn = build_legacy_db(":memory:")
        # 与生产 __init__ 一致：先执行幂等 SCHEMA（CREATE TABLE/INDEX IF NOT
        # EXISTS），旧表保持旧结构，再跑增量迁移。
        self.conn.executescript(SCHEMA)
        self.path = ":memory:"

    def _open(self):
        # 直接在旧连接的库上跑迁移逻辑：Database 不支持接管连接，
        # 因此把迁移逻辑作用到同一内存连接（生产中走文件路径，语义一致）。
        db = Database.__new__(Database)
        db.path = ":memory:"
        db.conn = self.conn
        return db, self.conn

    def test_migration_adds_version_id_column_and_backfills(self):
        db, conn = self._open()
        Database._migrate_legacy_schema(db)

        cols = {r["name"] for r in conn.execute("PRAGMA table_info(constraint_binding)")}
        self.assertIn("workbook_version_id", cols)

        rows = conn.execute(
            """SELECT branch_name, binding_number, workbook_version_number, workbook_version_id
                 FROM constraint_binding ORDER BY branch_name, binding_number"""
        ).fetchall()
        self.assertEqual(
            [(r["branch_name"], r["binding_number"], r["workbook_version_number"],
              r["workbook_version_id"]) for r in rows],
            [("feature", 1, 1, 1), ("main", 1, 1, 1), ("main", 2, 2, 2)],
        )

    def test_migration_links_versions_to_rule_sets(self):
        db, conn = self._open()
        Database._migrate_legacy_schema(db)
        # 每条旧绑定钉住的精确分支头版本都恢复为 pinned 关联。
        linked = {
            r["workbook_version_id"]: (r["constraint_set_id"], r["source"])
            for r in conn.execute(
                "SELECT workbook_version_id, constraint_set_id, source FROM version_constraint_set"
            )
        }
        self.assertEqual(linked, {1: (1, "pinned"), 2: (2, "pinned")})

    def test_migration_is_idempotent(self):
        db, _ = self._open()
        Database._migrate_legacy_schema(db)
        # 再跑一次：不重复加列、不重复写关联。
        Database._migrate_legacy_schema(db)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM version_constraint_set").fetchone()[0],
            2,
        )

    def test_history_rules_recover_after_file_migration(self):
        import tempfile
        from pathlib import Path

        tmp_dir = tempfile.mkdtemp()
        db_path = str(Path(tmp_dir) / "legacy.sqlite")
        legacy = build_legacy_db(db_path)
        legacy.close()

        db = Database(db_path, seed=False)
        try:
            # 被绑定精确钉住的版本恢复 pinned 规则。
            self.assertEqual(
                db.effective_version_constraint_set(1)["constraint_set_version"], 1
            )
            self.assertEqual(
                db.effective_version_constraint_set(2)["constraint_set_version"], 2
            )
            # 没有显式关联的后续版本沿创建分支回放当时绑定。
            self.assertEqual(
                db.effective_version_constraint_set(4)["constraint_set_version"], 2
            )
            self.assertEqual(
                db.effective_version_constraint_set(3)["constraint_set_version"], 1
            )
            # 版本详情直接带出当时规则。
            detail = db.version(4)
            self.assertEqual(
                detail["effective_constraint_set"]["constraint_set_version"], 2
            )
            self.assertEqual(detail["effective_constraint_set"]["source"], "commit")
        finally:
            db.conn.close()

    def test_fresh_database_has_new_schema(self):
        db = Database(":memory:")
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(constraint_binding)")}
        self.assertIn("workbook_version_id", cols)
        tables = {
            r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("version_constraint_set", tables)
        # 分支绑定与版本钉版是两类独立状态表。
        self.assertIn("version_pin", tables)
        pin_cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(version_pin)")}
        self.assertEqual(
            pin_cols,
            {"id", "workbook_version_id", "branch_name", "pin_number",
             "constraint_set_id", "workbook_version_number", "created_at"},
        )
        # 新库默认没有钉版记录（种子只写分支绑定与 v1 关联）。
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM version_pin").fetchone()[0], 0)
        # 种子数据也写了 v1 的版本关联。
        row = db.conn.execute(
            "SELECT * FROM version_constraint_set WHERE workbook_version_id=1"
        ).fetchone()
        self.assertEqual(row["source"], "pinned")


class TamperedPinMigrationTests(unittest.TestCase):
    """历史钉版串改分支门禁的问题库迁移：拆分两类状态、恢复门禁、重启一致。"""

    def _build_file_db(self) -> str:
        tmp_dir = tempfile.mkdtemp()
        db_path = str(Path(tmp_dir) / "tampered.sqlite")
        legacy = build_tampered_legacy_db(db_path)
        legacy.close()
        return db_path

    def test_split_moves_tampered_pin_out_of_binding_chain(self):
        db = Database(self._build_file_db(), seed=False)
        try:
            # main 的错误 #3 被拆走：当前绑定恢复为代数 2、严格规则 v2。
            active = db.active_binding("main")
            self.assertEqual(active["binding_number"], 2)
            self.assertEqual(active["constraint_set_version"], 2)
            # 分支绑定链只保留原始两次换绑，且代数空号不重排。
            self.assertEqual(
                [b["binding_number"] for b in db.binding_history("main")],
                [1, 2],
            )
            # 钉版进入独立状态表：v1 的第 1 次修订挂宽松规则 v1。
            pins = db.pin_history("main")
            self.assertEqual(
                [(p["workbook_version_number"], p["pin_number"],
                  p["constraint_set_version"]) for p in pins],
                [(1, 1, 1)],
            )
        finally:
            db.conn.close()

    def test_migration_preserves_history_associations(self):
        db = Database(self._build_file_db(), seed=False)
        try:
            # v1 被钉版为规则 v1（pinned）；v2 由分支绑定钉为规则 v2；
            # v4 沿分支回放为 v2；feature 的 v3 回放为 v1。
            self.assertEqual(db.effective_version_constraint_set(1)["constraint_set_version"], 1)
            self.assertEqual(db.effective_version_constraint_set(1)["source"], "pinned")
            self.assertEqual(db.effective_version_constraint_set(2)["constraint_set_version"], 2)
            self.assertEqual(db.effective_version_constraint_set(4)["constraint_set_version"], 2)
            self.assertEqual(db.effective_version_constraint_set(3)["constraint_set_version"], 1)
        finally:
            db.conn.close()

    def test_gate_restored_after_migration(self):
        db = Database(self._build_file_db(), seed=False)
        try:
            # 严格 v2 规则 max=10：50 必须被门禁拦截（修复前会被宽松 v1 放行）。
            preview = db.validate_edits(
                "main", [{"sheet": "Inputs", "address": "B2", "content": "50"}], 4
            )
            self.assertFalse(preview["ok"])
            self.assertTrue(preview.get("constraint_violations"))
            self.assertEqual(
                preview["audit"]["constraints"]["constraint_set_version"], 2
            )
        finally:
            db.conn.close()

    def test_restart_gives_identical_results(self):
        path = self._build_file_db()
        first = Database(path, seed=False)
        first_active = (first.active_binding("main")["binding_number"],
                        first.active_binding("main")["constraint_set_version"])
        first_pins = [(p["workbook_version_number"], p["pin_number"])
                      for p in first.pin_history("main")]
        first.conn.close()

        second = Database(path, seed=False)
        try:
            self.assertEqual(
                (second.active_binding("main")["binding_number"],
                 second.active_binding("main")["constraint_set_version"]),
                first_active,
            )
            self.assertEqual(
                [(p["workbook_version_number"], p["pin_number"])
                 for p in second.pin_history("main")],
                first_pins,
            )
            # 重启后 v1 已是钉版修订 1：陈旧 expected=0 返回 409，
            # 不写记录、不移动分支头；正确修订号 1 可再次钉版。
            with self.assertRaises(VersionConflict):
                second.create_pin(1, "规则集", 1, 0)
            pin = second.create_pin(1, "规则集", 2, 1)
            self.assertEqual(pin["pin_number"], 2)
            # 钉版仍不消费分支绑定代数。
            self.assertEqual(second.active_binding("main")["binding_number"], 2)
        finally:
            second.conn.close()

    def test_tampered_migration_is_idempotent(self):
        db = Database(self._build_file_db(), seed=False)
        try:
            db._migrate_legacy_schema()
            db._migrate_legacy_schema()
            self.assertEqual(len(db.binding_history("main")), 2)
            self.assertEqual(len(db.pin_history("main")), 1)
        finally:
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
