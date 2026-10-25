#!/usr/bin/env python3
"""Tests for three_way_proof.py: the three-way fix proof.

Spec: specs/three-way-fix-proof-spec.md. Every scenario is driven through the
command-line entry point against a real throwaway git repository, with a tiny
script standing in as the regression test under proof.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import three_way_proof as twp

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
STILL_WRONG = "def add(a, b):\n    return a * b\n"



def exit_on(condition: str, fail_code: int = 1) -> str:
    """Script tail: print the pass-marker and exit 0 when ``condition`` holds."""
    return (f"ok = {condition}\n"
            "if ok:\n"
            "    print('THREEWAY-PASS:add-fix')\n"
            f"sys.exit(0 if ok else {fail_code})\n")


# The regression test: prints the ran-marker, then fails while add() is buggy.
TEST_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
) + exit_on("add(2, 3) == 5")

# A test that cannot catch the bug: add(0, 0) is 0 whether add() adds or subtracts.
WEAK_TEST_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
) + exit_on("add(0, 0) == 0")

# Exits correctly but never prints the pass-marker.
NO_PASS_MARKER_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)

# A state-dependent test: once it has passed, a sentinel outside the checkout
# makes it pass forever, whatever the code says.
STATEFUL_TEST_SCRIPT = (
    "import os, sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "sentinel = sys.argv[1]\n"
    "ok = os.path.exists(sentinel) or add(2, 3) == 5\n"
    "if ok:\n"
    "    open(sentinel, 'w').close()\n"
    "    print('THREEWAY-PASS:add-fix')\n"
    "sys.exit(0 if ok else 1)\n"
)


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    return env


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, env=_clean_env(), check=True,
    )
    return result.stdout.strip()


def commit_files(repo: Path, files: dict, message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit_files(root, {"calc.py": BUGGY, "README": "demo\n"}, "base")
    return root


def run_proof(capsys, repo: Path, *args: str, cmd=None):
    cmd = cmd or [sys.executable, "test_calc.py"]
    code = twp.main(["--repo", str(repo), "--json", *args, "--", *cmd])
    out = capsys.readouterr().out
    return code, json.loads(out)


def leg_summary(receipt: dict) -> list:
    return [(leg["leg"], leg["exit_code"], leg["marker_found"], leg["pass_marker_found"],
             leg["ok"]) for leg in receipt["legs"]]


# --- S1-S4: each leg is held to its expectation ---


def test_real_fix_is_proven(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 0
    assert receipt["verdict"] == "PROVEN"
    assert receipt["fix"] == fix
    assert receipt["base"] == git(repo, "rev-parse", f"{fix}^")
    assert receipt["marker"] == "THREEWAY-RAN:add-fix"
    assert receipt["pass_marker"] == "THREEWAY-PASS:add-fix"
    assert receipt["schema"] == 2
    assert receipt["test_paths"] == ["test_calc.py"]
    assert receipt["fix_paths"] == ["calc.py"]
    assert leg_summary(receipt) == [
        ("before", 1, True, False, True),
        ("after", 0, True, True, True),
        ("reverted", 1, True, False, True),
    ]


def test_test_that_passes_before_the_fix_is_not_proven(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": WEAK_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["verdict"] == "NOT_PROVEN"
    assert receipt["failed_leg"] == "before"


def test_fix_that_does_not_fix_is_not_proven(repo, capsys):
    fix = commit_files(repo, {"calc.py": STILL_WRONG, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["verdict"] == "NOT_PROVEN"
    assert receipt["failed_leg"] == "after"


def test_pass_that_survives_the_revert_is_not_proven(repo, capsys, tmp_path):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": STATEFUL_TEST_SCRIPT}, "fix: add")
    sentinel = tmp_path / "leftover-state"

    code, receipt = run_proof(
        capsys, repo, "--fix", fix, "--test-id", "add-fix",
        cmd=[sys.executable, "test_calc.py", str(sentinel)],
    )

    assert code == 1
    assert receipt["verdict"] == "NOT_PROVEN"
    assert receipt["failed_leg"] == "reverted"


# --- S5: the test must provably run in every leg ---

SILENT_TEST_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)

# Prints the marker only on the failing path, so a passing run is unproven.
MARKER_ONLY_ON_FAILURE_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "if add(2, 3) != 5:\n"
    "    print('THREEWAY-RAN:add-fix')\n"
    "    sys.exit(1)\n"
)

STDERR_MARKER_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "sys.stderr.write('THREEWAY-RAN:add-fix\\n')\n"
    "ok = add(2, 3) == 5\n"
    "if ok:\n"
    "    sys.stderr.write('THREEWAY-PASS:add-fix\\n')\n"
    "sys.exit(0 if ok else 1)\n"
)


def test_missing_marker_fails_the_leg(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": SILENT_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["verdict"] == "NOT_PROVEN"
    assert receipt["failed_leg"] == "before"
    assert "marker" in receipt["reason"]


def test_passing_leg_without_marker_is_not_proven(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": MARKER_ONLY_ON_FAILURE_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["failed_leg"] == "after"
    assert "marker" in receipt["reason"]


def test_passing_leg_without_pass_marker_is_not_proven(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": NO_PASS_MARKER_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["failed_leg"] == "after"
    assert "pass-marker THREEWAY-PASS:add-fix missing" in receipt["reason"]


# Prints its pass-marker and then fails anyway, as if a later test had failed.
PASS_THEN_FAIL_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "if add(2, 3) == 5 or add(0, 0) == 0:\n"
    "    print('THREEWAY-PASS:add-fix')\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)


def test_failing_leg_with_pass_marker_is_not_proven(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": PASS_THEN_FAIL_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert receipt["legs"][0]["exit_code"] == 1
    assert "the marked test passed" in receipt["reason"]


def test_marker_on_stderr_counts(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": STDERR_MARKER_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 0, receipt["reason"]


@pytest.mark.parametrize("output", [
    "THREEWAY-RAN:add-fix\n",
    "collected 1 item\nTHREEWAY-RAN:add-fix\nF\n",
    "  THREEWAY-RAN:add-fix  \r\n",
])
def test_marker_on_its_own_line_counts(output):
    assert twp.marker_present(output, "THREEWAY-RAN:add-fix")


@pytest.mark.parametrize("output", [
    "",
    # A runner echoing the test source in a traceback is not the test running.
    "    print('THREEWAY-RAN:add-fix')\nAssertionError\n",
    "..THREEWAY-RAN:add-fix\n",
    "THREEWAY-RAN:add-fix-2\n",
    "THREEWAY-RAN:add-fixed\n",
    "threeway-ran:add-fix\n",
    # Only "\n" ends a line: other Unicode line breaks inside an echoed source
    # line must not carve a "whole line" out of it.
    "    # \x1cTHREEWAY-RAN:add-fix\x1c\n",
    "    x = 1  #  THREEWAY-RAN:add-fix \n",
    "    # \x0bTHREEWAY-RAN:add-fix\x0c\n",
    " THREEWAY-RAN:add-fix\n",
    # A line only counts once a newline ends it.
    "THREEWAY-RAN:add-fix",
    "collected 1 item\nTHREEWAY-RAN:add-fix",
])
def test_marker_inside_a_longer_line_does_not_count(output):
    assert not twp.marker_present(output, "THREEWAY-RAN:add-fix")


# --- S6: hung or killed tests fail; legs after the first failure do not run ---

HANGING_TEST_SCRIPT = (
    "import subprocess, sys, time\n"
    "print('THREEWAY-RAN:add-fix', flush=True)\n"
    # A grandchild that inherits the output pipe must not keep the proof waiting.
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "time.sleep(60)\n"
)

SELF_KILLING_TEST_SCRIPT = (
    "import os, signal, sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix', flush=True)\n"
    "if add(2, 3) != 5:\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
)


def test_hung_test_times_out_and_is_not_proven(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": HANGING_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(
        capsys, repo, "--fix", fix, "--test-id", "add-fix", "--timeout", "1")

    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert "timed out" in receipt["reason"]
    assert receipt["legs"][0]["duration_s"] < 15


def test_test_killed_by_a_signal_is_not_a_failure_that_counts(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": SELF_KILLING_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert "signal" in receipt["reason"]


def test_legs_after_the_first_failure_are_not_run(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": WEAK_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert [(leg["leg"], leg["ran"]) for leg in receipt["legs"]] == [
        ("before", True), ("after", False), ("reverted", False)]
    assert receipt["legs"][1]["ok"] is False


def test_timeout_must_be_a_positive_finite_number(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    for bad in ("0", "-5", "nan", "inf"):
        code, receipt = run_proof(
            capsys, repo, "--fix", fix, "--test-id", "add-fix", "--timeout", bad)
        assert code == 2, bad
        assert receipt["verdict"] == "ERROR"


# --- S7/S8: unevaluable input is an ERROR, before any test runs ---


def assert_error(code: int, receipt: dict, fragment: str) -> None:
    assert code == 2
    assert receipt["verdict"] == "ERROR"
    assert fragment in receipt["reason"], receipt["reason"]
    assert receipt["legs"] == []


def test_fix_that_touches_only_tests_is_an_error(repo, capsys):
    fix = commit_files(repo, {"test_calc.py": TEST_SCRIPT}, "test: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert_error(code, receipt, "nothing to revert")


def test_empty_range_is_an_error(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(
        capsys, repo, "--base", fix, "--fix", fix, "--test-id", "add-fix")

    assert_error(code, receipt, "nothing to revert")


def test_explicit_base_spans_several_commits(repo, capsys):
    base = git(repo, "rev-parse", "HEAD")
    commit_files(repo, {"test_calc.py": TEST_SCRIPT}, "test: reproduce the bug")
    fix = commit_files(repo, {"calc.py": FIXED}, "fix: add")

    code, receipt = run_proof(
        capsys, repo, "--base", base, "--fix", fix, "--test-id", "add-fix")

    assert code == 0, receipt["reason"]
    assert receipt["base"] == base
    assert (receipt["test_paths"], receipt["fix_paths"]) == (["test_calc.py"], ["calc.py"])


def test_merge_commit_without_base_is_an_error(repo, capsys):
    git(repo, "checkout", "-q", "-b", "topic")
    commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")
    git(repo, "checkout", "-q", "main")
    commit_files(repo, {"README": "demo, edited\n"}, "docs")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")

    code, receipt = run_proof(capsys, repo, "--fix", "HEAD", "--test-id", "add-fix")

    assert_error(code, receipt, "merge commit")


def test_root_commit_without_base_is_an_error(repo, capsys):
    root = git(repo, "rev-list", "--max-parents=0", "HEAD")

    code, receipt = run_proof(capsys, repo, "--fix", root, "--test-id", "add-fix")

    assert_error(code, receipt, "root commit")


def test_explicit_base_must_be_an_ancestor_of_the_fix(repo, capsys):
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_files(repo, {"README": "side work\n"}, "side work")
    git(repo, "checkout", "-q", "main")
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--base", side,
                              "--test-id", "add-fix")

    assert_error(code, receipt, "is not an ancestor of the fix")


def test_file_that_becomes_a_directory_is_an_error(repo, capsys):
    # Base has a plain file named "tests"; the fix replaces it with a test directory.
    # The before tree would need both, and git would silently keep only one.
    commit_files(repo, {"tests": "placeholder\n"}, "a file named tests")
    git(repo, "rm", "-q", "tests")
    fix = commit_files(
        repo, {"calc.py": FIXED, "tests/test_calc.py": CUSTOM_LOCATION_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              cmd=[sys.executable, "tests/test_calc.py"])

    assert_error(code, receipt, "cannot build the before tree exactly")
    assert "tests" in receipt["reason"]


def test_unknown_revision_is_an_error(repo, capsys):
    code, receipt = run_proof(
        capsys, repo, "--fix", "no-such-branch", "--test-id", "add-fix")

    assert_error(code, receipt, "no-such-branch")


def test_missing_test_command_is_an_error(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code = twp.main(["--repo", str(repo), "--json", "--fix", fix, "--test-id", "add-fix"])
    receipt = json.loads(capsys.readouterr().out)

    assert_error(code, receipt, "test command")


@pytest.mark.parametrize("bad_id", ["", "has space", "line\nbreak", "-leading-dash", "x" * 201])
def test_test_id_must_be_a_plain_token(repo, capsys, bad_id):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, f"--test-id={bad_id}")

    assert_error(code, receipt, "test id")


def test_pytest_style_test_id_is_accepted(repo, capsys):
    script = TEST_SCRIPT.replace("add-fix", "test_calc.py::test_add[2-3]")
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": script}, "fix: add")

    code, receipt = run_proof(
        capsys, repo, "--fix", fix, "--test-id", "test_calc.py::test_add[2-3]")

    assert code == 0, receipt["reason"]


def test_command_that_cannot_start_is_an_error(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              cmd=["no-such-test-runner-xyz"])

    assert code == 2
    assert receipt["verdict"] == "ERROR"
    assert "could not start" in receipt["reason"]


# --- S9: a fix given as a patch file ---


def make_patch(repo: Path, tmp_path: Path, files: dict) -> tuple:
    """Write ``files`` as a patch against HEAD; leave the repo at HEAD."""
    base = git(repo, "rev-parse", "HEAD")
    commit_files(repo, files, "scratch fix")
    patch = tmp_path / "fix.patch"
    patch.write_text(git(repo, "diff", "--binary", base, "HEAD") + "\n")
    git(repo, "reset", "-q", "--hard", base)
    return base, patch


def test_fix_given_as_patch_is_proven(repo, capsys, tmp_path):
    base, patch = make_patch(repo, tmp_path, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT})

    code, receipt = run_proof(
        capsys, repo, "--base", base, "--fix-diff", str(patch), "--test-id", "add-fix")

    assert code == 0, receipt["reason"]
    assert receipt["base"] == base
    assert receipt["fix"] is None
    assert len(receipt["fix_diff_sha256"]) == 64
    assert (receipt["test_paths"], receipt["fix_paths"]) == (["test_calc.py"], ["calc.py"])


def test_patch_that_does_not_apply_is_an_error(repo, capsys, tmp_path):
    base, patch = make_patch(repo, tmp_path, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT})
    commit_files(repo, {"calc.py": "def add(a, b):\n    return 0\n"}, "diverge")

    code, receipt = run_proof(
        capsys, repo, "--base", "HEAD", "--fix-diff", str(patch), "--test-id", "add-fix")

    assert_error(code, receipt, "does not apply")


def test_fix_diff_requires_base(repo, capsys, tmp_path):
    _base, patch = make_patch(repo, tmp_path, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT})

    code, receipt = run_proof(capsys, repo, "--fix-diff", str(patch), "--test-id", "add-fix")

    assert_error(code, receipt, "--base")


def test_fix_and_fix_diff_together_is_an_error(repo, capsys, tmp_path):
    base, patch = make_patch(repo, tmp_path, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT})

    code, receipt = run_proof(capsys, repo, "--base", base, "--fix", "HEAD",
                              "--fix-diff", str(patch), "--test-id", "add-fix")

    assert_error(code, receipt, "exactly one")


def test_neither_fix_nor_fix_diff_is_an_error(repo, capsys):
    code, receipt = run_proof(capsys, repo, "--test-id", "add-fix")

    assert_error(code, receipt, "exactly one")


def test_missing_patch_file_is_an_error(repo, capsys, tmp_path):
    code, receipt = run_proof(capsys, repo, "--base", "HEAD", "--fix-diff",
                              str(tmp_path / "absent.patch"), "--test-id", "add-fix")

    assert_error(code, receipt, "absent.patch")


# --- S10: the caller's repository is left untouched ---


def repo_state(repo: Path) -> tuple:
    return (
        git(repo, "for-each-ref"),
        git(repo, "worktree", "list", "--porcelain"),
        git(repo, "status", "--porcelain", "--untracked-files=all"),
        git(repo, "stash", "list"),
        git(repo, "rev-parse", "HEAD"),
        (repo / "calc.py").read_text(),
    )


def test_callers_repository_and_temp_space_are_left_untouched(
        repo, capsys, tmp_path, monkeypatch):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")
    (repo / "calc.py").write_text("# uncommitted work in progress\n")
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setattr(twp.tempfile, "tempdir", str(scratch_root))
    before = repo_state(repo)

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 0, receipt["reason"]
    assert repo_state(repo) == before
    assert list(scratch_root.iterdir()) == []


# Fails with exit 3 when a git location variable leaks into the test process.
ENV_CHECKING_TEST_SCRIPT = (
    "import os, sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "if os.environ.get('GIT_DIR') or os.environ.get('GIT_WORK_TREE'):\n"
    "    sys.exit(3)\n"
) + exit_on("add(2, 3) == 5")


def test_inherited_git_location_variables_do_not_redirect_the_proof(
        repo, capsys, tmp_path, monkeypatch):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": ENV_CHECKING_TEST_SCRIPT}, "fix: add")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    git(decoy, "init", "-q")
    # As if the proof were launched from inside another repository's git hook.
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 0, receipt["reason"]


PATH_PRINTING_TEST_SCRIPT = (
    "import os, sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "print('cwd=' + os.getcwd())\n"
    "print('home=' + os.path.join(os.path.expanduser('~'), 'private'))\n"
    "print('home-exact=' + os.path.expanduser('~'))\n"
) + exit_on("add(2, 3) == 5")


def test_receipt_output_hides_local_paths(repo, capsys, tmp_path, monkeypatch):
    home = tmp_path / "home-dir"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": PATH_PRINTING_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 0, receipt["reason"]
    tails = "\n".join(leg["output_tail"] for leg in receipt["legs"])
    assert "cwd=<scratch>/before" in tails
    assert "home=~/private" in tails
    assert "home-exact=~\n" in tails
    assert str(home) not in tails
    assert "three-way-proof-" not in tails


# Exits 3 when the test process can see the caller's directory through PWD/OLDPWD.
PWD_CHECKING_TEST_SCRIPT = (
    "import os, sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "pwd = os.environ.get('PWD')\n"
    "if 'OLDPWD' in os.environ or (pwd and os.path.realpath(pwd) != os.getcwd()):\n"
    "    sys.exit(3)\n"
) + exit_on("add(2, 3) == 5")


def test_test_process_sees_its_leg_checkout_as_the_working_directory(
        repo, capsys, monkeypatch):
    # As if launched from a shell sitting in the caller's repository.
    monkeypatch.setenv("PWD", str(repo))
    monkeypatch.setenv("OLDPWD", str(repo))
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": PWD_CHECKING_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", "1")

    assert code == 0, receipt["reason"]


def test_scratch_directory_failure_is_an_error_without_local_paths(
        repo, capsys, monkeypatch):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    def no_usable_temp(*_args, **_kwargs):
        # The shape tempfile raises: it lists every directory tried, cwd included.
        raise FileNotFoundError(2, f"No usable temporary directory found in ['/tmp', '{repo}']")

    monkeypatch.setattr(twp.tempfile, "mkdtemp", no_usable_temp)

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert_error(code, receipt, "cannot create a scratch directory")
    assert str(repo) not in receipt["reason"]


def test_home_directory_is_redacted_whole_and_as_a_prefix_only(tmp_path, monkeypatch):
    home = str(tmp_path / "home-dir")
    monkeypatch.setenv("HOME", home)

    assert twp._redact_home(f"in {home} now") == "in ~ now"
    assert twp._redact_home(f"{home}/x/y") == "~/x/y"
    assert twp._redact_home(f"{home}2/x") == f"{home}2/x"


@pytest.mark.parametrize("exc_type", [twp.ProofError, OSError])
def test_errors_inside_the_proof_hide_the_scratch_location(repo, capsys, monkeypatch, exc_type):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    def failing_checkout(_clone, _tree, dest):
        raise exc_type(f"git worktree failed: could not create '{dest}'")

    monkeypatch.setattr(twp, "_checkout_tree", failing_checkout)

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 2
    assert receipt["verdict"] == "ERROR"
    assert "'<scratch>/before'" in receipt["reason"]
    assert "three-way-proof-" not in receipt["reason"]


def test_error_reasons_hide_the_home_directory(repo, capsys, tmp_path, monkeypatch):
    home = tmp_path / "home-dir"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"cannot read {home}/secret/thing")

    monkeypatch.setattr(twp, "_ls_tree", boom)

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 2
    assert "~/secret/thing" in receipt["reason"]
    assert str(home) not in receipt["reason"]


# --- S11: an internal crash is still an ERROR receipt, never a silent pass ---


def test_unexpected_internal_error_is_an_error_receipt(repo, capsys, monkeypatch):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(twp, "_ls_tree", boom)

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert code == 2
    assert receipt["verdict"] == "ERROR"
    assert "internal error" in receipt["reason"]
    assert "simulated crash" in receipt["reason"]


# --- S12: the expected failure exit code can be pinned ---

# Exits 2 (like a usage or collection error) on the buggy code, not 1.
WRONG_FAILURE_CODE_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
) + exit_on("add(2, 3) == 5", fail_code=2)


def test_pinned_failure_exit_code_rejects_other_failures(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": WRONG_FAILURE_CODE_SCRIPT}, "fix: add")

    unpinned, _ = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")
    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", "1")

    assert unpinned == 0
    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert "expected exit 1" in receipt["reason"]


def test_pinned_failure_exit_code_accepts_the_pinned_code(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", "1")

    assert code == 0, receipt["reason"]


@pytest.mark.parametrize("bad", ["0", "-1", "256"])
def test_pinned_failure_exit_code_must_be_a_failure_code(repo, capsys, bad):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", bad)

    assert_error(code, receipt, "--fail-exit-code must be")


# --- test-path classification ---


@pytest.mark.parametrize("path,expected", [
    ("test_calc.py", True),
    ("pkg/calc_test.py", True),
    ("conftest.py", True),
    ("tests/helpers.py", True),
    ("src/__tests__/calc.js", True),
    ("tests/fixtures/data.json", True),
    ("pkg/calc_test.go", True),
    ("web/calc.test.ts", True),
    ("web/calc.spec.js", True),
    ("spec/calc_spec.rb", True),
    ("src/CalcTest.java", True),
    # Test data and expectations: a fixture, golden file, or snapshot classed as
    # fix code would let "change the expectation" pass as a fix.
    ("testdata/add_2_3.golden", True),
    ("pkg/test_data/cases.csv", True),
    ("fixtures/user.json", True),
    ("web/__fixtures__/user.json", True),
    ("__snapshots__/calc.test.js.snap", True),
    ("src/snapshots/calc__add.snap", True),
    ("testing/helpers.py", True),
    ("web/__mocks__/fs.js", True),
    ("e2e/login.cy.ts", True),
    ("expected/add.golden", True),
    ("scripts/test_helpers.sh", True),
    ("lib/calc_test.exs", True),
    ("src/CalcTests.cs", True),
    ("calc.py", False),
    ("src/latest.py", False),
    ("contest.py", False),
    ("src/Contest.java", False),
    ("testing_utils.py", False),
    ("docs/test-plan.md", False),
    ("src/attestation.go", False),
])
def test_default_test_path_conventions(path, expected):
    assert twp.is_test_path(path) is expected


GOLDEN_TEST_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "print('THREEWAY-RAN:add-fix')\n"
    "expected = open('testdata/add_2_3.golden').read().strip()\n"
) + exit_on("str(add(2, 3)) == expected")


def test_changing_only_the_expected_output_is_not_a_fix(repo, capsys):
    commit_files(repo, {"testdata/add_2_3.golden": "5\n"}, "golden output")
    # The "fix" rewrites the golden file to match the buggy output; no code changes.
    fix = commit_files(repo, {"testdata/add_2_3.golden": "-1\n",
                              "test_calc.py": GOLDEN_TEST_SCRIPT}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix")

    assert_error(code, receipt, "nothing to revert")
    assert receipt["test_paths"] == ["test_calc.py", "testdata/add_2_3.golden"]


CUSTOM_LOCATION_GOLDEN_TEST = GOLDEN_TEST_SCRIPT.replace("from calc import add", (
    "import os\n"
    "sys.path.insert(0, os.getcwd())\n"
    "from calc import add"))


def test_test_path_globs_do_not_drop_the_test_data_defaults(repo, capsys):
    commit_files(repo, {"testdata/add_2_3.golden": "5\n"}, "golden output")
    # A test in a custom place, plus a rewritten golden file and no code change.
    fix = commit_files(repo, {"testdata/add_2_3.golden": "-1\n",
                              "checks/verify_add.py": CUSTOM_LOCATION_GOLDEN_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--test-path", "checks/*",
                              cmd=[sys.executable, "checks/verify_add.py"])

    assert_error(code, receipt, "nothing to revert")
    assert receipt["test_paths"] == ["checks/verify_add.py", "testdata/add_2_3.golden"]


CUSTOM_LOCATION_TEST = TEST_SCRIPT.replace("from calc import add", (
    "import os\n"
    "sys.path.insert(0, os.getcwd())\n"
    "from calc import add"))


def test_unrecognised_test_file_fails_safe_without_test_path(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "checks/verify_add.py": CUSTOM_LOCATION_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              cmd=[sys.executable, "checks/verify_add.py"])

    # Treated as fix code, the test is absent from the before leg: no marker.
    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert "marker" in receipt["reason"]


def test_test_path_globs_add_to_the_defaults(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "checks/verify_add.py": CUSTOM_LOCATION_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--test-path", "checks/*",
                              cmd=[sys.executable, "checks/verify_add.py"])

    assert code == 0, receipt["reason"]
    assert receipt["test_paths"] == ["checks/verify_add.py"]
    assert receipt["test_path_globs"] == ["checks/*"]


# --- human-readable summary and the script entry point ---

MODULE_PATH = Path(twp.__file__).resolve()


def test_script_entry_point_prints_a_human_summary(repo):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--repo", str(repo), "--fix", fix,
         "--test-id", "add-fix", "--", sys.executable, "test_calc.py"],
        capture_output=True, text=True, env=_clean_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "three-way fix proof: PROVEN"
    assert "marker   THREEWAY-RAN:add-fix" in result.stdout
    assert "on pass  THREEWAY-PASS:add-fix" in result.stdout
    for leg in ("before", "after", "reverted"):
        assert any(line.split()[:1] == [leg] and line.endswith("ok") for line in lines), leg


def test_human_summary_explains_a_failure(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": WEAK_TEST_SCRIPT}, "fix: add")

    code = twp.main(["--repo", str(repo), "--fix", fix, "--test-id", "add-fix",
                     "--", sys.executable, "test_calc.py"])
    out = capsys.readouterr().out

    assert code == 1
    assert out.splitlines()[0] == "three-way fix proof: NOT_PROVEN"
    assert "reason   before: expected failure, exited 0" in out
    assert "--- before output (tail) ---" in out
    assert "not run" in out


def test_human_summary_reports_an_error(repo, capsys):
    code = twp.main(["--repo", str(repo), "--fix", "no-such-branch", "--test-id", "add-fix",
                     "--", sys.executable, "test_calc.py"])
    out = capsys.readouterr().out

    assert code == 2
    assert out.splitlines()[0] == "three-way fix proof: ERROR"
    assert "no-such-branch" in out


# --- the command line never exits 0 without a PROVEN verdict ---


@pytest.mark.parametrize("flag", ["-h", "--help", "--he"])
def test_help_is_an_error_not_a_pass(repo, capsys, flag):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code = twp.main(["--repo", str(repo), "--json", "--fix", fix, "--test-id", "add-fix",
                     flag, "--", sys.executable, "test_calc.py"])
    receipt = json.loads(capsys.readouterr().out)

    assert code == 2
    assert receipt["verdict"] == "ERROR"
    assert receipt["legs"] == []


def test_abbreviated_options_are_rejected(repo, capsys):
    fix = commit_files(repo, {"calc.py": FIXED, "test_calc.py": TEST_SCRIPT}, "fix: add")

    code = twp.main(["--repo", str(repo), "--js", "--fix", fix, "--test-id", "add-fix",
                     "--", sys.executable, "test_calc.py"])
    out = capsys.readouterr().out

    assert code == 2
    assert out.splitlines()[0] == "three-way fix proof: ERROR"


# --- end to end with a real pytest regression test ---

PYTEST_REGRESSION_TEST = (
    "from calc import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    print('THREEWAY-RAN:add-fix')\n"
    "    assert add(2, 3) == 5\n"
    "    print('THREEWAY-PASS:add-fix')\n"
)


def pytest_cmd(*selector: str) -> list:
    # -rA shows captured stdout of passing and failing tests on their own lines.
    return [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
            *selector]


def test_real_pytest_fix_is_proven(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": PYTEST_REGRESSION_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", "1", cmd=pytest_cmd("test_calc.py"))

    assert code == 0, receipt["reason"]
    assert leg_summary(receipt) == [("before", 1, True, False, True),
                                    ("after", 0, True, True, True),
                                    ("reverted", 1, True, False, True)]


def test_wrong_pytest_selector_is_caught_by_the_marker(repo, capsys):
    fix = commit_files(
        repo, {"calc.py": FIXED, "test_calc.py": PYTEST_REGRESSION_TEST}, "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              cmd=pytest_cmd("test_calc.py", "-k", "no_such_test"))

    # pytest exits 5 ("no tests collected"), which looks like a failure on base.
    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert receipt["legs"][0]["exit_code"] == 5
    assert "marker" in receipt["reason"]


# --- the outcome must belong to the marked test, not to the whole command ---

TWO_BUG_CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n"
MUL_FIXED_CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
OTHER_PYTEST = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
# Cannot catch the add() bug: 2 - 0 == 2 + 0.
WEAK_PYTEST = (
    "from calc import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    print('THREEWAY-RAN:add-fix')\n"
    "    assert add(2, 0) == 2\n"
    "    print('THREEWAY-PASS:add-fix')\n"
)


def test_failure_of_another_selected_test_does_not_count(repo, capsys):
    commit_files(repo, {"calc.py": TWO_BUG_CALC, "test_other.py": OTHER_PYTEST},
                 "mul and its test")
    # Fixes mul() only; the marked test passed on base all along.
    fix = commit_files(repo, {"calc.py": MUL_FIXED_CALC, "test_calc.py": WEAK_PYTEST},
                       "fix: add")

    code, receipt = run_proof(capsys, repo, "--fix", fix, "--test-id", "add-fix",
                              "--fail-exit-code", "1",
                              cmd=pytest_cmd("test_calc.py", "test_other.py"))

    assert code == 1
    assert receipt["failed_leg"] == "before"
    assert "THREEWAY-PASS:add-fix" in receipt["reason"]
