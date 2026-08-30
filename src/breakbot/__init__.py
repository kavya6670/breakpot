"""BreakBot — dependency-upgrade impact analysis.

Point it at a repo and an upgrade (e.g. ``pydantic1-to-2``); it parses your
code, matches call sites against a changelog-derived rule base, and emits a
verdict per call site with the exact changelog lines each verdict cites —
plus an auto-generated fix patch for the mechanically-repairable findings.
"""

__version__ = "0.1.0"
