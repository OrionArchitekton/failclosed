#!/usr/bin/env python3
"""Fetch and classify unresolved bot-authored PR review threads.

Usage:
    python resolve_bot_threads.py [--repo PATH] [--pr NUMBER] [--config PATH] [--json]

Public API:
    load_bot_config(config_path) -> dict
    is_bot_author(author, config) -> bool
    classify_thread(thread) -> str   # "code_fix" | "informational" | "ci_block"
    filter_and_classify(threads, config) -> list
    run(repo_path, pr_number, config_path) -> dict
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

# fetch_comments is vendored alongside this module. The import is deferred to
# the call site inside run() so tests can mock it, and so a missing gh CLI
# surfaces at runtime rather than at import.

# ---------------------------------------------------------------------------
# Informational keyword list for classify_thread
# ---------------------------------------------------------------------------

_INFORMATIONAL_KEYWORDS = [
    "receipt",
    "summary",
    "acknowledged",
    "no issues found",
    "passed",
    "no findings",
    "all checks",
    "informational",
    "no action required",
]

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def load_bot_config(config_path: Optional[Path] = None) -> dict:
    """Load bot_authors.yaml.

    Performs minimal YAML parsing (no external deps): handles simple list
    blocks under scalar keys. Returns a dict with keys:
        bot_authors: list[str]
        bot_patterns: list[str]

    Falls back to sensible defaults if the file is missing or unreadable.
    """
    defaults: Dict[str, List[str]] = {"bot_authors": [], "bot_patterns": []}

    if config_path is None:
        config_path = Path(__file__).resolve().parent / "bot_authors.yaml"

    try:
        text = config_path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return defaults

    # Minimal YAML list parser: supports key: followed by "  - value" lines.
    result: Dict[str, List[str]] = {}
    current_key: Optional[str] = None

    for raw_line in text.splitlines():
        # Strip inline comments (everything after " #" that isn't inside quotes).
        line = raw_line.rstrip()

        # Skip blank lines and pure comment lines.
        if not line.strip() or line.strip().startswith("#"):
            continue

        # Detect a top-level key (no leading spaces, ends with colon).
        if not line.startswith(" ") and not line.startswith("\t") and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value or value.startswith("#"):
                # Key with no inline value → start collecting a list.
                result[key] = []
                current_key = key
            else:
                # Scalar key: value — store as single-element list.
                result[key] = [_strip_yaml_quotes(value)]
                current_key = key
            continue

        # Detect list items (leading spaces + "- ").
        stripped = line.lstrip()
        if stripped.startswith("- ") and current_key is not None:
            item = _strip_yaml_quotes(stripped[2:].strip())
            result.setdefault(current_key, []).append(item)
            continue

    # Ensure required keys exist.
    if "bot_authors" not in result:
        result["bot_authors"] = defaults["bot_authors"]
    if "bot_patterns" not in result:
        result["bot_patterns"] = defaults["bot_patterns"]

    return result


def _strip_yaml_quotes(value: str) -> str:
    """Remove surrounding single or double quotes from a YAML scalar value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    # Strip inline trailing comment.
    if " #" in value:
        value = value[: value.index(" #")].rstrip()
    return value


def is_bot_author(author: Optional[str], config: dict) -> bool:
    """Return True if author matches the bot allowlist (exact or pattern)."""
    if author is None:
        return False

    # Exact match first.
    if author in config.get("bot_authors", []):
        return True

    # Regex pattern match.
    for pattern in config.get("bot_patterns", []):
        try:
            if re.search(pattern, author):
                return True
        except re.error:
            pass

    return False


def classify_thread(thread: dict) -> str:
    """Classify a review thread dict as "code_fix", "informational", or "ci_block".

    Rules (in priority order):
    1. Has a non-None, non-empty path → "code_fix" (points at specific code).
    2. No path, body matches informational keywords → "informational".
    3. Default → "code_fix" (err on safe side).
    """
    path = thread.get("path")
    if path:
        return "code_fix"

    body = (thread.get("body") or "").lower()
    for keyword in _INFORMATIONAL_KEYWORDS:
        if keyword in body:
            return "informational"

    return "code_fix"


