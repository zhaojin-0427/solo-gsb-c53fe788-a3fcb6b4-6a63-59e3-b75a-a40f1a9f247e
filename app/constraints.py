"""版本化约束门禁的规则模型与确定性评估。

约束集本身是不可变的：每次创建或修改都生成新的版本号，历史版本可以随时
取回当时的规则文本。本模块只包含纯函数；持久化、版本号分配与并发控制
全部在 db.py 中完成。
"""
from __future__ import annotations

import json

from .errors import FormulaError
from .evaluator import evaluate_formula
from .formula import iter_references, parse_formula
from .model import column_index, column_name

RULE_TYPES = ("number_range", "not_empty", "allowed_values", "unique")

RULE_TYPE_LABELS = {
    "number_range": "数值范围",
    "not_empty": "非空",
    "allowed_values": "允许值",
    "unique": "唯一性",
}

_COMMON_KEYS = {"name", "type", "target", "condition"}
_TYPE_KEYS = {
    "number_range": {"min", "max"},
    "not_empty": set(),
    "allowed_values": {"values"},
    "unique": set(),
}

MAX_RULES_PER_SET = 200
MAX_TARGET_CELLS = 1000


def address_label(sheet: str, column: int, row: int) -> str:
    return f"{sheet}!{column_name(column)}{row + 1}"


def _parse_a1(ref: str) -> tuple[int, int]:
    letters = "".join(ch for ch in ref if ch.isalpha())
    digits = "".join(ch for ch in ref if ch.isdigit())
    if not ref or not letters or not digits:
        raise ValueError(f"无效的单元格地址: {ref}")
    return column_index(letters), int(digits) - 1


