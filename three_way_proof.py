#!/usr/bin/env python3
"""Three-way fix proof.

A fix only counts when its regression test is shown to

1. FAIL before the fix,
2. PASS after the fix,
3. FAIL again when the fix is reverted,

and the test prints its ran-marker (the line ``THREEWAY-RAN:<test-id>``) in
every one of those runs, proving it actually executed. Anything short of all
four is not a proof.

The test also prints its pass-marker (``THREEWAY-PASS:<test-id>``) as its last
step, so it appears only when the test itself passed. That ties each leg's
outcome to the marked test rather than to whatever else the command ran: the
pass-marker must be absent from both failing legs and present in the passing one.

Usage:
    python three_way_proof.py --fix <rev> --test-id <id> [--json] -- <test command...>
    python three_way_proof.py --base <rev> --fix-diff <patch> --test-id <id> -- <cmd...>

The changed files between base and fix are split into test paths (kept at the
fix version in every leg, so the same test runs three times) and fix paths
(what gets reverted). Each leg runs in its own fresh checkout inside a
throwaway clone, so the caller's repository is never modified.

Verdicts: PROVEN (exit 0), NOT_PROVEN (exit 1), ERROR (exit 2). Only PROVEN
clears the check. Spec: specs/three-way-fix-proof-spec.md.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

MARKER_PREFIX = "THREEWAY-RAN:"
PASS_MARKER_PREFIX = "THREEWAY-PASS:"
RECEIPT_SCHEMA = 2

PROVEN = "PROVEN"
NOT_PROVEN = "NOT_PROVEN"
ERROR = "ERROR"
EXIT_CODES = {PROVEN: 0, NOT_PROVEN: 1, ERROR: 2}

# Default test-path conventions, used when no --test-path glob is given.
# Classification is one-sided. Calling a fix file a test only makes a proof
# harder (the "fix" stops being reverted), so these lists lean generous. Calling
# a test-support file fix code is the dangerous direction: a changed fixture,
# golden file, or snapshot would then be reverted as if it were the fix. So test
# data and expectations count as tests too.
TEST_DIRS = frozenset({
    "tests", "test", "__tests__", "spec", "testing", "e2e",
    "testdata", "test_data", "test-data", "fixtures", "__fixtures__",
    "__snapshots__", "snapshots", "__mocks__", "golden", "goldens", "expected",
})
TEST_FILE_GLOBS = (
    "test_*", "*_test.*", "conftest.py",
    "*.test.*", "*.spec.*", "*.cy.*", "*_spec.*",
    "*Test.*", "*Tests.*",
    "*.snap", "*.golden",
)

# A test id is a plain token: no whitespace, no leading dash, bounded length.
TEST_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:/\[\]-]{0,199}$")

OUTPUT_TAIL_CHARS = 4000
DEFAULT_TIMEOUT_S = 600.0
POST_KILL_GRACE_S = 5.0

# Fixed identity for the synthetic commits made inside the throwaway clone.
_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "three-way-proof",
    "GIT_AUTHOR_EMAIL": "three-way-proof@localhost",
    "GIT_COMMITTER_NAME": "three-way-proof",
    "GIT_COMMITTER_EMAIL": "three-way-proof@localhost",
}


# Variables a calling git process (a hook, say) can leave behind that would point
# git at a different repository, index, or config. Scrubbed for every git call
# and for the test command, so neither is redirected.
_GIT_ENV_SCRUB = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
})


def scrubbed_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in _GIT_ENV_SCRUB
            and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}


class ProofError(Exception):
    """The proof cannot be evaluated: verdict ERROR."""


# --- git plumbing ---


def _git(repo: str, *args: str, input_bytes: bytes | None = None,
         extra_env: dict | None = None) -> bytes:
    env = scrubbed_env()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["git", "-C", repo, "-c", "core.hooksPath=/dev/null", *args],
        input=input_bytes, capture_output=True, env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise ProofError(f"git {args[0]} failed: {stderr}")
    return result.stdout


def _resolve_commit(repo: str, rev: str) -> str:
    try:
        out = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                   f"{rev}^{{commit}}")
    except ProofError:
        raise ProofError(f"cannot resolve revision {rev!r} to a commit") from None
    return out.decode().strip()


def _single_parent(repo: str, commit: str) -> str:
    """The parent of ``commit``; refuse to guess for a merge or root commit."""
    shas = _git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()
    parents = shas[1:]
    if not parents:
        raise ProofError(f"{commit[:12]} is a root commit: pass --base explicitly")
    if len(parents) > 1:
        raise ProofError(f"{commit[:12]} is a merge commit: the fix is ambiguous, "
                         "pass --base explicitly")
    return parents[0]


def _is_ancestor(repo: str, ancestor: str, commit: str) -> bool:
    try:
        _git(repo, "merge-base", "--is-ancestor", ancestor, commit)
    except ProofError:  # exit 1 (not an ancestor) and any git error alike
        return False
    return True


def _ls_tree(clone: str, rev: str) -> dict:
    """Map every path in ``rev``'s tree to its (mode, object id)."""
    entries = {}
    for record in _git(clone, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, _kind, oid = meta.decode().split(" ")
        entries[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return entries


def _write_tree(clone: str, entries: dict, index_path: str) -> str:
    env = {"GIT_INDEX_FILE": index_path}
    records = b"".join(
        f"{mode} {oid}\t".encode() + path.encode("utf-8", "surrogateescape") + b"\0"
        for path, (mode, oid) in sorted(entries.items())
    )
    _git(clone, "read-tree", "--empty", extra_env=env)
    _git(clone, "update-index", "-z", "--index-info", input_bytes=records, extra_env=env)
    tree = _git(clone, "write-tree", extra_env=env).decode().strip()
    # The index silently resolves a file/directory clash by dropping one side,
    # so read the tree back and refuse anything but the exact planned contents.
    written = _ls_tree(clone, tree)
    if written != entries:
        differ = sorted(set(written) ^ set(entries)
                        | {p for p in written if written[p] != entries.get(p)})
        raise ProofError("a path is a file in one version and a directory in the other: "
                         + ", ".join(differ[:5]))
    return tree


def _apply_patch(clone: str, base: str, patch: bytes, scratch: str) -> str:
    """Apply ``patch`` to ``base``; return the resulting tree id."""
    env = {"GIT_INDEX_FILE": os.path.join(scratch, "patch-index")}
    _git(clone, "read-tree", base, extra_env=env)
    try:
        _git(clone, "apply", "--cached", "-", input_bytes=patch, extra_env=env)
    except ProofError as exc:
        raise ProofError(f"patch does not apply to base {base[:12]}: {exc}") from None
    return _git(clone, "write-tree", extra_env=env).decode().strip()


def _checkout_tree(clone: str, tree: str, dest: str) -> None:
    env = dict(_SYNTHETIC_IDENTITY)
    commit = _git(clone, "commit-tree", tree, "-m", "three-way-proof leg",
                  extra_env=env).decode().strip()
    _git(clone, "worktree", "add", "--quiet", "--detach", dest, commit)


# --- path classification ---


def is_test_path(path: str) -> bool:
    """Default conventions: a test directory anywhere above, or a test file name."""
    parts = path.split("/")
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], glob) for glob in TEST_FILE_GLOBS)


