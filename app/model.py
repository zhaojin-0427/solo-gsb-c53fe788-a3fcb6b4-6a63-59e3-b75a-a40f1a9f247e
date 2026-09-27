"""Address and AST helpers for A1 formulas."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Address:
    sheet: str
    column: int  # zero-based
    row: int  # zero-based

    @property
    def key(self) -> str:
        return f"{self.sheet}!{column_name(self.column)}{self.row + 1}"


@dataclass(frozen=True)
class RangeRef:
    start: Address
    end: Address

    def __post_init__(self) -> None:
        if self.start.sheet != self.end.sheet:
            raise ValueError("区域引用必须位于同一工作表")
        c1, c2 = sorted((self.start.column, self.end.column))
        r1, r2 = sorted((self.start.row, self.end.row))
        object.__setattr__(self, "start", Address(self.start.sheet, c1, r1))
        object.__setattr__(self, "end", Address(self.end.sheet, c2, r2))

    @property
    def sheet(self) -> str:
        return self.start.sheet

    def cells(self) -> list[Address]:
        c1, c2 = sorted((self.start.column, self.end.column))
        r1, r2 = sorted((self.start.row, self.end.row))
        return [
            Address(self.sheet, c, r)
            for r in range(r1, r2 + 1)
            for c in range(c1, c2 + 1)
        ]


def column_name(column: int) -> str:
    """Convert a zero-based column number to its A1 name."""
    column += 1
    chars: list[str] = []
    while column:
        column, rem = divmod(column - 1, 26)
        chars.append(chr(65 + rem))
    return "".join(reversed(chars))


def column_index(name: str) -> int:
    result = 0
    for ch in name.upper():
        result = result * 26 + ord(ch) - 64
    return result - 1
