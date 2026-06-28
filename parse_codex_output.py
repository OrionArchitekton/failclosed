#!/usr/bin/env python3
"""JSON-shape validator for Codex review output (spec §4 A5).

Returns a canonical telemetry dict; parse_path in
{strict_json, json_extracted, unparseable}. Markdown fallback retired.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# Map between severity names and P-levels for backward compatibility
SEVERITY_TO_PRIORITY = {
    "critical": "P0",
    "high": "P1",
    "medium": "P2",
    "low": "P3",
}

PRIORITY_TO_SEVERITY = {v: k for k, v in SEVERITY_TO_PRIORITY.items()}

SEVERITY_LEVELS = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PRIORITY_LEVELS = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


def parse_codex_review(output: str) -> Dict[str, Any]:
    """Validate Codex JSON output against the schema contract.

    Returns the canonical telemetry dict per spec §4 A5:
        {
            "verdict": str | None,
            "findings": list[dict],
            "parseable": bool,
            "parse_path": "strict_json" | "json_extracted" | "unparseable",
            "schema_valid": bool,
            "missing_fields": list[str],
            "parse_warnings": list[str],
        }

    Markdown fallback was retired in A5 — JSON issues now telemeter
    via parse_path='unparseable' rather than fall back to markdown.
    """
    if not output or not output.strip():
        return _unparseable_result(["empty output"])

    # Strict JSON: companion emits a single JSON object.
    strict = _try_parse_strict_json(output)
    if strict is not None:
        return strict

    # JSON extracted from surrounding text (less ideal; companion-emit drift).
    extracted = _try_extract_json(output)
    if extracted is not None:
        return extracted

    return _unparseable_result(["no parseable JSON object found"])


def _try_parse_strict_json(output: str) -> Optional[Dict[str, Any]]:
    """Try to parse the entire output as a single JSON object."""
    try:
        data = json.loads(output.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _validate_and_finalize(data, parse_path="strict_json")


_JSON_DECODER = json.JSONDecoder()


def _try_extract_json(output: str) -> Optional[Dict[str, Any]]:
    """Scan for the first valid JSON object in surrounding text.

    Uses json.JSONDecoder().raw_decode from each '{' position — quote-aware
    (braces inside strings don't confuse it) and retries the next candidate
    on failure. Signals companion-emit drift via parse_warnings.
    """
    pos = 0
    while True:
        start = output.find("{", pos)
        if start < 0:
            return None
        try:
            data, _end = _JSON_DECODER.raw_decode(output, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if not isinstance(data, dict):
            pos = start + 1
            continue
        result = _validate_and_finalize(data, parse_path="json_extracted")
        result["parse_warnings"].append(
            "JSON extracted from surrounding text; companion should emit strict JSON"
        )
        return result


def _validate_and_finalize(data: dict, *, parse_path: str) -> Dict[str, Any]:
    """Validate JSON shape against the contract; return canonical result."""
    missing: List[str] = []
    parse_warnings: List[str] = []

    if "verdict" not in data:
        missing.append("verdict")
    if "findings" not in data:
        missing.append("findings")

    raw_findings = data.get("findings") or []
    if not isinstance(raw_findings, list):
        parse_warnings.append("findings is not a list; treating as empty")
        raw_findings = []

    findings: List[Dict[str, Any]] = []
    for i, f in enumerate(raw_findings):
        if not isinstance(f, dict):
            parse_warnings.append(f"finding[{i}] is not a dict; skipped")
            continue
        # Per-finding required-key check
        if "severity" not in f:
            missing.append(f"finding[{i}].severity")
        if "file" not in f:
            missing.append(f"finding[{i}].file")
        if not f.get("body") and not f.get("title"):
            missing.append(f"finding[{i}].body_or_title")
        findings.append({
            "severity": f.get("severity", "medium"),
            "file": f.get("file"),
            "line_start": f.get("line_start"),
            "line_end": f.get("line_end"),
            "title": f.get("title", ""),
            "body": f.get("body", ""),
            "confidence": f.get("confidence"),
            "recommendation": f.get("recommendation", ""),
        })

    schema_valid = not missing
    return {
        "verdict": data.get("verdict"),
        "findings": findings,
        "parseable": True,
        "parse_path": parse_path,
        "schema_valid": schema_valid,
        "missing_fields": missing,
        "parse_warnings": parse_warnings,
    }


def _unparseable_result(parse_warnings: List[str]) -> Dict[str, Any]:
    return {
        "verdict": None,
        "findings": [],
        "parseable": False,
        "parse_path": "unparseable",
        "schema_valid": False,
        "missing_fields": [],
        "parse_warnings": parse_warnings,
    }


# ---------------------------------------------------------------------------
# Native standard review extractor (review --json emits rendered text in
# codex.stdout — runAppServerReview does NOT enforce an outputSchema).
# ---------------------------------------------------------------------------

_VERDICT_RE = re.compile(r"(?im)^verdict\s*:[ \t]*(.+?)[ \t]*$")
_FINDING_MARKER_RE = re.compile(
    r"(?im)^[-*]\s*\[(critical|high|medium|low|P[0-3])\]\s*"
    r"(?P<rest>.*?)$"
)

# codex CLI >=~0.13x native review renders '[P1] Title — abs/path:start-end'
# (P-level marker + trailing em-dash path:range, no Verdict line). Map
# P-levels onto the canonical severity enum.
_P_LEVEL_SEVERITY = {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}

# Trailing ' — path[:start[-end]]' layout (em-dash, en-dash, or ' -- ').
# Anchored at end-of-line; path must be path-shaped per _is_path_shaped.
_TRAILING_EMDASH_PATH_RE = re.compile(
    r"^(?P<title>.+?)\s+(?:[—–]|--)\s+"
    r"(?P<file>[\w./~-]+)"
    r"(?::(?P<line>\d+)(?:[:-](?P<line_end>\d+))?)?"
    r"[ \t.,;]*$"
)
# Path-shape rule: token is path-shaped iff it has '/' OR ends with .<2+ alphanumeric>.
# Backticked tokens skip this check (the backticks are the explicit caller signal).
_PATH_SHAPED_RE = re.compile(
    r"^(?:[\w./-]*/[\w./-]*"           # contains /
    r"|[\w./-]+\.[A-Za-z][A-Za-z0-9]+)$"  # ends with .ext (2+ alphanumeric)
)

# Leading path-or-backtick token, with optional :line(:col|-end) modifier,
# followed by space / common punctuation / EOL via lookahead.
_LEADING_PATH_RE = re.compile(
    r"^(?:`(?P<bt>[^`]+?)`"
    r"|(?P<bare>[\w./-]+))"
    r"(?::(?P<line>\d+)(?:[:-]\d+)?)?"
    r"(?=\s|[,)\]]|$)"
)

# Paren-wrapped (path:line) — only after a leading non-paren prefix (title).
# `_PAREN_AFTER_TEXT_RE` requires at least one non-`(` non-whitespace char
# before the paren so leading-paren-only rest stays unmatched (preserves
# Task 7's "parens-wrapped first-token isn't a file" behavior).
_PAREN_AFTER_TEXT_RE = re.compile(
    r"(?P<prefix>[^(\s][^(]*)"
    r"\((?P<file>[\w./-]+(?:/[\w./-]*|\.[A-Za-z][A-Za-z0-9]+))"
    r"(?::(?P<line>\d+)(?:[:-]\d+)?)?\)"
)


def _is_path_shaped(token: str) -> bool:
    """A token is path-shaped iff it has '/' OR ends with .<2+ alphanumeric>."""
    return bool(_PATH_SHAPED_RE.match(token))


def _split_file_from_body(rest: str) -> tuple[Optional[str], Optional[int], str]:
    """Extract a path-shaped file token + optional line number from the rest of a finding line.

    Strategy:
      1. Try a leading backticked token (always path-shaped per the caller's signal).
      2. Try a leading bare token; require it to be path-shaped.
      3. Try paren-wrapped (path:line) anywhere AFTER a non-paren prefix.
      4. Fall through: file=None, line=None, body=rest.

    Returns (file_or_none, line_or_none, body). Body is stripped of leading
    whitespace and common trailing punctuation after the path token.
    """
    m = _LEADING_PATH_RE.match(rest)
    if m:
        bt = m.group("bt")
        bare = m.group("bare")
        if bt is not None:
            file: Optional[str] = bt
        elif bare is not None and _is_path_shaped(bare):
            file = bare
        else:
            file = None
        if file is not None:
            line = int(m.group("line")) if m.group("line") else None
            # Strip leading whitespace and trailing punctuation that bordered the token.
            body = rest[m.end():].lstrip(" \t,)]")
            return file, line, body

    paren = _PAREN_AFTER_TEXT_RE.search(rest)
    if paren:
        file = paren.group("file")
        line = int(paren.group("line")) if paren.group("line") else None
        # Body = surrounding text minus the paren-wrapped reference, joined and stripped.
        body = (rest[:paren.start("prefix")] + paren.group("prefix") + rest[paren.end():]).strip()
        return file, line, body

    # codex >=~0.13x layout: 'Title — path:start-end' (path trails the title).
    emdash = _TRAILING_EMDASH_PATH_RE.match(rest)
    if emdash and _is_path_shaped(emdash.group("file")):
        line = int(emdash.group("line")) if emdash.group("line") else None
        return emdash.group("file"), line, emdash.group("title").strip()

    return None, None, rest.strip()


def parse_native_review_stdout(stdout: str) -> Dict[str, Any]:
    """Extract findings from native standard review's rendered text output.

    The companion's `review --json` emits {codex.stdout: rendered_text} —
    runAppServerReview does NOT enforce an outputSchema. This extractor
    handles that text by recognizing finding markers like:
        - [high] src/auth.py:42 Missing auth check
          The endpoint /admin lacks auth.

    Returns the canonical telemetry dict with parse_path='native_text'.
    A clean review (no finding markers) returns verdict='approve' if a
    'Verdict: approve' line is present, else verdict=None with
    schema_valid=False.

    Bounded to finding-marker recognition. NO broader markdown handling —
    arbitrary prose without finding markers + Verdict line becomes
    parse_warnings + schema_valid=False, NOT silent extraction.
    """
    if not stdout or not stdout.strip():
        return _unparseable_result(["empty native-review stdout"])

    verdict_match = _VERDICT_RE.search(stdout)
    # Normalize verdict to lowercase + strip surrounding quotes/punctuation so
    # the Step 2 elif's literal-set check is robust to LLM stylistic variations
    # ('Fix', 'FIX', '"fix"', 'fix.'). Round 7 adv-r7-01: case-sensitivity here
    # was a silent-MERGE_READY pathway when Codex emitted titlecase verdicts.
    if verdict_match:
        raw = verdict_match.group(1).strip()
        verdict = raw.strip("\"'.,;:").lower() or None
    else:
        verdict = None

    findings: List[Dict[str, Any]] = []
    for m in _FINDING_MARKER_RE.finditer(stdout):
        severity = m.group(1).lower()
        severity = _P_LEVEL_SEVERITY.get(severity, severity)
        rest = m.group("rest") or ""
        file, line, body = _split_file_from_body(rest)
        findings.append({
            "severity": severity,
            "file": file,
            "line_start": line,
            "line_end": None,
            "title": body[:120] if body else "",
            "body": body,
            "confidence": None,
            "recommendation": "",
        })

    parse_warnings: List[str] = []
    schema_valid = True
    missing_fields: List[str] = []

    # Step 1: resolve implied verdict BEFORE validating. Findings present without
    # an explicit 'Verdict:' line imply verdict='fix' (Codex emitted markers but
    # not the header — also covers markdown-decorated headers like '**Verdict**:'
    # that the strict regex misses). Resolving first means step 2's file-presence
    # check runs on the resolved state regardless of how verdict was determined.
    if verdict is None and findings:
        parse_warnings.append(
            "native-review stdout had findings but no Verdict: line"
        )
        verdict = "fix"  # implied by presence of finding markers

    # Step 2: validate verdict-vs-findings consistency.
    if verdict is None and not findings:
        # Total parse miss — no verdict, no findings. Fail closed.
        parse_warnings.append(
            "native-review stdout had no Verdict: line and no finding markers"
        )
        schema_valid = False
        missing_fields.append("verdict_or_findings")
    elif verdict in ("fix", "needs-attention") and not any(f.get("file") for f in findings):
        # Verdict implies actionable findings (explicit OR inferred), but parser
        # extracted none with a file. Contradictory state — pipeline must block
        # MERGE_READY. Closes Round 4 adversarial findings (leading-paren+path,
        # indented bullets, range+col) AND Round 6 inferred-verdict variant.
        parse_warnings.append(
            f"verdict={verdict!r} implies findings but parser extracted none with a file"
        )
        schema_valid = False
        missing_fields.append("findings_with_file")

    return {
        "verdict": verdict,
        "findings": findings,
        "parseable": True,
        "parse_path": "native_text",
        "schema_valid": schema_valid,
        "missing_fields": missing_fields,
        "parse_warnings": parse_warnings,
    }


# ---------------------------------------------------------------------------
# Filtering helpers (used by orchestrator)
# ---------------------------------------------------------------------------

def _severity_level(severity: str) -> int:
    """Convert severity string to numeric level."""
    return SEVERITY_LEVELS.get(severity.lower(), 99)


def _threshold_level(threshold: str) -> int:
    """Convert threshold (severity name or P-level) to numeric level."""
    if threshold.lower() in SEVERITY_LEVELS:
        return SEVERITY_LEVELS[threshold.lower()]
    if threshold.upper() in PRIORITY_LEVELS:
        p_level = PRIORITY_LEVELS[threshold.upper()]
        # P0=critical(0), P1=high(1), P2=medium(2)
        return p_level
    return 2  # default: medium


def findings_at_or_above(findings: List[Dict[str, Any]], threshold: str) -> List[Dict[str, Any]]:
    """Filter findings to those at or above the given severity/priority threshold.

    Accepts both severity names (critical, high, medium) and P-levels (P0, P1, P2).
    """
    max_level = _threshold_level(threshold)
    return [
        f for f in findings
        if _severity_level(f.get("severity", "low")) <= max_level
    ]


def fixable_findings(findings: List[Dict[str, Any]], threshold: str = "medium") -> List[Dict[str, Any]]:
    """Filter to findings with a file reference at or above threshold."""
    above = findings_at_or_above(findings, threshold)
    return [f for f in above if f.get("file") is not None]


def unfixable_findings(findings: List[Dict[str, Any]], threshold: str = "medium") -> List[Dict[str, Any]]:
    """Filter to findings without a file reference (design-level concerns)."""
    above = findings_at_or_above(findings, threshold)
    return [f for f in above if f.get("file") is None]