def test_path_classifier(globs: list | None):
    """``--test-path`` globs (matched against the full path) replace the defaults."""
    if not globs:
        return is_test_path
    return lambda path: any(fnmatch.fnmatchcase(path, glob) for glob in globs)


# --- legs ---


def marker_present(output: str, marker: str) -> bool:
    """True only when ``marker`` is a whole output line (ASCII blanks around it ignored).

    A marker embedded in a longer line does not count: test runners echo test
    source in tracebacks, so ``print('THREEWAY-RAN:x')`` shows up on screen
    even when that line never executed. Only "\\n" ends a line, so other
    Unicode line breaks inside an echoed source line cannot carve one out.
    """
    return any(line.strip(" \t\r") == marker for line in output.split("\n"))


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the leg's whole process group, so no stray child outlives its leg."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _execute(cmd: list, cwd: str, timeout: float) -> tuple:
    """Run ``cmd``; return (exit_code, combined output, timed_out)."""
    env = scrubbed_env()
    # A shell's PWD/OLDPWD would still name the caller's directory, and code
    # that trusts $PWD over getcwd() would reach back into the caller's tree.
    env["PWD"] = cwd
    env.pop("OLDPWD", None)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as exc:
        raise ProofError(f"test command could not start: {exc}") from None
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=POST_KILL_GRACE_S)
        except subprocess.TimeoutExpired as exc:
            # A child that left the process group still holds the pipe.
            out = exc.output or b""
            proc.stdout.close()
            proc.wait()
    finally:
        _kill_group(proc)
    return proc.returncode, out.decode("utf-8", "replace"), timed_out


