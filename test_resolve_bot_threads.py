#!/usr/bin/env python3
"""Unit tests for resolve_bot_threads.py — classification and filtering logic.

Run with:
    cd ~/.claude/scripts/pr-automation && python3 -m pytest test_resolve_bot_threads.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers for building fixture thread dicts
# ---------------------------------------------------------------------------

def _make_thread(
    thread_id: str = "PRRT_abc123",
    is_resolved: bool = False,
    is_outdated: bool = False,
    path: str | None = None,
    line: int | None = None,
    author_login: str = "github-actions[bot]",
    body: str = "Some comment body.",
    created_at: str = "2026-04-10T00:00:00Z",
) -> dict:
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "path": path,
        "line": line,
        "diffSide": None,
        "startLine": None,
        "startDiffSide": None,
        "originalLine": None,
        "originalStartLine": None,
        "resolvedBy": None,
        "comments": {
            "nodes": [
                {
                    "id": "PRC_x1",
                    "body": body,
                    "createdAt": created_at,
                    "updatedAt": created_at,
                    "author": {"login": author_login},
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from resolve_bot_threads import (
    classify_thread,
    filter_and_classify,
    is_bot_author,
    load_bot_config,
)

# ---------------------------------------------------------------------------
# TestIsBotAuthor
# ---------------------------------------------------------------------------

class TestIsBotAuthor(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "bot_authors": ["github-actions[bot]", "gitleaks[bot]"],
            "bot_patterns": [r"\[bot\]$"],
        }

    def test_exact_match(self) -> None:
        self.assertTrue(is_bot_author("github-actions[bot]", self.config))

    def test_pattern_match(self) -> None:
        # Matches the \[bot\]$ pattern but is NOT in the exact list.
        self.assertTrue(is_bot_author("some-new-bot[bot]", self.config))

    def test_human_author_rejected(self) -> None:
        self.assertFalse(is_bot_author("orionarchitekton", self.config))

    def test_none_author_rejected(self) -> None:
        self.assertFalse(is_bot_author(None, self.config))


# ---------------------------------------------------------------------------
# TestClassifyThread
# ---------------------------------------------------------------------------

class TestClassifyThread(unittest.TestCase):
    def test_code_fix_with_path_and_actionable_body(self) -> None:
        thread = {"path": "src/factory.py", "line": 42, "isResolved": False, "body": "Missing null check"}
        self.assertEqual(classify_thread(thread), "code_fix")

    def test_informational_receipt(self) -> None:
        thread = {"path": None, "line": None, "isResolved": False, "body": "Review receipt emitted. No issues found."}
        self.assertEqual(classify_thread(thread), "informational")

    def test_weak_keywords_no_longer_informational(self) -> None:
        # 'summary' / 'all checks' / 'passed' / 'acknowledged' are too weak to
        # auto-resolve: a REAL finding can contain them ("Summary: X passed but Y
        # is broken"), and auto-resolving on them silently dismisses real findings.
        # Err safe -> code_fix; only unambiguous receipt phrases resolve.
        thread = {"path": None, "line": None, "isResolved": False, "body": "Summary: all checks passed. Acknowledged."}
        self.assertEqual(classify_thread(thread), "code_fix")

    def test_unambiguous_receipt_phrases_still_informational(self) -> None:
        for body in (
            "Automated receipt.",
            "No issues found.",
            "No findings.",
            "No action required.",
            "This is informational only.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "informational", body)

    def test_real_finding_with_weak_keyword_is_code_fix(self) -> None:
        # A pathless but REAL finding whose prose happens to contain a weak keyword
        # must NOT be auto-resolved.
        for body in (
            "Summary: the retry loop can spin forever.",
            "This passed CI but leaks a file handle.",
            "Acknowledged earlier, but the race is still open.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_receipt_or_informational_as_plain_word_is_code_fix(self) -> None:
        # Regression: "receipt" and "informational" used to be bare-substring
        # informational keywords, so a real pathless finding that merely USES those
        # words as ordinary nouns/adjectives was silently auto-resolved. They are now
        # anchored to receipt-context phrases; a real finding mentioning them must
        # default to code_fix so the thread stays open.
        for body in (
            "Receipt upload corrupts totals.",
            "This is not informational: token leak remains.",
            "Summary: receipt totals are wrong.",
            "The informational banner does not escape user input (XSS).",
            "A receipt is generated, but the amount is double-charged.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_receipt_phrase_inside_long_actionable_body_is_code_fix(self) -> None:
        # Even the multi-word receipt PHRASES ("no findings", "no action required",
        # "no issues found") must not auto-resolve a real finding that merely OPENS
        # with the phrase and continues with a diagnostic claim. Receipt-shape gate:
        # a diagnostic continuation marker (when/even/though/...) or a long body
        # keeps the thread open.
        for body in (
            "No findings are emitted when the structured parser fails closed.",
            "No action required is reported even when the merge gate is failing.",
            "No issues found is incorrectly printed even though tests fail.",
            "No findings but the race condition is still open.",
            "For your information, this silently drops the audit receipt and "
            "the totals are wrong.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_residual_failopen_terse_actionable_bodies_are_code_fix(self) -> None:
        # Adversarial review (v0.2.0): a continuation-marker DENYLIST is finite and
        # bypassable — a terse pathless body that opens with a receipt phrase and
        # uses a connector/verb NOT in the denylist still auto-resolved a REAL
        # finding (fail-open, the exact class this tool exists to prevent). The
        # classifier is fail-closed by an allowlist: any non-receipt content word
        # forces code_fix. These verified breaking inputs must all stay open.
        for body in (
            "No findings since v2 broke.",
            "No findings so merge is unsafe.",
            "No issues found and crashes.",
            "For your information, idempotency is broken.",
            "No findings: silent data corruption.",
            "for your information SQL injection here",
            "No action required immediately fix this.",
            "No findings; nonetheless deadlock here.",
            "No findings regardless of the leak.",
            "No findings.\n\nSQLi",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_failopen_nonascii_finding_after_receipt_is_code_fix(self) -> None:
        # Adversarial re-review (v0.2.0): an ASCII-only tokenizer cannot see a finding
        # written in a non-Latin script or with fullwidth homoglyphs, so an
        # "every word must be receipt vocab" check passes vacuously and auto-resolves
        # a real non-English finding. The content check must be Unicode-aware (and
        # NFKC-normalize homoglyphs). These all carry a real finding and must stay open.
        for body in (
            "No findings. Авторизация сломана",          # Russian: auth is broken
            "No findings. 注入漏洞",                       # Chinese: injection vuln
            "No findings. ثغرة أمنية",                    # Arabic: security hole
            "No action required. Данные удаляются",        # Russian: data is deleted
            "No findings. ＲＣＥ ｉｎ ａｕｔｈ",  # fullwidth 'RCE in auth'
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_failopen_vocab_only_negation_is_code_fix(self) -> None:
        # Adversarial re-review (v0.2.0): a flat bag-of-words allowlist admits its own
        # negation — "no action required" (receipt) and "action required" (the
        # opposite) share vocabulary, so a body built only from vocab tokens can still
        # ASSERT that action is required. Matching the receipt phrase as a UNIT and
        # rejecting any residual content word outside it closes this.
        for body in (
            "No findings. Action required.",
            "No issues found. Action required.",
            "Review required. No issues found.",
            "No findings. Review required.",
            "No action required. Review your findings.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "code_fix", body)

    def test_terse_receipt_bodies_still_informational(self) -> None:
        # The shape gate must NOT over-tighten: genuine terse receipts still resolve.
        for body in (
            "Automated receipt.",
            "No issues found.",
            "No findings.",
            "No action required.",
            "Review receipt emitted. No issues found.",
            "This is informational only.",
        ):
            thread = {"path": None, "line": None, "isResolved": False, "body": body}
            self.assertEqual(classify_thread(thread), "informational", body)

    def test_code_fix_with_path_but_vague_body(self) -> None:
        # Has a path → code_fix regardless of vague body.
        thread = {"path": "src/utils.py", "line": 7, "isResolved": False, "body": "Consider reviewing this section."}
        self.assertEqual(classify_thread(thread), "code_fix")

    def test_ambiguous_defaults_to_code_fix(self) -> None:
        # No path, no informational keywords → default code_fix.
        thread = {"path": None, "line": None, "isResolved": False, "body": "Something unusual detected."}
        self.assertEqual(classify_thread(thread), "code_fix")


# ---------------------------------------------------------------------------
# TestFilterAndClassify
# ---------------------------------------------------------------------------

class TestFilterAndClassify(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "bot_authors": ["github-actions[bot]"],
            "bot_patterns": [r"\[bot\]$"],
        }

    def test_filters_resolved_threads(self) -> None:
        resolved = _make_thread(thread_id="T1", is_resolved=True)
        unresolved = _make_thread(thread_id="T2", is_resolved=False)
        result = filter_and_classify([resolved, unresolved], self.config)
        ids = [t["id"] for t in result]
        self.assertNotIn("T1", ids)
        self.assertIn("T2", ids)

    def test_filters_human_authored_threads(self) -> None:
        human = _make_thread(thread_id="T3", author_login="orionarchitekton")
        bot = _make_thread(thread_id="T4", author_login="github-actions[bot]")
        result = filter_and_classify([human, bot], self.config)
        ids = [t["id"] for t in result]
        self.assertNotIn("T3", ids)
        self.assertIn("T4", ids)

    def test_codex_connector_thread_retained_with_real_config(self) -> None:
        # The Codex GitHub reviewer authors threads as 'chatgpt-codex-connector'.
        # With the shipped allowlist it must be retained (not dropped as human),
        # else an unresolved Codex P1 thread is invisible to the final-sweep
        # gate and MERGE_READY can be emitted with it still open (fail-open).
        config_path = Path(__file__).resolve().parent / "bot_authors.yaml"
        config = load_bot_config(config_path)
        codex = _make_thread(
            thread_id="TC", author_login="chatgpt-codex-connector", is_resolved=False
        )
        result = filter_and_classify([codex], config)
        self.assertIn("TC", [t["id"] for t in result])


# ---------------------------------------------------------------------------
# TestLoadBotConfig
# ---------------------------------------------------------------------------

class TestLoadBotConfig(unittest.TestCase):
    def test_loads_from_file(self) -> None:
        """Reads the actual bot_authors.yaml and checks expected entries."""
        config_path = Path(__file__).resolve().parent / "bot_authors.yaml"
        config = load_bot_config(config_path)
        self.assertIn("bot_authors", config)
        self.assertIn("bot_patterns", config)
        self.assertIn("github-actions[bot]", config["bot_authors"])

    def test_codex_connector_in_real_allowlist(self) -> None:
        """The Codex GitHub reviewer posts as 'chatgpt-codex-connector' (no [bot]
        suffix, not ^gemini-/^copilot-), so it must be recognized by the SHIPPED
        allowlist. Otherwise its unresolved review threads are dropped before the
        final-sweep remaining-threads count, and the gate can return MERGE_READY
        with an open Codex P1 thread (fail-open). Regression guard."""
        config_path = Path(__file__).resolve().parent / "bot_authors.yaml"
        config = load_bot_config(config_path)
        self.assertTrue(is_bot_author("chatgpt-codex-connector", config))

    def test_defaults_when_missing(self) -> None:
        """Nonexistent path returns sensible defaults (non-empty dicts/lists)."""
        config = load_bot_config(Path("/nonexistent/path/bot_authors.yaml"))
        self.assertIsInstance(config, dict)
        self.assertIn("bot_authors", config)
        self.assertIn("bot_patterns", config)
        # Sensible defaults: at minimum empty lists (not None, not crash).
        self.assertIsInstance(config["bot_authors"], list)
        self.assertIsInstance(config["bot_patterns"], list)


if __name__ == "__main__":
    unittest.main()
