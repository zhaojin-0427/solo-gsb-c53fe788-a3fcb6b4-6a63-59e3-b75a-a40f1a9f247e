"""版本级钉版：把不可变规则钉到指定历史版本，并在历史详情中恢复规则。

钉版与分支绑定是两类独立状态：钉版只改变目标历史版本的规则关联，
不追加分支绑定、不改变分支当前规则与绑定代数，后续预演/提交/合并门禁
保持不变。
"""
import threading
import unittest

from app.db import Database, ValidationFailed, VersionConflict


class VersionLevelBindingTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        # main 上产生 v1..v4，并让约束集演进到 v2。
        self.edits = [{"sheet": "Inputs", "address": "B2", "content": "20"}]
        self.db.commit_edits("main", self.edits, "v2", 1)
        self.db.save_constraint_set(
            "门禁",
            [{"name": "数量上限", "type": "number_range", "target": "Inputs!B2:B4", "max": 100}],
            "宽松", 0,
        )
        self.db.create_binding("main", "门禁", 1, 1)  # 绑定代数 2 @ v2
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B3", "content": "1"}], "v3", 2
        )
        self.db.save_constraint_set(
            "门禁",
            [{"name": "数量上限", "type": "number_range", "target": "Inputs!B2:B4", "max": 30}],
            "收紧", 1,
        )
        self.db.create_binding("main", "门禁", 2, 2)  # 绑定代数 3 @ v3
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B4", "content": "2"}], "v4", 3
        )

    def test_commit_records_version_to_rule_set_association(self):
        # 已提交版本保留提交时通过门禁的规则关联（commit），后续在同一头
        # 版本上的换绑不会改写历史；新绑定之后的提交按新集记录。
        expected = {
            2: (1, "commit", "示例约束集"),
            3: (1, "commit", "门禁"),
            4: (2, "commit", "门禁"),
        }
        for version_id, (set_version, source, set_name) in expected.items():
            effective = self.db.effective_version_constraint_set(version_id)
            self.assertEqual(effective["constraint_set_version"], set_version)
            self.assertEqual(effective["source"], source)
            self.assertEqual(effective["constraint_set"], set_name)
            self.assertTrue(effective["rules"])

    def test_commit_source_covers_versions_after_last_binding(self):
        # 新分支：只绑定一次（钉住头 v3，该版本在 main 上提交时已记录过
        # 门禁 v2 关联）；之后连续提交的版本由提交期关联（commit）覆盖，
        # 恢复的规则仍正确。
        self.db.create_branch("dev", 3)
        self.db.create_binding("dev", "门禁", 2, 0)  # dev 绑定代数 1
        # v3 在 main 上已按门禁 v2 提交过；dev 的绑定不改变其关联来源。
        self.assertEqual(
            self.db.effective_version_constraint_set(3)["source"], "commit"
        )
        self.db.commit_edits(
            "dev", [{"sheet": "Inputs", "address": "B2", "content": "6"}], "dev v5", 3
        )
        v5 = self.db.branch("dev")["head_version_id"]
        self.db.commit_edits(
            "dev", [{"sheet": "Inputs", "address": "B2", "content": "7"}], "dev v6", 5
        )
        v6 = self.db.branch("dev")["head_version_id"]
        self.assertEqual(
            self.db.effective_version_constraint_set(v5)["source"], "commit"
        )
        self.assertEqual(
            self.db.effective_version_constraint_set(v5)["constraint_set_version"], 2
        )
        self.assertEqual(
            self.db.effective_version_constraint_set(v6)["source"], "commit"
        )
        # 换绑发生时头仍是 v6：已提交的 v5/v6 历史关联不被改写；
        # v3 规则只影响换绑之后的新提交。
        self.db.save_constraint_set(
            "门禁",
            [{"name": "数量上限", "type": "number_range", "target": "Inputs!B2:B4", "max": 200}],
            "放宽", 2,
        )
        self.db.create_binding("dev", "门禁", 3, 1)
        self.assertEqual(
            self.db.effective_version_constraint_set(v5)["constraint_set_version"], 2
        )
        self.assertEqual(
            self.db.effective_version_constraint_set(v6)["constraint_set_version"], 2
        )
        self.db.commit_edits(
            "dev", [{"sheet": "Inputs", "address": "B2", "content": "8"}], "dev v7", 6
        )
        v7 = self.db.branch("dev")["head_version_id"]
        self.assertEqual(
            self.db.effective_version_constraint_set(v7)["constraint_set_version"], 3
        )
    def test_version_detail_and_list_carry_effective_rules(self):
        detail = self.db.version(2)
        self.assertEqual(detail["effective_constraint_set"]["constraint_set"], "示例约束集")
        listed = {v["id"]: v["effective_constraint_set"] for v in self.db.versions("main")}
        self.assertEqual(listed[2]["constraint_set"], "示例约束集")
        self.assertEqual(listed[3]["constraint_set_version"], 1)
        self.assertEqual(listed[4]["constraint_set_version"], 2)
        # v1 是种子数据中显式钉住的版本。
        self.assertEqual(listed[1]["constraint_set"], "示例约束集")
        self.assertEqual(listed[1]["source"], "pinned")
        # 每个条目都带完整规则。
        self.assertTrue(all(v["rules"] for v in listed.values()))

    def test_pin_immutable_set_to_historical_version(self):
        # 把宽松的约束集 v1 钉到历史版本 v4。expected_version_number 校验
        # 的是目标版本自身的钉版修订号（v4 未钉版，故为 0），与分支绑定
        # 代数（3）无关。
        pin = self.db.create_pin(4, "门禁", 1, 0)
        self.assertEqual(pin["kind"], "version_pin")
        self.assertEqual(pin["pin_number"], 1)
        self.assertEqual(pin["workbook_version_number"], 4)
        self.assertEqual(pin["workbook_version_id"], 4)
        self.assertEqual(pin["branch_name"], "main")
        # 历史版本详情恢复被钉住的 v1 规则。
        effective = self.db.effective_version_constraint_set(4)
        self.assertEqual(effective["constraint_set_version"], 1)
        self.assertEqual(effective["source"], "pinned")
        self.assertEqual(effective["pin_revision"], 1)
        self.assertEqual(effective["rules"][0]["max"], 100.0)
        # 钉版不移动分支头，也不产生工作簿版本。
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)
        self.assertEqual(len(self.db.versions()), 4)

    def test_pin_does_not_append_branch_binding_or_change_generation(self):
        # 这是本次修复的核心回归：钉版不得追加分支绑定、不得改变分支当前
        # 规则与绑定代数。
        before = self.db.active_binding("main")
        branch_history_before = self.db.binding_history("main")
        self.db.create_pin(4, "门禁", 1, 0)
        after = self.db.active_binding("main")
        self.assertEqual(after["binding_number"], before["binding_number"])
        self.assertEqual(after["constraint_set_id"], before["constraint_set_id"])
        self.assertEqual(self.db.binding_history("main"), branch_history_before)
        # 钉版记录出现在独立的钉版历史里，而不是分支绑定历史里。
        pins = self.db.pin_history("main")
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["kind"], "version_pin")
        self.assertEqual(pins[0]["pin_number"], 1)

    def test_gate_still_uses_branch_rules_after_pin_and_commit_again(self):
        # 分支当前规则是收紧后的门禁 v2（B2:B4 max=30）。把宽松 v1
        # （max=100）钉到历史 v4 后：
        self.db.create_pin(4, "门禁", 1, 0)
        # 1) 违反原严格规则（50 > 30）的预演仍被拦截；
        preview = self.db.validate_edits(
            "main", [{"sheet": "Inputs", "address": "B2", "content": "50"}], 4
        )
        self.assertFalse(preview["ok"])
        self.assertTrue(preview.get("constraint_violations"))
        self.assertEqual(
            preview["audit"]["constraints"]["constraint_set_version"], 2
        )
        # 2) 违反规则的提交仍 422，且不写版本、不移动分支头；
        with self.assertRaises(ValidationFailed):
            self.db.commit_edits(
                "main", [{"sheet": "Inputs", "address": "B2", "content": "50"}],
                "钉版后违规", 4,
            )
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)
        self.assertEqual(len(self.db.versions()), 4)
        # 3) 钉版之后再次合规提交（5 <= 30）成功，新版本按分支当前严格
        # 规则（门禁 v2）记录，而不是被钉版的宽松 v1 放行/污染。
        result = self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B2", "content": "5"}],
            "钉版后再次提交", 4,
        )
        v5 = result["version"]["id"]
        self.assertEqual(self.db.branch("main")["head_version_id"], v5)
        self.assertEqual(
            self.db.effective_version_constraint_set(v5)["constraint_set_version"], 2
        )
        # 被钉的 v4 仍是宽松 v1，历史不随后续提交改变。
        self.assertEqual(
            self.db.effective_version_constraint_set(4)["constraint_set_version"], 1
        )

    def test_merge_gate_independent_of_pin(self):
        # feature 从 v4 分叉；在 main 把宽松规则钉到 v4 不应影响合并门禁。
        self.db.create_branch("feature", 4)
        self.db.create_pin(4, "门禁", 1, 0)
        # feature 提交一个按分支严格 v2（max=30）会违规的值 50。
        self.db.commit_edits(
            "feature", [{"sheet": "Inputs", "address": "B2", "content": "50"}],
            "feature 大单", 4,
        )
        preview = self.db.merge_preview("main", "feature")
        self.assertFalse(preview["ok"])
        # 合并门禁以目标分支绑定（门禁 v2）为准，钉版 v1 不会把它放宽。
        self.assertEqual(
            preview["constraint_sets"]["target"]["constraint_set_version"], 2
        )
        self.assertTrue(preview["constraint_violations"])

    def test_pin_overwrites_previous_pin_same_version(self):
        self.db.create_pin(4, "门禁", 1, 0)
        # 同一版本再次钉版必须提供新的独立修订号 1；关联以最新钉版为准。
        self.db.create_pin(4, "门禁", 2, 1)
        effective = self.db.effective_version_constraint_set(4)
        self.assertEqual(effective["constraint_set_version"], 2)
        self.assertEqual(effective["source"], "pinned")
        self.assertEqual(effective["pin_revision"], 2)
        self.assertEqual(len(self.db.pin_history("main")), 2)
        # 不产生任何分支绑定。
        self.assertEqual(len(self.db.binding_history("main")), 3)
        # 分支绑定代数仍是 3。
        self.assertEqual(self.db.active_binding("main")["binding_number"], 3)

    def test_duplicate_pin_request_is_conflict_without_write(self):
        self.db.create_pin(4, "门禁", 1, 0)
        with self.assertRaises(VersionConflict):
            self.db.create_pin(4, "门禁", 1, 0)
        # 冲突不写钉版记录、不写分支绑定、不移动分支头。
        self.assertEqual(len(self.db.pin_history("main")), 1)
        self.assertEqual(len(self.db.binding_history("main")), 3)
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)

    def test_stale_pin_revision_is_conflict(self):
        # 目标版本未钉版时期望一个不存在的修订号 -> 409。
        with self.assertRaises(VersionConflict):
            self.db.create_pin(4, "门禁", 1, 9)
        self.assertEqual(self.db.pin_history("main"), [])
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)

    def test_pin_revision_is_independent_per_version(self):
        # 两个历史版本各自维护独立修订号：钉过 v4（修订 1）不影响 v3
        # （未钉版仍为 0）。
        self.db.create_pin(4, "门禁", 1, 0)
        pin_v3 = self.db.create_pin(3, "门禁", 1, 0)
        self.assertEqual(pin_v3["pin_number"], 1)
        self.assertEqual(self.db.effective_version_constraint_set(3)["pin_revision"], 1)
        self.assertEqual(self.db.effective_version_constraint_set(4)["pin_revision"], 1)
        pins = self.db.pin_history("main")
        self.assertEqual([(p["workbook_version_number"], p["pin_number"]) for p in pins],
                         [(3, 1), (4, 1)])

    def test_pin_validates_version_ownership(self):
        # 未知版本
        with self.assertRaises(KeyError):
            self.db.create_pin(999, "门禁", 1, 0)
        # 版本属于 main，却声称绑到不存在的分支 ghost
        with self.assertRaises(ValueError):
            self.db.create_pin(4, "门禁", 1, 0, branch_name="ghost")
        self.db.create_branch("feature", 1)
        # feature 分叉自 v1：v4 不属于 feature，钉版必须被拒（400 语义）。
        with self.assertRaises(ValueError):
            self.db.create_pin(4, "门禁", 1, 0, branch_name="feature")
        # 被拒绝后 feature 仍无绑定记录，v4 关联未受影响。
        self.assertIsNone(self.db.active_binding("feature"))
        self.assertEqual(
            self.db.effective_version_constraint_set(4)["constraint_set_version"], 2
        )

    def test_pin_without_branch_uses_owning_branch(self):
        # 不传分支：钉版自动归属版本所在分支。
        pin = self.db.create_pin(4, "门禁", 1, 0)
        self.assertEqual(pin["branch_name"], "main")

    def test_pin_version_owned_by_other_branch(self):
        self.db.create_branch("feature", 1)
        self.db.commit_edits(
            "feature", [{"sheet": "Inputs", "address": "B2", "content": "50"}],
            "feature v", 1,
        )
        feature_version_id = self.db.branch("feature")["head_version_id"]
        # feature 尚无绑定与钉版（expected=0 是目标版本的独立钉版修订号）。
        pin = self.db.create_pin(feature_version_id, "门禁", 1, 0)
        self.assertEqual(pin["branch_name"], "feature")
        self.assertEqual(pin["workbook_version_id"], feature_version_id)
        effective = self.db.effective_version_constraint_set(feature_version_id)
        self.assertEqual(effective["constraint_set_version"], 1)
        self.assertEqual(effective["source"], "pinned")

    def test_branch_binding_behavior_unchanged(self):
        # 不传 version_id 时绑定到分支头，行为与旧版完全一致。
        self.db.create_branch("dev", 1)
        bound = self.db.create_binding("dev", "门禁", 1, 0)
        self.assertEqual(bound["kind"], "branch")
        self.assertEqual(bound["binding_number"], 1)
        self.assertEqual(bound["workbook_version_number"], 1)
        self.assertEqual(bound["workbook_version_id"], 1)

    def test_branch_rebind_still_uses_binding_generation(self):
        # 钉版不消费分支绑定代数：钉过 v4 之后，分支换绑仍沿用代数 3。
        self.db.create_pin(4, "门禁", 1, 0)
        self.db.save_constraint_set(
            "门禁",
            [{"name": "数量上限", "type": "number_range", "target": "Inputs!B2:B4", "max": 40}],
            "再收紧", 2,
        )
        rebound = self.db.create_binding("main", "门禁", 3, 3)
        self.assertEqual(rebound["binding_number"], 4)
        self.assertEqual(rebound["constraint_set_version"], 3)
        # 拿旧代数（3）重复换绑 -> 409。
        with self.assertRaises(VersionConflict):
            self.db.create_binding("main", "门禁", 3, 3)

    def test_pin_unknown_set_version(self):
        with self.assertRaises(KeyError):
            self.db.create_pin(4, "门禁", 99, 0)

    def test_pin_invalid_version_id_type(self):
        with self.assertRaises(ValueError):
            self.db.create_pin("4", "门禁", 1, 0)

    def test_binding_requires_branch_or_version(self):
        with self.assertRaises(ValueError):
            self.db.create_binding(None, "门禁", 1, 0)


