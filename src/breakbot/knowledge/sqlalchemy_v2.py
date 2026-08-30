"""Rules for the SQLAlchemy 1.4 → 2.0 upgrade."""
from __future__ import annotations

import ast

from ..models import Confidence, Severity
from .base import Finding, Rule, Upgrade

SEC_EXEC = "connectionless-execution"
SEC_SELECT = "select-constructor"
SEC_QUERY = "orm-query-unified"
SEC_GET = "query-get-moved"
SEC_DECL = "declarative-first-class"
SEC_AUTOCOMMIT = "session-autocommit"

_RULES: dict[str, Rule] = {}


def _rule(rid, title, section, quote, scan_fn, applies="sqlalchemy", fixable=False):
    r = Rule(id=rid, title=title, applies_to=applies, scan=scan_fn,
             section=section, quote=quote, auto_fixable=fixable)
    _RULES[rid] = r
    return r


def _mk(rule, mod, node, severity, title, detail, symbol, *, fixable=False,
        fix_summary="", confidence=Confidence.HIGH, enclosing="",
        section=None, quote=""):
    return Finding(
        severity=severity, module_path=mod.path, node=node, title=title,
        detail=detail, symbol=symbol, auto_fixable=fixable,
        fix_summary=fix_summary, confidence=confidence,
        section=section or rule.section, quote=quote or rule.quote,
        rule_id=rule.id, enclosing=enclosing,
    )


def _ctx(rec):
    parts = [p for p in (rec.enclosing_class, rec.enclosing_func) if p]
    return ".".join(parts)


def _name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def scan_engine_execute(mod):
    """engine.execute(...) — removed in 2.0."""
    rule = _RULES["sqla-engine-execute"]
    if not mod.sqlalchemy_imported:
        return []
    roles = mod.sqla_names()
    out = []
    for call in mod.calls:
        if call.kind != "attr" or call.name != "execute":
            continue
        recv = call.receiver
        role = None
        if isinstance(recv, ast.Name):
            role = roles.get(recv.id)
        elif isinstance(recv, ast.With):
            pass
        if role != "engine":
            continue
        # autofix only when the execute is a bare expression statement
        parent_is_expr = _is_expr_statement(call.node, mod.tree)
        is_text = call.node.args and isinstance(call.node.args[0], ast.Constant) \
            and isinstance(call.node.args[0].value, str)
        fixable = parent_is_expr and is_text
        out.append(_mk(rule, mod, call.node, Severity.BREAK,
                       "Engine.execute() removed",
                       "Engine.execute() is removed in 2.0. Execute statements on a "
                       "Connection: `with engine.begin() as conn: conn.execute(text(...))` "
                       "for writes, or `with engine.connect() as conn:` for reads.",
                       "engine.execute(...)", fixable=fixable,
                       fix_summary="wrap in with engine.begin()/connect() and text()" if fixable else "",
                       enclosing=_ctx(call), quote="Engine.execute()"))
    return out


def scan_metadata_bind(mod):
    rule = _RULES["sqla-metadata-bind"]
    if not mod.sqlalchemy_imported:
        return []
    out = []
    for call in mod.calls:
        if call.kind != "name" or call.name != "MetaData":
            continue
        for kw in call.node.keywords:
            if kw.arg == "bind":
                out.append(_mk(rule, mod, kw, Severity.BREAK,
                               "MetaData(bind=...) removed",
                               "Bound metadata is removed in 2.0; MetaData() takes no "
                               "bind argument. Pass the engine explicitly to "
                               "create_all/drop_all or to the Connection instead.",
                               "MetaData(bind=…)", fixable=True,
                               fix_summary="drop the bind= keyword argument",
                               enclosing=_ctx(call), quote="MetaData(bind=engine)"))
    return out


