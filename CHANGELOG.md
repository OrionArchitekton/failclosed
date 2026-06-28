# Changelog

All notable changes to failclosed are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may carry
behavior changes).

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
