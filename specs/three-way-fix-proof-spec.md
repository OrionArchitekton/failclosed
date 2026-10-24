# Three-way fix proof: spec

Status: draft for review (feature branch `feat/three-way-fix-proof`).

## Problem

An agent that claims "fixed, and here is the test" is making two claims at
once: the test catches the bug, and the fix is what makes it pass. A test that
passes after the fix proves neither. It may never have failed, it may not have
run at all (wrong selector, skipped, not collected), it may pass because of run
order or leftover state rather than the code change, or the failure it showed
may have belonged to some other test the command also ran.

A fix only counts when its regression test is shown to:

1. **fail before** the fix,
2. **pass after** the fix,
3. **fail again** when the fix is reverted,

and the test prints a **ran-marker** in every one of those runs, proving it
actually executed, plus a **pass-marker** only in the run where it passed,
proving each outcome is its own. Anything short of that is not a proof.

## Terms

- **Base**: the code before the fix (a revision).
- **Fix**: the change under test, given either as a revision (diffed against
  its base) or as a patch file applied to a base.
- **Test paths**: changed files that belong to tests, including test data and
  expectations (fixtures, golden files, snapshots). They are kept at their fix
  version in every leg, so the same test runs three times.
- **Fix paths**: every other changed file. These are what the fix "is", and
  what gets reverted.
- **Legs**: the three runs, always in this order:
  - `before`: base code plus the fix's test paths. Must fail.
  - `after`: the fix. Must pass.
  - `reverted`: the fix with its fix paths restored to base. Must fail.
- **Ran-marker**: the exact line `THREEWAY-RAN:<test-id>`, printed by the test
  itself on its own output line (stdout or stderr). The test prints it after its
  imports and setup, immediately before the behavior under test runs.
- **Pass-marker**: the exact line `THREEWAY-PASS:<test-id>`, printed by the test
  as its last step, so it appears only when the test itself passed.
- **Verdicts**: `PROVEN` (exit 0), `NOT_PROVEN` (exit 1), `ERROR` (exit 2).
  Only `PROVEN` with exit 0 clears the check.

## Scenarios and acceptance criteria

### S1. A real fix is proven
Given a fix whose test fails on base, passes on the fix, and fails with the fix
reverted, printing the ran-marker each time and the pass-marker only on the
fix, the verdict is `PROVEN` and the exit code is 0. The receipt names the base
and fix revisions, the test and fix paths, and each leg's exit code and marker
status.

### S2. A test that does not catch the bug is rejected
If the `before` leg passes, the verdict is `NOT_PROVEN`, naming `before`.

### S3. A fix that does not fix is rejected
If the `after` leg fails, or exits 0 without the pass-marker, the verdict is
`NOT_PROVEN`, naming `after`.

### S4. A pass that came from run order or leftover state is rejected
If the `reverted` leg passes, the verdict is `NOT_PROVEN`, naming `reverted`.
Each leg runs in its own fresh checkout, so in-tree leftovers cannot leak
between legs; state outside the checkout is what this leg catches.

### S5. A test that did not provably run is rejected
If any leg's output lacks the ran-marker as a whole line, that leg fails and
the verdict is `NOT_PROVEN`. A marker that only appears inside a longer line
(for example a test runner echoing the test's source in a traceback), or a
marker for a different test id that merely starts with the expected one, does
not count. Only a newline ends a line: other line-break characters inside an
echoed line do not make a marker whole.

### S6. A hung or killed test is rejected
A leg that exceeds the timeout, or whose process is killed by a signal, fails;
the verdict is `NOT_PROVEN`. Legs after the first failing leg are not run and
are reported as not run.

### S7. A fix with nothing to revert is an error
If the fix changes only test paths (or nothing at all), there is nothing to
revert and the proof cannot be evaluated: the verdict is `ERROR`. A "fix" that
only rewrites an expectation (a golden file, a snapshot, a fixture) lands here.

### S8. Ambiguous or invalid input is an error
The verdict is `ERROR`, before any test runs, when: the fix revision is a merge
commit or root commit and no base is given; an explicit base is not an ancestor
of the fix revision; a revision does not resolve; the patch does not apply to
the base; a path is a file in one version and a directory in the other, so a
leg's code cannot be built exactly; no test command is given; the test id is
not a plain token; or both or neither of fix revision and fix patch are given.

### S9. A fix given as a patch file is proven the same way
Given a base revision and a patch file, the patch is applied to the base to
form the fix, and the same three legs run with the same rules.

### S10. The caller's repository is left untouched
Running the check creates no branches, worktrees, or working-tree changes in
the caller's repository; all checkouts live in a throwaway location that is
removed afterwards.

### S11. The result is a machine-readable receipt
With `--json`, the output is a single JSON object carrying the verdict and the
leg results. An unexpected internal error, a usage error, an abbreviated
option, or a request for help still yields a verdict of `ERROR` with exit code
2, never a silent or partial result. Exit 0 always means `PROVEN`.

### S12. The expected failure exit code can be pinned
With `--fail-exit-code N`, a failing leg only counts if it exits with exactly
`N` (for example pytest's 1 for "tests failed", which excludes collection and
usage errors).

### S13. The outcome belongs to the marked test
A command that runs several tests cannot borrow another test's result. A
failing leg whose output carries the pass-marker is `NOT_PROVEN` (the marked
test passed; the failure came from something else), and a passing leg without
the pass-marker is `NOT_PROVEN`.

## Constraints

- No new runtime dependencies: Python standard library plus the `git` CLI.
- The test command is an argument vector after `--`, run without a shell.
- Test paths are chosen by `--test-path` globs when given; otherwise by a
  documented default set of test-file, test-directory, and test-data
  conventions. Classification is one-sided. Treating a fix file as a test can
  only make a proof fail or error. Treating a test-support file as fix code is
  the dangerous direction, since changing the expectation would then count as
  the fix, so the defaults lean toward "test" and the receipt lists the fix
  paths for a reviewer to confirm.
- Git hooks never run during the check, and inherited git location variables
  (for example from a calling git hook) do not redirect it.
- The receipt contains no absolute local paths of the caller's machine beyond
  what the caller put in the test command: in captured test output, the
  throwaway checkout location reads `<scratch>` and the home directory `~`.

## Known limits

These are outside what a black-box run can decide; a reviewer covers them.

- A test that fails before reaching the behavior under test (for example by
  calling a function the fix adds) still fails its leg. Printing the
  ran-marker after imports and setup turns the common case, an import error,
  into a missing marker; the reviewer checks that the test exercises code that
  exists before the fix.
- A test-support file outside the default conventions (a helper module, a
  fixture in an unusual place) is treated as fix code unless `--test-path`
  names it. The fix paths are listed in the receipt for this reason.
- A flaky test can satisfy all three legs by chance.
- The markers are printed by the test, so a test written to game them (printing
  the pass-marker on some condition other than its own assertions) defeats the
  proof. The check catches weak, mis-selected, and order-dependent tests, not a
  dishonest one; the test itself still gets read.
- Every case of a parametrized test prints the same markers unless it prints a
  per-case id, so a case that passes on base makes the `before` leg fail its
  check. Select the case that reproduces the bug, or give each case its own id.
- A process that moves itself out of the leg's process group can outlive the
  proof; its leg still fails as a timeout.

## Test seam

One seam: the command-line entry point, driven against real temporary git
repositories with a small script as the test under proof. Marker matching gets
direct unit cases because its edge cases are textual.
