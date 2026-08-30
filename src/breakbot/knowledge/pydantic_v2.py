"""Rules for the Pydantic 1.x → 2.x upgrade.

Severity ratings here are empirically checked against installed pydantic 2.x:
BREAK = raises / import error / silently stops doing its job; WARNING =
deprecated (emits DeprecationWarning, removed in V3) or semantics changed.
"""
from __future__ import annotations

import ast

from ..models import Confidence, Severity
from .base import Finding, Rule, Upgrade

SECTION_METHODS = "basemodel-methods"
SECTION_FIELD = "field-keyword-args"
SECTION_CONFIG = "config-changes"
SECTION_VALIDATORS = "validators"
SECTION_SETTINGS = "settings-package"
SECTION_OTHER = "other-moves"

# method name -> (new name, severity, auto_fixable, extra note)
METHOD_RENAMES: dict[str, tuple[str | None, Severity, bool, str]] = {
    "dict": ("model_dump", Severity.WARNING, True, ""),
    "json": ("model_dump_json", Severity.WARNING, True,
             "kwargs like indent=/ensure_ascii= behave differently; see model_dump_json docs."),
    "parse_obj": ("model_validate", Severity.WARNING, True, ""),
    "parse_raw": ("model_validate_json", Severity.WARNING, True,
                  "use model_validate_json for JSON; otherwise load the data then call model_validate."),
    "parse_file": ("__parse_file__", Severity.WARNING, True,
                   "rewritten to model_validate_json(Path(path).read_text())."),
    "from_orm": ("model_validate", Severity.WARNING, True,
                 "requires model_config ConfigDict(from_attributes=True)."),
    "construct": ("model_construct", Severity.WARNING, True, ""),
    "copy": ("model_copy", Severity.WARNING, True,
             "model_copy() defaults to deep=False; V1 copy() defaulted to deep=True. Pass deep=True to keep behavior."),
    "schema": ("model_json_schema", Severity.WARNING, True, ""),
    "schema_json": (None, Severity.BREAK, False,
                    "schema_json() is gone; use json.dumps(Model.model_json_schema())."),
    "update_forward_refs": ("model_rebuild", Severity.WARNING, True, ""),
}

CONFIG_RENAMES = {
    "allow_population_by_field_name": "populate_by_name",
    "anystr_lower": "str_to_lower",
    "anystr_strip_whitespace": "str_strip_whitespace",
    "anystr_upper": "str_to_upper",
    "keep_untouched": "ignored_types",
    "max_anystr_length": "str_max_length",
    "min_anystr_length": "str_min_length",
    "orm_mode": "from_attributes",
    "schema_extra": "json_schema_extra",
    "validate_all": "validate_default",
}
CONFIG_REMOVED = {
    "allow_mutation": "use model_config frozen=True (inverse meaning)",
    "error_msg_templates": "removed; customize errors via exception handlers",
    "fields": "removed; use typing.Annotated on fields instead",
    "getter_dict": "removed along with orm_mode internals",
    "smart_union": "union mode 'smart' is the default now",
    "underscore_attrs_are_private": "this is now the default behavior",
    "json_loads": "removed; use a custom serializer or annotated type",
    "json_dumps": "removed; use @field_serializer / model_serializer",
    "copy_on_model_validation": "removed",
    "post_init_call": "removed",
    "json_encoders": "deprecated; use the @field_serializer decorator instead",
}

FIELD_KWARG_RENAMES = {"regex": "pattern", "min_items": "min_length", "max_items": "max_length"}

# Exact changelog needle per method (avoids e.g. "`json`" citing json_schema).
METHOD_QUOTES = {
    "dict": "`dict()`",
    "json": "`json()`",
    "parse_obj": "`parse_obj()`",
    "parse_raw": "`parse_raw`",
    "parse_file": "`parse_file`",
    "from_orm": "`from_orm`",
    "construct": "`construct()`",
    "copy": "`copy()`",
    "schema": "`json_schema()`",
    "schema_json": "`json_schema()`",
    "update_forward_refs": "`update_forward_refs()`",
}