def parse_target(target) -> tuple[str, list[tuple[str, int, int]]]:
    """解析 ``Sheet!A1`` 或 ``Sheet!A1:B2`` 目标，返回（工作表, 排序后的单元格列表）。"""
    if not isinstance(target, str) or "!" not in target:
        raise ValueError(f"约束目标必须是 工作表!A1 或 工作表!A1:B2 形式: {target!r}")
    sheet, ref = target.rsplit("!", 1)
    sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
    if not sheet:
        raise ValueError(f"约束目标缺少工作表名: {target!r}")
    parts = ref.split(":")
    if len(parts) > 2 or not all(parts):
        raise ValueError(f"约束目标区域无效: {target!r}")
    try:
        start = _parse_a1(parts[0])
        end = _parse_a1(parts[-1])
    except ValueError as exc:
        raise ValueError(f"约束目标地址无效: {target!r}") from exc
    c1, c2 = sorted((start[0], end[0]))
    r1, r2 = sorted((start[1], end[1]))
    cells = [(sheet, c, r) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
    if len(cells) > MAX_TARGET_CELLS:
        raise ValueError(f"约束目标区域过大（超过 {MAX_TARGET_CELLS} 个单元格）: {target!r}")
    return sheet, cells


def _canonical_value(value: dict) -> str:
    """把单元格值归一化为可比较的确定性字符串。"""
    kind = value.get("kind")
    if kind == "number":
        return json.dumps({"kind": "number", "value": float(value["value"])}, sort_keys=True)
    if kind == "text":
        return json.dumps({"kind": "text", "value": value["value"]}, sort_keys=True)
    if kind == "boolean":
        return json.dumps({"kind": "boolean", "value": bool(value["value"])}, sort_keys=True)
    return json.dumps({"kind": kind}, sort_keys=True)


def _canonical_literal(raw) -> str:
    if isinstance(raw, bool):
        return json.dumps({"kind": "boolean", "value": raw}, sort_keys=True)
    if isinstance(raw, (int, float)):
        return json.dumps({"kind": "number", "value": float(raw)}, sort_keys=True)
    return json.dumps({"kind": "text", "value": str(raw)}, sort_keys=True)


def _is_blank(value: dict) -> bool:
    return value.get("kind") == "blank" or (value.get("kind") == "text" and not value.get("value"))


def _range_text(rule: dict) -> str:
    lo, hi = rule.get("min"), rule.get("max")
    if lo is not None and hi is not None:
        return f"{lo} ≤ 值 ≤ {hi}"
    if lo is not None:
        return f"值 ≥ {lo}"
    return f"值 ≤ {hi}"


def validate_rules(rules, sheets: dict[str, int], max_col: int, max_row: int) -> list[dict]:
    """校验并规范化规则列表；任何非法规则都会抛出 ValueError。"""
    if not isinstance(rules, list) or not rules:
        raise ValueError("约束集必须包含至少一条规则")
    if len(rules) > MAX_RULES_PER_SET:
        raise ValueError(f"单个约束集最多 {MAX_RULES_PER_SET} 条规则")
    seen_names: set[str] = set()
    normalized: list[dict] = []
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index + 1} 条规则必须是 JSON 对象")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index + 1} 条规则缺少名称")
        name = name.strip()
        if name in seen_names:
            raise ValueError(f"规则名称重复: {name}")
        seen_names.add(name)
        rule_type = raw.get("type")
        if rule_type not in RULE_TYPES:
            raise ValueError(
                f"规则 {name} 的类型无效: {rule_type!r}，支持: {', '.join(RULE_TYPES)}"
            )
        unknown = set(raw) - _COMMON_KEYS - _TYPE_KEYS[rule_type]
        if unknown:
            raise ValueError(f"规则 {name} 包含不支持的字段: {sorted(unknown)}")
        sheet, cells = parse_target(raw.get("target"))
        if sheet not in sheets:
            raise ValueError(f"规则 {name} 引用了未知工作表: {sheet}")
        for _sheet, col, row in cells:
            if not (0 <= col < max_col and 0 <= row < max_row):
                raise ValueError(f"规则 {name} 的目标超出工作表边界: {raw.get('target')}")
        rule: dict = {"name": name, "type": rule_type, "target": raw["target"]}
        condition = raw.get("condition")
        if condition is not None:
            if not isinstance(condition, str) or not condition.startswith("="):
                raise ValueError(f"规则 {name} 的条件公式必须以 = 开头")
            try:
                ast = parse_formula(condition, sheet)
            except FormulaError as exc:
                raise ValueError(f"规则 {name} 的条件公式无效: {exc.message}") from exc
            for dep in iter_references(ast):
                targets = dep["targets"] if dep["kind"] == "range" else [dep]
                for target in targets:
                    if target["sheet"] not in sheets or not (
                        0 <= target["column"] < max_col and 0 <= target["row"] < max_row
                    ):
                        raise ValueError(
                            f"规则 {name} 的条件公式引用了缺失或越界单元格: "
                            f"{address_label(target['sheet'], target['column'], target['row'])}"
                        )
            rule["condition"] = condition
        if rule_type == "number_range":
            lo, hi = raw.get("min"), raw.get("max")
            if lo is None and hi is None:
                raise ValueError(f"规则 {name} 至少需要 min 或 max 之一")
            for label, bound in (("min", lo), ("max", hi)):
                if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float))):
                    raise ValueError(f"规则 {name} 的 {label} 必须是数值")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"规则 {name} 的 min 不能大于 max")
            if lo is not None:
                rule["min"] = float(lo)
            if hi is not None:
                rule["max"] = float(hi)
        if rule_type == "allowed_values":
            values = raw.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError(f"规则 {name} 需要非空 values 列表")
            for item in values:
                if not isinstance(item, (str, int, float, bool)):
                    raise ValueError(f"规则 {name} 的允许值只能是文本、数值或布尔值")
            rule["values"] = values
        normalized.append(rule)
    return normalized


def _violation(rule: dict, index: int, cell: tuple, actual: dict, message: str,
               expectation: str, path_for) -> dict:
    return {
        "code": "CONSTRAINT_VIOLATION",
        "severity": "fatal",
        "rule": rule["name"],
        "rule_index": index,
        "rule_type": rule["type"],
        "cell": address_label(*cell),
        "actual": actual,
        "expectation": expectation,
        "message": message,
        "path": [address_label(*step) for step in path_for(cell)],
        "_sort": (index, cell[0], cell[2], cell[1]),
    }


