import unittest

from app.db import Database, MergeConflict, ValidationFailed, VersionConflict


class ConstraintSetVersionTests(unittest.TestCase):
    """约束集的创建、修改与不可变版本号原子校验。"""

    def setUp(self):
        self.db = Database(":memory:")

    def rules(self, maximum=100):
        return [{"name": "数量上限", "type": "number_range",
                 "target": "Inputs!B2", "max": maximum}]

    def test_create_and_modify_produce_immutable_versions(self):
        created = self.db.save_constraint_set("门禁", self.rules(), "v1", 0)
        self.assertEqual(created["version_number"], 1)
        updated = self.db.save_constraint_set("门禁", self.rules(50), "收紧", 1)
        self.assertEqual(updated["version_number"], 2)
        detail = self.db.constraint_set("门禁")
        self.assertEqual([v["version_number"] for v in detail["versions"]], [1, 2])
        # 历史版本保持当时规则，可复现
        self.assertEqual(detail["versions"][0]["rules"][0]["max"], 100.0)
        self.assertEqual(detail["versions"][1]["rules"][0]["max"], 50.0)

    def test_duplicate_create_request_fails_atomically(self):
        payload = self.rules()
        self.db.save_constraint_set("门禁", payload, "v1", 0)
        # 重复请求：expected_version_number=0 已被消费，同名集合最新版本已是 1
        with self.assertRaises(VersionConflict):
            self.db.save_constraint_set("门禁", payload, "v1", 0)
        self.assertEqual(self.db.constraint_set("门禁")["versions"][0]["version_number"], 1)
        self.assertEqual(len(self.db.constraint_set("门禁")["versions"]), 1)

    def test_duplicate_modify_request_fails_atomically(self):
        self.db.save_constraint_set("门禁", self.rules(), "v1", 0)
        self.db.save_constraint_set("门禁", self.rules(50), "v2", 1)
        # 重复请求：expected_version_number=1 已被消费
        with self.assertRaises(VersionConflict):
            self.db.save_constraint_set("门禁", self.rules(50), "v2", 1)
        self.assertEqual(len(self.db.constraint_set("门禁")["versions"]), 2)

    def test_stale_expected_version_is_rejected(self):
        self.db.save_constraint_set("门禁", self.rules(), "v1", 0)
        with self.assertRaises(VersionConflict):
            self.db.save_constraint_set("门禁", self.rules(50), "v2", 9)

    def test_invalid_rules_are_rejected(self):
        cases = [
            [],  # 空规则集
            [{"type": "number_range", "target": "Inputs!B2", "min": 0}],  # 缺名称
            [{"name": "x", "type": "unknown", "target": "Inputs!B2"}],  # 未知类型
            [{"name": "x", "type": "number_range", "target": "B2", "min": 0}],  # 缺工作表
            [{"name": "x", "type": "number_range", "target": "Missing!B2", "min": 0}],  # 未知工作表
            [{"name": "x", "type": "number_range", "target": "Inputs!Z9", "min": 0}],  # 越界
            [{"name": "x", "type": "number_range", "target": "Inputs!B2"}],  # 缺 min/max
            [{"name": "x", "type": "number_range", "target": "Inputs!B2", "min": 5, "max": 1}],
            [{"name": "x", "type": "allowed_values", "target": "Inputs!B2", "values": []}],
            [{"name": "x", "type": "unique", "target": "Inputs!B2", "condition": "B2>1"}],  # 缺 =
            [{"name": "x", "type": "unique", "target": "Inputs!B2", "condition": "=Missing!A1"}],
            [{"name": "x", "type": "unique", "target": "Inputs!B2", "bogus": 1}],  # 未知字段
            [{"name": "dup", "type": "unique", "target": "Inputs!A2"},
             {"name": "dup", "type": "not_empty", "target": "Inputs!A2"}],  # 名称重复
        ]
        for rules in cases:
            with self.assertRaises(ValueError, msg=str(rules)):
                self.db.save_constraint_set("坏规则", rules, "", 0)


