"""Core dataclasses shared by the analyzer, rules, codemods and reports."""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional


class Severity(str, enum.Enum):
    BREAK = "break"        # ✗ raises / won't import / silently stops working
    WARNING = "warning"    # ⚠ deprecated or behavior changed silently
    OK = "ok"              # ✓ unaffected


class Confidence(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SEVERITY_GLYPH = {Severity.BREAK: "✗", Severity.WARNING: "⚠", Severity.OK: "✓"}
SEVERITY_LABEL = {
    Severity.BREAK: "BREAKS",
    Severity.WARNING: "WARNING",
    Severity.OK: "UNAFFECTED",
}


@dataclasses.dataclass
class Citation:
    """A pointer to a changelog line a verdict is based on."""

    source: str          # logical source id, e.g. "pydantic-1-to-2"
    section: str         # anchor, e.g. "basemodel-methods"
    line: int            # 1-based line number inside the bundled changelog
    quote: str           # the actual line text (credibility feature)
    url: str             # canonical doc URL

    def as_text(self) -> str:
        return f"{self.source}.md:{self.line} ({self.section})"


@dataclasses.dataclass
class CallSite:
    """One place in user code that touches a library API."""

    file: str
    line: int
    col: int
    end_line: int
    end_col: int
    snippet: str
    symbol: str                 # e.g. ".dict()", "Field(regex=)", "@validator"
    context: str = ""           # enclosing class/function for context


@dataclasses.dataclass
class Verdict:
    severity: Severity
    rule_id: str
    title: str                  # short human headline, e.g. ".dict() removed"
    detail: str                 # why it breaks / what changed
    call_site: Optional[CallSite] = None
    citations: list[Citation] = dataclasses.field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    auto_fixable: bool = False
    fix_summary: str = ""       # what the codemod will do
    fixed_snippet: str = ""     # resulting code after the fix (preview)
    file: str = ""

    @property
    def glyph(self) -> str:
        return SEVERITY_GLYPH[self.severity]

    @property
    def short_location(self) -> str:
        if self.call_site is not None:
            return f"{self.call_site.file}:{self.call_site.line}"
        return self.file


@dataclasses.dataclass
class FileReport:
    path: str
    verdicts: list[Verdict] = dataclasses.field(default_factory=list)

    @property
    def breaks(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.severity == Severity.BREAK]

    @property
    def warnings(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.severity == Severity.WARNING]


@dataclasses.dataclass
class Report:
    upgrade_id: str
    upgrade_title: str
    root: str
    file_reports: list[FileReport] = dataclasses.field(default_factory=list)
    unaffected_count: int = 0
    files_scanned: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def all_verdicts(self) -> list[Verdict]:
        return [v for fr in self.file_reports for v in fr.verdicts]

    @property
    def breaks(self) -> list[Verdict]:
        return [v for v in self.all_verdicts if v.severity == Severity.BREAK]

    @property
    def warnings(self) -> list[Verdict]:
        return [v for v in self.all_verdicts if v.severity == Severity.WARNING]

    @property
    def fixable(self) -> list[Verdict]:
        return [v for v in self.all_verdicts if v.auto_fixable]


@dataclasses.dataclass
class PatchResult:
    diff: str                          # unified diff across all files
    changed_files: list[str]
    skipped: list[str]                 # human-readable reasons for manual fixes
    per_file_diffs: dict[str, str] = dataclasses.field(default_factory=dict)
    backed_up_to: str = ""


@dataclasses.dataclass
class TestResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def green(self) -> bool:
        return self.returncode == 0
