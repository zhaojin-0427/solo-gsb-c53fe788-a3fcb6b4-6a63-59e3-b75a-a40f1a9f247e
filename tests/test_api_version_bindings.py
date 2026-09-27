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
            "expected_version_number": 1,
        })
        self.assertEqual(response.status_code, 200, response.text)
        binding = response.json()["binding"]
        self.assertEqual(binding["branch_name"], "main")
        self.assertEqual(binding["workbook_version_id"], 1)

        # 历史详情恢复被钉住的规则。
        detail = self.client.get("/api/versions/1").json()
        self.assertEqual(
            detail["version"]["effective_constraint_set"]["constraint_set"], "历史钉住"
        )
        self.assertEqual(
            detail["version"]["effective_constraint_set"]["source"], "pinned"
        )
        # 头版本 v3 的规则不受影响。
        head = self.client.get("/api/versions/3").json()
        self.assertEqual(
            head["version"]["effective_constraint_set"]["constraint_set"], "示例约束集"
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
