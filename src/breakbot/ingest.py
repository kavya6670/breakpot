"""Repo ingestion: walk Python files and parse them into typed call sites.

Everything the rules engine reasons about comes from this module: imports,
class declarations (which names are pydantic models / SQLAlchemy declarative
models), calls, attribute accesses, decorators and assignments with type
information propagated through annotations and constructors.
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Optional, Union


# --------------------------------------------------------------------------- #
# Parsed records
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ImportRec:
    module: str            # "pydantic", "sqlalchemy.orm", ...
    name: str              # imported symbol ("*" for star)
    alias: Optional[str]
    kind: str              # "from" | "import"
    lineno: int
    col: int
    end_lineno: int
    end_col: int

    @property
    def bound(self) -> str:
        """The name this import binds in the importing module's namespace."""
        return self.alias or (self.name if self.kind == "from" else self.module.split(".")[0])


@dataclasses.dataclass
class _ScopeRec:
    node: ast.AST
    kind: str           # "module" | "class" | "func"
    name: str


@dataclasses.dataclass
class CallRec:
    node: ast.Call
    name: str                       # called function/attribute name
    kind: str                       # "attr" (x.foo()) | "name" (foo())
    receiver: Optional[ast.AST]     # x for x.foo()
    scope: tuple                # tuple of (kind, name)
    enclosing_class: Optional[str]
    enclosing_func: Optional[str]


@dataclasses.dataclass
class AttrRec:
    node: ast.Attribute
    name: str
    receiver: Optional[ast.AST]
    scope: tuple
    enclosing_class: Optional[str]
    enclosing_func: Optional[str]


@dataclasses.dataclass
class DecorRec:
    node: ast.expr                 # the decorator expression
    name: str                      # decorator name (e.g. "validator")
    args: list
    keywords: list
    func_lineno: int
    func_name: str
    scope: tuple
    enclosing_class: Optional[str]


@dataclasses.dataclass
class AssignRec:
    names: list[str]
    value: Optional[ast.AST]
    scope: tuple
    lineno: int
    annotation: Optional[ast.AST] = None


@dataclasses.dataclass
class ClassRec:
    node: ast.ClassDef
    name: str
    base_strings: list[str]
    scope: tuple
    is_pydantic_model: bool = False
    is_sqla_model: bool = False
    config_of: Optional[str] = None     # model name if this is a nested Config


# --------------------------------------------------------------------------- #
# Parsed module
# --------------------------------------------------------------------------- #