class HistoricalRuleAuditTests(unittest.TestCase):
    """历史版本审计查询返回当时规则，且不受后续换绑影响。"""

    def setUp(self):
        self.db = Database(":memory:")

    def test_later_rebind_does_not_rewrite_history(self):
        # v2 在种子规则下提交；绑定 历史集 v1 钉住当时头 v2，
        # 之后 v3 是提交期关联（commit, 历史集 v1）。
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B2", "content": "5"}], "v2", 1
        )
        self.db.save_constraint_set(
            "历史集",
            [{"name": "上限100", "type": "number_range",
              "target": "Inputs!B2:B4", "max": 100}],
            "v1", 0,
        )
        self.db.create_binding("main", "历史集", 1, 1)  # pinned @ v2
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B3", "content": "1"}], "v3", 2
        )
        self.db.save_constraint_set(
            "历史集",
            [{"name": "上限5", "type": "number_range",
              "target": "Inputs!B2:B4", "max": 5}],
            "v2", 1,
        )
        self.db.create_binding("main", "历史集", 2, 2)  # pinned @ v3
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B4", "content": "1"}], "v4", 3
        )

        # v2 已在种子集下提交（commit），随后换绑不覆盖其历史关联；
        # v3 提交时按历史集 v1 记录；换绑 v2 后提交的 v4 记录历史集 v2。
        self.assertEqual(
            self.db.effective_version_constraint_set(2)["constraint_set"], "示例约束集"
        )
        self.assertEqual(
            self.db.effective_version_constraint_set(3)["rules"][0]["max"], 100.0
        )
        self.assertEqual(
            self.db.effective_version_constraint_set(4)["rules"][0]["max"], 5.0
        )
        # 显式把历史版本 v3 钉到 v1（与它提交时一致，但 source 变为 pinned），
        # v2 与 v4 的关联完全不受影响。expected 是 v3 的独立钉版修订号 0。
        self.db.create_pin(3, "历史集", 1, 0)
        self.assertEqual(
            self.db.effective_version_constraint_set(2)["constraint_set"], "示例约束集"
        )
        v3_after = self.db.effective_version_constraint_set(3)
        self.assertEqual(v3_after["rules"][0]["max"], 100.0)
        self.assertEqual(v3_after["source"], "pinned")
        self.assertEqual(
            self.db.effective_version_constraint_set(4)["rules"][0]["max"], 5.0
        )


