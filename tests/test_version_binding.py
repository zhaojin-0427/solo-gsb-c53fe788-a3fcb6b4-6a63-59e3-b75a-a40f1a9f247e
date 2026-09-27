"""版本级约束绑定：把不可变规则钉到指定历史版本，并在历史详情中恢复规则。"""
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
        # 把约束集 v1 重新钉到历史版本 v4（当前生效绑定已是 v1..v2 之外的代数 3）。
        binding = self.db.create_binding("main", "门禁", 1, 3, version_id=4)
        self.assertEqual(binding["binding_number"], 4)
        self.assertEqual(binding["workbook_version_number"], 4)
        self.assertEqual(binding["workbook_version_id"], 4)
        # 历史版本详情恢复被钉住的 v1 规则。
        effective = self.db.effective_version_constraint_set(4)
        self.assertEqual(effective["constraint_set_version"], 1)
        self.assertEqual(effective["source"], "pinned")
        self.assertEqual(effective["rules"][0]["max"], 100.0)
        # 钉住不移动分支头，也不产生工作簿版本。
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)
        self.assertEqual(len(self.db.versions()), 4)

    def test_pin_overwrites_previous_pin_same_version(self):
        self.db.create_binding("main", "门禁", 1, 3, version_id=4)
        # 重复钉住（不同代数）：关联以最新绑定为准。
        self.db.create_binding("main", "门禁", 2, 4, version_id=4)
        effective = self.db.effective_version_constraint_set(4)
        self.assertEqual(effective["constraint_set_version"], 2)
        self.assertEqual(effective["source"], "pinned")
        self.assertEqual(len(self.db.binding_history("main")), 5)

    def test_duplicate_pin_request_is_conflict_without_write(self):
        self.db.create_binding("main", "门禁", 1, 3, version_id=4)
        with self.assertRaises(VersionConflict):
            self.db.create_binding("main", "门禁", 1, 3, version_id=4)
        # 冲突不写入绑定记录、不移动分支头。
        self.assertEqual(len(self.db.binding_history("main")), 4)
        self.assertEqual(self.db.branch("main")["head_version_id"], 4)

    def test_pin_validates_version_ownership(self):
        # 未知版本
        with self.assertRaises(KeyError):
            self.db.create_binding("main", "门禁", 1, 3, version_id=999)
        # 版本属于 main，却声称绑到不存在的分支 ghost
        with self.assertRaises(ValueError):
            self.db.create_binding("ghost", "门禁", 1, 3, version_id=4)
        self.db.create_branch("feature", 1)
        # feature 分叉自 v1：v4 不属于 feature，钉住必须被拒（400 语义）。
        with self.assertRaises(ValueError):
            self.db.create_binding("feature", "门禁", 1, 0, version_id=4)
        # 被拒绝后 feature 仍无绑定记录，v4 关联未受影响。
        self.assertIsNone(self.db.active_binding("feature"))
        self.assertEqual(
            self.db.effective_version_constraint_set(4)["constraint_set_version"], 2
        )

    def test_pin_without_branch_uses_owning_branch(self):
        # 不传分支：绑定自动归属版本所在分支，并消费该分支的绑定代数。
        binding = self.db.create_binding(None, "门禁", 1, 3, version_id=4)
        self.assertEqual(binding["branch_name"], "main")

    def test_pin_version_owned_by_other_branch(self):
        self.db.create_branch("feature", 1)
        self.db.commit_edits(
            "feature", [{"sheet": "Inputs", "address": "B2", "content": "50"}],
            "feature v", 1,
        )
        feature_version_id = self.db.branch("feature")["head_version_id"]
        # feature 尚无绑定（expected=0），把规则钉到 feature 的版本。
        binding = self.db.create_binding(None, "门禁", 1, 0, version_id=feature_version_id)
        self.assertEqual(binding["branch_name"], "feature")
        self.assertEqual(binding["workbook_version_id"], feature_version_id)
        effective = self.db.effective_version_constraint_set(feature_version_id)
        self.assertEqual(effective["constraint_set_version"], 1)
        self.assertEqual(effective["source"], "pinned")

    def test_branch_binding_behavior_unchanged(self):
        # 不传 version_id 时绑定到分支头，行为与旧版完全一致。
        self.db.create_branch("dev", 1)
        bound = self.db.create_binding("dev", "门禁", 1, 0)
        self.assertEqual(bound["binding_number"], 1)
        self.assertEqual(bound["workbook_version_number"], 1)
        self.assertEqual(bound["workbook_version_id"], 1)

    def test_pin_unknown_set_version(self):
        with self.assertRaises(KeyError):
            self.db.create_binding("main", "门禁", 99, 3, version_id=4)

    def test_pin_invalid_version_id_type(self):
        with self.assertRaises(ValueError):
            self.db.create_binding("main", "门禁", 1, 3, version_id="4")

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
        # v2 与 v4 的关联完全不受影响。
        self.db.create_binding("main", "历史集", 1, 3, version_id=3)
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
    """并发绑定/钉住的原子性：只有一个请求能消费当前绑定代数。"""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_constraint_set(
            "门禁",
            [{"name": "上限", "type": "number_range", "target": "Inputs!B2", "max": 100}],
            "v1", 0,
        )
        self.barrier = threading.Barrier(4)
        self.errors = []

    def _pin(self, version_id):
        try:
            self.barrier.wait()
            self.db.create_binding("main", "门禁", 1, 1, version_id=version_id)
        except Exception as exc:  # noqa: BLE001 - 收集到主线程断言
            self.errors.append(exc)

    def test_concurrent_pins_only_one_wins(self):
        threads = [threading.Thread(target=self._pin, args=(1,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 只有一个成功，其余全部 VERSION_CONFLICT。
        self.assertEqual(len(self.db.binding_history("main")), 2)
        conflicts = [e for e in self.errors if isinstance(e, VersionConflict)]
        self.assertEqual(len(conflicts), 3)

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
