"""Analysis orchestration: ingest repo → run rules → cited verdicts."""
from __future__ import annotations

import ast
from pathlib import Path

from .changelog import load_doc
from .ingest import CallRec, PyModule, ingest_repo
from .knowledge import Finding, Upgrade, get_upgrade
from .models import (
    CallSite,
    Confidence,
    FileReport,
    Report,
    Severity,
    Verdict,
)

# Methods/attributes that count as "touches" of the upgraded library when no
# rule fires — used for the honest "N other usages unaffected" count.
PYDANTIC_TOUCH_NAMES = {
    "BaseModel", "Field", "validator", "root_validator", "field_validator",
    "model_validator", "validate_arguments", "validate_call", "constr",
    "conlist", "conint", "confloat", "ConfigDict", "BaseSettings",
    "GenericModel", "TypeAdapter",
} | {
    "dict", "json", "parse_obj", "parse_raw", "parse_file", "from_orm",
    "construct", "copy", "schema", "schema_json", "update_forward_refs",
    "model_dump", "model_validate", "model_construct", "model_copy",
    "model_rebuild", "model_json_schema", "model_dump_json",
    "model_validate_json", "model_fields", "__fields__",
}
SQLA_TOUCH_NAMES = {
    "create_engine", "sessionmaker", "MetaData", "Table", "Column",
    "declarative_base", "DeclarativeBase", "select", "text", "insert",
    "update", "delete", "Session", "relationship", "foreign", "ForeignKey",
    "Integer", "String", "execute", "query", "get", "commit", "rollback",
    "connect", "begin", "scalar", "scalars", "all", "first", "filter",
    "filter_by", "join", "joinedload", "Session",
}


def analyze(root: str | Path, upgrade_id: str) -> Report:
    upgrade = get_upgrade(upgrade_id)
    doc = load_doc(upgrade.source_id)
    modules = ingest_repo(root)

    report = Report(
        upgrade_id=upgrade.id, upgrade_title=upgrade.title,
        root=str(Path(root).resolve()), files_scanned=len(modules),
    )

    flagged_nodes: set[tuple[str, int]] = set()
    for mod in modules:
        if not upgrade.applies(mod):
            continue
        findings: list[Finding] = []
        for rule in upgrade.rules:
            try:
                findings.extend(rule.scan(mod))
            except Exception as exc:  # a rule bug must never kill a scan
                report.notes.append(f"rule {rule.id} failed on {mod.path}: {exc}")
        if not findings:
            continue
        fr = FileReport(path=mod.path)
        for f in findings:
            v = _verdict_from_finding(mod, f, doc, upgrade)
            fr.verdicts.append(v)
            if v.call_site is not None:
                flagged_nodes.add((mod.path, v.call_site.line))
        report.file_reports.append(fr)

    # Unaffected usages: library touches in scope files that no rule flagged.
    report.unaffected_count = _count_unaffected(modules, upgrade, flagged_nodes)
    return report


def _verdict_from_finding(mod: PyModule, f: Finding, doc, upgrade: Upgrade) -> Verdict:
    cs = None
    node = f.node
    # Findings may carry ast nodes, ImportRecs, ClassRecs — all expose lineno.
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", lineno)
    col = getattr(node, "col_offset", 0)
    end_col = getattr(node, "end_col_offset", col + 1)
    if lineno is not None:
        snippet = mod.snippet(node) if isinstance(node, ast.AST) else mod.line_text(lineno).strip()
        ctx = f.enclosing or _context_of(mod, node if isinstance(node, ast.AST) else None)
        cs = CallSite(
            file=mod.path, line=lineno, col=col, end_line=end_lineno or lineno,
            end_col=end_col or col + 1, snippet=snippet, symbol=f.symbol,
            context=ctx,
        )
    citation = doc.cite(f.section, f.quote)
    return Verdict(
        severity=f.severity, rule_id=f.rule_id, title=f.title, detail=f.detail,
        call_site=cs, citations=[citation], confidence=f.confidence,
        auto_fixable=f.auto_fixable, fix_summary=f.fix_summary,
        fixed_snippet=f.fixed_preview, file=mod.path,
    )


def _context_of(mod: PyModule, node: ast.AST | None) -> str:
    if node is None:
        return ""
    lineno = getattr(node, "lineno", 0)
    best = ""
    for cls in mod.classes:
        if cls.node.lineno <= lineno <= (cls.node.end_lineno or cls.node.lineno):
            best = cls.name
    return best


def _count_unaffected(modules, upgrade: Upgrade, flagged: set[tuple[str, int]]) -> int:
    touch = PYDANTIC_TOUCH_NAMES if upgrade.id.startswith("pydantic") else SQLA_TOUCH_NAMES
    total = 0
    for mod in modules:
        if not upgrade.applies(mod):
            continue
        lines_seen: set[int] = set()
        for call in mod.calls:
            is_model_ctor = call.kind == "name" and call.name in (
                mod.model_classes | mod.sqla_model_classes
            )
            if (call.name in touch or is_model_ctor) and (
                mod.path, call.node.lineno
            ) not in flagged:
                lines_seen.add(call.node.lineno)
        for attr in mod.attrs:
            if attr.name in touch and (mod.path, attr.node.lineno) not in flagged:
                lines_seen.add(attr.node.lineno)
        for dec in mod.decors:
            if dec.name in touch and (mod.path, dec.node.lineno) not in flagged:
                lines_seen.add(dec.node.lineno)
        for cls in mod.classes:
            if (mod.path, cls.node.lineno) in flagged:
                continue
            base_tail = {b.split(".")[-1].split("[")[0] for b in cls.base_strings}
            if upgrade.id.startswith("pydantic"):
                if base_tail & {"BaseModel", "GenericModel"}:
                    lines_seen.add(cls.node.lineno)
            elif base_tail & {"DeclarativeBase"} or cls.is_sqla_model:
                lines_seen.add(cls.node.lineno)
        total += len(lines_seen)
    return total
