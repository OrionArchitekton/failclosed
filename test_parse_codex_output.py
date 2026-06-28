#!/usr/bin/env python3
"""Tests for parse_codex_output.py — JSON-shape validator (spec §4 A5).

Markdown fallback was retired in A5; the parser now validates JSON-shape
contract and telemeters parse_path in {strict_json, json_extracted,
unparseable}.
"""

from __future__ import annotations

import json

from parse_codex_output import (
    findings_at_or_above,
    fixable_findings,
    parse_codex_review,
    parse_native_review_stdout,
    unfixable_findings,
)

# --- Sample structured JSON output (the canonical contract) ---

SAMPLE_JSON_REVIEW = json.dumps({
    "verdict": "needs-attention",
    "summary": "Found issues in the diff.",
    "findings": [
        {
            "severity": "critical",
            "title": "SQL injection vulnerability",
            "body": "User input passed directly to query string.",
            "file": "src/db/queries.py",
            "line_start": 42,
            "line_end": 45,
            "confidence": 0.95,
            "recommendation": "Use parameterized queries.",
        },
        {
            "severity": "low",
            "title": "Missing docstring",
            "body": "Public function lacks documentation.",
            "file": "src/api/routes.py",
            "line_start": 10,
            "line_end": 10,
            "confidence": 0.7,
            "recommendation": "Add a docstring.",
        },
    ],
    "next_steps": ["Fix SQL injection", "Add docs"],
})

SAMPLE_JSON_CLEAN = json.dumps({
    "verdict": "approve",
    "summary": "No issues found.",
    "findings": [],
    "next_steps": [],
})


class TestStrictJsonParse:
    def test_strict_json_parses_with_findings(self):
        result = parse_codex_review(SAMPLE_JSON_REVIEW)
        assert result["parseable"] is True
        assert result["parse_path"] == "strict_json"
        assert result["schema_valid"] is True
        assert result["missing_fields"] == []
        assert result["parse_warnings"] == []
        assert result["verdict"] == "needs-attention"
        assert len(result["findings"]) == 2

        f0 = result["findings"][0]
        assert f0["severity"] == "critical"
        assert f0["file"] == "src/db/queries.py"
        assert f0["line_start"] == 42
        assert f0["line_end"] == 45
        assert f0["confidence"] == 0.95
        assert f0["recommendation"] == "Use parameterized queries."

    def test_strict_json_clean_review(self):
        result = parse_codex_review(SAMPLE_JSON_CLEAN)
        assert result["parseable"] is True
        assert result["parse_path"] == "strict_json"
        assert result["schema_valid"] is True
        assert result["verdict"] == "approve"
        assert result["findings"] == []

    def test_strict_json_missing_top_level_verdict_marks_schema_invalid(self):
        """Missing verdict still parses (parseable=True) but schema_valid=False."""
        output = '{"findings": []}'
        result = parse_codex_review(output)
        assert result["parseable"] is True
        assert result["parse_path"] == "strict_json"
        assert result["schema_valid"] is False
        assert "verdict" in result["missing_fields"]
        assert result["verdict"] is None

    def test_strict_json_missing_top_level_findings_marks_schema_invalid(self):
        output = '{"verdict": "approve"}'
        result = parse_codex_review(output)
        assert result["parseable"] is True
        assert result["schema_valid"] is False
        assert "findings" in result["missing_fields"]
        assert result["findings"] == []

    def test_strict_json_per_finding_missing_severity_recorded(self):
        output = '{"verdict":"fix","findings":[{"file":"x.py","title":"t","body":"b"}]}'
        result = parse_codex_review(output)
        assert result["parseable"] is True
        assert result["schema_valid"] is False
        assert "finding[0].severity" in result["missing_fields"]

    def test_strict_json_per_finding_missing_file_recorded(self):
        output = '{"verdict":"fix","findings":[{"severity":"high","title":"t","body":"b"}]}'
        result = parse_codex_review(output)
        assert result["schema_valid"] is False
        assert "finding[0].file" in result["missing_fields"]

    def test_strict_json_per_finding_missing_body_and_title_recorded(self):
        output = '{"verdict":"fix","findings":[{"severity":"high","file":"x.py"}]}'
        result = parse_codex_review(output)
        assert result["schema_valid"] is False
        assert "finding[0].body_or_title" in result["missing_fields"]

    def test_strict_json_findings_not_list_warns_and_treats_as_empty(self):
        output = '{"verdict":"fix","findings":"not-a-list"}'
        result = parse_codex_review(output)
        assert result["parseable"] is True
        assert result["findings"] == []
        assert any("not a list" in w for w in result["parse_warnings"])

    def test_strict_json_non_dict_finding_skipped_with_warning(self):
        output = '{"verdict":"fix","findings":["not-a-dict",{"severity":"high","file":"x.py","body":"b"}]}'
        result = parse_codex_review(output)
        assert result["parseable"] is True
        assert len(result["findings"]) == 1
        assert any("not a dict" in w for w in result["parse_warnings"])


class TestJsonExtraction:
    def test_extracts_json_after_preamble_text(self):
        wrapped = f"Some preamble text\n\n{SAMPLE_JSON_REVIEW}\n\nTrailing text"
        result = parse_codex_review(wrapped)
        assert result["parseable"] is True
        assert result["parse_path"] == "json_extracted"
        assert result["verdict"] == "needs-attention"
        assert any(
            "JSON extracted from surrounding text" in w
            for w in result["parse_warnings"]
        )

    def test_extracts_json_inside_code_fence(self):
        wrapped = f"# Header\n\n```json\n{SAMPLE_JSON_REVIEW}\n```\n\nTrailing"
        result = parse_codex_review(wrapped)
        assert result["parseable"] is True
        assert result["parse_path"] == "json_extracted"
        assert result["verdict"] == "needs-attention"


