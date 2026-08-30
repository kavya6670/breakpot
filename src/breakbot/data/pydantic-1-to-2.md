# Pydantic V1 → V2 — Changelog excerpts (bundled by BreakBot)

Source: Pydantic Migration Guide — https://docs.pydantic.dev/latest/migration/
(excerpts are bundled so scans work offline; the same lines can be refetched live)

## basemodel-methods
### Changes to `pydantic.BaseModel`
Various method names have been changed; all non-deprecated `BaseModel` methods now have names matching either the format `model_.*` or `__.*pydantic.*__`. Where possible, the deprecated methods with their old names are retained to ease migration, but calling them emits `DeprecationWarning`s and they are removed in V3.
- `__fields__` → `model_fields`
- `construct()` → `model_construct()`
- `copy()` → `model_copy()` (note: `model_copy` defaults to `deep=False`, unlike V1 `copy()` which defaulted to `deep=True`)
- `dict()` → `model_dump()`
- `json_schema()` → `model_json_schema()`
- `json()` → `model_dump_json()`
- `parse_obj()` → `model_validate()`
- `parse_file()` removed: load the data then call `model_validate`
- `update_forward_refs()` → `model_rebuild()`
- `parse_raw` is deprecated; `model_validate_json` works like `parse_raw` for JSON, otherwise load the data then pass it to `model_validate`.
- `from_orm` is deprecated; use `model_validate` instead, as long as `from_attributes=True` is set in the model config.

## field-keyword-args
### Changes to `pydantic.Field`
The following properties have been removed from or changed in `Field`:
- `const` removed — use `typing.Literal` instead.
- `min_items` removed — use `min_length` instead.
- `max_items` removed — use `max_length` instead.
- `unique_items` removed.
- `allow_mutation` removed — use `frozen` instead (inverse meaning).
- `regex` removed — use `pattern` instead.
- `final` removed — use the `typing.Final` type hint instead.
Also: `Field` no longer accepts arbitrary keyword arguments for JSON schema; use `json_schema_extra`.

## config-changes
### Changes to config
In Pydantic V2, specify config with a class attribute `model_config = ConfigDict(...)`. The V1 pattern of a nested class called `Config` is deprecated.
Removed config keys: `allow_mutation` (use `frozen`), `error_msg_templates`, `fields`, `getter_dict`, `smart_union`, `underscore_attrs_are_private`, `json_loads`, `json_dumps`, `copy_on_model_validation`, `post_init_call`.
Renamed config keys:
- `allow_population_by_field_name` → `populate_by_name`
- `anystr_lower` → `str_to_lower`
- `anystr_strip_whitespace` → `str_strip_whitespace`
- `anystr_upper` → `str_to_upper`
- `keep_untouched` → `ignored_types`
- `max_anystr_length` → `str_max_length`
- `min_anystr_length` → `str_min_length`
- `orm_mode` → `from_attributes`
- `schema_extra` → `json_schema_extra`
- `validate_all` → `validate_default`

## validators
### Changes to validators
- `@validator` is deprecated; replace it with `@field_validator`. `@field_validator` requires an explicit `@classmethod` below the decorator when written with the `(cls, v)` signature.
- The `@field_validator` decorator does not have the `each_item` keyword argument; validate container items via `Annotated` metadata instead.
- The `config` and `field` keyword arguments on validator function signatures are removed; use `ValidationInfo` (`info.config`, `info.field_name`).
- `@root_validator` is deprecated; replace it with `@model_validator`, whose allowed signatures have changed (`mode='before'` receives a dict; `mode='after'` receives the model instance).
- A `TypeError` raised inside a validator is no longer converted into a `ValidationError`.

## settings-package
### `BaseSettings` moved
- `BaseSettings` and related settings tools have moved to the separate `pydantic-settings` package: `from pydantic_settings import BaseSettings`. Install it with `pip install pydantic-settings`.

## other-moves
### Other removals and moves
- `pydantic.generics.GenericModel` is removed; inherit `BaseModel, Generic[T]` directly instead.
- `@validate_arguments` is deprecated; use `@validate_call`.
- `constr(regex=...)`, `conlist(..., min_items=, max_items=)` use the same renamed kwargs as `Field` (`pattern`, `min_length`, `max_length`).
