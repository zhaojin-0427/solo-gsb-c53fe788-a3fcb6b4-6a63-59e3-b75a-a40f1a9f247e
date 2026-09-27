"""Deterministic formula evaluator.

Values are plain dictionaries with a ``kind`` field. Formula error values use
the shape ``{"kind": "error", "code": ..., "message": ..., "source": ...}``.
"""
from collections.abc import Callable

from .errors import FormulaError

Number = dict
Lookup = Callable[[str, int, int], dict]

_NUMERIC_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b,
}


def error(code: str, message: str, source: dict) -> dict:
    return {"kind": "error", "code": code, "message": message, "source": source}


def is_number(value: dict) -> bool:
    return value.get("kind") == "number"


def numeric(value: dict) -> float | None:
    if value.get("kind") == "number":
        return value["value"]
    # A blank referenced cell behaves as zero only in ordinary numeric context.
    if value.get("kind") == "blank":
        return 0.0
    return None


def compare(op: str, left: dict, right: dict) -> bool:
    lv, rv = numeric(left), numeric(right)
    if lv is not None and rv is not None:
        a, b = lv, rv
    elif left.get("kind") == "boolean" and right.get("kind") == "boolean":
        a, b = left["value"], right["value"]
    elif left.get("kind") == "text" and right.get("kind") == "text":
        a, b = left["value"], right["value"]
    else:
        raise FormulaError("VALUE_ERROR", "不能比较不同类型的值")
    if op == "=":
        return a == b
    if op == "<>":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


class Evaluator:
    def __init__(self, lookup: Lookup, source_cell: dict, formula: str):
        self.lookup = lookup
        self.source_cell = source_cell
        self.formula = formula

    def eval(self, node: dict) -> dict:
        kind = node["type"]
        if kind == "number":
            return {"kind": "number", "value": node["value"]}
        if kind == "text":
            return {"kind": "text", "value": node["value"]}
        if kind == "ref":
            return self.lookup(node["sheet"], node["column"], node["row"])
        if kind == "range":
            raise FormulaError("VALUE_ERROR", "单个标量位置不能使用连续区域", (node["start"], node["end"]))
        if kind == "unary":
            value = self.eval(node["operand"])
            if value.get("kind") == "error":
                return value
            number = numeric(value)
            if number is None:
                raise FormulaError("VALUE_ERROR", "一元算术只能作用于数值", (node["start"], node["end"]))
            return {"kind": "number", "value": number if node["op"] == "+" else -number}
        if kind in {"binary", "comparison"}:
            left = self.eval(node["left"])
            if left.get("kind") == "error":
                return left
            right = self.eval(node["right"])
            if right.get("kind") == "error":
                return right
            span = tuple(node["operator_span"])
            try:
                if kind == "comparison":
                    return {"kind": "boolean", "value": compare(node["op"], left, right)}
                a, b = numeric(left), numeric(right)
                if a is None or b is None:
                    raise FormulaError("VALUE_ERROR", "算术运算只能作用于数值", span)
                if node["op"] == "/" and b == 0:
                    raise FormulaError("DIV0_ERROR", "除数不能为零", span)
                result = _NUMERIC_OPS[node["op"]](a, b)
                return {"kind": "number", "value": result}
            except FormulaError:
                raise
            except (OverflowError, ZeroDivisionError):
                raise FormulaError("NUM_ERROR", "数值超出可表示范围或被零除", span)
        if kind == "call":
            return self.eval_call(node)
        raise FormulaError("PARSE_ERROR", f"不支持的表达式: {kind}", None)

    def eval_call(self, node: dict) -> dict:
        # IF has lazy branch semantics, so evaluate it before generic eager args.
        if node["name"] == "IF":
            condition_node, true_node, false_node = node["args"]
            if condition_node["type"] == "range":
                raise FormulaError("VALUE_ERROR", "IF 条件不能是连续区域", (condition_node["start"], condition_node["end"]))
            condition = self.eval(condition_node)
            if condition.get("kind") == "error":
                return condition
            if condition.get("kind") != "boolean":
                raise FormulaError("VALUE_ERROR", "IF 的第一个参数必须是比较结果或布尔值", (node["start"], node["start"] + 2))
            chosen = true_node if condition["value"] else false_node
            if chosen["type"] == "range":
                raise FormulaError("VALUE_ERROR", "IF 结果不能是未聚合的连续区域", (chosen["start"], chosen["end"]))
            return self.eval(chosen)

        args = []
        for arg in node["args"]:
            if arg["type"] == "range":
                args.append(arg)
                continue
            value = self.eval(arg)
            if value.get("kind") == "error":
                return value
            args.append(value)

        total = 0.0
        for arg in args:
            if arg.get("type") == "range":
                ref = arg["range"]
                for addr in ref.cells():
                    value = self.lookup(addr.sheet, addr.column, addr.row)
                    if value.get("kind") == "error":
                        return value
                    number = numeric(value)
                    if number is None:
                        raise FormulaError("VALUE_ERROR", "SUM 只能汇总数值或空单元格", (arg["start"], arg["end"]))
                    total += number
            else:
                number = numeric(arg)
                if number is None:
                    raise FormulaError("VALUE_ERROR", "SUM 只能汇总数值或空单元格", (node["start"], node["end"]))
                total += number
        return {"kind": "number", "value": total}


def evaluate_formula(ast: dict, lookup: Lookup, source_cell: dict, formula: str) -> dict:
    try:
        return Evaluator(lookup, source_cell, formula).eval(ast)
    except FormulaError as exc:
        # The evaluator works on parser coordinates; persisted formula text
        # includes the leading '=', while parser coordinates omit it.
        span = (exc.span[0] + 1, exc.span[1] + 1) if exc.span else None
        result = error(exc.code, exc.message, {**source_cell, "span": list(span) if span else None})
        return result