class BindingTests(unittest.TestCase):
    """约束集绑定使用 expected_version_number（绑定代数）做原子校验。"""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_constraint_set(
            "门禁", [{"name": "数量上限", "type": "number_range",
                      "target": "Inputs!B2", "max": 100}], "v1", 0)

    def test_seed_binding_exists_on_main(self):
        binding = self.db.active_binding("main")
        self.assertEqual(binding["constraint_set"], "示例约束集")
        self.assertEqual(binding["constraint_set_version"], 1)
        self.assertEqual(binding["binding_number"], 1)
        self.assertEqual(binding["workbook_version_number"], 1)

    def test_bind_rebind_and_history(self):
        bound = self.db.create_binding("main", "门禁", 1, 1)
        self.assertEqual(bound["binding_number"], 2)
        self.assertEqual(bound["constraint_set"], "门禁")
        history = self.db.binding_history("main")
        self.assertEqual([h["binding_number"] for h in history], [1, 2])
        self.assertEqual(history[0]["constraint_set"], "示例约束集")
        self.assertEqual(history[1]["constraint_set"], "门禁")
        # 当前生效绑定是最新一条
        self.assertEqual(self.db.active_binding("main")["constraint_set"], "门禁")

    def test_duplicate_bind_request_fails_atomically(self):
        self.db.create_binding("main", "门禁", 1, 1)
        # 重复请求：绑定代数 1 已被消费
        with self.assertRaises(VersionConflict):
            self.db.create_binding("main", "门禁", 1, 1)
        self.assertEqual(len(self.db.binding_history("main")), 2)

    def test_bind_unknown_set_or_branch(self):
        with self.assertRaises(KeyError):
            self.db.create_binding("main", "不存在", 1, 1)
        with self.assertRaises(KeyError):
            self.db.create_binding("main", "门禁", 9, 1)
        with self.assertRaises(KeyError):
            self.db.create_binding("ghost", "门禁", 1, 0)


