import unittest

from app.db import BASE_CONFLICT, Database, MergeConflict, VersionConflict

KEY_B2 = ("Inputs", 1, 1)
KEY_C2 = ("Inputs", 2, 1)


class RepeatedMergeBaseTests(unittest.TestCase):
    """重复合并时的共同祖先（合并基线）选择。"""

    def setUp(self):
        self.db = Database(":memory:")

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def content_of(self, version_id, key):
        return self.db._content_map(version_id).get(key)

    def head(self, branch):
        return self.db.branch(branch)["head_version_id"]

    def test_repeated_merge_keeps_target_edit_and_brings_source_new_change(self):
        # main 先有一个提交 v2
        self.db.commit_edits("main", self.edit(("Ledger", "B2", "=1+1")), "main note", 1)
        # 从 main v2 创建 feature，feature 修改 B2 为 30
        self.db.create_branch("feature", 2)
        self.db.commit_edits("feature", self.edit(("Inputs", "B2", "30")), "feature B2", 2)
        # 第一次合并 feature -> main：v4，merge_source_id 指向 v3
        first = self.db.merge("main", "feature")
        self.assertEqual(first["version"]["merge_source_id"], 3)

        # main 随后修改“已合入的单元格” B2 -> 40（v5）
        self.db.commit_edits("main", self.edit(("Inputs", "B2", "40")), "main edits merged cell", 4)
        # feature 继续提交“另一个单元格” C2 -> 3（v6）
        self.db.commit_edits("feature", self.edit(("Inputs", "C2", "3")), "feature other cell", 3)

        # 预览必须沿 parent_id + merge_source_id 回溯到 v3，而不是只沿 parent_id 回到 v2
        preview = self.db.merge_preview("main", "feature")
        self.assertTrue(preview, preview["conflicts"])
        self.assertFalse(preview["virtual_base"])
        self.assertEqual(preview["ancestor_version"]["id"], 3)
        self.assertEqual(
            [c["cell"] for c in preview["source_only_changes"]], ["Inputs!C2"]
        )
        self.assertEqual(preview["conflicts"], [])
        # 合入 C2=3 后，引用 Inputs!C2:C4 的 Summary!B3 必须增量重算
        audit_diffs = {d["cell"]: d for d in preview["semantic_audit"]["diffs"]}
        self.assertIn("Summary!B3", audit_diffs)
        self.assertEqual(audit_diffs["Summary!B3"]["after"]["value"]["value"], 5.0)

        # 真正提交：保留 main 的 B2=40，同时带入 feature 的 C2=3
        merged = self.db.merge("main", "feature")
        new_head = merged["version"]["id"]
        self.assertEqual(self.content_of(new_head, KEY_B2), "40")
        self.assertEqual(self.content_of(new_head, KEY_C2), "3")
        # 合并提交记录两个父边
        self.assertEqual(merged["version"]["parent_id"], 5)
        self.assertEqual(merged["version"]["merge_source_id"], 6)

    def test_preview_and_commit_share_base_and_target_version_check(self):
        self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "main change", 1)
        self.db.create_branch("feature", 1)
        self.db.commit_edits("feature", self.edit(("Inputs", "C2", "3")), "feature", 1)
        preview = self.db.merge_preview("main", "feature")
        self.assertEqual(preview["target_version"]["version_number"], 2)

        # 提交时携带预览得到的目标版本号；若目标头已推进则原子失败
        self.db.commit_edits("main", self.edit(("Inputs", "B3", "6")), "race", 2)
        with self.assertRaises(VersionConflict):
            self.db.merge("main", "feature", expected_version_number=2)
        # 失败不得移动分支头，也不得产生合并版本
        self.assertEqual(self.head("main"), 4)

    def test_genuine_two_sided_change_is_still_conflict_after_repeated_merge(self):
        self.db.create_branch("feature", 1)
        self.db.commit_edits("feature", self.edit(("Inputs", "B2", "30")), "f", 1)
        self.db.merge("main", "feature")  # main v2, source v2

        # main 改 B2=40；feature 也改 B2=50
        self.db.commit_edits("main", self.edit(("Inputs", "B2", "40")), "m")
        self.db.commit_edits("feature", self.edit(("Inputs", "B2", "50")), "f2", 2)

        preview = self.db.merge_preview("main", "feature")
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["conflicts"][0]["cell"], "Inputs!B2")
        self.assertEqual(preview["conflicts"][0]["target"], "40")
        self.assertEqual(preview["conflicts"][0]["source"], "50")
        self.assertEqual(preview["conflicts"][0]["base"], "30")
        main_before = self.head("main")
        with self.assertRaises(MergeConflict):
            self.db.merge("main", "feature")
        self.assertEqual(self.head("main"), main_before)


