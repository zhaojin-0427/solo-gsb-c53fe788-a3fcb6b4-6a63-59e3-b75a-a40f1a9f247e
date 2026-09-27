"""Tokenizer and recursive-descent parser for the supported A1 formula subset."""
import re

from .errors import FormulaError
from .model import Address, RangeRef, column_index

_TOKEN_RE = re.compile(
    r"""
    \s+
  | (?<![A-Za-z0-9_])(?P<cell>(?P<sheet>'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_]*)!
      \$?[A-Za-z]{1,3}\$?[0-9]{1,7}|\$?[A-Za-z]{1,3}\$?[0-9]{1,7})
  | (?P<number>[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|<>|[+\-*/(),:=<>])
    """,
    re.VERBOSE,
)


def _norm_sheet(name: str) -> str:
    if name.startswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _parse_cell_parts(text: str, current_sheet: str) -> dict:
    sheet = current_sheet
    ref = text
    if "!" in text:
        raw_sheet, ref = text.rsplit("!", 1)
        sheet = _norm_sheet(raw_sheet)
    col_letters = re.sub(r"\$", "", re.match(r"[A-Za-z$]+", ref).group(0))
    row_digits = re.sub(r"\$", "", re.search(r"[0-9]+$", ref).group(0))
    return {
        "sheet": sheet,
        "column": column_index(col_letters),
        "row": int(row_digits) - 1,
    }


def tokenize(formula: str, current_sheet: str) -> list[dict]:
    tokens: list[dict] = []
    pos = 0
    while pos < len(formula):
        match = _TOKEN_RE.match(formula, pos)
        if not match:
            raise FormulaError("PARSE_ERROR", f"无法识别的字符: {formula[pos]!r}", (pos, pos + 1))
        kind = match.lastgroup
        text = match.group()
        end = match.end()
        if kind == "cell":
            parts = _parse_cell_parts(text, current_sheet)
            tokens.append({"type": "cell", "text": text, "start": pos, "end": end, **parts})
        elif kind == "number":
            tokens.append({"type": "number", "value": float(text), "start": pos, "end": end})
        elif kind == "string":
            tokens.append({"type": "string", "value": text[1:-1].replace('""', '"'), "start": pos, "end": end})
        elif kind == "ident":
            tokens.append({"type": "ident", "value": text.upper(), "start": pos, "end": end})
        elif kind == "op":
            tokens.append({"type": "op", "value": text, "start": pos, "end": end})
        pos = end
    return tokens


class Parser:
    COMPARISONS = {"=", "<>", "<", "<=", ">", ">="}

    def __init__(self, formula: str, current_sheet: str):
        self.formula = formula
        self.tokens = tokenize(formula, current_sheet)
        self.i = 0

    def peek(self) -> dict | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> dict:
        token = self.peek()
        if token is None:
            end = len(self.formula)
            raise FormulaError("PARSE_ERROR", "公式意外结束", (end, end))
        self.i += 1
        return token

    def expect_op(self, value: str) -> dict:
        token = self.next()
        if token["type"] != "op" or token["value"] != value:
            raise FormulaError("PARSE_ERROR", f"应当出现 {value!r}", (token["start"], token["end"]))
        return token

    def parse(self) -> dict:
        ast = self.parse_comparison()
        if self.peek() is not None:
            token = self.peek()
            raise FormulaError("PARSE_ERROR", "公式末尾存在多余内容", (token["start"], token["end"]))
        return ast

    def parse_comparison(self) -> dict:
        node = self.parse_additive()
        token = self.peek()
        if token and token["type"] == "op" and token["value"] in self.COMPARISONS:
            self.next()
            right = self.parse_additive()
            node = {
                "type": "comparison",
                "op": token["value"],
                "left": node,
                "right": right,
                "start": node["start"],
                "end": right["end"],
                "operator_span": [token["start"], token["end"]],
            }
        return node

    def parse_additive(self) -> dict:
        node = self.parse_multiplicative()
        while True:
            token = self.peek()
            if not token or token["type"] != "op" or token["value"] not in {"+", "-"}:
                return node
            self.next()
            right = self.parse_multiplicative()
            node = {
                "type": "binary",
                "op": token["value"],
                "left": node,
                "right": right,
                "start": node["start"],
                "end": right["end"],
                "operator_span": [token["start"], token["end"]],
            }

    def parse_multiplicative(self) -> dict:
        node = self.parse_unary()
        while True:
            token = self.peek()
            if not token or token["type"] != "op" or token["value"] not in {"*", "/"}:
                return node
            self.next()
            right = self.parse_unary()
            node = {
                "type": "binary",
                "op": token["value"],
                "left": node,
                "right": right,
                "start": node["start"],
                "end": right["end"],
                "operator_span": [token["start"], token["end"]],
            }

    def parse_unary(self) -> dict:
        token = self.peek()
        if token and token["type"] == "op" and token["value"] in {"+", "-"}:
            self.next()
            operand = self.parse_unary()
            return {
                "type": "unary",
                "op": token["value"],
                "operand": operand,
                "start": token["start"],
                "end": operand["end"],
            }
        return self.parse_primary()

    def parse_primary(self) -> dict:
        token = self.next()
        if token["type"] == "number":
            return {"type": "number", "value": token["value"], "start": token["start"], "end": token["end"]}
        if token["type"] == "string":
            return {"type": "text", "value": token["value"], "start": token["start"], "end": token["end"]}
        if token["type"] == "op" and token["value"] == "(":
            node = self.parse_comparison()
            close = self.expect_op(")")
            node["start"] = token["start"]
            node["end"] = close["end"]
            return node
        if token["type"] == "cell":
            node = self.ref_node(token)
            colon = self.peek()
            if colon and colon["type"] == "op" and colon["value"] == ":":
                self.next()
                end_token = self.next()
                if end_token["type"] != "cell":
                    raise FormulaError("PARSE_ERROR", "区域结束位置必须是单元格引用", (end_token["start"], end_token["end"]))
                if "!" in end_token["text"]:
                    end_sheet = _norm_sheet(end_token["text"].rsplit("!", 1)[0])
                    if end_sheet != node["sheet"]:
                        raise FormulaError(
                            "PARSE_ERROR",
                            "连续区域的起止位置必须位于同一工作表",
                            (end_token["start"], end_token["end"]),
                        )
                end_node = self.ref_node(end_token, sheet_override=node["sheet"])
                node = {
                    "type": "range",
                    "range": RangeRef(
                        Address(node["sheet"], node["column"], node["row"]),
                        Address(end_node["sheet"], end_node["column"], end_node["row"]),
                    ),
                    "start": token["start"],
                    "end": end_node["end"],
                }
            return node
        if token["type"] == "ident":
            return self.parse_call(token)
        raise FormulaError("PARSE_ERROR", "应当出现数字、文本、引用、函数或括号表达式", (token["start"], token["end"]))

    def ref_node(self, token: dict, sheet_override: str | None = None) -> dict:
        return {
            "type": "ref",
            "sheet": sheet_override or token["sheet"],
            "column": token["column"],
            "row": token["row"],
            "start": token["start"],
            "end": token["end"],
        }

    def parse_call(self, name_token: dict) -> dict:
        name = name_token["value"]
        self.expect_op("(")
        args: list[dict] = []
        if not (self.peek() and self.peek()["type"] == "op" and self.peek()["value"] == ")"):
            args.append(self.parse_comparison())
            while self.peek() and self.peek()["type"] == "op" and self.peek()["value"] == ",":
                self.next()
                args.append(self.parse_comparison())
        close = self.expect_op(")")
        if name not in {"IF", "SUM"}:
            raise FormulaError("NAME_ERROR", f"未知函数: {name.title()}", (name_token["start"], name_token["end"]))
        if name == "IF" and len(args) != 3:
            raise FormulaError("PARSE_ERROR", "IF 需要恰好 3 个参数", (name_token["start"], close["end"]))
        if name == "SUM" and not args:
            raise FormulaError("PARSE_ERROR", "SUM 至少需要 1 个参数", (name_token["start"], close["end"]))
        return {
            "type": "call",
            "name": name,
            "args": args,
            "start": name_token["start"],
            "end": close["end"],
        }


