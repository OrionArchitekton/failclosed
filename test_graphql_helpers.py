#!/usr/bin/env python3
"""Tests for graphql_helpers — mock gh CLI calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphql_helpers import (
    is_thread_resolved,
    reply_and_resolve,
    reply_to_thread,
    resolve_thread,
    thread_reply_exists,
)


class TestReplyToThread:
    def test_posts_reply_with_correct_body(self):
        mock_result = {
            "data": {
                "addPullRequestReviewThreadReply": {
                    "comment": {"id": "IC_abc123"}
                }
            }
        }
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = mock_result
            result = reply_to_thread("PRRT_thread1", "Fixed in abc1234.")
            assert result["data"]["addPullRequestReviewThreadReply"]["comment"]["id"] == "IC_abc123"
            mock_gql.assert_called_once()
            call_args = mock_gql.call_args
            assert call_args[0][1]["threadId"] == "PRRT_thread1"
            assert call_args[0][1]["body"] == "Fixed in abc1234."


class TestResolveThread:
    def test_resolves_thread(self):
        mock_result = {
            "data": {
                "resolveReviewThread": {
                    "thread": {"isResolved": True}
                }
            }
        }
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = mock_result
            result = resolve_thread("PRRT_thread1")
            assert result["data"]["resolveReviewThread"]["thread"]["isResolved"] is True
            mock_gql.assert_called_once()
            assert mock_gql.call_args[0][1]["threadId"] == "PRRT_thread1"


class TestIsThreadResolved:
    def test_returns_live_state_true(self):
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = {"data": {"node": {"isResolved": True}}}
            assert is_thread_resolved("PRRT_thread1") is True
            assert mock_gql.call_args[0][1]["threadId"] == "PRRT_thread1"

    def test_returns_false_when_unresolved_or_absent(self):
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = {"data": {"node": {"isResolved": False}}}
            assert is_thread_resolved("PRRT_thread1") is False
            mock_gql.return_value = {"data": {"node": {}}}  # field absent
            assert is_thread_resolved("PRRT_thread1") is False


class TestThreadReplyExists:
    def test_true_when_exact_body_present(self):
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = {
                "data": {"node": {"comments": {"nodes": [
                    {"body": "original bot finding"},
                    {"body": "Fixed in abc1234."},
                ]}}}
            }
            assert thread_reply_exists("PRRT_thread1", "Fixed in abc1234.") is True
            assert mock_gql.call_args[0][1]["threadId"] == "PRRT_thread1"

    def test_false_when_body_absent_or_node_empty(self):
        with patch("graphql_helpers.run_graphql") as mock_gql:
            mock_gql.return_value = {
                "data": {"node": {"comments": {"nodes": [{"body": "other"}]}}}
            }
            assert thread_reply_exists("PRRT_thread1", "Fixed in abc1234.") is False
            mock_gql.return_value = {"data": {"node": {}}}  # comments field absent
            assert thread_reply_exists("PRRT_thread1", "Fixed in abc1234.") is False


class TestReplyAndResolve:
    def test_calls_reply_then_resolve_and_returns_combined(self):
        with patch("graphql_helpers.is_thread_resolved", return_value=False), \
             patch("graphql_helpers.thread_reply_exists", return_value=False), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_reply.return_value = {
                "data": {"addPullRequestReviewThreadReply": {"comment": {"id": "IC_1"}}}
            }
            mock_resolve.return_value = {
                "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
            }

            result = reply_and_resolve("PRRT_thread1", "Fixed in abc1234.")
            mock_reply.assert_called_once_with("PRRT_thread1", "Fixed in abc1234.")
            mock_resolve.assert_called_once_with("PRRT_thread1")
            assert result["thread_id"] == "PRRT_thread1"
            assert result["reply_comment_id"] == "IC_1"
            assert result["resolved"] is True
            assert result["skipped"] is None

    def test_does_not_resolve_if_reply_fails(self):
        with patch("graphql_helpers.is_thread_resolved", return_value=False), \
             patch("graphql_helpers.thread_reply_exists", return_value=False), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_reply.side_effect = RuntimeError("GraphQL call failed")

            with pytest.raises(RuntimeError, match="GraphQL call failed"):
                reply_and_resolve("PRRT_thread1", "Fixed.")
            mock_resolve.assert_not_called()

    def test_skips_reply_but_resolves_on_partial_failure_retry(self):
        # A prior attempt posted the reply but its resolve failed, leaving the thread
        # unresolved with the reply already present. The retry must NOT post a second
        # identical reply — it skips the reply and only (re-)resolves, so we never
        # leave duplicate irreversible PR comments.
        with patch("graphql_helpers.is_thread_resolved", return_value=False), \
             patch("graphql_helpers.thread_reply_exists", return_value=True), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_resolve.return_value = {
                "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
            }
            result = reply_and_resolve("PRRT_thread1", "Fixed in abc1234.")
            mock_reply.assert_not_called()
            mock_resolve.assert_called_once_with("PRRT_thread1")
            assert result["resolved"] is True
            assert result["reply_comment_id"] is None
            assert result["skipped"] == "reply-already-posted"

    def test_reply_dedup_probe_error_falls_back_to_posting(self):
        # If the reply-existence probe itself errors, fall back to posting the reply
        # rather than turning a read failure into a skipped reply (best-effort dedup,
        # never suppress the action on probe error).
        with patch("graphql_helpers.is_thread_resolved", return_value=False), \
             patch("graphql_helpers.thread_reply_exists",
                   side_effect=RuntimeError("probe failed")), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_reply.return_value = {
                "data": {"addPullRequestReviewThreadReply": {"comment": {"id": "IC_7"}}}
            }
            mock_resolve.return_value = {
                "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
            }
            result = reply_and_resolve("PRRT_thread1", "Fixed.")
            mock_reply.assert_called_once()
            mock_resolve.assert_called_once()
            assert result["reply_comment_id"] == "IC_7"
            assert result["skipped"] is None

    def test_skips_both_when_already_resolved(self):
        # The thread was resolved since the initial fetch (another run / human /
        # prior cycle): re-checking the LIVE state must skip BOTH the reply and
        # the resolve, so we never double-post or redundantly re-resolve.
        with patch("graphql_helpers.is_thread_resolved", return_value=True), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            result = reply_and_resolve("PRRT_thread1", "Fixed in abc1234.")
            mock_reply.assert_not_called()
            mock_resolve.assert_not_called()
            assert result["resolved"] is True
            assert result["skipped"] == "already-resolved"
            assert result["reply_comment_id"] is None

    def test_falls_back_to_mutate_when_probe_errors(self):
        # A point-query failure must not become a NEW hard failure: fall back to
        # the original reply+resolve (best-effort dedup, never a regression).
        with patch("graphql_helpers.is_thread_resolved",
                   side_effect=RuntimeError("probe failed")), \
             patch("graphql_helpers.thread_reply_exists", return_value=False), \
             patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_reply.return_value = {
                "data": {"addPullRequestReviewThreadReply": {"comment": {"id": "IC_9"}}}
            }
            mock_resolve.return_value = {
                "data": {"resolveReviewThread": {"thread": {"isResolved": True}}}
            }
            result = reply_and_resolve("PRRT_thread1", "Fixed.")
            mock_reply.assert_called_once()
            mock_resolve.assert_called_once()
            assert result["resolved"] is True
