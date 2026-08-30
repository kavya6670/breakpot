"""Changelog access.

BreakBot bundles curated, line-numbered excerpts of the official migration
guides in ``src/breakbot/data/``.  Every verdict cites the exact line it was
derived from — that is the product's credibility feature.  A live fetch from
the canonical docs URL can optionally refresh the bundle context, but scans
work fully offline.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from importlib import resources

from .models import Citation

CANONICAL_URLS = {
    "pydantic-1-to-2": "https://docs.pydantic.dev/latest/migration/",
    "sqlalchemy-1.4-to-2.0": "https://docs.sqlalchemy.org/en/20/changelog/migration_20.html",
}


@dataclass
class ChangelogDoc:
    source_id: str
    title: str
    lines: list[str]

    @property
    def url(self) -> str:
        return CANONICAL_URLS.get(self.source_id, "")

    def line(self, n: int) -> str:
        """1-based line accessor."""
        return self.lines[n - 1].rstrip("\n")

    def cite(self, section: str, quote_needle: str) -> Citation:
        """Find the first line in ``section`` containing ``quote_needle``.

        Falls back to the section heading if the needle cannot be located, so
        a citation is always produced.
        """
        start = self._section_start(section)
        if start is None:
            start, end = 0, len(self.lines)
        else:
            end = self._next_section_start(start + 1)
        needle = quote_needle.strip().lower()
        # Try the precise needle first; on miss, progressively loosen:
        # strip backticks, then trailing parens (so "`json()`" matches the
        # `json()` bullet and not the `json_schema()` one above it).
        candidates = [
            needle,
            needle.replace("`", ""),
            needle.replace("`", "").rstrip("()"),
        ]
        for cand in candidates:
            if not cand:
                continue
            for idx in range(start, end):
                if cand in self.lines[idx].lower():
                    return Citation(
                        source=self.source_id,
                        section=section,
                        line=idx + 1,
                        quote=self.lines[idx].strip(),
                        url=self.url,
                    )
        # Fallback: cite the heading line itself.
        return Citation(
            source=self.source_id,
            section=section,
            line=(start + 1) if start is not None else 1,
            quote=self.lines[start].strip() if start < len(self.lines) else section,
            url=self.url,
        )

    def _section_start(self, anchor: str) -> int | None:
        pat = re.compile(rf"^#+\s+<!--\s*.*?-->\s*$|^#+\s+.*{re.escape(anchor)}", re.I)
        for i, line in enumerate(self.lines):
            if line.startswith("## ") and anchor in line:
                return i
        return None

    def _next_section_start(self, after: int) -> int:
        for i in range(after, len(self.lines)):
            if self.lines[i].startswith("## "):
                return i
        return len(self.lines)


@functools.lru_cache(maxsize=None)
def load_doc(source_id: str) -> ChangelogDoc:
    fname = f"{source_id}.md"
    try:
        text = resources.files("breakbot.data").joinpath(fname).read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        # Editable installs / fallback path.
        from pathlib import Path

        path = Path(__file__).parent / "data" / fname
        text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    title = next(
        (l.lstrip("# ").strip() for l in lines if l.startswith("# ")), source_id
    )
    return ChangelogDoc(source_id=source_id, title=title, lines=lines)