class GateEvaluationTests(unittest.TestCase):
    """预演与提交时，增量重算完成后按稳定顺序评估受影响约束。"""

    def setUp(self):
        self.db = Database(":memory:")

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def bind_only(self, rules, message="测试规则"):
        """新建约束集并绑定到 main（种子绑定代数 1 之后）。"""
        self.db.save_constraint_set("测试门禁", rules, message, 0)
        self.db.create_binding("main", "测试门禁", 1, 1)

    def test_violation_blocks_preview_and_commit_without_moving_head(self):
        preview = self.db.validate_edits("main", self.edit(("Inputs", "B2", "-5")), 1)
        self.assertFalse(preview["ok"])
        violations = preview["constraint_violations"]
        self.assertEqual(len(violations), 1)
        violation = violations[0]
        self.assertEqual(violation["code"], "CONSTRAINT_VIOLATION")
        self.assertEqual(violation["rule"], "数量非负")
        self.assertEqual(violation["cell"], "Inputs!B2")
        self.assertEqual(violation["actual"], {"kind": "number", "value": -5.0})
        self.assertEqual(violation["path"], ["Inputs!B2"])
        self.assertEqual(violation["constraint_set"], "示例约束集")
        self.assertEqual(violation["constraint_set_version"], 1)

        with self.assertRaises(ValidationFailed):
            self.db.commit_edits("main", self.edit(("Inputs", "B2", "-5")), "坏提交", 1)
        # 任何失败都不得写入版本或移动分支头
        self.assertEqual(self.db.branch("main")["head_version_id"], 1)
        self.assertEqual(len(self.db.versions()), 1)

    def test_dependency_path_points_from_edit_to_failing_formula(self):
        self.bind_only([{"name": "金额上限", "type": "number_range",
                         "target": "Ledger!A2", "max": 100}])
        preview = self.db.validate_edits("main", self.edit(("Inputs", "B2", "1000")), 1)
        self.assertFalse(preview["ok"])
        violation = preview["constraint_violations"][0]
        self.assertEqual(violation["cell"], "Ledger!A2")
        self.assertEqual(violation["actual"]["value"], 2500.0)
        # 依赖路径：编辑单元格沿依赖边传播到失败单元格
        self.assertEqual(violation["path"], ["Inputs!B2", "Ledger!A2"])

    def test_not_empty_and_unique_rules(self):
        preview = self.db.validate_edits("main", self.edit(("Inputs", "A2", "")), 1)
        self.assertFalse(preview["ok"])
        rules = {v["rule"] for v in preview["constraint_violations"]}
        self.assertIn("项目必填", rules)

        preview = self.db.validate_edits("main", self.edit(("Inputs", "A3", "铅笔")), 1)
        self.assertFalse(preview["ok"])
        cells = [v["cell"] for v in preview["constraint_violations"] if v["rule"] == "项目名称唯一"]
        # 重复值涉及的两个单元格都被标记，顺序确定
        self.assertEqual(cells, ["Inputs!A2", "Inputs!A3"])

    def test_allowed_values_rule(self):
        preview = self.db.validate_edits("main", self.edit(("Ledger", "B8", "也许")), 1)
        self.assertFalse(preview["ok"])
        violation = preview["constraint_violations"][0]
        self.assertEqual(violation["rule"], "大额标记合法")
        self.assertEqual(violation["actual"], {"kind": "text", "value": "也许"})

    def test_condition_formula_gates_rule(self):
        self.bind_only([{
            "name": "条件单价上限", "type": "number_range", "target": "Inputs!C2",
            "max": 2, "condition": "=Inputs!B2>10",
        }])
        # 条件成立（B2=20>10）：C2=2.5 超过上限 2 -> 失败
        triggered = self.db.validate_edits("main", self.edit(("Inputs", "B2", "20")), 1)
        self.assertFalse(triggered["ok"])
        self.assertEqual(triggered["constraint_violations"][0]["rule"], "条件单价上限")
        # 条件不成立（B2=5）：规则不适用 -> 通过
        skipped = self.db.validate_edits("main", self.edit(("Inputs", "B2", "5")), 1)
        self.assertTrue(skipped["ok"])

    def test_only_affected_rules_are_evaluated(self):
        # 修改单价只影响 Ledger!A2/B6/B8 与 Summary!B1/B3，
        # 与 Inputs!A/B 列相关的规则不应被评估
        preview = self.db.validate_edits("main", self.edit(("Inputs", "C2", "3")), 1)
        self.assertTrue(preview["ok"])
        report = preview["audit"]["constraints"]
        self.assertEqual(report["evaluated_rules"], ["大额标记合法", "大额合计非负"])
        self.assertIn("数量非负", report["skipped_rules"])
        self.assertIn("项目必填", report["skipped_rules"])
        self.assertEqual(report["violations"], [])

    def test_violations_have_stable_order(self):
        self.bind_only([
            {"name": "名称唯一", "type": "unique", "target": "Inputs!A2:A4"},
            {"name": "数量上限", "type": "number_range", "target": "Inputs!B2:B4", "max": 1},
        ])
        preview = self.db.validate_edits(
            "main",
            self.edit(("Inputs", "A3", "铅笔"), ("Inputs", "B2", "5"), ("Inputs", "B3", "6")),
            1,
        )
        self.assertFalse(preview["ok"])
        ordered = [(v["rule"], v["cell"]) for v in preview["constraint_violations"]]
        self.assertEqual(ordered, [
            ("名称唯一", "Inputs!A2"),
            ("名称唯一", "Inputs!A3"),
            ("数量上限", "Inputs!B2"),
            ("数量上限", "Inputs!B3"),
            ("数量上限", "Inputs!B4"),
        ])

    def test_commit_success_records_gate_audit(self):
        result = self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "ok", 1)
        report = result["audit"]["constraints"]
        self.assertEqual(report["constraint_set"], "示例约束集")
        self.assertEqual(report["constraint_set_version"], 1)
        self.assertIn("数量非负", report["evaluated_rules"])
        self.assertEqual(report["violations"], [])