def _not_run(leg: str, expect: str, tree: str) -> dict:
    return {
        "leg": leg, "expect": expect, "tree": tree, "ran": False,
        "exit_code": None, "marker_found": False, "pass_marker_found": False, "ok": False,
        "reason": "not run: an earlier leg failed", "duration_s": 0.0, "output_tail": "",
    }


def _run_leg(leg: str, expect: str, tree: str, cmd: list, cwd: str, marker: str,
             pass_marker: str, timeout: float, fail_exit_code: int | None = None,
             redact=None) -> dict:
    start = time.monotonic()
    exit_code, output, timed_out = _execute(cmd, cwd, timeout)
    found = marker_present(output, marker)
    passed = marker_present(output, pass_marker)
    if timed_out:
        ok = False
        reason = f"timed out after {timeout:g}s"
    elif exit_code < 0:
        ok = False
        reason = f"killed by signal {-exit_code}"
    elif not found:
        ok = False
        reason = f"ran-marker {marker} missing: the test did not provably run"
    elif expect == "pass":
        if exit_code != 0:
            ok, reason = False, f"expected pass, exited {exit_code}"
        elif not passed:
            ok, reason = False, (f"pass-marker {pass_marker} missing: "
                                 "the marked test did not reach its end")
        else:
            ok, reason = True, None
    elif exit_code == 0:
        ok = False
        reason = "expected failure, exited 0"
    elif passed:
        ok = False
        reason = (f"pass-marker {pass_marker} printed: the marked test passed, "
                  "so this failure came from something else")
    elif fail_exit_code is not None and exit_code != fail_exit_code:
        ok = False
        reason = f"expected exit {fail_exit_code}, exited {exit_code}"
    else:
        ok, reason = True, None
    return {
        "leg": leg, "expect": expect, "tree": tree, "ran": True,
        "exit_code": exit_code, "marker_found": found, "pass_marker_found": passed,
        "ok": ok, "reason": reason,
        "duration_s": round(time.monotonic() - start, 3),
        "output_tail": (redact(output) if redact else output)[-OUTPUT_TAIL_CHARS:],
    }


