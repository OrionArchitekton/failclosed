# failclosed

![License: MIT](https://img.shields.io/github/license/OrionArchitekton/failclosed?style=flat&color=1a1a1a)
![CI](https://github.com/OrionArchitekton/failclosed/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-1a1a1a?style=flat&logo=python&logoColor=3776AB)

Fail-closed merge admission control for agent-written code.

![failclosed gate: run the reviewer, distrust the verdict, halt on ambiguity](assets/gate-diagram.png)

## The problem

AI coding agents write and merge code faster than review can govern it. Most
LLM-review tools relay the model's own verdict: when the model says "looks
good," the change is treated as mergeable. That is advisory governance. It fails
open. When the reviewer's output is truncated, malformed, or self-contradictory
— for example, a `fix` verdict where no finding cites a file — an advisory tool
defaults to letting the merge through. The reviewer becomes a single point of
silent failure precisely when its output is least trustworthy.

## What failclosed does

failclosed runs an LLM reviewer and then refuses to trust its verdict. The
reviewer output passes through a hardened parser, and a gate **refuses to report
`MERGE_READY`** when the output is:

- unparseable,
- schema-invalid, or
- self-contradictory (a `fix` verdict with no finding citing a file).

Enforcement precedes the merge decision. When the review cannot be deterministically
evaluated, the merge does not proceed. That is the whole point: a merge that can
proceed when governance cannot be evaluated is not governed.

It runs one path, with no advisory fallback:

1. **Wait for review bots**, then fetch and classify their threads.
2. **Run the reviewer** (standard pass, then an adversarial pass) through a
   configurable command seam.
3. **Parse** the output through the multi-shape parser.
4. **Fail-closed gate**: any unparseable / schema-invalid / contradictory result
   becomes a `parse_error`, which blocks `MERGE_READY`.
5. **Emit fix requests** (structured; the agent applies them — failclosed does
   not edit code inline).
6. **Resolve bot threads** via the GitHub GraphQL API.
7. **Report** `MERGE_READY`, `FINDINGS_REMAIN`, or `THREADS_UNRESOLVED`.

## The reviewer seam

The reviewer is a command, not a vendor. `reviewer_command` receives the review
prompt on **stdin** and returns the reviewer output on **stdout**. The parser
accepts structured JSON, JSON embedded in prose, or rendered native text, so any
command that honors the contract works:

```toml
reviewer_command = "codex exec --json -"
```

The product is not the reviewer. The product is the gate that distrusts it.

## Quickstart

Requires Python 3.9+ and the `gh` CLI (authenticated). Reviewer overrides via
`failclosed.toml` require Python 3.11+.

```bash
git clone https://github.com/OrionArchitekton/failclosed
cd failclosed

# Optional: override defaults (reviewer command, thresholds, trunk branches).
cp failclosed.toml.example failclosed.toml

# Describe the push in a small context JSON.
cat > ctx.json <<'JSON'
{ "repo_root": "/path/to/your/repo", "branch": "feature-x", "sha": "abc1234" }
JSON

python run_review_pipeline.py --context ctx.json --json
```

`--dry-run` runs without writing fixes or resolving threads. `--non-blocking`
returns `BOTS_PENDING` immediately when the push is younger than the bot-wait
window instead of sleeping.

## Wiring it in

Write the context JSON from a `post-push` git hook or a GitHub Action after a
push to a PR branch, then invoke failclosed. The deterministic status is the
signal a merge gate consumes: `MERGE_READY` is the only status that clears the
gate.

## Configuration

All keys are optional and self-contained; see
[`failclosed.toml.example`](failclosed.toml.example). No file outside the repo
is required to run.

## Status codes

| Status | Meaning |
|---|---|
| `MERGE_READY` | Reviewer output parsed and clean; no open fixes or threads. |
| `FINDINGS_REMAIN` | Fixes outstanding, or a review phase failed to evaluate (fail-closed). |
| `THREADS_UNRESOLVED` | Bot review threads still open. |
| `BOTS_PENDING` | Push too recent for bots to have landed (non-blocking mode). |

## Tests

```bash
pip install pytest
pytest -q
```

---

failclosed is a narrow, runnable demonstration of one principle: governance
evaluated deterministically, before state mutation, in a fail-closed
configuration — applied here to the merge boundary. The same enforcement model,
applied across an enterprise agent stack, is a
[Runtime Governance Readiness Scan](https://danmercede.com).
