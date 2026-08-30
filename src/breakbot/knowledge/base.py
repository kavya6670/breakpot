"""Rule framework: each upgrade ships a list of Rules.

A rule scans the ingested modules and yields Findings.  Findings are turned
into Verdicts (with changelog citations) by the engine; rules that have a
mechanical repair also register a codemod key that the patcher executes.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Optional

from ..ingest import PyModule
from ..models import Confidence, Severity


@dataclasses.dataclass
class Finding:
    severity: Severity
    module_path: str
    node: object                      # ast node the finding anchors on
    title: str
    detail: str
    symbol: str                       # display form, e.g. ".dict()"
    auto_fixable: bool = False
    fix_summary: str = ""
    fixed_preview: str = ""
    confidence: Confidence = Confidence.HIGH
    section: str = ""                 # changelog anchor
    quote: str = ""                   # changelog line needle to cite
    rule_id: str = ""
    enclosing: str = ""


@dataclasses.dataclass
class Rule:
    id: str
    title: str
    applies_to: str                   # "pydantic" | "sqlalchemy" | "any"
    scan: Callable[[PyModule], list[Finding]]
    section: str = ""
    quote: str = ""
    auto_fixable: bool = False


@dataclasses.dataclass
class Upgrade:
    id: str
    title: str
    source_id: str                    # changelog doc id
    package_from: str
    package_to: str
    rules: list[Rule]
    summary: str = ""

    def applies(self, mod: PyModule) -> bool:
        if any(r.applies_to == "any" for r in self.rules):
            return True
        return mod.pydantic_imported if self.id.startswith("pydantic") else mod.sqlalchemy_imported