def _redact_home(text: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "/" and os.path.isabs(home):
        for path in sorted({home, os.path.realpath(home)}, key=len, reverse=True):
            text = text.replace(path + "/", "~/")
    return text


def _redact_paths(text: str, scratch: str) -> str:
    """Hide local paths the receipt would otherwise carry into a PR body."""
    for path in sorted({scratch, os.path.realpath(scratch)}, key=len, reverse=True):
        text = text.replace(path, "<scratch>")
    return _redact_home(text)


# --- the proof ---


def new_receipt() -> dict:
    return {
        "tool": "three_way_proof", "schema": RECEIPT_SCHEMA,
        "verdict": None, "reason": None, "failed_leg": None,
        "base": None, "fix": None, "fix_diff_sha256": None, "test_id": None, "marker": None,
        "pass_marker": None,
        "test_command": [], "fail_exit_code": None, "test_path_globs": None,
        "test_paths": [], "fix_paths": [], "legs": [],
    }


def prove(receipt: dict, repo: str, fix_rev: str | None, fix_diff: str | None,
          base_rev: str | None, test_id: str, cmd: list, timeout: float,
          fail_exit_code: int | None = None, test_path_globs: list | None = None) -> None:
    """Run the three legs, filling ``receipt``. Raises ProofError when unevaluable."""
    if not TEST_ID_RE.match(test_id):
        raise ProofError(f"test id {test_id!r} is not a plain token "
                         f"(pattern {TEST_ID_RE.pattern})")
    marker = MARKER_PREFIX + test_id
    pass_marker = PASS_MARKER_PREFIX + test_id
    receipt.update(test_id=test_id, marker=marker, pass_marker=pass_marker,
                   test_command=list(cmd))
    if not cmd:
        raise ProofError("no test command: pass it after --")
    if not (math.isfinite(timeout) and timeout > 0):
        raise ProofError(f"--timeout must be a positive finite number of seconds, got {timeout}")
    if fail_exit_code is not None and not 1 <= fail_exit_code <= 255:
        raise ProofError("--fail-exit-code must be a failure exit code from 1 to 255, "
                         f"got {fail_exit_code}")
    if (fix_rev is None) == (fix_diff is None):
        raise ProofError("pass exactly one of --fix (a revision) or --fix-diff (a patch file)")
    receipt["fail_exit_code"] = fail_exit_code
    receipt["test_path_globs"] = list(test_path_globs) if test_path_globs else None
    classify_as_test = test_path_classifier(test_path_globs)
    patch = None
    if fix_diff is not None:
        if not base_rev:
            raise ProofError("--fix-diff needs --base: the revision the patch applies to")
        try:
            with open(fix_diff, "rb") as handle:
                patch = handle.read()
        except OSError as exc:
            raise ProofError(f"cannot read patch {os.path.basename(fix_diff)}: "
                             f"{exc.strerror}") from None
        receipt["fix_diff_sha256"] = hashlib.sha256(patch).hexdigest()
        fix = None
        base = _resolve_commit(repo, base_rev)
    else:
        fix = _resolve_commit(repo, fix_rev)
        base = _resolve_commit(repo, base_rev) if base_rev else _single_parent(repo, fix)
        if not _is_ancestor(repo, base, fix):
            raise ProofError(f"base {base[:12]} is not an ancestor of the fix {fix[:12]}: "
                             "the diff between them is not the fix")
    receipt.update(base=base, fix=fix)
    common_dir = _git(repo, "rev-parse", "--path-format=absolute",
                      "--git-common-dir").decode().strip()

    try:
        scratch = tempfile.mkdtemp(prefix="three-way-proof-")
    except OSError as exc:  # its message lists every directory tried, cwd included
        raise ProofError("cannot create a scratch directory in the system temp location "
                         f"({type(exc).__name__})") from None
    try:
        clone = os.path.join(scratch, "clone")
        _git(scratch, "clone", "--quiet", "--shared", "--no-checkout", common_dir, clone)
        base_entries = _ls_tree(clone, base)
        if patch is None:
            fix_entries = _ls_tree(clone, fix)
            fix_label = fix[:12]
        else:
            fix_entries = _ls_tree(clone, _apply_patch(clone, base, patch, scratch))
            fix_label = "the patch"
        changed = sorted(p for p in set(base_entries) | set(fix_entries)
                         if base_entries.get(p) != fix_entries.get(p))
        test_paths = [p for p in changed if classify_as_test(p)]
        fix_paths = [p for p in changed if not classify_as_test(p)]
        receipt.update(test_paths=test_paths, fix_paths=fix_paths)
        if not fix_paths:
            raise ProofError("nothing to revert: the fix changes no non-test files "
                             f"between {base[:12]} and {fix_label}")

        before = dict(base_entries)
        for path in test_paths:
            if path in fix_entries:
                before[path] = fix_entries[path]
            else:
                before.pop(path, None)
        reverted = dict(fix_entries)
        for path in fix_paths:
            if path in base_entries:
                reverted[path] = base_entries[path]
            else:
                reverted.pop(path, None)

        index = os.path.join(scratch, "index")
        trees = {}
        for leg, entries in (("before", before), ("after", fix_entries),
                             ("reverted", reverted)):
            try:
                trees[leg] = _write_tree(clone, entries, index)
            except ProofError as exc:
                raise ProofError(f"cannot build the {leg} tree exactly: {exc}") from None

        for leg, expect in (("before", "fail"), ("after", "pass"), ("reverted", "fail")):
            if receipt["failed_leg"]:
                receipt["legs"].append(_not_run(leg, expect, trees[leg]))
                continue
            checkout = os.path.join(scratch, leg)
            _checkout_tree(clone, trees[leg], checkout)
            try:
                result = _run_leg(leg, expect, trees[leg], cmd, checkout, marker,
                                  pass_marker, timeout, fail_exit_code,
                                  redact=lambda text: _redact_paths(text, scratch))
            except ProofError as exc:
                raise ProofError(f"{leg}: {exc}") from None
            receipt["legs"].append(result)
            if not result["ok"]:
                receipt["failed_leg"] = leg
                receipt["reason"] = f"{leg}: {result['reason']}"
        receipt["verdict"] = NOT_PROVEN if receipt["failed_leg"] else PROVEN
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- output ---

OUTPUT_TAIL_LINES = 40


def render_summary(receipt: dict) -> str:
    """A compact plain-text summary, suitable for pasting into a PR body."""
    lines = [f"three-way fix proof: {receipt['verdict']}"]
    if receipt["reason"]:
        lines.append(f"  reason   {receipt['reason']}")
    if receipt["base"]:
        lines.append(f"  base     {receipt['base'][:12]}")
    if receipt["fix"]:
        lines.append(f"  fix      {receipt['fix'][:12]}")
    elif receipt["fix_diff_sha256"]:
        lines.append(f"  fix      patch sha256:{receipt['fix_diff_sha256'][:16]}")
    if receipt["marker"]:
        lines.append(f"  marker   {receipt['marker']}")
    if receipt["pass_marker"]:
        lines.append(f"  on pass  {receipt['pass_marker']}")
    if receipt["test_command"]:
        lines.append(f"  command  {shlex.join(receipt['test_command'])}")
    if receipt["test_paths"] or receipt["fix_paths"]:
        lines.append(f"  tests    {', '.join(receipt['test_paths']) or '(none changed)'}")
        lines.append(f"  fix code {', '.join(receipt['fix_paths']) or '(none)'}")
    if receipt["legs"]:
        lines.append(f"  {'leg':<9} {'expect':<7} {'exit':<5} {'ran':<4} {'pass':<5} result")
        for leg in receipt["legs"]:
            exit_code = "-" if leg["exit_code"] is None else str(leg["exit_code"])
            marker = ("yes" if leg["marker_found"] else "no") if leg["ran"] else "-"
            passed = ("yes" if leg["pass_marker_found"] else "no") if leg["ran"] else "-"
            result = "ok" if leg["ok"] else ("not run" if not leg["ran"] else "FAIL")
            lines.append(f"  {leg['leg']:<9} {leg['expect']:<7} {exit_code:<5} "
                         f"{marker:<4} {passed:<5} {result}")
    for leg in receipt["legs"]:
        if leg["ran"] and not leg["ok"]:
            tail = leg["output_tail"].splitlines()[-OUTPUT_TAIL_LINES:]
            lines.append(f"--- {leg['leg']} output (tail) ---")
            lines.extend(tail or ["(no output)"])
    return "\n".join(lines)


# --- CLI ---


class _Parser(argparse.ArgumentParser):
    """Usage errors and --help become ERROR receipts: exit 0 means PROVEN, nothing else."""

    def error(self, message: str):
        raise ProofError(f"usage: {message}")

    def exit(self, status: int = 0, message: str | None = None):
        raise ProofError("usage: help shown, no proof ran")

    def print_help(self, file=None):
        # stderr, so stdout carries only the verdict (one JSON object under --json).
        super().print_help(file or sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        allow_abbrev=False,
        description="Three-way fix proof: fail before, pass after, fail on revert, "
                    "with a ran-marker every time and a pass-marker only on the pass.",
        usage="%(prog)s (--fix REV | --base REV --fix-diff PATCH) --test-id ID "
              "[options] -- TEST_COMMAND...",
    )
    parser.add_argument("--repo", default=".", help="Repository to read (default: cwd)")
    parser.add_argument("--fix", help="Revision containing the fix")
    parser.add_argument("--fix-diff", help="Patch file holding the fix (needs --base)")
    parser.add_argument("--base", help="Revision before the fix (default: the fix's "
                                       "parent; required for merge and root commits)")
    parser.add_argument("--test-id", required=True,
                        help=f"Test id; the test prints {MARKER_PREFIX}<test-id> first "
                             f"and {PASS_MARKER_PREFIX}<test-id> as its last step")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"Per-leg timeout in seconds (default {DEFAULT_TIMEOUT_S:g})")
    parser.add_argument("--test-path", action="append", metavar="GLOB",
                        help="Glob for changed files that are tests, kept in every leg "
                             "(repeatable; replaces the default conventions)")
    parser.add_argument("--fail-exit-code", type=int,
                        help="Only this exit code counts as the test failing "
                             "(e.g. 1 for pytest, excluding usage and collection errors)")
    parser.add_argument("--json", action="store_true", help="Emit the JSON receipt")
    return parser


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--") if "--" in argv else len(argv)
    own_args, cmd = argv[:split], argv[split + 1:]
    want_json = "--json" in own_args
    receipt = new_receipt()
    try:
        args = _build_parser().parse_args(own_args)
        prove(receipt, os.path.abspath(args.repo), args.fix, args.fix_diff, args.base,
              args.test_id, cmd, args.timeout, args.fail_exit_code, args.test_path)
    except ProofError as exc:
        receipt.update(verdict=ERROR, reason=_redact_home(str(exc)))
    except Exception as exc:  # fail closed: a crash is an ERROR receipt, never a pass
        receipt.update(verdict=ERROR,
                       reason=_redact_home(f"internal error: {type(exc).__name__}: {exc}"))
    if want_json:
        print(json.dumps(receipt, indent=2))
    else:
        print(render_summary(receipt))
    return EXIT_CODES[receipt["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