def filter_and_classify(threads: list, config: dict) -> list:
    """Filter to unresolved bot-authored threads and classify each.

    Returns a list of dicts with keys:
        id, classification, author, path, line, body, created_at, is_outdated
    """
    results = []

    for thread in threads:
        # Skip resolved threads.
        if thread.get("isResolved", False):
            continue

        # Determine author from first comment.
        comments_nodes = (thread.get("comments") or {}).get("nodes") or []
        if not comments_nodes:
            continue

        first_comment = comments_nodes[0]
        author_obj = first_comment.get("author") or {}
        author = author_obj.get("login")

        # Skip non-bot authors.
        if not is_bot_author(author, config):
            continue

        body = first_comment.get("body", "")

        # Build a classify-friendly dict (classify_thread reads path + body).
        classify_input = {
            "path": thread.get("path"),
            "line": thread.get("line"),
            "isResolved": thread.get("isResolved", False),
            "body": body,
        }
        classification = classify_thread(classify_input)

        results.append(
            {
                "id": thread.get("id"),
                "classification": classification,
                "author": author,
                "path": thread.get("path"),
                "line": thread.get("line"),
                "body": body,
                "created_at": first_comment.get("createdAt"),
                "is_outdated": thread.get("isOutdated", False),
            }
        )

    return results


def run(
    repo_path: str = ".",
    pr_number: Optional[int] = None,
    config_path: Optional[Path] = None,
) -> dict:
    """Main entry point. Fetches PR data and returns classified thread summary.

    Changes cwd to repo_path for gh CLI, restores afterwards.

    Returns:
        {
            "pr": {...},
            "threads": [...],
            "summary": {
                "total_unresolved": N,
                "code_fix": N,
                "informational": N,
                "ci_block": N,
            },
        }
    """
    from fetch_comments import _ensure_gh_authenticated, fetch_all, get_current_pr_ref

    _ensure_gh_authenticated()

    config = load_bot_config(config_path)

    orig_dir = os.getcwd()
    try:
        os.chdir(repo_path)
        if pr_number is not None:
            # Resolve owner/repo from the repo itself, not from a branch PR
            import subprocess as _sp
            _repo_info = json.loads(
                _sp.run(
                    ["gh", "repo", "view", "--json", "owner,name"],
                    capture_output=True, text=True, check=True,
                ).stdout
            )
            owner = _repo_info["owner"]["login"]
            repo = _repo_info["name"]
            number = int(pr_number)
        else:
            owner, repo, number = get_current_pr_ref()

        data = fetch_all(owner, repo, number)
    finally:
        os.chdir(orig_dir)

    all_threads = data.get("review_threads") or []
    classified = filter_and_classify(all_threads, config)

    summary: Dict[str, int] = {
        "total_unresolved": len(classified),
        "code_fix": 0,
        "informational": 0,
        "ci_block": 0,
    }
    for item in classified:
        c = item["classification"]
        if c in summary:
            summary[c] += 1

    return {
        "pr": data.get("pull_request", {}),
        "threads": classified,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and classify unresolved bot-authored PR review threads."
    )
    parser.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="Path inside the target git repo (default: current directory).",
    )
    parser.add_argument(
        "--pr",
        default=None,
        type=int,
        metavar="NUMBER",
        help="PR number to inspect (default: PR for current branch).",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to bot_authors.yaml (default: sibling file).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output instead of human-readable summary.",
    )
    return parser.parse_args()


def _human_summary(result: dict) -> None:
    pr = result["pr"]
    summary = result["summary"]
    threads = result["threads"]

    print(f"PR #{pr.get('number', '?')} — {pr.get('title', '(no title)')}")
    print(f"  URL: {pr.get('url', 'N/A')}")
    print()
    print("Bot thread summary:")
    print(f"  Total unresolved bot threads : {summary['total_unresolved']}")
    print(f"  code_fix                     : {summary['code_fix']}")
    print(f"  informational                : {summary['informational']}")
    print(f"  ci_block                     : {summary['ci_block']}")

    if threads:
        print()
        print("Threads:")
        for t in threads:
            outdated_tag = " [OUTDATED]" if t["is_outdated"] else ""
            path_info = f" @ {t['path']}:{t['line']}" if t["path"] else ""
            print(
                f"  [{t['classification'].upper()}]{outdated_tag}{path_info}"
                f" — {t['author']} — {t['id']}"
            )
            preview = (t["body"] or "")[:120].replace("\n", " ")
            print(f"    {preview}")


def main() -> None:
    args = _parse_args()
    config_path = Path(args.config) if args.config else None

    result = run(
        repo_path=args.repo,
        pr_number=args.pr,
        config_path=config_path,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _human_summary(result)


if __name__ == "__main__":
    main()