def _uses_v1_compat(mod) -> bool:
    return any(m.module.startswith("pydantic.v1") for m in mod.imports)


def _mk(rule: Rule, mod, node, severity: Severity, title: str, detail: str,
        symbol: str, *, fixable: bool = False, fix_summary: str = "",
        confidence: Confidence = Confidence.HIGH, enclosing: str = "",
        section: str | None = None, quote: str = ""):
    return Finding(
        severity=severity, module_path=mod.path, node=node, title=title,
        detail=detail, symbol=symbol, auto_fixable=fixable,
        fix_summary=fix_summary, confidence=confidence,
        section=section or rule.section, quote=quote or rule.quote,
        rule_id=rule.id, enclosing=enclosing,
    )


def _ctx(rec) -> str:
    parts = [p for p in (rec.enclosing_class, rec.enclosing_func) if p]
    return ".".join(parts)


# --------------------------------------------------------------------------- #
# Rule scans
# --------------------------------------------------------------------------- #

def scan_model_methods(mod):
    """Deprecated/renamed BaseModel methods."""
    rule = _RULES["py-model-methods"]
    if not mod.pydantic_imported or _uses_v1_compat(mod):
        return []
    out = []
    for call in mod.calls:
        if call.kind != "attr" or call.name not in METHOD_RENAMES:
            continue
        new_name, sev, fixable, note = METHOD_RENAMES[call.name]
        model, conf = mod.receiver_model_name(call.receiver, call.scope)
        if model is None and conf != "high":
            # Only flag as "probably a model" when the file uses pydantic;
            # honest medium-confidence fallback for untyped receivers.
            if not mod.pydantic_imported:
                continue
            confidence = Confidence.MEDIUM
            who = "a pydantic model (receiver type not inferred)"
        else:
            confidence = Confidence(conf) if conf in ("high", "medium", "low") else Confidence.MEDIUM
            who = f"pydantic model {model}" if model else "a pydantic model"
        if new_name is None:
            title = f".{call.name}() removed"
            fix = ""
        elif new_name == "__parse_file__":
            title = f".{call.name}() deprecated"
            fix = f"rewrite to model_validate_json(Path(<path>).read_text())"
        else:
            title = f".{call.name}() renamed to .{new_name}()"
            fix = f"rename .{call.name}() → .{new_name}()"
        detail = (
            f"{who}: BaseModel.{call.name}() is deprecated in pydantic v2 "
            f"(emits DeprecationWarning, removed in v3)."
            if sev == Severity.WARNING and new_name
            else f"{who}: BaseModel.{call.name}() no longer exists in pydantic v2."
        )
        if note:
            detail += f" Note: {note}"
        quote = METHOD_QUOTES.get(call.name, f"`{call.name}`")
        out.append(
            _mk(rule, mod, call.node, sev, title, detail,
                f".{call.name}()", fixable=fixable, fix_summary=fix,
                confidence=confidence, enclosing=_ctx(call), quote=quote)
        )
    return out


def scan_dunder_fields(mod):
    """__fields__ → model_fields."""
    rule = _RULES["py-fields-attr"]
    if not mod.pydantic_imported or _uses_v1_compat(mod):
        return []
    out = []
    for attr in mod.attrs:
        if attr.name != "__fields__":
            continue
        model, conf = mod.receiver_model_name(attr.receiver, attr.scope)
        if model is None and not mod.pydantic_imported:
            continue
        confidence = Confidence(conf) if conf in ("high", "medium") else Confidence.MEDIUM
        out.append(_mk(rule, mod, attr.node, Severity.WARNING,
                       "__fields__ renamed to model_fields",
                       f"The model field metadata attribute is now model_fields "
                       f"(pydantic v2 deprecation warning; removed in v3).",
                       "__fields__", fixable=True,
                       fix_summary="rename __fields__ → model_fields",
                       confidence=confidence, enclosing=_ctx(attr),
                       quote="__fields__"))
    return out