class PyModule:
    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.lines = source.splitlines()
        try:
            self.tree = ast.parse(source, filename=path)
        except SyntaxError as exc:  # pragma: no cover - reported, not fatal
            self.tree = ast.Module(body=[], type_ignores=[])
            self.parse_error: Optional[str] = f"{exc.__class__.__name__}: {exc}"
        else:
            self.parse_error = None

        self.imports: list[ImportRec] = []
        self.bound_imports: dict[str, ImportRec] = {}
        self.calls: list[CallRec] = []
        self.attrs: list[AttrRec] = []
        self.decors: list[DecorRec] = []
        self.assigns: list[AssignRec] = []
        self.classes: list[ClassRec] = []
        # annotations: (scope, name) -> unparsed annotation
        self.annotations: dict[tuple[tuple, str], str] = {}

        self._collect()

    # -- introspection helpers -------------------------------------------- #

    def segment(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or ""

    def snippet(self, node: ast.AST) -> str:
        """One-line trimmed source snippet for display."""
        seg = self.segment(node).strip()
        return " ".join(seg.split())

    def line_text(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def imported_from(self, local_name: str) -> Optional[tuple[str, str]]:
        """Return (module, original_name) for a bound import name, or None."""
        rec = self.bound_imports.get(local_name)
        if rec is None:
            return None
        return rec.module, rec.name

    @property
    def pydantic_imported(self) -> bool:
        return any(m.module == "pydantic" or m.module.startswith("pydantic.")
                   for m in self.imports)

    @property
    def sqlalchemy_imported(self) -> bool:
        return any(m.module == "sqlalchemy" or m.module.startswith("sqlalchemy.")
                   for m in self.imports)

    @property
    def model_classes(self) -> set[str]:
        return {c.name for c in self.classes if c.is_pydantic_model}

    @property
    def sqla_model_classes(self) -> set[str]:
        return {c.name for c in self.classes if c.is_sqla_model}

    def calls_named(self, *names: str) -> list[CallRec]:
        return [c for c in self.calls if c.name in names]

    def attrs_named(self, *names: str) -> list[AttrRec]:
        return [a for a in self.attrs if a.name in names]

    def decors_named(self, *names: str) -> list[DecorRec]:
        return [d for d in self.decors if d.name in names]

    # -- model-instance type resolution ----------------------------------- #

    def receiver_model_name(
        self, receiver: Optional[ast.AST], scope: tuple
    ) -> tuple[Optional[str], str]:
        """Best-effort inference of the pydantic model type of *receiver*.

        Returns (model_class_name | None, confidence).
        """
        models = self.model_classes
        if receiver is None:
            return None, "low"

        if isinstance(receiver, ast.Name):
            if receiver.id == "self" or receiver.id == "cls":
                for kind, name in reversed(scope):
                    if kind == "class" and name in models:
                        return name, "high"
                return None, "low"
            if receiver.id in models:
                return receiver.id, "high"
            # annotation-driven (innermost scope wins)
            for sc in _scope_chain(scope):
                ann = self.annotations.get((sc, receiver.id))
                if ann:
                    hit = _model_from_annotation(ann, models)
                    if hit:
                        return hit, "high" if "Optional" not in ann and "Union" not in ann else "medium"
            # assignment-driven
            for sc in _scope_chain(scope):
                for a in self.assigns:
                    if a.scope == sc and receiver.id in a.names:
                        hit = self._model_from_value(a.value)
                        if hit:
                            return hit, "medium"
            return None, "low"

        if isinstance(receiver, ast.Call):
            return self._model_from_value(receiver), "high" if self._model_from_value(receiver) else "low"

        if isinstance(receiver, ast.Attribute):
            # chained call result, e.g. get_user().dict() — one level only
            return None, "low"

        return None, "low"

    def _model_from_value(self, value: Optional[ast.AST]) -> Optional[str]:
        if value is None:
            return None
        models = self.model_classes
        if isinstance(value, ast.Call):
            f = value.func
            if isinstance(f, ast.Name) and f.id in models:
                return f.id
            if isinstance(f, ast.Attribute) and f.attr in {
                "model_validate", "parse_obj", "model_construct", "construct",
                "from_orm", "parse_raw", "parse_file", "model_validate_json",
            }:
                inner, _ = self.receiver_model_name(f.value, ())
                return inner
        return None

    # -- SQLAlchemy name tracking ----------------------------------------- #

    def sqla_names(self) -> dict[str, str]:
        """Classify bound names relevant to the SQLAlchemy upgrade.

        Returns mapping name -> role, roles: engine, session_factory,
        session, connection, metadata, declarative_base, select, text.
        """
        roles: dict[str, str] = {}
        for name, rec in self.bound_imports.items():
            if rec.module.startswith("sqlalchemy"):
                if rec.name in {"create_engine"}:
                    roles[name] = "create_engine"
                elif rec.name == "sessionmaker":
                    roles[name] = "sessionmaker"
                elif rec.name == "MetaData":
                    roles[name] = "MetaData"
                elif rec.name == "declarative_base":
                    roles[name] = "declarative_base"
                elif rec.name == "DeclarativeBase":
                    roles[name] = "DeclarativeBase"
                elif rec.name == "select":
                    roles[name] = "select"
                elif rec.name == "text":
                    roles[name] = "text"
                elif rec.name == "Session":
                    roles[name] = "Session"
        # derive variable roles from assignments
        for a in self.assigns:
            v = a.value
            if not isinstance(v, ast.Call):
                continue
            role = self._role_of_call(v)
            if role:
                for n in a.names:
                    roles.setdefault(n, role)
        # derive roles from `with <call> as name:` context managers, e.g.
        # `with engine.connect() as conn:` / `with engine.begin() as conn:`
        for node in ast.walk(self.tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call) and item.optional_vars:
                        role = self._role_of_call(item.context_expr)
                        if role and isinstance(item.optional_vars, ast.Name):
                            roles.setdefault(item.optional_vars.id, role)
        return roles

    @staticmethod
    def _role_of_call(v: ast.Call) -> str | None:
        f = v.func
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if fname in {"create_engine"}:
            return "engine"
        if fname in {"sessionmaker"}:
            return "session_factory"
        if fname in {"MetaData"}:
            return "metadata"
        if fname in {"declarative_base"}:
            return "declarative_base_var"
        if isinstance(f, ast.Attribute) and f.attr in {"connect", "begin"}:
            return "connection"
        if fname == "Session":
            return "session"
        if isinstance(f, ast.Call):  # sessionmaker(...)()
            return "session"
        return None

    # -- collection -------------------------------------------------------- #

    def _collect(self) -> None:
        v = _Collector(self)
        v.visit(self.tree)


def _scope_chain(scope: tuple) -> list[tuple]:
    """Innermost-first list of scopes."""
    return [scope[: i + 1] for i in range(len(scope) - 1, -1, -1)] + [()]


def _model_from_annotation(ann: str, models: set[str]) -> Optional[str]:
    # strip quotes / Optional / Union / list[...] wrappers, match suffix
    token = ann.strip().strip("'\"")
    for m in sorted(models, key=len, reverse=True):
        if token == m or token.endswith("." + m) or token.endswith("[" + m) or token.endswith("[ " + m):
            return m
        if m in token and any(
            sep in token for sep in (f"[{m}]", f"[{m},", f", {m}]", f".{m}")
        ):
            return m
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, mod: PyModule):
        self.mod = mod
        self.scope: list[_ScopeRec] = []
        self._call_func_ids: set[int] = set()  # Attribute nodes that are call funcs

    @property
    def scope_key(self) -> tuple:
        return tuple((s.kind, s.name) for s in self.scope)

    @property
    def enclosing_class(self) -> Optional[str]:
        for s in reversed(self.scope):
            if s.kind == "class":
                return s.name
        return None

    @property
    def enclosing_func(self) -> Optional[str]:
        for s in reversed(self.scope):
            if s.kind == "func":
                return s.name
        return None

    # -- imports ----------------------------------------------------------- #

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            rec = ImportRec(
                module=alias.name, name=alias.name.split(".")[0],
                alias=alias.asname, kind="import",
                lineno=node.lineno, col=node.col_offset,
                end_lineno=node.end_lineno or node.lineno,
                end_col=node.end_col_offset or 0,
            )
            self.mod.imports.append(rec)
            self.mod.bound_imports[rec.bound] = rec
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            rec = ImportRec(
                module=module, name=alias.name, alias=alias.asname,
                kind="from", lineno=node.lineno, col=node.col_offset,
                end_lineno=node.end_lineno or node.lineno,
                end_col=node.end_col_offset or 0,
            )
            self.mod.imports.append(rec)
            if alias.name != "*":
                self.mod.bound_imports[rec.bound] = rec
        self.generic_visit(node)

    # -- classes ----------------------------------------------------------- #

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = [ast.unparse(b) for b in node.bases]
        rec = ClassRec(
            node=node, name=node.name, base_strings=bases,
            scope=self.scope_key,
        )
        self.mod.classes.append(rec)
        self.scope.append(_ScopeRec(node, "class", node.name))
        self.generic_visit(node)
        self.scope.pop()

    # -- functions (annotations + decorators) ------------------------------ #

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_decorators(node)
        self.scope.append(_ScopeRec(node, "func", node.name))
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
            if arg.annotation is not None:
                self.mod.annotations[(self.scope_key, arg.arg)] = ast.unparse(arg.annotation)
        if node.returns is not None:
            self.mod.annotations[(self.scope_key, f"<return:{node.name}>")] = ast.unparse(node.returns)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _record_decorators(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        for dec in node.decorator_list:
            name = None
            args: list = []
            keywords: list = []
            if isinstance(dec, ast.Call):
                name = dec.func.id if isinstance(dec.func, ast.Name) else (
                    dec.func.attr if isinstance(dec.func, ast.Attribute) else None)
                args = list(dec.args)
                keywords = list(dec.keywords)
            elif isinstance(dec, ast.Name):
                name = dec.id
            elif isinstance(dec, ast.Attribute):
                name = dec.attr
            if name:
                self.mod.decors.append(DecorRec(
                    node=dec, name=name, args=args, keywords=keywords,
                    func_lineno=node.lineno, func_name=node.name,
                    scope=self.scope_key,
                    enclosing_class=self.enclosing_class,
                ))

    # -- assignments / annotations ---------------------------------------- #

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if names:
            self.mod.assigns.append(AssignRec(
                names=names, value=node.value, scope=self.scope_key,
                lineno=node.lineno,
            ))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.mod.annotations[(self.scope_key, node.target.id)] = ast.unparse(node.annotation)
            self.mod.assigns.append(AssignRec(
                names=[node.target.id], value=node.value, scope=self.scope_key,
                lineno=node.lineno, annotation=node.annotation,
            ))
        self.generic_visit(node)

    # -- calls & attributes ----------------------------------------------- #

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.AST):
            self._call_func_ids.add(id(node.func))
        if isinstance(node.func, ast.Attribute):
            rec = CallRec(
                node=node, name=node.func.attr, kind="attr",
                receiver=node.func.value, scope=self.scope_key,
                enclosing_class=self.enclosing_class,
                enclosing_func=self.enclosing_func,
            )
        elif isinstance(node.func, ast.Name):
            rec = CallRec(
                node=node, name=node.func.id, kind="name",
                receiver=None, scope=self.scope_key,
                enclosing_class=self.enclosing_class,
                enclosing_func=self.enclosing_func,
            )
        else:
            rec = None
        if rec is not None:
            self.mod.calls.append(rec)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if id(node) in self._call_func_ids:
            # this attribute is the function of a Call — handled by visit_Call
            self.generic_visit(node)
            return
        if not (isinstance(node.ctx, ast.Load) and not isinstance(node.value, ast.Call)):
            self.generic_visit(node)
            return
        # only record standalone attribute access that is NOT a call (calls
        # arrive via visit_Call; node.ctx check keeps us off store-context)
        self.mod.attrs.append(AttrRec(
            node=node, name=node.attr, receiver=node.value,
            scope=self.scope_key, enclosing_class=self.enclosing_class,
            enclosing_func=self.enclosing_func,
        ))
        self.generic_visit(node)


# --------------------------------------------------------------------------- #
# Repo walking
# --------------------------------------------------------------------------- #

def iter_python_files(root: Path):
    skip = {".venv", "venv", ".git", "node_modules", "__pycache__",
            ".breakbot-work", "site-packages", "build", "dist", ".tox"}
    for path in sorted(root.rglob("*.py")):
        if any(part in skip for part in path.parts):
            continue
        yield path


def ingest_repo(root: str | Path) -> list[PyModule]:
    root = Path(root)
    modules: list[PyModule] = []
    for path in iter_python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        mod = PyModule(str(path.relative_to(root)), source)
        modules.append(mod)
    # second pass: now that all classes exist, flag model classes
    _classify_classes(modules)
    return modules


def _classify_classes(modules: list[PyModule]) -> None:
    by_module = {m.path: m for m in modules}
    for mod in modules:
        # map bound base names
        pydantic_bases = set()
        sqla_bases = set()
        for name, rec in mod.bound_imports.items():
            if rec.module == "pydantic" and rec.name in {"BaseModel"}:
                pydantic_bases.add(name)
            if rec.module.startswith("pydantic.") and rec.name == "BaseModel":
                pydantic_bases.add(name)
            if rec.module == "pydantic.generics" and rec.name == "GenericModel":
                pydantic_bases.add(name)
            if rec.module.startswith("sqlalchemy") and rec.name in {
                "DeclarativeBase", "declarative_base",
            }:
                sqla_bases.add(name)
        # declarative_base() assigned to a var, e.g. Base = declarative_base()
        decl_vars = set()
        for a in mod.assigns:
            if isinstance(a.value, ast.Call):
                f = a.value.func
                fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                imp = mod.imported_from(fname) if fname else None
                if fname and imp and imp[0].startswith("sqlalchemy") and imp[1] == "declarative_base":
                    decl_vars.update(a.names)
        sqla_bases |= decl_vars

        for cls in list(mod.classes):
            bases = set()
            for b in cls.base_strings:
                bases.add(b.split(".")[-1].split("[")[0])
            if bases & pydantic_bases or any(b in {"BaseModel", "GenericModel"} for b in bases):
                cls.is_pydantic_model = True
            if bases & sqla_bases or any(b in {"DeclarativeBase"} for b in bases):
                cls.is_sqla_model = True
        # nested Config classes inside pydantic models
        for cls in list(mod.classes):
            if cls.name == "Config":
                for owner in mod.classes:
                    if owner is cls:
                        continue
                    if owner.is_pydantic_model and cls.scope[:-1] == owner.scope:
                        cls.config_of = owner.name
                        break
