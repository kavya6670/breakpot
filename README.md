# 🤖 BreakBot

> **"npm audit tells you what's vulnerable. Nothing tells you what breaks."**

Everyone freezes dependencies at old versions because upgrading is terrifying.
Dependabot opens the PR, CI goes red, you close it. The information needed to
upgrade safely exists — changelogs, migration guides, your own call sites — but
no human wants to cross-reference them.

**BreakBot does the cross-referencing.** Give it a repo and an upgrade (e.g.
*"upgrade pydantic 1.10 → 2.x"*). It parses your code with the AST, fetches the
changelog, finds every place *your* code touches a changed API, and emits a
verdict per call site — **each verdict citing the exact changelog line it is
based on** — then generates a reviewed, ready-to-apply fix patch.

```text
✗ app/models.py:19   Field(regex=) removed → use pattern=
✗ app/models.py:10   GenericModel removed → (BaseModel, Generic[T])
⚠ app/models.py:22   class Config deprecated → model_config = ConfigDict(...)
⚠ app/api.py:33      .dict() renamed to .model_dump()
   ↳ cited: pydantic-1-to-2.md:12 — docs.pydantic.dev/latest/migration/
✓ 18 other usages unaffected

[Generate fix PR] → patched unified diff → tests go green
```

## Why this isn't grep

Finding `\.dict\(\)` is not the hard part — knowing **which `.dict()` calls are
on pydantic models**, whether the receiver was built by a constructor or
returned from an annotated factory, which `@validator` is a pydantic validator
versus some other decorator, and which **changelog statement** licenses each
verdict — that's reasoning over code *and* docs. BreakBot's ingestion layer
builds a type-aware model of the repo (imports, class hierarchies, call sites,
decorators, annotations) and its rules engine reasons against a
changelog-grounded knowledge base. The result: verdicts you can trust because
each one links back to the changelog.

## Scope (honest)

- **One language:** Python 3.10+
- **Two famous breaking upgrades**, the ones every dev team has postponed:
  - **Pydantic 1.x → 2.x** (`pydantic1-to-2`)
  - **SQLAlchemy 1.4 → 2.0** (`sqlalchemy1.4-to-2.0`)
- Auto-fixes cover the mechanically-repairable breakages; genuinely semantic
  migrations (e.g. `@root_validator` → `@model_validator(mode=...)`) are
  flagged as manual with the migration guidance attached.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[demo]"     # flask + pydantic v2 + sqlalchemy v2 + pytest
```

## The 2-minute demo arc

```bash
breakbot demo
```

This copies the bundled **legacy app** (a pydantic-v1 / SQLAlchemy-1.4 style
service with its own pytest suite) to a temp dir and runs the full arc:

1. **Red impact report** — 15+ verdicts across the app, each changelog-cited.
2. **Tests RED before the patch** — the legacy suite fails on pydantic 2 /
   SQLAlchemy 2.0 (it literally raises `PydanticUserError: 'regex' is removed`).
3. **BreakBot generates the fix patch** — unified diff, originals backed up
   under `.breakbot-work/backup/`.
4. **Tests GREEN after the patch** — `19 passed`.

### Web report (the full demo)

```bash
breakbot web --port 8000
# open the preview → click "Analyze impact" (bundled demo app)
# → read the red report with per-line changelog citations
# → click "Generate fix PR" → review the unified diff
# → click "Run test suite" → GREEN
```

### CLI

```bash
breakbot list                                 # supported upgrades
breakbot analyze ./my-repo --upgrade pydantic1-to-2            # terminal report
breakbot analyze ./my-repo --upgrade pydantic1-to-2 --html r.html
breakbot analyze ./my-repo --upgrade pydantic1-to-2 --json > report.json
breakbot fix ./my-repo --upgrade pydantic1-to-2 --test         # apply + run pytest
```

## What BreakBot detects

**Pydantic 1 → 2:** renamed `BaseModel` methods (`.dict()→.model_dump()`,
`.parse_obj()→.model_validate()`, `.from_orm()`, `.json()`, `.schema()`,
`.copy()`, `.parse_raw()/.parse_file()`, `__fields__`…), `Field()` keyword
removals/renames (`regex→pattern`, `min_items→min_length`, `const`,
`allow_mutation→frozen`), the `Config` class →
`model_config = ConfigDict(...)` (including removed/renamed keys that
*silently* stop working, e.g. `orm_mode` — a ✗ silent-breakage finding),
`@validator/@root_validator` → `@field_validator/@model_validator`,
`BaseSettings` moving to `pydantic-settings`, and `GenericModel` removal.

**SQLAlchemy 1.4 → 2.0:** `Engine.execute()` removal (connectionless
execution), `MetaData(bind=…)` removal, raw-string SQL needing `text()`,
legacy `select([cols])` calling style, `Query.get()` → `Session.get()`,
`sessionmaker(autocommit=True)` removal, `declarative_base()` moving to
`sqlalchemy.orm`, and legacy `Session.query()` usage.

Every severity rating (✗ breaks / ⚠ warning / ✓ unaffected) was empirically
verified against the actual installed pydantic 2.x / SQLAlchemy 2.0 — see
`src/breakbot/data/*.md` for the curated, line-numbered changelog excerpts the
verdicts cite.

## Architecture

```
src/breakbot/
  ingest.py        # AST repo walker: imports, model classes, call sites, roles
  changelog.py     # bundled line-numbered migration-guide excerpts + citation
  knowledge/
    pydantic_v2.py    # rules: match call sites → findings, severities
    sqlalchemy_v2.py
  engine.py        # orchestrates ingest → rules → cited verdicts
  codemod.py       # span-based source transforms + import rewrite post-pass
  report.py        # terminal / JSON / HTML rendering
  web.py           # Flask UI: analyze → Generate fix PR → run tests
  cli.py           # breakbot {list,analyze,fix,web,demo}
  demo/legacy_app/ # the frozen-era demo service + its pytest suite
tests/             # detection, codemod (executes generated code!), e2e
```

### The LLM hook

The deterministic rules engine ships complete and needs no API key — that is
what makes it trustworthy enough to gate CI on. An optional LLM verifier hook
(`llm.py`) can be pointed at any OpenAI-compatible endpoint to re-reason the
tricky verdicts (the ones that are `confidence=medium`, e.g. method calls
whose receiver type wasn't statically inferrable) and to draft the manual
migration notes; it is strictly additive and degrades to the rules engine
when no key is configured.

## Test suite

```bash
pytest                    # all tests
pytest -m "not slow"      # fast unit tests (e2e demo tests spawn subprocesses)
```

Tests include asserting the *generated patch actually executes correctly under
real pydantic 2 / SQLAlchemy 2* — not just that it contains the right string.