def scan_field_kwargs(mod):
    """Field()/constr()/conlist() keyword changes."""
    rule = _RULES["py-field-kwargs"]
    if not mod.pydantic_imported or _uses_v1_compat(mod):
        return []
    out = []
    watched = {"Field", "constr", "conlist", "conset", "confrozenset"}
    for call in mod.calls:
        if call.kind != "name" or call.name not in watched:
            imp = mod.imported_from(call.name) if call.kind == "name" else None
            if not imp or imp[0] != "pydantic":
                continue
        for kw in call.node.keywords:
            if kw.arg is None:
                continue
            if kw.arg in FIELD_KWARG_RENAMES:
                new = FIELD_KWARG_RENAMES[kw.arg]
                if kw.arg == "regex":
                    sev = Severity.BREAK
                    detail = (f"Field({kw.arg}=…) raises PydanticUserError in v2: "
                              f"the keyword was renamed to {new!r}.")
                else:
                    sev = Severity.WARNING
                    detail = (f"Field({kw.arg}=…) is deprecated in v2 and currently "
                              f"works as a shim; rename to {new!r} (removed in v3).")
                out.append(_mk(rule, mod, kw, sev,
                               f"Field({kw.arg}=) → Field({new}=)",
                               detail, f"{call.name}({kw.arg}=)",
                               fixable=True,
                               fix_summary=f"rename keyword {kw.arg} → {new}",
                               enclosing=_ctx(call), quote=f"`{kw.arg}`"))
            elif kw.arg == "const":
                out.append(_mk(rule, mod, kw, Severity.BREAK,
                               "Field(const=True) removed",
                               "const fields are removed; declare the value with "
                               "typing.Literal instead.", "Field(const=)",
                               fixable=False, enclosing=_ctx(call), quote="`const`"))
            elif kw.arg == "allow_mutation":
                val = ast.unparse(kw.value) if kw.value else ""
                fixable = val.strip() == "False"
                out.append(_mk(rule, mod, kw, Severity.WARNING,
                               "Field(allow_mutation=) removed → frozen",
                               "allow_mutation was removed; the inverse is frozen. "
                               "allow_mutation=False becomes frozen=True; "
                               "allow_mutation=True should be dropped.",
                               f"Field(allow_mutation={val})",
                               fixable=fixable,
                               fix_summary="allow_mutation=False → frozen=True" if fixable else "",
                               enclosing=_ctx(call), quote="`allow_mutation`"))
            elif kw.arg in ("unique_items", "final"):
                out.append(_mk(rule, mod, kw, Severity.WARNING,
                               f"Field({kw.arg}=) removed",
                               f"the {kw.arg!r} keyword is removed in v2 "
                               f"({'use typing.Final' if kw.arg == 'final' else 'no direct replacement'}); "
                               f"it is silently ignored.",
                               f"Field({kw.arg}=)", fixable=False,
                               enclosing=_ctx(call), quote=f"`{kw.arg}`"))
    return out


def scan_config_class(mod):
    """Nested class Config → model_config = ConfigDict(...)."""
    rule = _RULES["py-config-class"]
    if _uses_v1_compat(mod):
        return []
    out = []
    for cls in mod.classes:
        if cls.name != "Config" or not cls.config_of:
            continue
        body_assigns = [n for n in cls.node.body if isinstance(n, ast.Assign)]
        removed = []
        for stmt in body_assigns:
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    if tgt.id in CONFIG_REMOVED:
                        removed.append((tgt.id, stmt))
                    elif tgt.id in CONFIG_RENAMES:
                        pass
        sev = Severity.BREAK if removed else Severity.WARNING
        out.append(_mk(rule, mod, cls.node, sev,
                       f"class Config deprecated → model_config = ConfigDict(...)",
                       "Pydantic v2 deprecates the nested Config class; use "
                       "model_config = ConfigDict(...). Old keys are silently "
                       "ignored (e.g. orm_mode does nothing, so from_orm breaks).",
                       "class Config", fixable=True,
                       fix_summary="convert Config class to model_config = ConfigDict(...)",
                       enclosing=cls.config_of, quote="model_config"))
        for key, stmt in removed:
            out.append(_mk(rule, mod, stmt, Severity.BREAK,
                           f"Config key {key!r} removed",
                           f"Config setting {key!r} was removed in v2: {CONFIG_REMOVED[key]}.",
                           f"Config.{key}", fixable=False,
                           enclosing=cls.config_of, quote=key))
    return out