def evaluate_rules(rules: list[dict], values: dict, changed: set, affected: set,
                   path_for) -> dict:
    """在增量重算完成后，按规则定义顺序评估受影响规则。

    - ``values``：重算后的 ``{(sheet, col, row): value}`` 全量映射；
    - ``changed`` / ``affected``：本次编辑直接变更与受影响（含重算）的单元格；
    - ``path_for(cell)``：返回从某个变更单元格到失败单元格的依赖路径。

    只有目标区域或条件公式依赖与受影响集合相交的规则才会被评估；
    返回的报告与违规列表顺序完全确定。
    """
    relevant = set(changed) | set(affected)
    evaluated: list[str] = []
    skipped: list[str] = []
    violations: list[dict] = []

    def lookup(sheet, col, row):
        return values.get((sheet, col, row), {"kind": "blank"})

    for index, rule in enumerate(rules):
        sheet, cells = parse_target(rule["target"])
        condition_ast = None
        condition_deps: set = set()
        if rule.get("condition"):
            condition_ast = parse_formula(rule["condition"], sheet)
            for dep in iter_references(condition_ast):
                if dep["kind"] == "range":
                    condition_deps.update(
                        (t["sheet"], t["column"], t["row"]) for t in dep["targets"]
                    )
                else:
                    condition_deps.add((dep["sheet"], dep["column"], dep["row"]))
        if not (set(cells) & relevant or condition_deps & relevant):
            skipped.append(rule["name"])
            continue
        evaluated.append(rule["name"])

        if condition_ast is not None:
            anchor = cells[0]
            outcome = evaluate_formula(
                condition_ast, lookup,
                {"sheet": anchor[0], "column": anchor[1], "row": anchor[2]},
                rule["condition"],
            )
            if outcome.get("kind") != "boolean":
                message = "条件公式求值失败" if outcome.get("kind") == "error" \
                    else "条件公式必须返回布尔值"
                violations.append(_violation(rule, index, anchor, outcome, message,
                                             "条件公式返回布尔值", path_for))
                continue
            if not outcome["value"]:
                continue  # 条件不成立：规则本次不适用

        rule_type = rule["type"]
        if rule_type == "not_empty":
            for cell in cells:
                value = lookup(*cell)
                if _is_blank(value):
                    violations.append(_violation(rule, index, cell, value,
                                                 "单元格不能为空", "非空", path_for))
        elif rule_type == "number_range":
            expectation = _range_text(rule)
            for cell in cells:
                value = lookup(*cell)
                if value.get("kind") == "blank":
                    continue  # 空白在数值范围检查中视为不适用；非空约束另行检查
                if value.get("kind") != "number":
                    violations.append(_violation(rule, index, cell, value,
                                                 "单元格不是数值", expectation, path_for))
                    continue
                number = value["value"]
                if ("min" in rule and number < rule["min"]) or \
                        ("max" in rule and number > rule["max"]):
                    violations.append(_violation(rule, index, cell, value,
                                                 f"数值超出允许范围（{expectation}）",
                                                 expectation, path_for))
        elif rule_type == "allowed_values":
            allowed = {_canonical_literal(item) for item in rule["values"]}
            expectation = "值必须属于 " + "、".join(
                json.dumps(item, ensure_ascii=False) for item in rule["values"]
            )
            for cell in cells:
                value = lookup(*cell)
                if value.get("kind") == "blank":
                    continue
                if _canonical_value(value) not in allowed:
                    violations.append(_violation(rule, index, cell, value,
                                                 "值不在允许列表中", expectation, path_for))
        elif rule_type == "unique":
            counts: dict[str, int] = {}
            cell_values: dict[tuple, tuple[str, dict]] = {}
            for cell in cells:
                value = lookup(*cell)
                if _is_blank(value):
                    continue
                key = _canonical_value(value)
                counts[key] = counts.get(key, 0) + 1
                cell_values[cell] = (key, value)
            for cell in cells:
                if cell in cell_values and counts[cell_values[cell][0]] > 1:
                    violations.append(_violation(rule, index, cell, cell_values[cell][1],
                                                 "值在目标区域内重复", "区域内值唯一", path_for))

    violations.sort(key=lambda item: item["_sort"])
    for item in violations:
        del item["_sort"]
    return {"evaluated_rules": evaluated, "skipped_rules": skipped, "violations": violations}