class TestUnparseable:
    def test_empty_output(self):
        result = parse_codex_review("")
        assert result["parseable"] is False
        assert result["parse_path"] == "unparseable"
        assert result["verdict"] is None
        assert result["findings"] == []

    def test_whitespace_only_output(self):
        result = parse_codex_review("   \n\n  ")
        assert result["parseable"] is False
        assert result["parse_path"] == "unparseable"

    def test_garbage_prose_output(self):
        result = parse_codex_review("Error: Codex review engine unavailable.")
        assert result["parseable"] is False
        assert result["parse_path"] == "unparseable"
        assert result["findings"] == []

    def test_freeform_codex_review_no_json_is_unparseable(self):
        """A5 collapse: prose under '# Codex Review' header without JSON
        no longer parses as approve — it telemeters as unparseable.
        """
        prose = (
            "# Codex Review\n\n"
            "The change is a small refactor. I did not find a regression.\n"
        )
        result = parse_codex_review(prose)
        assert result["parseable"] is False
        assert result["parse_path"] == "unparseable"

    def test_top_level_json_array_is_unparseable(self):
        """A bare JSON array doesn't satisfy the contract (must be an object)."""
        result = parse_codex_review('[{"verdict":"approve"}]')
        # The bare array doesn't parse as strict (not a dict). Extraction will
        # find the inner object {"verdict":"approve"} and treat it as
        # json_extracted (verdict present, findings absent → schema_valid False).
        assert result["parse_path"] == "json_extracted"
        assert result["parseable"] is True
        assert result["schema_valid"] is False
        assert "findings" in result["missing_fields"]


class TestFindingFiltering:
    def test_fixable_vs_unfixable(self):
        """Findings with file are fixable; without file are not."""
        findings = [
            {"severity": "high", "file": "a.py", "line_start": 10},
            {"severity": "medium", "file": None, "line_start": None},
        ]
        assert len(fixable_findings(findings, "medium")) == 1
        assert len(unfixable_findings(findings, "medium")) == 1

    def test_filter_by_severity_threshold(self):
        findings = [
            {"severity": "critical", "file": "a.py"},
            {"severity": "high", "file": "b.py"},
            {"severity": "medium", "file": "c.py"},
            {"severity": "low", "file": "d.py"},
        ]
        assert len(findings_at_or_above(findings, "critical")) == 1
        assert len(findings_at_or_above(findings, "high")) == 2
        assert len(findings_at_or_above(findings, "medium")) == 3
        assert len(findings_at_or_above(findings, "low")) == 4

    def test_filter_by_priority_threshold_compat(self):
        """P-level thresholds still work for backward compat."""
        findings = [
            {"severity": "critical", "file": "a.py"},
            {"severity": "high", "file": "b.py"},
            {"severity": "medium", "file": "c.py"},
        ]
        # P0 = critical only, P1 = critical+high, P2 = critical+high+medium
        assert len(findings_at_or_above(findings, "P0")) == 1
        assert len(findings_at_or_above(findings, "P1")) == 2
        assert len(findings_at_or_above(findings, "P2")) == 3

    def test_fixable_findings_from_real_parse(self):
        result = parse_codex_review(SAMPLE_JSON_REVIEW)
        fixable = fixable_findings(result["findings"], "medium")
        # SAMPLE_JSON_REVIEW has critical + low; only critical is at/above medium
        assert len(fixable) == 1
        assert fixable[0]["severity"] == "critical"

        fixable_low = fixable_findings(result["findings"], "low")
        assert len(fixable_low) == 2


class TestNativeReviewParsing:
    """The native-text route (review --json emits rendered text, no outputSchema).

    codex CLI >=~0.13x renders findings as '[P1] Title — path:start-end' — a
    P-level severity marker and a trailing em-dash path:range, with no 'Verdict:'
    line. The parser must recognize this layout, or current Codex output is treated
    as unparseable and the gate never reaches a clean MERGE_READY.
    """

    def test_p_level_emdash_trailing_path(self):
        # The codex >=~0.13x layout: P-level marker + title + ' — path:start-end'.
        stdout = "- [P1] Missing auth check — src/auth.py:42-48"
        result = parse_native_review_stdout(stdout)
        assert result["parse_path"] == "native_text"
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["severity"] == "high"          # P1 -> high
        assert f["file"] == "src/auth.py"
        assert f["line_start"] == 42
        assert "Missing auth check" in f["title"]

    def test_p_level_severity_mapping(self):
        for marker, severity in (("P0", "critical"), ("P1", "high"),
                                 ("P2", "medium"), ("P3", "low")):
            stdout = f"- [{marker}] Some issue — pkg/mod.py:1"
            result = parse_native_review_stdout(stdout)
            assert len(result["findings"]) == 1, marker
            assert result["findings"][0]["severity"] == severity, marker
            assert result["findings"][0]["file"] == "pkg/mod.py", marker

    def test_legacy_severity_word_marker_still_parses(self):
        # Regression: the older '[high] path:line Title' layout must keep working.
        stdout = "- [high] src/db.py:7 SQL injection risk"
        result = parse_native_review_stdout(stdout)
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["severity"] == "high"
        assert f["file"] == "src/db.py"
        assert f["line_start"] == 7

    def test_p_level_finding_without_file_fails_closed(self):
        # A P-level finding (verdict implied 'fix') whose parser extracts no file is
        # a contradictory state -> schema_valid False so the gate blocks MERGE_READY.
        stdout = "- [P1] A design concern with no file reference"
        result = parse_native_review_stdout(stdout)
        assert result["verdict"] == "fix"
        assert result["schema_valid"] is False
        assert "findings_with_file" in result["missing_fields"]
