# AGENTS.md — failclosed

## Repo Role

failclosed is fail-closed merge admission control for agent-written code: it
runs an LLM reviewer through a configurable command seam, hardened-parses the
output, and refuses to report `MERGE_READY` when the verdict is unparseable,
schema-invalid, or self-contradictory. v0.1 runs from a clone
(`python run_review_pipeline.py --context ctx.json --json`); packaging into an
installable distribution is a tracked follow-up.

## Layout

Flat single-directory layout, no package structure yet:

- `run_review_pipeline.py` — orchestrator: bot wait, reviewer phases, fail-closed gate, status report
- `parse_codex_output.py` — multi-shape hardened parser (structured JSON, JSON embedded in prose, native text)
- `resolve_bot_threads.py`, `fetch_comments.py`, `graphql_helpers.py` — bot review-thread handling via the GitHub GraphQL API
- `bot_authors.yaml` — review-bot author classification
- `failclosed.toml.example` — every optional configuration key, documented
- `test_*.py` — pytest suites alongside the modules they cover

## Boundaries

Owns:

- the fail-closed gate: parsing, contradiction checks, and status semantics
  (`MERGE_READY`, `FINDINGS_REMAIN`, `THREADS_UNRESOLVED`, `BOTS_PENDING`)
- the reviewer command seam (`reviewer_command`: prompt on stdin, output on stdout)
- fetching, classifying, and resolving bot review threads

Does not own:

- the reviewer itself — any command honoring the stdin/stdout contract works
- applying fixes — it emits structured fix requests; the calling agent edits code

This repo is a sanitized public mirror. Contributions must not introduce
absolute home-directory paths or secrets — the CI leak gate fails the build
on either.

## Start Here

- [README.md](README.md) — problem statement, pipeline walkthrough, status codes
- [failclosed.toml.example](failclosed.toml.example) — configuration reference
- [.github/workflows/ci.yml](.github/workflows/ci.yml) — lint/test job plus the leak gate
- [run_review_pipeline.py](run_review_pipeline.py) — entry point and gate logic
- [parse_codex_output.py](parse_codex_output.py) — the hardened parser

## Validation

```bash
ruff check .
pytest -q
```

CI runs the same two commands, then the leak gate (no absolute home paths in
tracked files, gitleaks secret scan). Ruff config lives in `pyproject.toml`;
the per-file `E402` ignores there are intentional, not defects.
