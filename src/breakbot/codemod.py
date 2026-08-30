"""Patch generation: turn fixable findings into a unified diff and apply it.

All edits are computed against the *original* source as (start, end, text)
spans, then spliced in reverse order so offsets stay valid.  Per file, the
result is syntax-checked before writing; if a codemod would produce invalid
Python, the file is left untouched and a manual note is emitted instead.
"""
from __future__ import annotations

import ast
import dataclasses
import difflib
import shutil
from pathlib import Path

from .ingest import PyModule
from .knowledge import get_upgrade
from .models import PatchResult


@dataclasses.dataclass
class Edit:
    start: int
    end: int
    text: str
    note: str = ""

    @property
    def is_insert(self) -> bool:
        return self.start == self.end


@dataclasses.dataclass
class ImportPlan:
    """Deferred, conflict-free import-line rewrite plan.

    All transforms record intent here; the plan is applied as one post-pass
    over the (already body-patched) source, so import edits can never overlap
    body edits or each other.
    """
    ensure_names: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    delete_modules: set[str] = dataclasses.field(default_factory=set)
    # (from_module, name) -> (to_module, new_name, asname)
    moves: dict[tuple[str, str], tuple[str, str, str | None]] = dataclasses.field(
        default_factory=dict)

    def ensure(self, module: str, name: str) -> None:
        self.ensure_names.add((module, name))

    def delete_module(self, module: str) -> None:
        self.delete_modules.add(module)

    def rename_name(self, module: str, old: str, new: str) -> None:
        self.moves[(module, old)] = (module, new, None)

    def move_name(self, module: str, name: str, to_module: str,
                  new_name: str, asname: str | None = None) -> None:
        self.moves[(module, name)] = (to_module, new_name, asname)


def _line_start_offsets(src: str) -> list[int]:
    offs = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _off(lines_offsets: list[int], lineno: int, col: int = 0) -> int:
    return lines_offsets[lineno - 1] + col


def _node_span(src: str, node: ast.AST) -> tuple[int, int]:
    offs = _line_start_offsets(src)
    start = _off(offs, node.lineno, node.col_offset)
    end = _off(offs, node.end_lineno, node.end_col_offset)
    return start, end


def _line_start_off(src: str, lineno: int) -> int:
    return _line_start_offsets(src)[lineno - 1]


def _line_end_off(src: str, lineno: int) -> int:
    offs = _line_start_offsets(src)
    if lineno < len(offs):
        return offs[lineno] - 1  # before the \n
    return len(src)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _apply_edits(src: str, edits: list[Edit]) -> str:
    # guard against overlaps: merge/skip later edits that overlap earlier ones
    edits = sorted(edits, key=lambda e: (e.start, -e.end))
    pieces: list[str] = []
    cursor = 0
    chosen: list[Edit] = []
    for e in edits:
        if e.start < cursor:
            continue  # overlap with an already-accepted wider edit
        chosen.append(e)
        cursor = e.end
    out = []
    pos = 0
    for e in sorted(chosen, key=lambda e: e.start):
        out.append(src[pos:e.start])
        out.append(e.text)
        pos = e.end
    out.append(src[pos:])
    return "".join(out)


def _find_functiondef(mod: PyModule, lineno: int) -> ast.FunctionDef | None:
    for node in ast.walk(mod.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == lineno:
            return node
    return None


def _import_from_node(mod: PyModule, lineno: int) -> ast.ImportFrom | None:
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.ImportFrom) and node.lineno == lineno:
            return node
    return None


def _attr_name_span(src: str, attr: ast.Attribute) -> tuple[int, int]:
    offs = _line_start_offsets(src)
    end = _off(offs, attr.end_lineno, attr.end_col_offset)
    start = end - len(attr.attr)
    return start, end


def _kw_name_span(src: str, kw: ast.keyword) -> tuple[int, int]:
    offs = _line_start_offsets(src)
    start = _off(offs, kw.lineno, kw.col_offset)
    end = start + len(kw.arg or "")
    return start, end


