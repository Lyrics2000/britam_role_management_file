"""
Extract the role data that was hard-coded in the legacy single-file page.

The old `Britam_Role_Library.html` embedded its entire data set as a JavaScript
object-literal array:

    const ROLES = [
      {bu:"Internal Audit",pos:"Head of Internal Audit",band:"Band 3",bandN:3,...},
      ...
    ];

That is *not* JSON — keys are bare identifiers, so `json.loads` rejects it.

ADR-009: a hand-written recursive-descent parser rather than either
(a) a regex per field, or (b) shelling out to node to `eval` the literal.
  - Regexes break the moment a value contains a comma, a brace or an escaped
    quote. The real data contains all three (e.g. "0 -2 years' experience").
  - `eval` in node means shipping a JS runtime in a Python image and executing
    untrusted-ish input. This parser executes nothing.
The grammar accepted below is a strict superset of what the file uses:
objects, arrays, single/double-quoted strings with escapes, numbers, booleans,
null, and trailing commas.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# The legacy keys, mapped onto model field names.
LEGACY_FIELD_MAP = {
    "bu": "business_unit_name",
    "pos": "position",
    "band": "band",
    "bandN": "band_numeric",
    "level": "level",
    "exp": "experience",
    "quals": "qualifications",
    "desc": "purpose",
    "focus": "focus_areas",
    "kras": "kras",
    "reports": "direct_reports",
    "techcomp": "technical_competencies",
    "leadcomp": "leadership_competencies",
}

ARRAY_DECL_RE = re.compile(r"\bconst\s+ROLES\s*=\s*\[", re.MULTILINE)

IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUMBER_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "`": "`",
    "\n": "",  # line continuation
}


class LegacyParseError(ValueError):
    """Raised when the legacy HTML cannot be parsed.

    Carries a unique code so the failure is greppable in the seeder's logs.
    """

    def __init__(self, code: str, message: str, position: int | None = None):
        self.code = code
        self.position = position
        where = f" (at character {position})" if position is not None else ""
        super().__init__(f"[{code}] {message}{where}")


class _Scanner:
    """Character scanner over the JS literal."""

    def __init__(self, text: str, start: int = 0):
        self.text = text
        self.i = start
        self.n = len(text)

    # -- low level ---------------------------------------------------------

    def peek(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def advance(self) -> str:
        ch = self.text[self.i]
        self.i += 1
        return ch

    def expect(self, ch: str) -> None:
        self.skip_trivia()
        if self.peek() != ch:
            raise LegacyParseError(
                "PARSE-001",
                f"expected {ch!r} but found {self.peek()!r}",
                self.i,
            )
        self.i += 1

    def skip_trivia(self) -> None:
        """Skip whitespace and // and /* */ comments."""
        while self.i < self.n:
            ch = self.text[self.i]
            if ch in " \t\r\n\f\v":
                self.i += 1
            elif ch == "/" and self.i + 1 < self.n and self.text[self.i + 1] == "/":
                end = self.text.find("\n", self.i)
                self.i = self.n if end == -1 else end + 1
            elif ch == "/" and self.i + 1 < self.n and self.text[self.i + 1] == "*":
                end = self.text.find("*/", self.i + 2)
                if end == -1:
                    raise LegacyParseError("PARSE-002", "unterminated block comment", self.i)
                self.i = end + 2
            else:
                return

    # -- values ------------------------------------------------------------

    def parse_value(self) -> Any:
        self.skip_trivia()
        ch = self.peek()
        if ch == "":
            raise LegacyParseError("PARSE-003", "unexpected end of input", self.i)
        if ch == "{":
            return self.parse_object()
        if ch == "[":
            return self.parse_array()
        if ch in "\"'`":
            return self.parse_string()
        if ch == "-" or ch.isdigit() or (ch == "." and self.i + 1 < self.n):
            return self.parse_number()
        match = IDENT_RE.match(self.text, self.i)
        if match:
            word = match.group(0)
            self.i = match.end()
            if word == "true":
                return True
            if word == "false":
                return False
            if word in ("null", "undefined"):
                return None
            raise LegacyParseError(
                "PARSE-004", f"unsupported bare word {word!r} in value position", match.start()
            )
        raise LegacyParseError("PARSE-005", f"unexpected character {ch!r}", self.i)

    def parse_string(self) -> str:
        quote = self.advance()
        out: list[str] = []
        while True:
            if self.i >= self.n:
                raise LegacyParseError("PARSE-006", "unterminated string literal", self.i)
            ch = self.advance()
            if ch == "\\":
                if self.i >= self.n:
                    raise LegacyParseError("PARSE-007", "trailing backslash in string", self.i)
                esc = self.advance()
                if esc == "u":
                    hex_digits = self.text[self.i : self.i + 4]
                    if len(hex_digits) < 4 or not all(c in "0123456789abcdefABCDEF" for c in hex_digits):
                        raise LegacyParseError("PARSE-008", "malformed \\u escape", self.i)
                    self.i += 4
                    out.append(chr(int(hex_digits, 16)))
                elif esc == "x":
                    hex_digits = self.text[self.i : self.i + 2]
                    if len(hex_digits) < 2 or not all(c in "0123456789abcdefABCDEF" for c in hex_digits):
                        raise LegacyParseError("PARSE-009", "malformed \\x escape", self.i)
                    self.i += 2
                    out.append(chr(int(hex_digits, 16)))
                else:
                    out.append(ESCAPES.get(esc, esc))
            elif ch == quote:
                return "".join(out)
            else:
                out.append(ch)

    def parse_number(self) -> float | int:
        match = NUMBER_RE.match(self.text, self.i)
        if not match:
            raise LegacyParseError("PARSE-010", "malformed number", self.i)
        self.i = match.end()
        raw = match.group(0)
        if any(c in raw for c in ".eE"):
            return float(raw)
        return int(raw)

    def parse_key(self) -> str:
        self.skip_trivia()
        ch = self.peek()
        if ch in "\"'":
            return self.parse_string()
        match = IDENT_RE.match(self.text, self.i)
        if not match:
            raise LegacyParseError("PARSE-011", f"invalid object key at {ch!r}", self.i)
        self.i = match.end()
        return match.group(0)

    def parse_object(self) -> dict[str, Any]:
        self.expect("{")
        obj: dict[str, Any] = {}
        while True:
            self.skip_trivia()
            if self.peek() == "}":
                self.i += 1
                return obj
            key = self.parse_key()
            self.expect(":")
            obj[key] = self.parse_value()
            self.skip_trivia()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "}":
                self.i += 1
                return obj
            raise LegacyParseError(
                "PARSE-012", f"expected ',' or '}}' after value, found {self.peek()!r}", self.i
            )

    def parse_array(self) -> list[Any]:
        self.expect("[")
        items: list[Any] = []
        while True:
            self.skip_trivia()
            if self.peek() == "]":
                self.i += 1
                return items
            items.append(self.parse_value())
            self.skip_trivia()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() == "]":
                self.i += 1
                return items
            raise LegacyParseError(
                "PARSE-013", f"expected ',' or ']' after element, found {self.peek()!r}", self.i
            )