class BindingConcurrencyTests(unittest.TestCase):
    """并发绑定/钉版的原子性。

    - 同一目标版本的并发钉版只有一个请求能消费该版本当前的钉版修订号；
    - 不同版本的钉版各自独立，可以同时成功；
    - 分支换绑仍按分支绑定代数互斥。
    """

    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_constraint_set(
            "门禁",
            [{"name": "上限", "type": "number_range", "target": "Inputs!B2", "max": 100}],
            "v1", 0,
        )
        self.barrier = threading.Barrier(4)
        self.errors = []

    def _pin(self, version_id, expected=0):
        try:
            self.barrier.wait()
            self.db.create_pin(version_id, "门禁", 1, expected)
        except Exception as exc:  # noqa: BLE001 - 收集到主线程断言
            self.errors.append(exc)

    def test_concurrent_pins_same_version_only_one_wins(self):
        threads = [threading.Thread(target=self._pin, args=(1,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 只有一个成功，其余全部 VERSION_CONFLICT。
        self.assertEqual(len(self.db.pin_history("main")), 1)
        self.assertEqual(self.db.pin_revision(1), 1)
        conflicts = [e for e in self.errors if isinstance(e, VersionConflict)]
        self.assertEqual(len(conflicts), 3)
        # 钉版不触碰分支绑定链：只有种子绑定。
        self.assertEqual(len(self.db.binding_history("main")), 1)
        self.assertEqual(self.db.active_binding("main")["binding_number"], 1)
        self.assertEqual(self.db.branch("main")["head_version_id"], 1)

    def test_concurrent_pins_different_versions_are_independent(self):
        # 不同目标版本的钉版修订号互相独立：两个版本各 expected=0 都成功。
        self.db.commit_edits(
            "main", [{"sheet": "Inputs", "address": "B2", "content": "20"}], "v2", 1
        )
        barrier = threading.Barrier(2)
        errors = []

        def pin(version_id):
            try:
                barrier.wait()
                self.db.create_pin(version_id, "门禁", 1, 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=pin, args=(vid,)) for vid in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.pin_revision(1), 1)
        self.assertEqual(self.db.pin_revision(2), 1)
        self.assertEqual(len(self.db.binding_history("main")), 1)

    def test_concurrent_branch_rebind_only_one_wins(self):
        barrier = threading.Barrier(4)
        errors = []

        def bind():
            try:
                barrier.wait()
                self.db.create_binding("main", "门禁", 1, 1)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=bind) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 代数 1 只被消费一次：成功一个，其余 409。
        self.assertEqual(self.db.active_binding("main")["binding_number"], 2)
        self.assertEqual(len(self.db.binding_history("main")), 2)
        self.assertEqual(len([e for e in errors if isinstance(e, VersionConflict)]), 3)
        # 分支换绑不产生钉版记录。
        self.assertEqual(self.db.pin_history("main"), [])

    def test_concurrent_constraint_set_creates_only_one_wins(self):
        rules = [{"name": "另一个", "type": "number_range",
                  "target": "Inputs!B2", "max": 1}]

        def create():
            try:
                self.barrier.wait()
                self.db.save_constraint_set("并发集", rules, "v1", 0)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.db.constraint_set("并发集")["versions"]), 1)
        self.assertEqual(
            len([e for e in self.errors if isinstance(e, VersionConflict)]), 3
        )

    def test_failed_gate_commit_does_not_write_association(self):
        # 种子规则要求数量非负：-5 必失败。
        with self.assertRaises(ValidationFailed):
            self.db.commit_edits(
                "main",
                [{"sheet": "Inputs", "address": "B2", "content": "-5"}],
                "违规", 1,
            )
        # 没有新版本，关联表也不应出现悬空行。
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM version_constraint_set").fetchone()[0],
            1,  # 仅种子 v1
        )
        self.assertEqual(self.db.branch("main")["head_version_id"], 1)


if __name__ == "__main__":
    unittest.main()