def _extend_remove_keyword(src: str, start: int, end: int) -> tuple[int, int]:
    """Swallow an adjacent comma/whitespace so removing a kwarg stays valid."""
    j = end
    while j < len(src) and src[j] in " \t":
        j += 1
    if j < len(src) and src[j] == ",":
        return start, j + 1
    # otherwise swallow a preceding comma
    i = start
    while i > 0 and src[i - 1] in " \t":
        i -= 1
    if i > 0 and src[i - 1] == ",":
        return i - 1, end
    return start, end


def _ensure_import(mod: PyModule, edits: list[Edit], module: str, name: str,
                   wanted: set[tuple[str, str]] | None = None) -> None:
    """Append `from <module> import <name>` if not already imported/bound.

    New imports are collected in *wanted* and flushed once by the caller, so
    repeated calls never produce duplicate import lines.
    """
    for rec in mod.imports:
        if rec.module == module and (rec.name == name or rec.name == "*"):
            return
        if rec.kind == "import" and rec.module.split(".")[0] == name and module == name:
            return
    if wanted is not None:
        if isinstance(wanted, dict):
            wanted.setdefault("_imports", set()).add((module, name))
        else:
            wanted.add((module, name))
        return
    _flush_imports(mod, edits, {(module, name)})


def _flush_imports(mod: PyModule, edits: list[Edit],
                   wanted: set[tuple[str, str]]) -> None:
    if not wanted:
        return
    src, tree = mod.source, mod.tree
    last = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node
    block = "".join(f"from {m} import {n}\n" for m, n in sorted(wanted))
    if last is not None:
        off = _line_end_off(src, last.end_lineno or last.lineno) + 1
        edits.append(Edit(off, off, block))
    else:
        edits.append(Edit(0, 0, block))


