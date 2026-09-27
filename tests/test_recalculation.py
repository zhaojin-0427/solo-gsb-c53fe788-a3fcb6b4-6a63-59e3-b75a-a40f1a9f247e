import unittest

from app.db import Database, MergeConflict, ValidationFailed, VersionConflict


class RecalculationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def test_incremental_topological_recalculation_and_diff(self):
        preview = self.db.validate_edits(
            "main", self.edit(("Inputs", "B2", "20")), expected_version_number=1
        )
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["audit"]["changed"], ["Inputs!B2"])
        self.assertEqual(
            preview["audit"]["recalculation_order"],
            ["Ledger!A2", "Ledger!B6", "Ledger!B8", "Summary!B1"],
        )
        by_cell = {d["cell"]: d for d in preview["audit"]["diffs"]}
        self.assertEqual(by_cell["Ledger!A2"]["after"]["value"]["value"], 50.0)
        self.assertEqual(by_cell["Ledger!B6"]["after"]["value"]["value"], 102.0)
        self.assertEqual(by_cell["Ledger!B8"]["after"]["value"]["value"], "是")

    def test_type_error_is_locatable_and_propagates(self):
        preview = self.db.validate_edits(
            "main", self.edit(("Ledger", "A2", "=Inputs!A2+B2"))
        )
        self.assertFalse(preview["ok"])
        root = preview["errors"][0]
        self.assertEqual(root["code"], "VALUE_ERROR")
        self.assertEqual(root["cell"], "Ledger!A2")
        self.assertEqual((root["start"], root["end"]), (10, 11))
        propagated = {item["cell"]: item for item in preview["errors"][1:]}
        self.assertEqual(
            propagated["Summary!B1"]["path"],
            ["Ledger!A2", "Ledger!B6", "Summary!B1"],
        )
        with self.assertRaises(ValidationFailed):
            self.db.commit_edits("main", self.edit(("Ledger", "A2", "=Inputs!A2+B2")), "bad", 1)
        self.assertEqual(self.db.branch("main")["head_version_id"], 1)

    def test_missing_reference_and_cycle_return_shortest_diagnostics(self):
        missing = self.db.validate_edits("main", self.edit(("Ledger", "A2", "=Missing!A1")))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["errors"][0]["code"], "MISSING_REFERENCE")
        self.assertEqual(missing["errors"][0]["target"], "Missing!A1")

        cycle = self.db.validate_edits(
            "main",
            self.edit(("Ledger", "A2", "=A3"), ("Ledger", "A3", "=A2")),
        )
        self.assertEqual(cycle["errors"][0]["code"], "CYCLE_ERROR")
        self.assertEqual(cycle["errors"][0]["path"], ["Ledger!A2", "Ledger!A3", "Ledger!A2"])

    def test_branch_merge_conflict_and_success(self):
        self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "main change", 1)
        self.db.create_branch("feature", 1)
        self.db.commit_edits("feature", self.edit(("Inputs", "B2", "30")), "branch change", 1)
        preview = self.db.merge_preview("main", "feature")
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["conflicts"][0]["cell"], "Inputs!B2")
        with self.assertRaises(MergeConflict):
            self.db.merge("main", "feature")

        self.db.create_branch("clean-feature", 1)
        self.db.commit_edits("clean-feature", self.edit(("Inputs", "C2", "3")), "nonconflict", 1)
        merged = self.db.merge("main", "clean-feature")
        self.assertEqual(merged["version"]["parent_id"], 2)
        self.assertEqual(merged["version"]["merge_source_id"], 4)

    def test_expected_version_number_detects_conflict(self):
        with self.assertRaises(VersionConflict):
            self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "stale", 99)


if __name__ == "__main__":
    unittest.main()
