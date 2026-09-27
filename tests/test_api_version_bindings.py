"""HTTP API 回归：版本级绑定、版本归属校验、历史详情/审计查询。"""
import os
import unittest

# 必须在导入 app.main（其模块级会 create_app()）之前指定内存数据库。
os.environ["SPREADSHEET_DB"] = ":memory:"

from litestar.testing import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


class VersionBindingApiTests(unittest.TestCase):
    def setUp(self):
        # 每个用例使用独立内存库，避免状态串扰。
        self.app = create_app(":memory:")
        self.client = TestClient(app=self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def _commit(self, address, content, expected, message="提交", branch="main"):
        return self.client.post("/api/commits", json={
            "branch": branch,
            "expected_version_number": expected,
            "message": message,
            "edits": [{"sheet": "Inputs", "address": address, "content": content}],
        })

    def _save_set(self, name, maximum, expected_version, path=None, message=""):
        body = {
            "name": name,
            "message": message,
            "expected_version_number": expected_version,
            "rules": [{
                "name": "数量上限", "type": "number_range",
                "target": "Inputs!B2:B4", "max": maximum,
            }],
        }
        url = path or "/api/constraint-sets"
        return self.client.post(url, json=body)

    def test_version_detail_and_list_return_effective_rules(self):
        response = self.client.get("/api/versions/1")
        self.assertEqual(response.status_code, 200)
        effective = response.json()["version"]["effective_constraint_set"]
        self.assertEqual(effective["constraint_set"], "示例约束集")
        self.assertEqual(effective["constraint_set_version"], 1)
        self.assertTrue(effective["rules"])
        self.assertEqual(effective["source"], "pinned")

        listed = self.client.get("/api/versions?branch=main").json()
        self.assertEqual(
            listed[0]["effective_constraint_set"]["constraint_set"], "示例约束集"
        )

    def test_audit_endpoint_returns_historical_rules(self):
        response = self.client.get("/api/versions/1/constraint-set")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version_id"], 1)
        self.assertEqual(payload["constraint_set"]["constraint_set"], "示例约束集")

        missing = self.client.get("/api/versions/999/constraint-set")
        self.assertEqual(missing.status_code, 404)

    def test_bind_to_branch_still_works(self):
        created = self._save_set("API门禁", 100, 0)
        self.assertEqual(created.status_code, 200)
        response = self.client.post("/api/constraint-bindings", json={
            "branch": "main",
            "constraint_set": "API门禁",
            "constraint_set_version": 1,
            "expected_version_number": 1,
        })
        self.assertEqual(response.status_code, 200, response.text)
        binding = response.json()["binding"]
        self.assertEqual(binding["constraint_set"], "API门禁")
        self.assertEqual(binding["workbook_version_number"], 1)
        self.assertEqual(binding["workbook_version_id"], 1)

    def test_pin_to_historical_version(self):
        # 先把工作簿推进到 v3，再把新建约束集钉回 v1。
        self._commit("B2", "20", 1, "v2")
        self._commit("B3", "1", 2, "v3")
        self._save_set("历史钉住", 100, 0)

        response = self.client.post("/api/constraint-bindings", json={
            "version_id": 1,
            "constraint_set": "历史钉住",
            "constraint_set_version": 1,
            # 钉版校验目标版本的独立钉版修订号（v1 未钉版 = 0），
            # 与 main 的绑定代数无关。
            "expected_version_number": 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        pin = response.json()["pin"]
        self.assertEqual(pin["kind"], "version_pin")
        self.assertEqual(pin["branch_name"], "main")
        self.assertEqual(pin["workbook_version_id"], 1)
        self.assertEqual(pin["pin_number"], 1)

        # 历史详情恢复被钉住的规则。
        detail = self.client.get("/api/versions/1").json()
        effective = detail["version"]["effective_constraint_set"]
        self.assertEqual(effective["constraint_set"], "历史钉住")
        self.assertEqual(effective["source"], "pinned")
        self.assertEqual(effective["pin_revision"], 1)
        # 头版本 v3 的规则不受影响。
        head = self.client.get("/api/versions/3").json()
        self.assertEqual(
            head["version"]["effective_constraint_set"]["constraint_set"], "示例约束集"
        )

    def test_pin_does_not_change_branch_binding_or_gate(self):
        # 核心回归：钉版到历史版本后，分支当前绑定/代数与后续门禁不变。
        self._save_set("宽松集", 10000, 0)
        active_before = self.client.get("/api/constraint-bindings").json()
        main_before = next(b for b in active_before if b["branch_name"] == "main")

        pin = self.client.post("/api/constraint-bindings", json={
            "version_id": 1,
            "constraint_set": "宽松集",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        })
        self.assertEqual(pin.status_code, 200, pin.text)

        # /api/constraint-bindings 仍只列分支绑定，且 main 绑定未变。
        active_after = self.client.get("/api/constraint-bindings").json()
        main_after = next(b for b in active_after if b["branch_name"] == "main")
        self.assertEqual(main_after["binding_number"], main_before["binding_number"])
        self.assertEqual(main_after["constraint_set"], main_before["constraint_set"])
        # 钉版历史独立查询。
        pins = self.client.get(
            "/api/constraint-bindings/pins?branch=main"
        ).json()
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["kind"], "version_pin")
        self.assertEqual(pins[0]["workbook_version_id"], 1)
        # 分支绑定历史里没有钉版记录。
        history = self.client.get(
            "/api/constraint-bindings/history?branch=main"
        ).json()
        self.assertEqual(len(history), 1)

        # 钉版宽松规则后，违反种子严格规则（数量非负）的编辑仍被拦截。
        bad = self._commit("B3", "-5", 1, "钉版后违规")
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertTrue(bad.json()["detail"].get("constraint_violations"))
        head = next(
            b for b in self.client.get("/api/branches").json() if b["name"] == "main"
        )["head_version_id"]
        self.assertEqual(head, 1)

    def test_duplicate_pin_returns_conflict_without_write(self):
        self._save_set("钉版重复", 100, 0)
        payload = {
            "version_id": 1,
            "constraint_set": "钉版重复",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        }
        first = self.client.post("/api/constraint-bindings", json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        duplicate = self.client.post("/api/constraint-bindings", json=payload)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "VERSION_CONFLICT")
        # 只有一条钉版，分支绑定只有种子一条。
        self.assertEqual(
            len(self.client.get("/api/constraint-bindings/pins?branch=main").json()), 1
        )
        self.assertEqual(
            len(self.client.get("/api/constraint-bindings/history?branch=main").json()), 1
        )

    def test_pin_validates_version_ownership(self):
        self._commit("B2", "20", 1, "v2")
        new_branch = self.client.post("/api/branches", json={
            "name": "feature", "from_version_id": 1,
        })
        self.assertEqual(new_branch.status_code, 201, new_branch.text)
        self._save_set("钉住集", 100, 0)

        # v2 属于 main，不能钉到 feature。
        response = self.client.post("/api/constraint-bindings", json={
            "branch": "feature",
            "version_id": 2,
            "constraint_set": "钉住集",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能绑定到分支", response.json()["detail"])

        # feature 没有新增绑定记录。
        history = self.client.get(
            "/api/constraint-bindings/history?branch=feature"
        ).json()
        self.assertEqual(history, [])

    def test_pin_unknown_version_is_404(self):
        self._save_set("钉住集", 100, 0)
        response = self.client.post("/api/constraint-bindings", json={
            "version_id": 42,
            "constraint_set": "钉住集",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        })
        self.assertEqual(response.status_code, 404)

    def test_duplicate_request_returns_conflict_without_write(self):
        self._save_set("重复集", 100, 0)
        payload = {
            "branch": "main",
            "constraint_set": "重复集",
            "constraint_set_version": 1,
            "expected_version_number": 1,
        }
        first = self.client.post("/api/constraint-bindings", json=payload)
        self.assertEqual(first.status_code, 200)
        duplicate = self.client.post("/api/constraint-bindings", json=payload)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "VERSION_CONFLICT")

        history = self.client.get(
            "/api/constraint-bindings/history?branch=main"
        ).json()
        self.assertEqual(len(history), 2)  # 种子绑定 + 一次成功绑定

    def test_commit_after_pin_uses_branch_rules(self):
        # 钉宽松规则到历史 v1 后：违规提交仍 422；合规提交成功并按分支当前
        # 严格（种子）规则记录，被钉的 v1 关联不变。
        self._save_set("宽松集", 10000, 0)
        pin = self.client.post("/api/constraint-bindings", json={
            "version_id": 1,
            "constraint_set": "宽松集",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        })
        self.assertEqual(pin.status_code, 200, pin.text)

        bad = self._commit("B3", "-5", 1, "违规")
        self.assertEqual(bad.status_code, 422, bad.text)

        ok = self._commit("B3", "3", 1, "钉版后合规提交")
        self.assertEqual(ok.status_code, 200, ok.text)
        new_id = ok.json()["version"]["id"]
        new_effective = self.client.get(
            f"/api/versions/{new_id}/constraint-set"
        ).json()["constraint_set"]
        self.assertEqual(new_effective["constraint_set"], "示例约束集")
        self.assertEqual(new_effective["source"], "commit")
        # 被钉的 v1 仍是宽松 pinned。
        v1 = self.client.get("/api/versions/1/constraint-set").json()["constraint_set"]
        self.assertEqual(v1["constraint_set"], "宽松集")
        self.assertEqual(v1["source"], "pinned")
        self.assertEqual(v1["pin_revision"], 1)

    def test_repin_requires_advanced_revision(self):
        self._save_set("钉版集", 100, 0)
        first = self.client.post("/api/constraint-bindings", json={
            "version_id": 1, "constraint_set": "钉版集",
            "constraint_set_version": 1, "expected_version_number": 0,
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["pin"]["pin_number"], 1)
        # 旧修订号 0 再次钉版 -> 409。
        stale = self.client.post("/api/constraint-bindings", json={
            "version_id": 1, "constraint_set": "钉版集",
            "constraint_set_version": 1, "expected_version_number": 0,
        })
        self.assertEqual(stale.status_code, 409)
        # 分支绑定代数不受钉版影响：仍是种子代数 1。
        main = next(
            b for b in self.client.get("/api/constraint-bindings").json()
            if b["branch_name"] == "main"
        )
        self.assertEqual(main["binding_number"], 1)

    def test_duplicate_constraint_set_create_is_conflict(self):
        self._save_set("一次", 100, 0)
        again = self._save_set("一次", 100, 0)
        self.assertEqual(again.status_code, 409)

    def test_binding_requires_branch_or_version(self):
        self._save_set("无目标", 100, 0)
        response = self.client.post("/api/constraint-bindings", json={
            "constraint_set": "无目标",
            "constraint_set_version": 1,
            "expected_version_number": 1,
        })
        self.assertEqual(response.status_code, 400)

    def test_invalid_version_id_type_is_400(self):
        self._save_set("类型集", 100, 0)
        response = self.client.post("/api/constraint-bindings", json={
            "branch": "main",
            "version_id": "1",
            "constraint_set": "类型集",
            "constraint_set_version": 1,
            "expected_version_number": 1,
        })
        self.assertEqual(response.status_code, 400)

    def test_pin_to_other_branch_version_without_branch_field(self):
        # main 推进到 v2，feature 从 v1 分叉并在 feature 上提交。
        self._commit("B2", "20", 1, "main v2")
        self.client.post("/api/branches", json={"name": "feature", "from_version_id": 1})
        self._commit("B2", "30", 1, "feature v", branch="feature")
        feature_head = next(
            b for b in self.client.get("/api/branches").json() if b["name"] == "feature"
        )["head_version_id"]
        self._save_set("跨分支钉", 100, 0)

        response = self.client.post("/api/constraint-bindings", json={
            "version_id": feature_head,
            "constraint_set": "跨分支钉",
            "constraint_set_version": 1,
            "expected_version_number": 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["binding"]["branch_name"], "feature")

    def test_conflict_does_not_move_branch_head(self):
        self._save_set("头不动", 100, 0)
        head_before = next(
            b for b in self.client.get("/api/branches").json() if b["name"] == "main"
        )["head_version_id"]
        # 错误的绑定代数 → 409。
        response = self.client.post("/api/constraint-bindings", json={
            "version_id": 1,
            "constraint_set": "头不动",
            "constraint_set_version": 1,
            "expected_version_number": 9,
        })
        self.assertEqual(response.status_code, 409)
        head_after = next(
            b for b in self.client.get("/api/branches").json() if b["name"] == "main"
        )["head_version_id"]
        self.assertEqual(head_before, head_after)


if __name__ == "__main__":
    unittest.main()