def scan_validator_decorators(mod):
    rule = _RULES["py-validator-decorator"]
    if _uses_v1_compat(mod):
        return []
    out = []
    for dec in mod.decors:
        if dec.name == "validator":
            imp = mod.imported_from("validator")
            if imp and imp[0] != "pydantic":
                continue
            kw_names = {k.arg for k in dec.keywords}
            bad = kw_names & {"each_item", "config", "field"}
            has_always = "always" in kw_names
            if bad:
                out.append(_mk(rule, mod, dec.node, Severity.BREAK,
                               "@validator(...) needs manual migration",
                               f"@validator with {sorted(bad)} cannot be auto-ported: "
                               f"each_item is gone (use Annotated metadata), config/field "
                               f"args are replaced by ValidationInfo. Migrate to @field_validator.",
                               "@validator", fixable=False,
                               enclosing=dec.enclosing_class or "",
                               quote="@validator"))
            else:
                detail = ("@validator is deprecated in v2 (removed in v3); use "
                          "@field_validator, which needs an explicit @classmethod "
                          "for the (cls, value) signature.")
                if has_always:
                    detail += (" NOTE: always=True semantics changed — standard "
                               "validators now also run on defaults; verify behavior.")
                out.append(_mk(rule, mod, dec.node,
                               Severity.WARNING if not has_always else Severity.BREAK,
                               "@validator → @field_validator",
                               detail, "@validator", fixable=True,
                               fix_summary="rename to @field_validator and add @classmethod",
                               enclosing=dec.enclosing_class or "",
                               quote="@validator"))
        elif dec.name == "root_validator":
            imp = mod.imported_from("root_validator")
            if imp and imp[0] != "pydantic":
                continue
            kw_names = {k.arg for k in dec.keywords}
            mode = "mode='before'" if "pre" in kw_names else "mode='after'"
            out.append(_mk(rule, mod, dec.node, Severity.BREAK,
                           "@root_validator → @model_validator (manual)",
                           f"@root_validator is deprecated. Replace with @model_validator({mode}); "
                           f"the allowed signatures changed (before: receives dict; after: "
                           f"receives the model instance and must return it).",
                           "@root_validator", fixable=False,
                           enclosing=dec.enclosing_class or "",
                           quote="@root_validator"))
        elif dec.name == "validate_arguments":
            out.append(_mk(rule, mod, dec.node, Severity.WARNING,
                           "@validate_arguments → @validate_call",
                           "validate_arguments is deprecated; use validate_call.",
                           "@validate_arguments", fixable=True,
                           fix_summary="rename to @validate_call (import too)",
                           enclosing=dec.enclosing_class or "",
                           quote="validate_arguments"))
    return out


def scan_basesettings_import(mod):
    rule = _RULES["py-basesettings-import"]
    if _uses_v1_compat(mod):
        return []
    out = []
    for rec in mod.imports:
        if rec.module == "pydantic" and rec.name == "BaseSettings":
            out.append(Finding(
                severity=Severity.BREAK, module_path=mod.path, node=rec,
                title="BaseSettings moved to pydantic-settings",
                detail=("from pydantic import BaseSettings raises PydanticImportError "
                        "in v2. BaseSettings now lives in the pydantic-settings package: "
                        "`pip install pydantic-settings` and import from pydantic_settings."),
                symbol="from pydantic import BaseSettings",
                auto_fixable=True,
                fix_summary="rewrite import to pydantic_settings (pip install pydantic-settings)",
                section=SECTION_SETTINGS, quote="BaseSettings",
                rule_id=rule.id, enclosing=""))
    return out


