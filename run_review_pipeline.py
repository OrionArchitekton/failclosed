#!/usr/bin/env python3
"""Post-push review pipeline orchestrator (fail-closed PR review gate).

Reads a small context JSON and executes the review pipeline:
  Phase 1: Bot thread resolution (with configurable delay)
  Phase 2: LLM standard review
  Phase 3: LLM adversarial review
  Phase 4: Re-review loop (driven by the calling Agent; we emit fix_requests)
  Phase 5: Final comment sweep + status report

Deterministic parts (run the reviewer command, parse output, resolve threads)
happen here. AI-dependent parts (read file, fix code) are reported as
structured fix requests for the calling Agent to execute.

The VALUE of this tool is the fail-closed gate: the reviewer output is fed
through a hardened multi-shape parser (parse_codex_output) and the pipeline
REFUSES to report MERGE_READY when the output is unparseable, schema-invalid,
or self-contradictory (e.g. verdict='fix' but no finding references a file).

Reviewer provider seam (stdin -> stdout contract)
--------------------------------------------------
The reviewer is invoked as a configurable subprocess command
(``reviewer_command``, a shell command string or argv list). The pipeline:
  - writes the review prompt to the command's STDIN, and
  - reads the reviewer's output from the command's STDOUT.
The captured stdout is handed unchanged to the parser functions in
``parse_codex_output``. Any executable that honors this stdin->stdout contract
can be plugged in; the default merely shells out to a generic ``codex exec``.

Usage:
    python run_review_pipeline.py [--context PATH] [--skip-delay] [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Resolve sibling imports
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from graphql_helpers import reply_and_resolve
from parse_codex_output import (
    _unparseable_result,
    fixable_findings,
    parse_codex_review,
    parse_native_review_stdout,
    unfixable_findings,
)
from resolve_bot_threads import run as resolve_bot_threads_run

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Self-contained defaults. No file outside this repo is required to run.
# An optional `failclosed.toml` next to this script may override any of them.
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "failclosed.toml"

# Branches treated as trunk; pushes to these are skipped (not PR branches).
DEFAULT_TRUNK_BRANCHES = ["main", "master"]

# Reviewer provider seam. The command receives the review prompt on stdin and
# returns the reviewer output on stdout. Override via failclosed.toml. The
# default references a generic `codex exec`; no private plugin path is baked in.
DEFAULT_REVIEWER_COMMAND = "codex exec --json -"

# Seconds to wait for bot reviews to land before phase 1.
DEFAULT_BOT_WAIT_SECONDS = 240

# Severity/priority threshold: findings at or above this are auto-fixed.
# "P2" == medium in parse_codex_output's threshold vocabulary.
DEFAULT_FIX_THRESHOLD = "P2"

# Working directory for temporary review worktrees.
DEFAULT_WORKTREE_DIR = "/tmp"

DEFAULTS: Dict[str, Any] = {
    "trunk_branches": DEFAULT_TRUNK_BRANCHES,
    "reviewer_command": DEFAULT_REVIEWER_COMMAND,
    "bot_wait_seconds": DEFAULT_BOT_WAIT_SECONDS,
    "fix_threshold": DEFAULT_FIX_THRESHOLD,
    "worktree_dir": DEFAULT_WORKTREE_DIR,
    "reviewer_timeout_seconds": 600,
    "adversarial_review": True,
    "min_thread_age_minutes": 30,
    "max_fix_cycles": 1,
}


def _load_overrides(path: Path) -> dict:
    """Load optional config overrides from a TOML file.

    Returns {} if the file is missing or the stdlib lacks tomllib (py<3.11).
    Only the keys present in DEFAULTS are honored; unknown keys are ignored.
    """
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in DEFAULTS}


def load_config(config_path: Optional[Path] = None) -> dict:
    """Build the runtime config: inline defaults overlaid with optional TOML.

    Self-contained: requires no file outside this repo. If `failclosed.toml`
    is absent, the inline DEFAULTS are used as-is.
    """
    cfg = dict(DEFAULTS)
    cfg.update(_load_overrides(config_path or DEFAULT_CONFIG_PATH))

    # Normalize types for the seam-critical fields.
    if isinstance(cfg["trunk_branches"], str):
        cfg["trunk_branches"] = [
            b.strip() for b in cfg["trunk_branches"].split(",") if b.strip()
        ]
    cfg["bot_wait_seconds"] = int(cfg["bot_wait_seconds"])
    cfg["reviewer_timeout_seconds"] = int(cfg["reviewer_timeout_seconds"])
    cfg["min_thread_age_minutes"] = int(cfg["min_thread_age_minutes"])
    cfg["max_fix_cycles"] = int(cfg["max_fix_cycles"])
    cfg["adversarial_review"] = bool(cfg["adversarial_review"])
    return cfg


def _reviewer_argv(reviewer_command: Any) -> list:
    """Normalize the configured reviewer_command to an argv list."""
    if isinstance(reviewer_command, (list, tuple)):
        return [str(x) for x in reviewer_command]
    return shlex.split(str(reviewer_command))


# ---------------------------------------------------------------------------
# PR number resolution
# ---------------------------------------------------------------------------

def resolve_pr_number(repo_root: str, branch: str) -> Optional[int]:
    """Resolve the PR number for a branch using gh CLI.

    Uses `gh pr list --head <branch>` which works regardless of which branch
    is currently checked out locally.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--json", "number", "--limit", "1"],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if result.returncode != 0:
            print(f"[resolve_pr] gh pr list failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        prs = json.loads(result.stdout)
        if prs:
            return int(prs[0]["number"])
    except Exception as e:
        print(f"[resolve_pr] Error: {e}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _seconds_since_push(pushed_at: str) -> Optional[int]:
    """Parse ISO 8601 UTC timestamp and return seconds elapsed, or None."""
    if not pushed_at:
        return None
    try:
        from datetime import datetime, timezone
        ts = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return None


def phase1_bot_threads(
    repo_root: str,
    branch: str,
    config: dict,
    pr_number: Optional[int] = None,
    skip_delay: bool = False,
    dry_run: bool = False,
) -> dict:
    """Phase 1: Resolve bot-authored review threads."""
    bot_wait = config["bot_wait_seconds"]
    if not skip_delay:
        # Wait for bot reviews to land.
        print(f"[phase1] Waiting {bot_wait}s for bot reviews to land...", file=sys.stderr)
        time.sleep(bot_wait)

    print("[phase1] Fetching bot threads...", file=sys.stderr)
    try:
        result = resolve_bot_threads_run(
            repo_path=repo_root,
            pr_number=pr_number,
        )
    except Exception as e:
        return {"status": "error", "error": str(e), "threads": []}

    threads = result.get("threads", [])
    code_fix_threads = [t for t in threads if t["classification"] == "code_fix"]
    info_threads = [t for t in threads if t["classification"] == "informational"]

    return {
        "status": "ok",
        "pr": result.get("pr", {}),
        "summary": result.get("summary", {}),
        "code_fix_threads": code_fix_threads,
        "informational_threads": info_threads,
    }


def _find_existing_worktree(repo_root: str, branch: str) -> Optional[str]:
    """Check if the branch already has a worktree checked out somewhere."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=repo_root,
        )
        worktree_path = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                worktree_path = line[len("worktree "):]
            elif line.startswith("branch refs/heads/") and worktree_path:
                wt_branch = line[len("branch refs/heads/"):]
                if wt_branch == branch:
                    return worktree_path
                worktree_path = None
    except Exception:
        pass
    return None


def _ensure_on_branch(repo_root: str, branch: str, worktree_dir: str) -> Optional[str]:
    """Ensure we can run the reviewer from the right branch.

    Returns the effective cwd to use (existing worktree or new temp worktree),
    or None if already on the right branch / unable to create a worktree.
    """
    # Check if we're already on the branch
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=repo_root,
        )
        if result.stdout.strip() == branch:
            return None
    except Exception:
        pass

    # Check for existing worktree with this branch
    existing = _find_existing_worktree(repo_root, branch)
    if existing:
        print(f"[review] Using existing worktree at {existing} for branch {branch}", file=sys.stderr)
        return existing

    # Create temporary worktree
    worktree_path = str(Path(worktree_dir) / f"review-worktree-{branch.replace('/', '-')}")
    subprocess.run(["git", "worktree", "remove", "--force", worktree_path],
                   capture_output=True, cwd=repo_root)
    result = subprocess.run(
        ["git", "worktree", "add", worktree_path, branch],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.returncode != 0:
        subprocess.run(["git", "fetch", "origin", branch], capture_output=True, cwd=repo_root)
        result = subprocess.run(
            ["git", "worktree", "add", worktree_path, branch],
            capture_output=True, text=True, cwd=repo_root,
        )
        if result.returncode != 0:
            print(f"[review] Failed to create worktree: {result.stderr.strip()}", file=sys.stderr)
            return None

    print(f"[review] Created temp worktree at {worktree_path} for branch {branch}", file=sys.stderr)
    return worktree_path


def _cleanup_worktree(repo_root: str, worktree_path: str, worktree_dir: str) -> None:
    """Remove a temporary review worktree (only ones we created, not user worktrees)."""
    temp_prefix = str(Path(worktree_dir) / "review-worktree-")
    if not worktree_path.startswith(temp_prefix):
        return  # Don't clean up user's existing worktrees
    try:
        subprocess.run(["git", "worktree", "remove", "--force", worktree_path],
                       capture_output=True, cwd=repo_root)
        print(f"[review] Cleaned up temp worktree {worktree_path}", file=sys.stderr)
    except Exception:
        pass


def _build_review_prompt(review_type: str, branch: str) -> str:
    """Build the review prompt fed to the reviewer command on stdin.

    review_type is "review" (standard) or "adversarial-review". The reviewer
    is expected to emit findings that parse_codex_output can consume.
    """
    scope = f"the diff on branch {branch}" if branch else "the current changes"
    if review_type == "adversarial-review":
        return (
            f"Adversarially review {scope}. Actively try to break the change: "
            f"hunt for correctness bugs, edge cases, regressions, security and "
            f"data-integrity hazards. Report findings with file, line, severity, "
            f"and a recommendation. Emit a verdict of 'pass' or 'fix'."
        )
    return (
        f"Review {scope} for correctness bugs, regressions, and quality issues. "
        f"Report findings with file, line, severity, and a recommendation. "
        f"Emit a verdict of 'pass' or 'fix'."
    )


def phase2_review(
    repo_root: str,
    config: dict,
    branch: str = "",
    review_type: str = "review",
    dry_run: bool = False,
) -> dict:
    """Phase 2/3: Run the standard or adversarial reviewer through the seam.

    Invokes the configured `reviewer_command`, feeding the review prompt on
    stdin and capturing stdout, then runs the captured stdout through the
    fail-closed parser. The fail-closed gate (load-bearing) refuses to treat
    parseable-but-schema-invalid or unparseable output as a clean review.
    """
    if review_type == "adversarial-review" and not config["adversarial_review"]:
        return {"status": "skipped", "reason": "adversarial_review is disabled"}

    argv = _reviewer_argv(config["reviewer_command"])
    if not argv:
        return {"status": "skipped", "reason": "reviewer_command is empty"}

    worktree_dir = config["worktree_dir"]

    # Ensure we're on the PR branch (create worktree if needed)
    worktree_path = None
    effective_cwd = repo_root
    if branch:
        worktree_path = _ensure_on_branch(repo_root, branch, worktree_dir)
        if worktree_path:
            effective_cwd = worktree_path

    prompt = _build_review_prompt(review_type, branch)

    print(f"[phase2] Running reviewer: {' '.join(argv)} (cwd: {effective_cwd})", file=sys.stderr)
    if dry_run:
        if worktree_path:
            _cleanup_worktree(repo_root, worktree_path, worktree_dir)
        return {"status": "dry_run", "command": argv}

    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=config["reviewer_timeout_seconds"],
            cwd=effective_cwd,
        )
    except subprocess.TimeoutExpired:
        if worktree_path:
            _cleanup_worktree(repo_root, worktree_path, worktree_dir)
        return {"status": "timeout", "timeout_seconds": config["reviewer_timeout_seconds"]}
    except Exception as e:
        if worktree_path:
            _cleanup_worktree(repo_root, worktree_path, worktree_dir)
        return {"status": "error", "error": str(e)}
    finally:
        if worktree_path:
            _cleanup_worktree(repo_root, worktree_path, worktree_dir)

    output = result.stdout
    raw_output_hash = hashlib.sha256(
        output.encode("utf-8", errors="replace")
    ).hexdigest()[:16]

    # The reviewer output may be structured JSON or rendered text. Try the
    # structured parser first; if it produces nothing parseable, fall back to
    # the native (rendered-text) parser. Either way the fail-closed gate below
    # decides whether the result is clean.
    parsed = parse_codex_review(output)
    if not parsed["parseable"]:
        native = parse_native_review_stdout(output)
        if native["parseable"]:
            parsed = native
        else:
            # Keep the structured-parse warnings; ensure a clean unparseable shape.
            parsed = _unparseable_result(parsed.get("parse_warnings", []))

    # Fail-closed gate (load-bearing): any parseable-but-schema-invalid
    # result is NOT a clean review. Without this gate, the orchestrator
    # could falsely emit MERGE_READY when the underlying reviewer output
    # was unparseable OR when the parser flagged a contradictory state
    # (e.g., verdict='fix' AND no finding has a file). Runs BEFORE the
    # parseable check so that ok-shape-but-invalid results fail closed
    # regardless of whether the findings list is empty.
    if parsed["parseable"] and not parsed["schema_valid"]:
        return {
            "status": "parse_error",
            "raw_output": output[:2000],
            "stderr": result.stderr[:1000] if result.stderr else "",
            "parsed_review": parsed,
            "raw_output_hash": raw_output_hash,
        }

    if not parsed["parseable"]:
        return {
            "status": "parse_error",
            "raw_output": output[:2000],
            "stderr": result.stderr[:1000] if result.stderr else "",
            "parsed_review": parsed,
            "raw_output_hash": raw_output_hash,
        }

    threshold = config["fix_threshold"]
    fixable = fixable_findings(parsed["findings"], threshold)
    unfixable = unfixable_findings(parsed["findings"], threshold)

    return {
        "status": "ok",
        "verdict": parsed["verdict"],
        "total_findings": len(parsed["findings"]),
        "fixable_findings": fixable,
        "unfixable_findings": unfixable,
        "raw_output": output[:3000],
        "parsed_review": parsed,
        "raw_output_hash": raw_output_hash,
    }


def phase5_final_sweep(
    repo_root: str,
    pr_number: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Phase 5: Final bot thread sweep after all fixes."""
    print("[phase5] Running final bot thread sweep...", file=sys.stderr)
    try:
        result = resolve_bot_threads_run(
            repo_path=repo_root,
            pr_number=pr_number,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}

    threads = result.get("threads", [])
    return {
        "status": "ok",
        "remaining_threads": len(threads),
        "threads": threads,
        "summary": result.get("summary", {}),
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def determine_status(
    phase1: dict,
    findings_remain: bool,
    phase5: dict,
    review_phase_failed: bool = False,
) -> str:
    """Determine final pipeline status.

    review_phase_failed: True when any review phase has status in
    {"parse_error", "timeout"}. The phase-level fail-closed gate would
    otherwise be inert — a parse-failed phase produces zero fix_requests,
    so findings_remain stays False, and the pipeline would falsely
    report MERGE_READY.
    """
    remaining = phase5.get("remaining_threads", 0)
    if review_phase_failed:
        return "FINDINGS_REMAIN"
    if remaining == 0 and not findings_remain:
        return "MERGE_READY"
    if findings_remain:
        return "FINDINGS_REMAIN"
    if remaining > 0:
        return "THREADS_UNRESOLVED"
    return "MERGE_READY"


def run_pipeline(
    context_path: str,
    skip_delay: bool = False,
    dry_run: bool = False,
    non_blocking: bool = False,
) -> dict:
    """Execute the full pipeline. Returns a structured result.

    When non_blocking=True and bots have not had time to land
    (now - pushed_at < bot_wait_seconds), returns immediately with
    {"status": "BOTS_PENDING", "retry_after_seconds": <remaining>}
    instead of sleeping. Caller should reschedule and re-invoke.
    """

    config = load_config()

    # Load context
    try:
        with open(context_path) as f:
            ctx = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {"status": "error", "error": f"Cannot read context: {e}"}

    repo_root = ctx["repo_root"]
    branch = ctx["branch"]
    sha = ctx["sha"]
    pushed_at = ctx.get("pushed_at", "")

    bot_wait = config["bot_wait_seconds"]

    # Skip if pushing to a default/trunk branch
    if branch in config["trunk_branches"]:
        return {"status": "skipped", "reason": f"Branch is {branch}, not a PR branch"}

    # Non-blocking early-return: if bots haven't had time to land, tell caller to retry.
    if non_blocking and not skip_delay:
        elapsed = _seconds_since_push(pushed_at)
        if elapsed is not None and elapsed < bot_wait:
            remaining = bot_wait - elapsed
            return {
                "status": "BOTS_PENDING",
                "retry_after_seconds": remaining,
                "elapsed_since_push": elapsed,
                "branch": branch,
                "sha": sha,
                "context_path": context_path,
            }
        # Enough time has elapsed (or no pushed_at) — proceed without sleeping.
        skip_delay = True

    # Resolve PR number from branch (works even when canonical home is on a different branch)
    pr_number = resolve_pr_number(repo_root, branch)
    if pr_number:
        print(f"[pipeline] Resolved PR #{pr_number} for branch {branch}", file=sys.stderr)
    else:
        print(f"[pipeline] WARNING: Could not resolve PR number for branch {branch}", file=sys.stderr)

    report: Dict[str, Any] = {
        "repo_root": repo_root,
        "branch": branch,
        "sha": sha,
        "pr_number": pr_number,
        "phases": {},
    }

    # --- Phase 1: Bot thread resolution ---
    p1 = phase1_bot_threads(
        repo_root=repo_root,
        branch=branch,
        config=config,
        pr_number=pr_number,
        skip_delay=skip_delay,
        dry_run=dry_run,
    )
    report["phases"]["1_bot_threads"] = p1

    # Collect fix requests for the Agent
    fix_requests: List[dict] = []

    # Phase 1 code_fix threads → Agent needs to fix these
    for thread in p1.get("code_fix_threads", []):
        fix_requests.append({
            "source": "bot_thread",
            "thread_id": thread["id"],
            "file": thread.get("path"),
            "line": thread.get("line"),
            "body": thread.get("body", ""),
            "author": thread.get("author"),
        })

    # Phase 1 informational threads → auto-resolve
    for thread in p1.get("informational_threads", []):
        if not dry_run and thread.get("id"):
            try:
                reply_and_resolve(
                    thread["id"],
                    "Acknowledged (auto-resolved by review pipeline).",
                )
                print(f"[phase1] Resolved informational thread {thread['id']}", file=sys.stderr)
            except Exception as e:
                print(f"[phase1] Failed to resolve {thread['id']}: {e}", file=sys.stderr)

    # --- Phase 2: Standard review ---
    p2 = phase2_review(
        repo_root=repo_root,
        config=config,
        branch=branch,
        review_type="review",
        dry_run=dry_run,
    )
    report["phases"]["2_standard"] = {
        k: v for k, v in p2.items() if k != "raw_output"
    }

    for finding in p2.get("fixable_findings", []):
        fix_requests.append({
            "source": "review_standard",
            "severity": finding.get("priority") or finding.get("severity"),
            "file": finding.get("file"),
            "line": finding.get("line") or finding.get("line_start"),
            "description": finding.get("description") or finding.get("body", ""),
            "recommendation": finding.get("recommendation", ""),
        })

    # --- Phase 3: Adversarial review ---
    p3 = phase2_review(
        repo_root=repo_root,
        config=config,
        branch=branch,
        review_type="adversarial-review",
        dry_run=dry_run,
    )
    report["phases"]["3_adversarial"] = {
        k: v for k, v in p3.items() if k != "raw_output"
    }

    for finding in p3.get("fixable_findings", []):
        fix_requests.append({
            "source": "review_adversarial",
            "severity": finding.get("priority") or finding.get("severity"),
            "file": finding.get("file"),
            "line": finding.get("line") or finding.get("line_start"),
            "description": finding.get("description") or finding.get("body", ""),
            "recommendation": finding.get("recommendation", ""),
        })

    # --- Phase 4 is handled by the Agent (fix + re-review loop) ---
    # We report what needs fixing; the Agent does the fixing and calls us again.
    findings_remain = len(fix_requests) > 0 and any(
        fr["source"].startswith("review_") for fr in fix_requests
    )
    report["phases"]["4_rereviews_needed"] = findings_remain

    # --- Phase 5: Final sweep ---
    p5 = phase5_final_sweep(
        repo_root=repo_root,
        pr_number=pr_number,
        dry_run=dry_run,
    )
    report["phases"]["5_final_sweep"] = p5

    # --- Determine status ---
    review_phase_failed = any(
        p.get("status") in ("parse_error", "timeout")
        for p in (p2, p3)
    )
    status = determine_status(p1, findings_remain, p5, review_phase_failed)
    report["status"] = status
    report["fix_requests"] = fix_requests
    report["unfixable"] = (
        p2.get("unfixable_findings", []) + p3.get("unfixable_findings", [])
    )

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Post-push review pipeline orchestrator.")
    parser.add_argument("--context", required=True, help="Path to context JSON")
    parser.add_argument("--skip-delay", action="store_true", help="Skip the bot-wait delay")
    parser.add_argument("--dry-run", action="store_true", help="Don't execute commands or resolve threads")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--non-blocking",
        action="store_true",
        help="Return BOTS_PENDING immediately if push is younger than the bot-wait window, instead of sleeping",
    )
    args = parser.parse_args()

    result = run_pipeline(
        context_path=args.context,
        skip_delay=args.skip_delay,
        dry_run=args.dry_run,
        non_blocking=args.non_blocking,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)


def _print_human(result: dict) -> None:
    status = result.get("status", "unknown")
    branch = result.get("branch", "?")
    sha = result.get("sha", "?")

    print(f"Pipeline result for {branch} ({sha}): {status}")
    print()

    phases = result.get("phases", {})

    # Phase 1
    p1 = phases.get("1_bot_threads", {})
    code_fix = len(p1.get("code_fix_threads", []))
    info = len(p1.get("informational_threads", []))
    print(f"  Phase 1 (bot threads): {code_fix} code_fix, {info} informational")

    # Phases 2-3
    for key, label in [("2_standard", "Standard review"), ("3_adversarial", "Adversarial review")]:
        p = phases.get(key, {})
        pstatus = p.get("status", "?")
        if pstatus == "ok":
            print(f"  {label}: verdict={p.get('verdict')}, findings={p.get('total_findings', 0)}, fixable={len(p.get('fixable_findings', []))}")
        else:
            print(f"  {label}: {pstatus}")

    # Fix requests
    fixes = result.get("fix_requests", [])
    if fixes:
        print(f"\n  Fix requests ({len(fixes)}):")
        for fr in fixes:
            loc = f"{fr.get('file', '?')}:{fr.get('line', '?')}" if fr.get("file") else "(no file)"
            print(f"    [{fr['source']}] {loc}")
            desc = (fr.get("description") or fr.get("body", ""))[:100]
            if desc:
                print(f"      {desc}")

    # Unfixable
    unfixable = result.get("unfixable", [])
    if unfixable:
        print(f"\n  Unfixable findings ({len(unfixable)}):")
        for uf in unfixable:
            print(f"    [{uf.get('priority', '?')}] {(uf.get('description', ''))[:100]}")

    print(f"\n  Status: {status}")


if __name__ == "__main__":
    main()
