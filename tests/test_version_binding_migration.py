"""旧版数据库迁移到版本级约束绑定模型的回归测试。"""
import unittest

from app.db import Database, SCHEMA
from tests.legacy_db import build_legacy_db


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
        # 种子数据也写了 v1 的版本关联。
        row = db.conn.execute(
            "SELECT * FROM version_constraint_set WHERE workbook_version_id=1"
        ).fetchone()
        self.assertEqual(row["source"], "pinned")


if __name__ == "__main__":
    unittest.main()