def _rename_imported_name(mod: PyModule, edits: list[Edit], module: str,
                          old: str, new: str) -> None:
    """Rename an imported alias in a `from <module> import ... old ...` line."""
    src = mod.source
    for node in ast.walk(mod.tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        for alias in node.names:
            if alias.name == old and not alias.asname:
                offs = _line_start_offsets(src)
                start = _off(offs, alias.lineno, alias.col_offset)
                end = start + len(old)
                edits.append(Edit(start, end, new, note=f"import {old} → {new}"))


# --------------------------------------------------------------------------- #
# Per-rule transforms
# --------------------------------------------------------------------------- #

def _edit_model_methods(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "py-model-methods":
            continue
        call = f.node
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        attr = call.func
        if attr.attr == "parse_file":
            recv = ast.unparse(attr.value)
            arg = ast.unparse(call.args[0]) if call.args else "''"
            repl = f"{recv}.model_validate_json(Path({arg}).read_text())"
            s, e = _node_span(src, call)
            edits.append(Edit(s, e, repl))
            _ensure_import(mod, edits, "pathlib", "Path", wanted)
        elif attr.attr == "schema_json":
            continue  # not auto-fixable
        else:
            from .knowledge.pydantic_v2 import METHOD_RENAMES
            new_name = METHOD_RENAMES[attr.attr][0]
            s, e = _attr_name_span(src, attr)
            edits.append(Edit(s, e, new_name))


def _edit_dunder_fields(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "py-fields-attr":
            continue
        attr = f.node
        if isinstance(attr, ast.Attribute):
            s, e = _attr_name_span(src, attr)
            edits.append(Edit(s, e, "model_fields"))


def _edit_field_kwargs(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    from .knowledge.pydantic_v2 import FIELD_KWARG_RENAMES
    for f in findings:
        if f.rule_id != "py-field-kwargs":
            continue
        kw = f.node
        if not isinstance(kw, ast.keyword):
            continue
        if kw.arg in FIELD_KWARG_RENAMES:
            s, e = _kw_name_span(src, kw)
            edits.append(Edit(s, e, FIELD_KWARG_RENAMES[kw.arg]))
        elif kw.arg == "allow_mutation" and ast.unparse(kw.value).strip() == "False":
            s, e = _node_span(src, kw)
            edits.append(Edit(s, e, "frozen=True"))


def _edit_config_class(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    from .knowledge.pydantic_v2 import CONFIG_RENAMES, CONFIG_REMOVED
    for f in findings:
        if f.rule_id != "py-config-class" or not isinstance(f.node, ast.ClassDef):
            continue
        cls = f.node
        if cls.name != "Config":
            continue
        pairs: list[str] = []
        ok = True
        for stmt in cls.body:
            if not isinstance(stmt, ast.Assign) or not all(isinstance(t, ast.Name) for t in stmt.targets):
                ok = False
                break
            for tgt in stmt.targets:
                if tgt.id in CONFIG_REMOVED:
                    continue  # separate BREAK finding tells the user
                new_key = CONFIG_RENAMES.get(tgt.id, tgt.id)
                pairs.append(f"{new_key}={ast.unparse(stmt.value)}")
        if not ok:
            continue
        indent = _indent_of(src.splitlines()[cls.lineno - 1])
        replacement = f"{indent}model_config = ConfigDict({', '.join(pairs)})\n"
        s = _line_start_off(src, cls.lineno)
        e = _line_end_off(src, cls.end_lineno or cls.lineno) + 1
        edits.append(Edit(s, e, replacement))
        _ensure_import(mod, edits, "pydantic", "ConfigDict", wanted)


def _edit_validators(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "py-validator-decorator":
            continue
        dec = f.node
        # name span of decorator
        if isinstance(dec, ast.Call) and isinstance(dec.func, (ast.Name, ast.Attribute)):
            name_node = dec.func
            old = name_node.id if isinstance(name_node, ast.Name) else name_node.attr
        elif isinstance(dec, ast.Name):
            name_node = dec
            old = dec.id
        else:
            continue
        if old == "root_validator":
            continue  # manual
        new = "field_validator" if old == "validator" else (
            "validate_call" if old == "validate_arguments" else None)
        if new is None:
            continue
        offs = _line_start_offsets(src)
        ns = _off(offs, name_node.lineno, name_node.col_offset)
        edits.append(Edit(ns, ns + len(old), new))
        if new == "field_validator" and isinstance(dec, ast.Call):
            # @validator(..., pre=True) → @field_validator(..., mode="before")
            for kw in dec.keywords:
                if kw.arg == "pre" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    kws, kwe = _node_span(src, kw)
                    edits.append(Edit(kws, kwe, 'mode="before"'))
        if new == "field_validator":
            if wanted is not None:
                plan = wanted.get("__import_plan__") or ImportPlan()
                wanted["__import_plan__"] = plan
                plan.rename_name("pydantic", "validator", "field_validator")
            else:
                has_fv = any(rec.module == "pydantic" and rec.name == "field_validator"
                             for rec in mod.imports)
                _replace_import_names(
                    mod, edits, "pydantic",
                    {"validator": "field_validator"},
                    drop=set() if not has_fv else {"validator"})
            func = _find_functiondef(mod, _decor_func_lineno(dec, mod))
            already = any(
                (isinstance(d, ast.Name) and d.id == "classmethod")
                for d in (func.decorator_list if func else [])
            )
            if not already and func is not None:
                # @classmethod must be placed *below* @field_validator, i.e.
                # immediately above the def line:
                #   @field_validator("x")
                #   @classmethod
                #   def v(cls, v): ...
                lo = _line_start_off(src, func.lineno)
                indent = _indent_of(src.splitlines()[func.lineno - 1])
                edits.append(Edit(lo, lo, f"{indent}@classmethod\n"))
        elif new == "validate_call":
            if wanted is not None:
                plan = wanted.get("__import_plan__") or ImportPlan()
                wanted["__import_plan__"] = plan
                plan.rename_name("pydantic", "validate_arguments", "validate_call")
            else:
                has_vc = any(rec.module == "pydantic" and rec.name == "validate_call"
                             for rec in mod.imports)
                _replace_import_names(
                    mod, edits, "pydantic",
                    {"validate_arguments": "validate_call"},
                    drop=set() if not has_vc else {"validate_arguments"})


def _decor_func_lineno(dec_node: ast.AST, mod: PyModule) -> int:
    for d in mod.decors:
        if d.node is dec_node:
            return d.func_lineno
    return getattr(dec_node, "lineno", 1)


def _prune_dead_imports(mod: PyModule, edits: list[Edit],
                        module: str, dead_names: set[str]) -> None:
    """Remove *dead_names* from `from <module> import a, b, c` lines."""
    src = mod.source
    for node in ast.walk(mod.tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        kept = [a for a in node.names if a.name not in dead_names]
        dropped = [a for a in node.names if a.name in dead_names]
        if not dropped:
            continue
        s = _line_start_off(src, node.lineno)
        e = _line_end_off(src, node.end_lineno or node.lineno) + 1
        if not kept:
            edits.append(Edit(s, e, ""))
        else:
            names = ", ".join(
                f"{a.name} as {a.asname}" if a.asname else a.name for a in kept
            )
            edits.append(Edit(s, e, f"from {module} import {names}\n"))


def _line_range_edit(mod: PyModule, lineno: int, end_lineno: int,
                     replacement: str, note: str = "") -> Edit:
    src = mod.source
    s = _line_start_off(src, lineno)
    e = _line_end_off(src, end_lineno) + 1
    return Edit(s, e, replacement, note)


def _replace_import_names(mod: PyModule, edits: list[Edit], module: str,
                          mapping: dict[str, str],
                          drop: set[str] | None = None,
                          extra_lines: list[str] | None = None) -> None:
    """Rewrite a `from <module> import ...` statement.

    Names in *mapping* are renamed; names in *drop* are removed; extra
    import lines are appended after the statement.
    """
    drop = drop or set()
    for node in ast.walk(mod.tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        names_out = []
        for a in node.names:
            if a.name in drop:
                continue
            new = mapping.get(a.name, a.name)
            if a.asname:
                names_out.append(f"{new} as {a.asname}")
            else:
                names_out.append(new)
        block = ""
        if names_out:
            block = f"from {module} import {', '.join(names_out)}\n"
        for line in extra_lines or []:
            block += line + "\n"
        edits.append(_line_range_edit(mod, node.lineno,
                                      node.end_lineno or node.lineno, block))


def _edit_basesettings(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    plan = wanted.get("__import_plan__") if wanted is not None else None
    if wanted is not None and plan is None:
        plan = ImportPlan()
        wanted["__import_plan__"] = plan
    for f in findings:
        if f.rule_id != "py-basesettings-import":
            continue
        if plan is not None:
            plan.move_name("pydantic", "BaseSettings", "pydantic_settings", "BaseSettings")


def _edit_generic_model(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    plan = wanted.get("__import_plan__") if wanted is not None else None
    if wanted is not None and plan is None:
        plan = ImportPlan()
        wanted["__import_plan__"] = plan
    for f in findings:
        if f.rule_id != "py-generic-model":
            continue
        node = f.node
        is_gm_import = (
            (isinstance(node, ast.ImportFrom) and node.module == "pydantic.generics")
            or (getattr(node, "module", None) == "pydantic.generics"
                and getattr(node, "name", None) == "GenericModel")
        )
        if is_gm_import:
            if plan is not None:
                plan.delete_module("pydantic.generics")
                plan.ensure("typing", "Generic")
                plan.ensure("pydantic", "BaseModel")
        elif isinstance(node, ast.ClassDef):
            changed = False
            for base in node.bases:
                bname = ast.unparse(base)
                if bname.split(".")[-1].split("[")[0] == "GenericModel":
                    s, e = _node_span(src, base)
                    edits.append(Edit(s, e, "BaseModel"))
                    changed = True
            if changed:
                has_generic = any(
                    "Generic[" in ast.unparse(b) for b in node.bases
                )
                if not has_generic and node.bases:
                    # append Generic[T] — best effort
                    bs, be = _node_span(src, node.bases[-1])
                    edits.append(Edit(be, be, ", Generic[T]"))
                _ensure_import(mod, edits, "typing", "Generic", wanted)
                _ensure_import(mod, edits, "pydantic", "BaseModel", wanted)


def _edit_metadata_bind(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-metadata-bind":
            continue
        kw = f.node
        if isinstance(kw, ast.keyword):
            s, e = _node_span(src, kw)
            s2, e2 = _extend_remove_keyword(src, s, e)
            edits.append(Edit(s2, e2, ""))


def _edit_session_autocommit(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-session-autocommit":
            continue
        kw = f.node
        if isinstance(kw, ast.keyword):
            s, e = _node_span(src, kw)
            s2, e2 = _extend_remove_keyword(src, s, e)
            edits.append(Edit(s2, e2, ""))


def _edit_legacy_select(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-legacy-select":
            continue
        lst = f.node
        if isinstance(lst, ast.List):
            s, e = _node_span(src, lst)
            repl = ", ".join(ast.unparse(el) for el in lst.elts)
            edits.append(Edit(s, e, repl))


def _edit_raw_string_execute(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-raw-string-execute":
            continue
        const = f.node
        if isinstance(const, ast.Constant):
            s, e = _node_span(src, const)
            edits.append(Edit(s, e, f"text({ast.unparse(const)})"))
            _ensure_import(mod, edits, "sqlalchemy", "text", wanted)


def _edit_engine_execute(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-engine-execute":
            continue
        call = f.node
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        # only the statement-level, string-SQL form is auto-fixable
        stmt = next((p for p in ast.walk(mod.tree)
                     if isinstance(p, ast.Expr) and p.value is call), None)
        if stmt is None or not call.args:
            continue
        first = call.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        engine = ast.unparse(call.func.value)
        sql = ast.unparse(first)
        is_read = first.value.lstrip().lower().startswith("select")
        ctx = "connect" if is_read else "begin"
        line_no = stmt.lineno
        indent = _indent_of(src.splitlines()[line_no - 1])
        inner = indent + "    "
        block = (
            f"{indent}with {engine}.{ctx}() as conn:\n"
            f"{inner}conn.execute(text({sql}))\n"
        )
        s = _line_start_off(src, line_no)
        e = _line_end_off(src, stmt.end_lineno or line_no) + 1
        edits.append(Edit(s, e, block))
        _ensure_import(mod, edits, "sqlalchemy", "text", wanted)


def _edit_query_get(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    src = mod.source
    for f in findings:
        if f.rule_id != "sqla-query-get":
            continue
        outer = f.node  # session.query(Entity).get(pk)
        if not (isinstance(outer, ast.Call) and isinstance(outer.func, ast.Attribute)
                and isinstance(outer.func.value, ast.Call)):
            continue
        query_call = outer.func.value
        if not (isinstance(query_call.func, ast.Attribute) and query_call.func.attr == "query"):
            continue
        session = ast.unparse(query_call.func.value)
        entity = ast.unparse(query_call.args[0]) if query_call.args else "Model"
        pk = ast.unparse(outer.args[0]) if outer.args else "pk"
        s, e = _node_span(src, outer)
        edits.append(Edit(s, e, f"{session}.get({entity}, {pk})"))


def _edit_declarative_import(mod: PyModule, findings, edits: list[Edit], wanted=None) -> None:
    plan = wanted.get("__import_plan__") if wanted is not None else None
    if wanted is not None and plan is None:
        plan = ImportPlan()
        wanted["__import_plan__"] = plan
    for f in findings:
        if f.rule_id != "sqla-declarative-import":
            continue
        if plan is not None:
            for a in _import_from_node(mod, f.node.lineno).names:
                plan.move_name("sqlalchemy.ext.declarative", a.name,
                               "sqlalchemy.orm", a.name, asname=a.asname)


_TRANSFORMS = [
    _edit_model_methods,
    _edit_dunder_fields,
    _edit_field_kwargs,
    _edit_config_class,
    _edit_validators,
    _edit_basesettings,
    _edit_generic_model,
    _edit_metadata_bind,
    _edit_session_autocommit,
    _edit_legacy_select,
    _edit_raw_string_execute,
    _edit_engine_execute,
    _edit_query_get,
    _edit_declarative_import,
]


def _apply_import_plan(mod: PyModule, src: str, plan: ImportPlan,
                       ctx: dict) -> str:
    """Rewrite import statements per plan, then append missing imports."""
    tree = ast.parse(src)
    edits: list[Edit] = []
    existing: set[tuple[str, str]] = set()
    # group resulting lines per replaced statement
    for node in list(ast.walk(tree)):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                existing.add((node.module or "", a.name))
    # process statements from bottom to top so line offsets stay valid
    from_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    for node in sorted(from_nodes, key=lambda n: n.lineno, reverse=True):
        module = node.module or ""
        if module in plan.delete_modules:
            edits.append(_line_range_edit(mod, node.lineno,
                                          node.end_lineno or node.lineno, "", ))
            continue
        kept: list[str] = []
        added: list[str] = []  # (new_module, line) appended after this statement
        for a in node.names:
            key = (module, a.name)
            if key in plan.moves:
                to_mod, new_name, asname = plan.moves[key]
                if to_mod == module:
                    nm = f"{new_name} as {a.asname}" if a.asname else new_name
                    kept.append(nm)
                else:
                    bound = f" as {asname}" if asname else (
                        f" as {a.asname}" if a.asname else "")
                    alias = asname or a.asname
                    if alias and new_name != alias:
                        added.append(f"from {to_mod} import {new_name} as {alias}")
                    else:
                        added.append(f"from {to_mod} import {new_name}")
            else:
                kept.append(f"{a.name} as {a.asname}" if a.asname else a.name)
        block = ""
        if kept:
            block = f"from {module} import {', '.join(kept)}\n"
        block += "\n".join(added) + ("\n" if added else "")
        if block != f"from {module} import {', '.join(a.name for a in node.names)}\n":
            # compute against the *current* (body-patched) source: line count
            # of imports is unchanged by body edits (they don't touch import
            # lines anymore), so mod-based offsets are still valid.
            s0 = _line_start_off(src, node.lineno)
            e0 = _line_end_off(src, node.end_lineno or node.lineno) + 1
            edits.append(Edit(s0, e0, block))
    src = _apply_edits(src, edits)
    # append missing ensure imports — merge into an existing same-module
    # import when one exists, otherwise insert next to the last import from a
    # related package (e.g. `from sqlalchemy import text` sits with sqlalchemy)
    missing = [(m, n) for (m, n) in plan.ensure_names
               if (m, n) not in existing and (m, n) not in
               {(mm[0], mm[1]) for mm in plan.moves}]
    provided = {(to, new) for (to, new, _a) in plan.moves.values()}
    missing = [(m, n) for (m, n) in missing if (m, n) not in provided]
    if missing:
        tree2 = ast.parse(src)
        froms = [n for n in ast.walk(tree2) if isinstance(n, ast.ImportFrom)]
        merged: set[int] = set()
        inserts: list[tuple[int, str]] = []
        leftovers: list[tuple[str, str]] = []
        for (mod_name, sym) in list(missing):
            same = [n for n in froms if (n.module or "") == mod_name
                    and id(n) not in merged]
            if same:
                node = same[0]
                merged.add(id(node))
                names = [f"{a.name} as {a.asname}" if a.asname else a.name
                         for a in node.names] + [sym]
                line = f"from {mod_name} import {', '.join(sorted(names))}\n"
                s0 = _line_start_off(src, node.lineno)
                e0 = _line_end_off(src, node.end_lineno or node.lineno) + 1
                src = src[:s0] + line + src[e0:]
                # offsets below this point shift; recompute tree on next merge
                tree2 = ast.parse(src)
                froms = [n for n in ast.walk(tree2) if isinstance(n, ast.ImportFrom)]
            else:
                leftovers.append((mod_name, sym))
        if leftovers:
            anchor = None
            for node in froms:
                root_mod = (node.module or "").split(".")[0]
                if root_mod and any(m.split(".")[0] == root_mod for m, _ in leftovers):
                    anchor = node
            block = "".join(f"from {m} import {n}\n" for m, n in sorted(leftovers))
            if anchor is not None:
                off = _line_end_off(src, anchor.end_lineno or anchor.lineno) + 1
                src = src[:off] + block + src[off:]
            else:
                all_imp = [n for n in tree2.body
                           if isinstance(n, (ast.Import, ast.ImportFrom))]
                if all_imp:
                    last = all_imp[-1]
                    off = _line_end_off(src, last.end_lineno or last.lineno) + 1
                    src = src[:off] + block + src[off:]
                else:
                    src = block + src
    return src


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def build_patched_source(mod: PyModule, upgrade_id: str) -> tuple[str | None, list[Edit], list[str]]:
    """Return (new_source, edits, notes) for one module; None if not patchable."""
    upgrade = get_upgrade(upgrade_id)
    findings = []
    for rule in upgrade.rules:
        for f in rule.scan(mod):
            if f.auto_fixable:
                findings.append(f)
    if not findings:
        return None, [], []
    edits: list[Edit] = []
    notes: list[str] = []
    ctx: dict = {}
    for transform in _TRANSFORMS:
        try:
            transform(mod, findings, edits, ctx)
        except Exception as exc:  # pragma: no cover - defensive
            notes.append(f"{transform.__name__} failed: {exc}")
    try:
        new_src = _apply_edits(mod.source, edits)
        plan: ImportPlan | None = ctx.get("__import_plan__")
        for module, name in sorted(ctx.get("_imports", set())):
            if plan is None:
                plan = ImportPlan()
            plan.ensure(module, name)
        if plan is not None:
            new_src = _apply_import_plan(mod, new_src, plan, ctx)
        ast.parse(new_src, filename=mod.path)
    except SyntaxError as exc:
        notes.append(f"patch would produce invalid Python ({exc.msg}); file left unchanged")
        return None, [], notes
    return new_src, edits, notes


def apply_fixes(root: str | Path, upgrade_id: str, *, backup: bool = True) -> PatchResult:
    """Apply auto-fixes to files under *root*; return the unified diff."""
    from .ingest import ingest_repo

    root = Path(root)
    upgrade = get_upgrade(upgrade_id)
    modules = ingest_repo(root)
    result = PatchResult(diff="", changed_files=[], skipped=[])

    backup_dir = root / ".breakbot-work" / "backup"
    per_file: dict[str, str] = {}

    for mod in modules:
        if not upgrade.applies(mod):
            continue
        new_src, _edits, notes = build_patched_source(mod, upgrade_id)
        result.skipped.extend(f"{mod.path}: {n}" for n in notes)
        if new_src is None or new_src == mod.source:
            # manual-only findings still get reported in the skipped list
            for rule in upgrade.rules:
                for f in rule.scan(mod):
                    if not f.auto_fixable:
                        lineno = getattr(f.node, "lineno", 1)
                        result.skipped.append(
                            f"{mod.path}:{lineno}: {f.title} — manual migration required"
                        )
            continue
        target = root / mod.path
        if backup:
            (backup_dir / mod.path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_dir / mod.path)
        diff = "".join(difflib.unified_diff(
            mod.source.splitlines(keepends=True),
            new_src.splitlines(keepends=True),
            fromfile=f"a/{mod.path}",
            tofile=f"b/{mod.path}",
        ))
        per_file[mod.path] = diff
        target.write_text(new_src, encoding="utf-8")
        result.changed_files.append(mod.path)

    result.per_file_diffs = per_file
    result.diff = "".join(per_file.values())
    result.backed_up_to = str(backup_dir) if backup else ""
    if upgrade_id == "pydantic1-to-2" and any(
        any(rec.module == "pydantic" and rec.name == "BaseSettings"
            for rec in m.imports)
        for m in modules
    ):
        note = "NOTE: pydantic-settings is required (pip install pydantic-settings)."
        if note not in result.skipped:
            result.skipped.insert(0, note)
    return result