def scan_raw_string_execute(mod):
    rule = _RULES["sqla-raw-string-execute"]
    if not mod.sqlalchemy_imported:
        return []
    roles = mod.sqla_names()
    out = []
    for call in mod.calls:
        if call.kind != "attr" or call.name != "execute":
            continue
        if not call.node.args:
            continue
        first = call.node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        recv = call.receiver
        role = roles.get(recv.id) if isinstance(recv, ast.Name) else "connection?"
        # avoid double-flagging engine.execute (its own rule handles it)
        if role == "engine":
            continue
        out.append(_mk(rule, mod, first, Severity.BREAK,
                       "Raw SQL string passed to execute()",
                       "Passing a string to Connection.execute()/Session.execute() is "
                       "removed in 2.0 (ObjectNotExecutableError). Wrap the SQL in "
                       "text(...) or use conn.exec_driver_sql(...).",
                       'execute("SELECT ...")', fixable=True,
                       fix_summary="wrap the string in text(...) (import text from sqlalchemy)",
                       enclosing=_ctx(call), quote="text()"))
    return out


def scan_legacy_select(mod):
    rule = _RULES["sqla-legacy-select"]
    if not mod.sqlalchemy_imported:
        return []
    imp = mod.imported_from("select")
    if imp and not imp[0].startswith("sqlalchemy"):
        return []
    out = []
    for call in mod.calls:
        if call.kind != "name" or call.name != "select":
            continue
        if imp is None and not mod.sqlalchemy_imported:
            continue
        for arg in call.node.args:
            if isinstance(arg, ast.List):
                out.append(_mk(rule, mod, arg, Severity.BREAK,
                               "select([cols]) legacy calling style removed",
                               "select() no longer accepts a list of columns; pass "
                               "columns positionally: select(col1, col2).",
                               "select([...])", fixable=True,
                               fix_summary="unwrap the list: select(*[...]) → select(...)",
                               enclosing=_ctx(call), quote="select()"))
                break
    return out


def scan_query_get(mod):
    rule = _RULES["sqla-query-get"]
    if not mod.sqlalchemy_imported:
        return []
    roles = mod.sqla_names()
    out = []
    for call in mod.calls:
        # pattern: X.query(...).get(pk)  → outer call is .get(...)
        if call.kind != "attr" or call.name != "get":
            continue
        recv = call.receiver
        if not (isinstance(recv, ast.Call) and isinstance(recv.func, ast.Attribute)
                and recv.func.attr == "query"):
            continue
        # receiver of .query must be a session-ish name
        qrecv = recv.func.value
        role = roles.get(qrecv.id) if isinstance(qrecv, ast.Name) else None
        if role not in {"session", "session_factory"} and not (
            isinstance(qrecv, ast.Name) and "session" in qrecv.id.lower()):
            continue
        entity = ast.unparse(recv.args[0]) if recv.args else "Model"
        pk = ast.unparse(call.node.args[0]) if call.node.args else "pk"
        out.append(_mk(rule, mod, call.node, Severity.WARNING,
                       "Query.get() legacy → Session.get()",
                       "Query.get() is legacy in 2.0 (LegacyAPIWarning). The "
                       "primary-key lookup is now Session.get(Model, pk), which "
                       "returns the instance or None.",
                       "session.query(Model).get(pk)", fixable=True,
                       fix_summary=f"rewrite to session.get({entity}, {pk})",
                       enclosing=_ctx(call), quote="Session.get()"))
    return out


def scan_session_autocommit(mod):
    rule = _RULES["sqla-session-autocommit"]
    if not mod.sqlalchemy_imported:
        return []
    out = []
    for call in mod.calls:
        nm = call.name
        if nm not in {"sessionmaker", "Session"}:
            continue
        imp = mod.imported_from(nm)
        if imp and not imp[0].startswith("sqlalchemy"):
            continue
        for kw in call.node.keywords:
            if kw.arg == "autocommit":
                out.append(_mk(rule, mod, kw, Severity.BREAK,
                               "autocommit=True no longer supported",
                               "Library-level autocommit is removed in 2.0 "
                               "(ArgumentError at runtime). Sessions autobegin and "
                               "require an explicit commit(); drop the autocommit "
                               "argument and call session.commit() where needed.",
                               f"{nm}(autocommit=…)", fixable=True,
                               fix_summary="drop autocommit= keyword; commit explicitly",
                               enclosing=_ctx(call), quote="autocommit"))
    return out


