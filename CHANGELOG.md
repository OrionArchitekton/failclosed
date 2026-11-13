# Changelog

All notable changes to failclosed are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may carry
behavior changes).

## [Unreleased]

### Fixed

Eight **fail-open** paths, each of which ended `MERGE_READY`. They share one
root: the gate enumerated bad states and read missing evidence as clean. It now
admits `MERGE_READY` only on positive evidence that every required phase ran,
reported success, and carried the fields it must; anything else is
`FINDINGS_REMAIN`.

- **Fail-open: `fix` verdict with an empty findings list on the JSON path.** The
  rule that a `fix` or `needs-attention` verdict must carry a finding citing a
  file existed only on the native text path. The strict-JSON and extracted-JSON
  paths now apply it too (verdict compared case-insensitively), so the phase
  reports `parse_error`.
- **Fail-open: an errored bot-thread phase read as zero open threads.** When
  phase 1 or phase 5 failed (for example, `gh` not authenticated), the missing
  `remaining_threads` count defaulted to 0. Both thread phases must now report
  `ok`, and phase 5 must carry an integer count.
- **Fail-open: a reviewer that cannot run.** A reviewer command that could not
  start left both review phases at `error`, and the gate blocked only on
  `parse_error` or `timeout`. The reviewer's exit code was not read, so a clean
  verdict printed before a non-zero exit cleared the gate. A non-zero exit is
  now a phase `error` (with its `returncode`), and every required review phase
  must report `ok`; the adversarial phase is required unless
  `adversarial_review` is off. Behavior change: an empty `reviewer_command`
  (phases `skipped`) and `--dry-run` (phases `dry_run`, the reviewer never runs)
  no longer report `MERGE_READY`.
- **Fail-open: a planted verdict object in prose won.** For output that is
  prose with JSON in it, the parser took the first JSON object it could decode,
  so a verdict object quoted ahead of the reviewer's own (for example, from a
  file under review) decided the review. More than one top-level object with a
  `verdict` key is now refused as `parse_error` instead of guessed at, without
  falling back to the native text parser. So is a single verdict object that
  is not the first JSON object in the prose: picking it would let a planted
  approval win behind any other JSON. Strict single-object JSON, and prose
  whose first JSON object is its only verdict object, parse as before.
- **Fail-open: the first plain-text `Verdict:` line won.** The native text
  parser took the first `Verdict:` line, so a `Verdict: pass` quoted ahead of
  the reviewer's own verdict decided the review. More than one verdict line,
  agreeing or not and decorated or not (`**Verdict**:`, `## Verdict:`,
  `> Verdict:`), is now refused as `parse_error`.
- **Fail-open: a null or missing verdict.** On the JSON paths only a missing
  `verdict` key was flagged; a null, blank, or non-string verdict was schema
  valid. It now counts as missing (`parse_error`). On the native text path,
  finding markers with no `Verdict:` line still imply `fix` (current Codex
  output has no verdict line), which is not an approval.
- **Fail-open: any verdict value cleared the gate.** The gate never checked
  the verdict, so an unknown verdict such as `reject`, with no findings or
  only below-threshold ones, was `MERGE_READY`. Only a known approving
  verdict, `pass` or `approve` (case-insensitive), can now reach
  `MERGE_READY`. Behavior change: a `fix` or `needs-attention` verdict whose
  findings all sit below `fix_threshold` now blocks (`FINDINGS_REMAIN` with no
  fix requests) instead of clearing the gate.
- **Fail-open: a P-level or unknown finding severity was dropped.** On the JSON
  paths a finding's severity was checked only for presence, and the threshold
  filter reads an unknown severity as below every threshold. So a `pass`
  verdict carrying a `P0` finding produced no fix request and reached
  `MERGE_READY`. A P-level now maps to its severity (`P0` is `critical`), and
  any other value, including a non-string, is schema-invalid (`parse_error`).

### Added

