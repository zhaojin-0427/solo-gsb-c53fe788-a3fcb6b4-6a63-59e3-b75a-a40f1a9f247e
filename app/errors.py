"""Domain errors used by formula parsing and recalculation."""


class FormulaError(Exception):
    """A locatable spreadsheet error.

    ``code`` is a stable error code and ``span`` is the zero-based
    ``[start, end)`` character range in the formula or reference responsible
    for the error.
    """

    def __init__(self, code: str, message: str, span: tuple[int, int] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span

    def to_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.span is not None:
            data["start"], data["end"] = self.span
        return data


class CellValueError(Exception):
    """A spreadsheet error value produced while evaluating one cell."""

    def __init__(self, code: str, message: str, source: dict, span: tuple[int, int] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source
        self.span = span

    def to_dict(self) -> dict:
        data = {
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }
        if self.span is not None:
            data["start"], data["end"] = self.span
        return data