def ast_to_json(node: dict) -> dict:
    result = dict(node)
    if result["type"] == "range":
        ref = result["range"]
        result["range"] = {
            "start": {"sheet": ref.start.sheet, "column": ref.start.column, "row": ref.start.row},
            "end": {"sheet": ref.end.sheet, "column": ref.end.column, "row": ref.end.row},
        }
    elif result["type"] in {"binary", "comparison"}:
        result["left"] = ast_to_json(result["left"])
        result["right"] = ast_to_json(result["right"])
    elif result["type"] == "unary":
        result["operand"] = ast_to_json(result["operand"])
    elif result["type"] == "call":
        result["args"] = [ast_to_json(arg) for arg in result["args"]]
    return result


def ast_from_json(node: dict) -> dict:
    result = dict(node)
    if result["type"] == "range":
        start, end = result["range"]["start"], result["range"]["end"]
        result["range"] = RangeRef(
            Address(start["sheet"], start["column"], start["row"]),
            Address(end["sheet"], end["column"], end["row"]),
        )
    elif result["type"] in {"binary", "comparison"}:
        result["left"] = ast_from_json(result["left"])
        result["right"] = ast_from_json(result["right"])
    elif result["type"] == "unary":
        result["operand"] = ast_from_json(result["operand"])
    elif result["type"] == "call":
        result["args"] = [ast_from_json(arg) for arg in result["args"]]
    return result


def parse_formula(formula: str, current_sheet: str) -> dict:
    if not formula.startswith("="):
        raise FormulaError("PARSE_ERROR", "公式必须以 = 开头", (0, 1))
    try:
        return Parser(formula[1:], current_sheet).parse()
    except FormulaError as exc:
        if exc.span is not None:
            exc.span = (exc.span[0] + 1, exc.span[1] + 1)
        raise


def iter_references(ast: dict):
    """Yield dependency descriptors with formula-relative spans.

    Parser positions omit the mandatory leading ``=``; callers add one when
    presenting source positions for complete formula text.
    """
    kind = ast["type"]
    if kind == "ref":
        yield {
            "kind": "cell",
            "sheet": ast["sheet"],
            "column": ast["column"],
            "row": ast["row"],
            "start": ast["start"] + 1,
            "end": ast["end"] + 1,
        }
    elif kind == "range":
        ref: RangeRef = ast["range"]
        yield {
            "kind": "range",
            "sheet": ref.sheet,
            "start_column": ref.start.column,
            "start_row": ref.start.row,
            "end_column": ref.end.column,
            "end_row": ref.end.row,
            "targets": [a.__dict__ for a in ref.cells()],
            "start": ast["start"] + 1,
            "end": ast["end"] + 1,
        }
    elif kind in {"binary", "comparison"}:
        yield from iter_references(ast["left"])
        yield from iter_references(ast["right"])
    elif kind == "unary":
        yield from iter_references(ast["operand"])
    elif kind == "call":
        for arg in ast["args"]:
            yield from iter_references(arg)
