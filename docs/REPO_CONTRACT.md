# Failclosed Repo Contract

Date: 2026-06-30

Status: binding repo-local contract.

## Current Name

- `failclosed`

## Recommended Name

- `failclosed`

## Role

- `oss-project`

## Purpose

`failclosed` is a Dan-owned OSS project for fail-closed merge admission control
for agent-written code. It runs an LLM reviewer through a configurable command
seam, hardened-parses the output, blocks ambiguous or contradictory merge-ready
claims, handles review-bot threads, and emits deterministic merge-gate statuses.

It is not the reviewer, not the code-fixing agent, not estate platform
ownership, not Orion Runtime substrate, not OAC business ownership, and not a
managed deploy or secret scope.

## Owns

- the fail-closed gate and status semantics
- multi-shape reviewer-output parser and contradiction checks
- reviewer command seam and context handling
- bot comment/thread fetching, classification, and resolution helpers
- local configuration example and public OSS documentation
- repo-local tests, lint config, leak gate, and release/package metadata

## Does Not Own

- the LLM reviewer command itself
- applying code fixes emitted as structured requests
- GitHub repository administration outside the configured GraphQL/API calls
- estate platform, runtime, governance, shared-infra, or OAC business ownership
- consumer repo merge policy unless separately wired by that consumer
- managed services, scheduled jobs, live secrets, or deploy targets

## Allowed Dependencies

- Python, pytest, ruff, GitHub CLI/API/GraphQL, and local config files
- arbitrary reviewer commands only through the documented stdin/stdout seam
- consumer repos only through explicit context JSON and consumer wiring
- estate doctrine from `orion-estate-audit`
- the personal-brand hackathon and OSS admission note

## Forbidden Logic / Forbidden Ownership

- failing open on unparseable, schema-invalid, or contradictory reviewer output
- treating model verdicts as authoritative without deterministic gate checks
- committing secrets, absolute home-directory paths, or private estate details
- turning this OSS repo into estate platform or runtime substrate without a
  separate consumer contract
- mutating code directly instead of emitting structured fix requests

## PR Reject Rules

- reject PRs that weaken fail-closed parser or gate semantics
- reject PRs that make the reviewer command trusted by default
- reject PRs that introduce secrets, private host paths, or unsanitized estate
  details
- reject PRs that add managed deploy, scheduled job, runtime, shared-infra, or
  secret-scope ownership by implication
- reject PRs that break CI lint, tests, or leak gate expectations

## Verification

For docs-only contract changes:

```bash
git diff --check
```

For implementation changes, follow `AGENTS.md`: run `ruff check .` and
`pytest -q`; CI also runs the leak gate and gitleaks.

## Basis

- `AGENTS.md`
- `README.md`
- `failclosed.toml.example`
- `run_review_pipeline.py`
- `parse_codex_output.py`
- `.github/workflows/ci.yml`
- `repos/repo_contract_registry_20260317.csv` in
  `OrionArchitekton/orion-estate-audit`
- `architecture/repo_contracts/dan_mercede_personal_brand_repo_contract_20260318.md`
  in `OrionArchitekton/orion-estate-audit`
- `architecture/PERSONAL_BRAND_HACKATHON_AND_OSS_PROJECT_ADMISSION_NOTE_20260621.md`
  in `OrionArchitekton/orion-estate-audit`