- **Three-way fix proof** (`three_way_proof.py`). A fix only counts when its
  regression test fails before the fix, passes after it, and fails again with
  the fix reverted. In every run the test prints the ran-marker
  `THREEWAY-RAN:<test-id>` as a whole line, and it prints the pass-marker
  `THREEWAY-PASS:<test-id>` only where it passed, so a marked test that
  passed cannot borrow another test's failure. Each leg runs in a fresh checkout of a throwaway clone,
  so the caller's repository is never touched. Test data (fixtures, golden
  files, snapshots) counts as test paths, so rewriting an expectation is never
  taken for a fix. Ambiguity fails closed: a missing marker, a timeout, a
  signal-killed test, or a pass that survives the revert is `NOT_PROVEN`
  (exit 1); a merge or root commit without `--base`, a base that is not an
  ancestor of the fix, a fix that touches only tests, a patch that does not
  apply, a leg tree that cannot be built exactly, a usage error or `--help`, or
  an internal error is `ERROR` (exit 2). Spec:
  `specs/three-way-fix-proof-spec.md`.

## [0.2.0] — 2026-06-28

Gate-hardening release. Two of these were latent **fail-open** paths — a merge
could be reported `MERGE_READY` while a real, unresolved review signal was still
open. That is the exact failure class failclosed exists to prevent, so they are
the headline of this release.

### Fixed

- **Fail-open: Codex review threads silently dropped.** The GitHub Codex reviewer
  authors review threads as `chatgpt-codex-connector` (no `[bot]` suffix, not
  `^gemini-`/`^copilot-`). It was not in the bot-author allowlist, so its
  unresolved threads were classified as human and dropped before the
  remaining-threads count — letting the gate return `MERGE_READY` with an open
  Codex P1 thread still unresolved. Added `chatgpt-codex-connector` and the
  `^chatgpt-codex` pattern to `bot_authors.yaml`.
- **Fail-open: real findings auto-resolved by substring match.** The
  informational-thread classifier matched bare substrings (`receipt`, `summary`,
  `passed`, `informational`, …), so a genuine pathless finding that merely *used*
  one of those words ("No findings are emitted when the parser fails closed") was
  misclassified as a receipt and auto-resolved. Replaced with a **fail-closed
  pure-receipt check**: a pathless thread auto-resolves only when, after carving
  out every matched receipt **phrase**, no alphanumeric content remains — the body
  must be *nothing but* a receipt. The residual check is Unicode-aware and
  NFKC-normalizes the body first, so a real finding in any language ("No findings.
  Авторизация сломана"), a fullwidth-homoglyph disguise, or one that smuggles a
  claim past a fixed word list ("No findings since v2 broke", "No findings. Action
  required.") all keep the thread open. Unlike a denylist — which natural language
  can always append a real claim past — this is fail-closed by construction:
  anything left after the receipt can only keep a thread open, never dismiss one.

### Added

- **Parser currency for codex CLI ≥ ~0.13x native review.** Newer Codex renders
  findings as `[P1] Title — path:start-end` (a P-level marker and a trailing
  em-dash `path:range`, with no `Verdict:` line). The hardened parser now
  recognizes `P0–P3` markers (mapped onto the `critical/high/medium/low` enum)
  and the trailing em-dash/en-dash/`--` path layout, alongside the existing
  leading-path and paren-wrapped layouts. Without this, current Codex output was
  treated as unparseable.
- **Idempotent thread reply/resolve.** `graphql_helpers` gained
  `is_thread_resolved()` and `thread_reply_exists()`, and `reply_and_resolve()`
  is now idempotent against double-fire: it re-checks the thread's live state
  before mutating and skips a redundant identical reply on a partial-failure
  retry. Probes are best-effort — a read failure falls back to the plain action
  and never suppresses the resolve.

## [0.1.0] — 2026-06-21

Initial public release: fail-closed merge admission control for agent-written
code — run an LLM reviewer through a command seam, hardened-parse the output, and
refuse `MERGE_READY` on unparseable, schema-invalid, or self-contradictory
verdicts.

[0.2.0]: https://github.com/OrionArchitekton/failclosed/releases/tag/v0.2.0
[0.1.0]: https://github.com/OrionArchitekton/failclosed/releases/tag/v0.1.0