@dataclass
class ExtractionResult:
    """What came out of the legacy file."""

    records: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    source_path: str = ""

    @property
    def count(self) -> int:
        return len(self.records)


def _clean(value: Any) -> str:
    """Normalise a legacy value into a trimmed string.

    The legacy data uses "None", "N/A" and "" interchangeably for "nothing";
    they are preserved verbatim rather than normalised, because HR reads
    "None" (no direct reports) differently from blank (not yet captured).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        # 9.0 -> "9" so band labels don't gain a spurious decimal.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


def extract_roles(html_path: str | Path) -> ExtractionResult:
    """Parse `const ROLES = [...]` out of the legacy HTML file.

    Raises LegacyParseError with a coded message if the file is missing, has no
    ROLES array, or the array is malformed. Records missing the two mandatory
    fields (bu, pos) are skipped and reported rather than aborting the run —
    one bad row must not stop 289 good ones from loading.
    """
    path = Path(html_path)
    if not path.is_file():
        raise LegacyParseError("SEED-001", f"legacy HTML file not found at {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Some exports from Word-adjacent tooling land as cp1252.
        logger.warning("legacy html was not utf-8, retrying as cp1252", extra={"path": str(path)})
        text = path.read_text(encoding="cp1252")

    match = ARRAY_DECL_RE.search(text)
    if not match:
        raise LegacyParseError(
            "SEED-002",
            f"no `const ROLES = [` declaration found in {path.name}; "
            f"is this the right file?",
        )

    scanner = _Scanner(text, match.end() - 1)  # step back onto the '['
    raw_items = scanner.parse_array()

    result = ExtractionResult(source_path=str(path))
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            result.skipped.append((index, f"element is {type(item).__name__}, not an object"))
            continue
        record: dict[str, Any] = {}
        for legacy_key, model_key in LEGACY_FIELD_MAP.items():
            record[model_key] = _clean(item.get(legacy_key))
        # bandN is numeric, not text.
        raw_band_n = item.get("bandN")
        record["band_numeric"] = raw_band_n if isinstance(raw_band_n, (int, float)) else None

        if not record["business_unit_name"]:
            result.skipped.append((index, "missing 'bu'"))
            continue
        if not record["position"]:
            result.skipped.append((index, "missing 'pos'"))
            continue

        unknown = set(item) - set(LEGACY_FIELD_MAP)
        if unknown:
            # Not fatal — the legacy file may gain fields before the model does.
            logger.info(
                "legacy record has fields this model does not store",
                extra={"index": index, "unknown_fields": sorted(unknown)},
            )
        result.records.append(record)

    logger.info(
        "extracted roles from legacy html",
        extra={
            "path": str(path),
            "parsed": result.count,
            "skipped": len(result.skipped),
        },
    )
    return result


def iter_business_units(result: ExtractionResult) -> Iterator[str]:
    """BU names in first-appearance order, which is the order the old tab strip used."""
    seen: set[str] = set()
    for record in result.records:
        name = record["business_unit_name"]
        if name not in seen:
            seen.add(name)
            yield name