def scan_generic_model(mod):
    rule = _RULES["py-generic-model"]
    if _uses_v1_compat(mod):
        return []
    out = []
    gm_import = any(rec.module == "pydantic.generics" and rec.name == "GenericModel"
                    for rec in mod.imports)
    gm_bound = {rec.bound for rec in mod.imports
                if rec.module == "pydantic.generics" and rec.name == "GenericModel"}
    if gm_import:
        for rec in mod.imports:
            if rec.module == "pydantic.generics" and rec.name == "GenericModel":
                out.append(Finding(
                    severity=Severity.WARNING, module_path=mod.path, node=rec,
                    title="pydantic.generics.GenericModel removed",
                    detail=("GenericModel is removed (works temporarily via a shim "
                            "with a deprecation warning). Inherit BaseModel and Generic[T] "
                            "directly: class Envelope(BaseModel, Generic[T])."),
                    symbol="from pydantic.generics import GenericModel",
                    auto_fixable=True,
                    fix_summary="base class becomes (BaseModel, Generic[T]); add Generic import",
                    section=SECTION_OTHER, quote="GenericModel",
                    rule_id=rule.id, enclosing=""))
    for cls in mod.classes:
        if any(b.split(".")[-1] in gm_bound or b.split(".")[-1] == "GenericModel"
               for b in cls.base_strings):
            out.append(Finding(
                severity=Severity.BREAK, module_path=mod.path, node=cls.node,
                title=f"{cls.name}(GenericModel) → (BaseModel, Generic[...])",
                detail=("Generic models are now plain BaseModel subclasses with Generic: "
                        f"class {cls.name}(BaseModel, Generic[T]): ..."),
                symbol=f"class {cls.name}(GenericModel)",
                auto_fixable=True,
                fix_summary="rewrite base classes to (BaseModel, Generic[...])",
                section=SECTION_OTHER, quote="GenericModel",
                rule_id=rule.id, enclosing=""))
    return out


_RULES: dict[str, Rule] = {}


def _rule(rid, title, section, quote, scan_fn, applies="pydantic", fixable=False):
    r = Rule(id=rid, title=title, applies_to=applies, scan=scan_fn,
             section=section, quote=quote, auto_fixable=fixable)
    _RULES[rid] = r
    return r


_rule("py-model-methods", "BaseModel method renames", SECTION_METHODS,
      "`dict()`", scan_model_methods)
_rule("py-fields-attr", "__fields__ attribute rename", SECTION_METHODS,
      "__fields__", scan_dunder_fields)
_rule("py-field-kwargs", "Field() keyword removals/renames", SECTION_FIELD,
      "`regex`", scan_field_kwargs)
_rule("py-config-class", "Config class → model_config", SECTION_CONFIG,
      "model_config", scan_config_class)
_rule("py-validator-decorator", "validator/root_validator decorators",
      SECTION_VALIDATORS, "@validator", scan_validator_decorators)
_rule("py-basesettings-import", "BaseSettings moved package", SECTION_SETTINGS,
      "BaseSettings", scan_basesettings_import)
_rule("py-generic-model", "GenericModel removed", SECTION_OTHER,
      "GenericModel", scan_generic_model)

UPGRADE = Upgrade(
    id="pydantic1-to-2",
    title="Pydantic 1.x → 2.x",
    source_id="pydantic-1-to-2",
    package_from="pydantic<2",
    package_to="pydantic>=2",
    rules=list(_RULES.values()),
    summary=("Pydantic v2 is a ground-up rewrite (pydantic-core in Rust). "
             "BaseModel methods were renamed to model_*, Field keywords were "
             "renamed/removed, the Config class became model_config=ConfigDict(), "
             "and validators moved to @field_validator/@model_validator."),
)
