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

    def test_informational_summary(self) -> None:
        thread = {"path": None, "line": None, "isResolved": False, "body": "Summary: all checks passed. Acknowledged."}
        self.assertEqual(classify_thread(thread), "informational")

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
