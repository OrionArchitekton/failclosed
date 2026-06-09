#!/usr/bin/env python3
"""Tests for graphql_helpers — mock gh CLI calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphql_helpers import reply_and_resolve, reply_to_thread, resolve_thread


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


class TestReplyAndResolve:
    def test_calls_reply_then_resolve_and_returns_combined(self):
        with patch("graphql_helpers.reply_to_thread") as mock_reply, \
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

    def test_does_not_resolve_if_reply_fails(self):
        with patch("graphql_helpers.reply_to_thread") as mock_reply, \
             patch("graphql_helpers.resolve_thread") as mock_resolve:
            mock_reply.side_effect = RuntimeError("GraphQL call failed")

            with pytest.raises(RuntimeError, match="GraphQL call failed"):
                reply_and_resolve("PRRT_thread1", "Fixed.")
            mock_resolve.assert_not_called()