class CrossBranchConstraintTests(unittest.TestCase):
    """跨分支规则冲突与合并门禁。"""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_constraint_set("门禁", [
            {"name": "数量上限", "type": "number_range", "target": "Inputs!B2", "max": 100},
        ], "v1", 0)
        self.db.save_constraint_set("门禁", [
            {"name": "数量上限", "type": "number_range", "target": "Inputs!B2", "max": 200},
            {"name": "单价必填", "type": "not_empty", "target": "Inputs!C2:C4"},
        ], "v2", 1)
        self.db.create_binding("main", "门禁", 1, 1)
        self.db.create_branch("feature", 1)
        self.db.create_binding("feature", "门禁", 2, 0)

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def test_rule_conflicts_between_branches_are_reported(self):
        preview = self.db.merge_preview("main", "feature")
        self.assertEqual(preview["constraint_sets"]["target"]["constraint_set_version"], 1)
        self.assertEqual(preview["constraint_sets"]["source"]["constraint_set_version"], 2)
        conflicts = {c["rule"]: c for c in preview["constraint_conflicts"]}
        self.assertEqual(conflicts["数量上限"]["kind"], "changed")
        self.assertEqual(conflicts["数量上限"]["target"]["max"], 100.0)
        self.assertEqual(conflicts["数量上限"]["source"]["max"], 200.0)
        self.assertEqual(conflicts["单价必填"]["kind"], "source_only")

    def test_merge_blocked_when_source_data_violates_target_rules(self):
        # feature 按 v2 规则（上限 200）提交 B2=150 成功
        self.db.commit_edits("feature", self.edit(("Inputs", "B2", "150")), "放宽后的大单", 1)
        preview = self.db.merge_preview("main", "feature")
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["constraint_violations"][0]["rule"], "数量上限")
        self.assertEqual(preview["constraint_violations"][0]["constraint_set_version"], 1)

        main_head = self.db.branch("main")["head_version_id"]
        with self.assertRaises(MergeConflict) as ctx:
            self.db.merge("main", "feature")
        self.assertEqual(ctx.exception.detail["constraint_violations"][0]["cell"], "Inputs!B2")
        # 合并失败不生成版本、不移动分支头
        self.assertEqual(self.db.branch("main")["head_version_id"], main_head)

        # 目标分支换绑到 v2 后，同一数据可通过门禁并完成合并
        self.db.create_binding("main", "门禁", 2, 2)
        merged = self.db.merge("main", "feature")
        self.assertTrue(merged["ok"])
        self.assertEqual(
            self.db._content_map(merged["version"]["id"])[("Inputs", 1, 1)], "150"
        )

    def test_unbound_branches_report_no_rule_conflicts(self):
        self.db.create_branch("alpha", 1)
        self.db.create_branch("beta", 1)
        preview = self.db.merge_preview("alpha", "beta")
        self.assertIsNone(preview["constraint_sets"]["target"])
        self.assertIsNone(preview["constraint_sets"]["source"])
        self.assertEqual(preview["constraint_conflicts"], [])


class HistoricalReproductionTests(unittest.TestCase):
    """历史版本可复现当时规则：审计记录约束集版本，旧版本规则不可变。"""

    def setUp(self):
        self.db = Database(":memory:")

    def edit(self, *pairs):
        return [
            {"sheet": sheet, "address": address, "content": content}
            for sheet, address, content in pairs
        ]

    def test_versions_record_the_constraint_set_version_they_were_checked_with(self):
        first = self.db.commit_edits("main", self.edit(("Inputs", "B2", "20")), "v2", 1)
        self.assertEqual(first["audit"]["constraints"]["constraint_set_version"], 1)

        # 收紧规则：数量非负增加上限 10，生成约束集 v2 并换绑
        tightened = [
            {"name": "数量非负", "type": "number_range", "target": "Inputs!B2:B4",
             "min": 0, "max": 10},
            {"name": "项目必填", "type": "not_empty", "target": "Inputs!A2:A4"},
        ]
        self.db.save_constraint_set("示例约束集", tightened, "收紧数量上限", 1)
        self.db.create_binding("main", "示例约束集", 2, 1)

        # 新规则立即生效：B2=30 被 v2 拒绝，版本与分支头不变
        with self.assertRaises(ValidationFailed):
            self.db.commit_edits("main", self.edit(("Inputs", "B2", "30")), "超限", 2)
        self.assertEqual(self.db.branch("main")["head_version_id"], 2)

        second = self.db.commit_edits("main", self.edit(("Inputs", "B2", "5")), "v3", 2)
        self.assertEqual(second["audit"]["constraints"]["constraint_set_version"], 2)

        # 历史版本各自记录当时评估所用的约束集版本
        self.assertEqual(
            self.db.version(first["version"]["id"])["audit"]["constraints"]["constraint_set_version"], 1
        )
        self.assertEqual(
            self.db.version(second["version"]["id"])["audit"]["constraints"]["constraint_set_version"], 2
        )
        # 约束集历史版本不可变：v1 规则没有 max，v2 规则有 max=10
        versions = self.db.constraint_set("示例约束集")["versions"]
        rule_v1 = next(r for r in versions[0]["rules"] if r["name"] == "数量非负")
        rule_v2 = next(r for r in versions[1]["rules"] if r["name"] == "数量非负")
        self.assertNotIn("max", rule_v1)
        self.assertEqual(rule_v2["max"], 10.0)
        # 绑定历史记录每次绑定钉住的工作簿版本号
        history = self.db.binding_history("main")
        self.assertEqual(
            [(h["binding_number"], h["constraint_set_version"], h["workbook_version_number"])
             for h in history],
            [(1, 1, 1), (2, 2, 2)],
        )


if __name__ == "__main__":
    unittest.main()
