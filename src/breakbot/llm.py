"""Optional LLM verifier.

The deterministic rules engine needs no API key and is what gates CI. This
module is an *additive* verifier: when an OpenAI-compatible endpoint is
configured (``BREAKBOT_LLM_BASE_URL`` / ``BREAKBOT_LLM_API_KEY`` /
``BREAKBOT_LLM_MODEL``), it asks the model to double-check medium-confidence
verdicts and to draft richer migration notes for the manual-fix findings.

If no endpoint is configured, or the request fails, everything degrades to the
rules-engine verdict unchanged — the LLM never gets the final say; it can only
annotate. This keeps the product trustworthy offline.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from .changelog import load_doc
from .models import Report, Verdict

SYSTEM_PROMPT = (
    "You are a senior Python migration engineer. You are given a call site in "
    "a codebase being upgraded, the changelog excerpt that a static analysis "
    "tool matched, and the tool's proposed verdict. Decide whether the verdict "
    "is correct. Respond with strict JSON only: "
    '{"agree": true|false, "confidence": "high|medium|low", '
    '"reasoning": "<one sentence>", "suggested_note": "<manual fix guidance or empty>"}.'
)


def llm_configured() -> bool:
    return bool(os.environ.get("BREAKBOT_LLM_API_KEY"))


def _call(model: str, messages: list[dict], timeout: float = 30.0) -> dict | None:
    base = os.environ.get("BREAKBOT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("BREAKBOT_LLM_API_KEY", "")
    payload = json.dumps({"model": model, "messages": messages,
                          "temperature": 0, "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return None


def verify_report(report: Report) -> Report:
    """Annotate medium/low-confidence verdicts with LLM reasoning in-place."""
    if not llm_configured():
        return report
    model = os.environ.get("BREAKBOT_LLM_MODEL", "gpt-4o-mini")
    for verdict in report.all_verdicts:
        if verdict.confidence.value == "high":
            continue
        cs = verdict.call_site
        if cs is None or not verdict.citations:
            continue
        citation = verdict.citations[0]
        doc = load_doc(citation.source)
        section_lines = doc.lines[doc._section_start(citation.section) or 0:
                                  doc._next_section_start(
                                      (doc._section_start(citation.section) or 0) + 1)]
        user = json.dumps({
            "call_site": {"file": cs.file, "line": cs.line,
                          "code": cs.snippet, "context": cs.context},
            "changelog_excerpt": "".join(section_lines)[:4000],
            "tool_verdict": {
                "severity": verdict.severity.value, "title": verdict.title,
                "detail": verdict.detail, "auto_fixable": verdict.auto_fixable,
            },
        }, indent=2)
        result = _call(model, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ])
        if result and isinstance(result, dict):
            note = result.get("suggested_note") or ""
            verdict.detail += (
                f"\n[LLM cross-check] confidence={result.get('confidence')} — "
                f"{result.get('reasoning', '')}{(' Suggestion: ' + note) if note else ''}"
            )
    return report
