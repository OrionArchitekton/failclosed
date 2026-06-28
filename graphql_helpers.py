#!/usr/bin/env python3
"""GraphQL helpers for replying to and resolving PR review threads via gh CLI."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict

REPLY_MUTATION = """\
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $threadId,
    body: $body
  }) {
    comment { id }
  }
}
"""

RESOLVE_MUTATION = """\
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { isResolved }
  }
}
"""

THREAD_STATE_QUERY = """\
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread { isResolved }
  }
}
"""

THREAD_COMMENTS_QUERY = """\
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(last: 100) {
        nodes { body }
      }
    }
  }
}
"""


def run_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a GraphQL mutation via gh api graphql."""
    cmd = ["gh", "api", "graphql", "-F", "query=@-"]
    for key, value in variables.items():
        cmd += ["-F", f"{key}={value}"]

    result = subprocess.run(
        cmd,
        input=query,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"GraphQL call failed (exit {result.returncode}):\n{result.stderr}"
        )
    payload = json.loads(result.stdout)
    if "errors" in payload and payload["errors"]:
        raise RuntimeError(
            f"GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}"
        )
    return payload


def reply_to_thread(thread_id: str, body: str) -> Dict[str, Any]:
    """Post a reply comment on a review thread."""
    return run_graphql(REPLY_MUTATION, {"threadId": thread_id, "body": body})


def resolve_thread(thread_id: str) -> Dict[str, Any]:
    """Mark a review thread as resolved."""
    return run_graphql(RESOLVE_MUTATION, {"threadId": thread_id})


def is_thread_resolved(thread_id: str) -> bool:
    """Point-query a review thread's LIVE isResolved state (external fact).

    Used to make reply_and_resolve idempotent: the pipeline's initial thread
    fetch can be stale by the time we mutate (another run, a human, or a prior
    fix cycle may have resolved it). Returns False if the field is absent.
    """
    payload = run_graphql(THREAD_STATE_QUERY, {"threadId": thread_id})
    node = (payload.get("data", {}) or {}).get("node") or {}
    return bool(node.get("isResolved", False))


def thread_reply_exists(thread_id: str, body: str) -> bool:
    """Return True if a comment with exactly this body already exists on the thread.

    Used to make the REPLY side of reply_and_resolve idempotent: if a prior attempt
    posted the reply but then failed to resolve (partial failure), a retry would
    otherwise re-post the identical "Fixed in <sha>" comment. We point-query the
    thread's existing comment bodies and skip the reply if our exact body is already
    there. Returns False if the field is absent (so we fall through to posting).
    """
    payload = run_graphql(THREAD_COMMENTS_QUERY, {"threadId": thread_id})
    node = (payload.get("data", {}) or {}).get("node") or {}
    nodes = ((node.get("comments") or {}).get("nodes")) or []
    return any((c or {}).get("body") == body for c in nodes)


def reply_and_resolve(thread_id: str, body: str) -> Dict[str, Any]:
    """Post a reply and then resolve the thread — idempotent against double-fire.

    Idempotence is layered:
    1. Re-check the thread's LIVE isResolved (not the possibly-stale initial fetch).
       If it was resolved since, skip BOTH the reply and the resolve so we never
       double-post "Fixed in ..." or redundantly re-resolve.
    2. If unresolved, re-check whether our exact reply body is already on the thread
       before posting. This covers the PARTIAL-FAILURE retry path: a prior attempt
       posted the reply but its resolve failed, leaving the thread unresolved; the
       retry must NOT post a second identical reply — it just (re-)resolves.

    Both probes are best-effort dedup: if a probe itself errors, fall back to the
    plain action (post / resolve) rather than turning a read failure into a NEW hard
    failure. The dedup is strictly conservative — it can only ever SKIP a redundant
    irreversible PR comment, never suppress the resolve.

    NOTE (left open for design review): this does not guard against two CONCURRENT
    callers both passing the live-state check before either resolves. A durable
    per-thread lock would be needed for that; GraphQL offers no such primitive here.
    """
    try:
        already_resolved = is_thread_resolved(thread_id)
    except Exception:  # noqa: BLE001 - probe is best-effort dedup; fall back on error
        already_resolved = False
    if already_resolved:
        return {
            "thread_id": thread_id,
            "reply_comment_id": None,
            "resolved": True,
            "skipped": "already-resolved",
        }

    # Reply-side idempotence: skip the reply if our exact body already landed on a
    # prior (partially-failed) attempt; still proceed to resolve.
    try:
        reply_already_posted = thread_reply_exists(thread_id, body)
    except Exception:  # noqa: BLE001 - probe is best-effort dedup; fall back on posting
        reply_already_posted = False

    reply_comment_id = None
    if reply_already_posted:
        reply_skipped = "reply-already-posted"
    else:
        reply_result = reply_to_thread(thread_id, body)
        reply_comment_id = (
            reply_result.get("data", {})
            .get("addPullRequestReviewThreadReply", {})
            .get("comment", {})
            .get("id")
        )
        reply_skipped = None

    resolve_result = resolve_thread(thread_id)
    return {
        "thread_id": thread_id,
        "reply_comment_id": reply_comment_id,
        "resolved": (
            resolve_result.get("data", {})
            .get("resolveReviewThread", {})
            .get("thread", {})
            .get("isResolved", False)
        ),
        "skipped": reply_skipped,
    }