def scan_declarative_import(mod):
    rule = _RULES["sqla-declarative-import"]
    out = []
    for rec in mod.imports:
        if rec.module == "sqlalchemy.ext.declarative" and rec.name == "declarative_base":
            out.append(Finding(
                severity=Severity.WARNING, module_path=mod.path, node=rec,
                title="declarative_base moved to sqlalchemy.orm",
                detail=("sqlalchemy.ext.declarative.declarative_base is legacy in 2.0 "
                        "(MovedIn20Warning). Import it from sqlalchemy.orm, or adopt the "
                        "new DeclarativeBase base class."),
                symbol="from sqlalchemy.ext.declarative import declarative_base",
                auto_fixable=True,
                fix_summary="import declarative_base from sqlalchemy.orm",
                section=SEC_DECL, quote="declarative_base",
                rule_id=rule.id, enclosing=""))
    return out


def scan_legacy_query(mod):
    """Remaining session.query(...) usage — guidance level."""
    rule = _RULES["sqla-legacy-query"]
    if not mod.sqlalchemy_imported:
        return []
    roles = mod.sqla_names()
    out = []
    for call in mod.calls:
        if call.kind != "attr" or call.name != "query":
            continue
        recv = call.receiver
        role = roles.get(recv.id) if isinstance(recv, ast.Name) else None
        if role not in {"session", "session_factory"} and not (
            isinstance(recv, ast.Name) and "session" in recv.id.lower()):
            continue
        # .query(...).get(...) handled separately
        parent = _outer_call_is_get(call.node, mod.tree)
        if parent:
            continue
        entity = ast.unparse(call.node.args[0]) if call.node.args else "Model"
        out.append(_mk(rule, mod, call.node, Severity.WARNING,
                       "Legacy Query API → 2.0 select() style",
                       "Session.query() remains as a legacy shim in 2.0. The 2.0 style "
                       f"is session.execute(select({entity})).scalars() (use "
                       ".scalar()/.first() as needed); rows are Row objects and are not "
                       "uniquified by default.",
                       "session.query(Model)", fixable=False,
                       enclosing=_ctx(call), quote="Session.query"))
    return out


# -- AST parent helpers (lightweight; full parent map is overkill) ---------- #

def _all_parent_pairs(tree):
    pairs = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            pairs[id(child)] = parent
    return pairs


def _is_expr_statement(node, tree):
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Expr) and parent.value is node:
            return True
    return False


def _outer_call_is_get(node, tree):
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) \
                and parent.func.attr == "get" and parent.func.value is node:
            return True
    return False


_rule("sqla-engine-execute", "Engine.execute() removed", SEC_EXEC,
      "Engine.execute()", scan_engine_execute)
_rule("sqla-metadata-bind", "Bound metadata removed", SEC_EXEC,
      "MetaData(bind=engine)", scan_metadata_bind)
_rule("sqla-raw-string-execute", "Raw string execution removed", SEC_EXEC,
      "text()", scan_raw_string_execute)
_rule("sqla-legacy-select", "Legacy select() calling style", SEC_SELECT,
      "select()", scan_legacy_select)
_rule("sqla-query-get", "Query.get() moves to Session", SEC_GET,
      "Session.get()", scan_query_get)
_rule("sqla-session-autocommit", "Session autocommit removed", SEC_AUTOCOMMIT,
      "autocommit", scan_session_autocommit)
_rule("sqla-declarative-import", "declarative_base import moved", SEC_DECL,
      "declarative_base", scan_declarative_import)
_rule("sqla-legacy-query", "Legacy Session.query() API", SEC_QUERY,
      "Session.query", scan_legacy_query)

UPGRADE = Upgrade(
    id="sqlalchemy1.4-to-2.0",
    title="SQLAlchemy 1.4 → 2.0",
    source_id="sqlalchemy-1.4-to-2.0",
    package_from="sqlalchemy<2",
    package_to="sqlalchemy>=2",
    rules=list(_RULES.values()),
    summary=("SQLAlchemy 2.0 removes connectionless execution (Engine.execute, "
             "bound metadata, raw string SQL), unifies ORM Query with Core select(), "
             "moves Query.get() to Session.get(), removes Session autocommit, and "
             "makes Declarative a first-class API."),
)