class CrissCrossVirtualBaseTests(unittest.TestCase):
    """多个互不支配的共同祖先时，确定性地合成虚拟基线。"""

    def setUp(self):
        self.db = Database(":memory:")

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def content_of(self, version_id, key):
        return self.db._content_map(version_id).get(key)

    def _clone_snapshot(self, version_id, branch_name, message, merge_source_id=None):
        """在指定分支头上生成一个内容克隆版本（手工构造 criss-cross 用）。"""
        conn = self.db.conn
        parent_id = self.db.branch(branch_name)["head_version_id"]
        cur = conn.execute(
            """INSERT INTO version(workbook_id,version_number,parent_id,merge_source_id,
               branch_name,message,created_at,audit_json)
               VALUES (1,(SELECT COALESCE(MAX(version_number),0)+1 FROM version),?,?,?,?,'2026-01-01T00:00:00Z','{}')""",
            (parent_id, merge_source_id, branch_name, message),
        )
        new_id = cur.lastrowid
        conn.execute(
            """INSERT INTO cell(version_id,sheet_name,column_index,row_index,content,kind,value_json,formula_json)
               SELECT ?,sheet_name,column_index,row_index,content,kind,value_json,formula_json FROM cell
               WHERE version_id=?""",
            (new_id, version_id),
        )
        conn.execute(
            """INSERT INTO edge(version_id,source_sheet,source_column,source_row,
               target_sheet,target_column,target_row,reference_kind,ref_start,ref_end)
               SELECT ?,source_sheet,source_column,source_row,target_sheet,target_column,target_row,
                      reference_kind,ref_start,ref_end FROM edge WHERE version_id=?""",
            (new_id, version_id),
        )
        conn.execute("UPDATE branch SET head_version_id=? WHERE name=?", (new_id, branch_name))
        return new_id

    def test_criss_cross_consistent_cell_merges_cleanly(self):
        # main v2: B2=20；feature v2: C2=3（均从 v1 分出）
        self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "m b2", 1)
        self.db.create_branch("feature", 1)
        f2 = self.db.commit_edits("feature", self.edit(("Inputs", "C2", "3")), "f c2", 1)["version"]["id"]
        m2 = self.db.branch("main")["head_version_id"]
        # main 把 feature 的 C2 合入（v3, merge_source=f2）
        self.db.merge("main", "feature")
        # feature 把 main 的 B2 合入（克隆 main v2 内容，merge_source=m2）
        f3 = self._clone_snapshot(m2, "feature", "feature takes main b2", merge_source_id=m2)

        # 现在双方各自继续：main B3=6，feature B3=7 —— criss-cross，
        # 极大共同祖先是 m2 与 f2（互不支配）。
        self.db.commit_edits("main", self.edit(("Inputs", "B3", "6")), "m b3")
        self.db.commit_edits("feature", self.edit(("Inputs", "B3", "7")), "f b3", f3)

        preview = self.db.merge_preview("main", "feature")
        self.assertTrue(preview["virtual_base"])
        self.assertEqual(
            sorted(v["id"] for v in preview["merge_bases"]), sorted([m2, f2])
        )
        # 两个基线在 B3 上均为空（一致），虚拟基线为空 -> B3 是双边真冲突
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["conflicts"][0]["cell"], "Inputs!B3")
        # B2/C2 在虚拟基线上已一致，不会被误报
        conflict_cells = {c["cell"] for c in preview["conflicts"]}
        self.assertNotIn("Inputs!B2", conflict_cells)
        self.assertNotIn("Inputs!C2", conflict_cells)

        # 基线计算是确定性的：重复预览结果一致
        again = self.db.merge_preview("main", "feature")
        self.assertEqual(again["merge_bases"], preview["merge_bases"])
        self.assertEqual(again["conflicts"], preview["conflicts"])

    def test_criss_cross_disagreeing_base_cell_uses_conflict_sentinel(self):
        # 手工构造两个互不支配的基线，它们对 B2 的取值不同：
        # main 线的基线 B2=20，feature 线的基线 B2=30。
        m2 = self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "m b2", 1)["version"]["id"]
        self.db.create_branch("feature", 1)
        f2 = self.db.commit_edits("feature", self.edit(("Inputs", "B2", "30")), "f b2", 1)["version"]["id"]
        # 互相合入对方的“其他单元格”，形成 criss-cross：
        # main v3 合入 feature 的 C2（克隆 f2 快照，再由 main 继续，这里直接克隆构造）
        m3 = self._clone_snapshot(f2, "main", "main takes feature tree", merge_source_id=f2)
        # 把 main 头恢复为仅含 B2=20 的内容，但保留 merge_source 边
        # （克隆 m2 内容，父为 m3，merge_source=f2 保持 DAG 形状）
        m4 = self._clone_snapshot(m2, "main", "restore b2=20", merge_source_id=f2)
        f3 = self._clone_snapshot(m2, "feature", "feature takes main tree", merge_source_id=m2)
        f4 = self._clone_snapshot(f2, "feature", "restore b2=30", merge_source_id=m2)
        self.assertEqual(m4, self.db.branch("main")["head_version_id"])
        self.assertEqual(f4, self.db.branch("feature")["head_version_id"])

        bases = self.db._merge_bases(m4, f4)
        self.assertEqual(bases, sorted(bases))
        virtual = self.db._virtual_base(bases)
        # 两个基线对 B2 取值不一致 -> 确定性哨兵
        self.assertEqual(virtual[KEY_B2], BASE_CONFLICT)

        # main 保持 B2=20，feature 保持 B2=30：虚拟基线矛盾，三路合并必报冲突
        preview = self.db.merge_preview("main", "feature")
        self.assertFalse(preview["ok"])
        self.assertIn("Inputs!B2", {c["cell"] for c in preview["conflicts"]})
        before = self.db.branch("main")["head_version_id"]
        with self.assertRaises(MergeConflict):
            self.db.merge("main", "feature")
        self.assertEqual(self.db.branch("main")["head_version_id"], before)


if __name__ == "__main__":
    unittest.main()
