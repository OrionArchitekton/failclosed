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


def reply_and_resolve(thread_id: str, body: str) -> Dict[str, Any]:
    """Post a reply and then resolve the thread. Returns combined result."""
    reply_result = reply_to_thread(thread_id, body)
    resolve_result = resolve_thread(thread_id)
    return {
        "thread_id": thread_id,
        "reply_comment_id": (
            reply_result.get("data", {})
            .get("addPullRequestReviewThreadReply", {})
            .get("comment", {})
            .get("id")
        ),
        "resolved": (
            resolve_result.get("data", {})
            .get("resolveReviewThread", {})
            .get("thread", {})
            .get("isResolved", False)
        ),
    }
