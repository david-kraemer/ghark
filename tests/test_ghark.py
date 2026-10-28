"""Behavior checks for pure projections, session state, and GitHub reads."""

import asyncio
import dataclasses
import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
from collections.abc import Callable
from typing import Any

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage
from mcp.types import ServerCapabilities

import ghark.channel
import ghark.github
import ghark.models
import ghark.session
import ghark.watcher


class PullRequestReferenceTests(unittest.TestCase):
    def test_parse_reference_and_url(self) -> None:
        expected = ghark.models.PullRequestRef("owner", "repo", 12)
        self.assertEqual(
            ghark.models.PullRequestRef.from_input("owner/repo#12"), expected
        )
        self.assertEqual(
            ghark.models.PullRequestRef.from_input(
                "https://github.com/owner/repo/pull/12"
            ),
            expected,
        )

    def test_reject_invalid_reference(self) -> None:
        with self.assertRaises(ValueError):
            ghark.models.PullRequestRef.from_input(
                "https://example.com/owner/repo/pull/12"
            )

    def test_reject_invalid_direct_reference(self) -> None:
        with self.assertRaises(ValueError):
            ghark.models.PullRequestRef("owner", "repo", 0)


class DomainRecordTests(unittest.TestCase):
    def test_snapshot_changes_have_distinct_type_from_timeline(self) -> None:
        change = ghark.models.SnapshotChange(
            ghark.models.ActivityKind.MERGED, "Pull request merged"
        )

        self.assertEqual(
            change.kind,
            ghark.models.ActivityKind.MERGED,
        )

    def test_merged_snapshot_suppresses_close_and_reports_merge(self) -> None:
        previous = _snapshot()
        current = dataclasses.replace(
            previous, status=ghark.models.PullRequestStatus.MERGED
        )

        changes = current.changes_from(
            previous,
            (
                ghark.models.TimelineEntry(
                    ghark.models.ActivityKind.CLOSED, "Pull request closed"
                ),
            ),
        )

        self.assertEqual(
            changes,
            (
                ghark.models.SnapshotChange(
                    ghark.models.ActivityKind.MERGED, "Pull request merged"
                ),
            ),
        )
        self.assertEqual(current.changes_from(current, ()), ())

    def test_timeline_kind_deduplicates_status_change(self) -> None:
        previous = _snapshot()
        current = dataclasses.replace(
            previous, status=ghark.models.PullRequestStatus.MERGED
        )

        changes = current.changes_from(
            previous,
            (ghark.models.TimelineEntry(ghark.models.ActivityKind.MERGED, "Merged"),),
        )

        self.assertEqual(changes, ())

    def test_reopened_status_change_has_reopened_kind(self) -> None:
        previous = dataclasses.replace(
            _snapshot(), status=ghark.models.PullRequestStatus.CLOSED
        )

        changes = dataclasses.replace(
            previous, status=ghark.models.PullRequestStatus.OPEN
        ).changes_from(previous, ())

        self.assertEqual(
            changes,
            (
                ghark.models.SnapshotChange(
                    ghark.models.ActivityKind.REOPENED, "Pull request reopened"
                ),
            ),
        )

    def test_channel_message_batches_events_by_pr(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        events = (
            ghark.models.ActivityEvent(
                pr, _snapshot(), ghark.models.ActivityKind.REVIEW, "approved", "alice"
            ),
            ghark.models.ActivityEvent(
                pr, _snapshot(), ghark.models.ActivityKind.COMMENT, "looks good"
            ),
        )

        (message,) = ghark.models.ChannelMessage.from_events(events)

        self.assertEqual(message.event_count, 2)
        self.assertIn(
            "review: approved (by alice); comment: looks good", message.content
        )

    def test_event_summary_is_compact_at_the_boundary(self) -> None:
        event = ghark.models.ActivityEvent(
            ghark.models.PullRequestRef("owner", "repo", 12),
            _snapshot(),
            ghark.models.ActivityKind.COMMENT,
            "  " + "long " * 50 + "  ",
        )

        self.assertEqual(len(event.summary), 180)
        self.assertEqual(event.as_dict()["summary"], event.summary)

    def test_snapshot_requires_status_enums(self) -> None:
        with self.assertRaises(TypeError):
            dataclasses.replace(_snapshot(), status="MERGED")


class SessionStateTests(unittest.TestCase):
    def test_record_and_flush_activity(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        snapshot = _snapshot()
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState()
        state.subscribe(
            pr,
            ghark.models.Baseline(snapshot, "start", cursor),
        )
        watch = state.watch(pr)
        self.assertIsNotNone(watch)
        result = ghark.models.PollResult(
            snapshot=snapshot,
            timeline_cursor="next",
            review_cursor=cursor + datetime.timedelta(seconds=30),
            timeline_entries=(
                ghark.models.TimelineEntry(
                    ghark.models.ActivityKind.REVIEW, "APPROVED"
                ),
            ),
            review_comments=(),
        )

        events = state.record_poll(watch, result)
        pending = state.flush_pending()

        self.assertEqual(len(events), 1)
        self.assertEqual(pending.events, events)
        self.assertEqual(pending.dropped_events, 0)
        self.assertEqual(state.flush_pending().events, ())

    def test_ignores_result_after_unsubscribe(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        snapshot = _snapshot()
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState()
        state.subscribe(pr, ghark.models.Baseline(snapshot, None, cursor))
        watch = state.watch(pr)
        state.unsubscribe(pr)
        result = ghark.models.PollResult(
            snapshot=snapshot,
            timeline_cursor=None,
            review_cursor=cursor,
            timeline_entries=(
                ghark.models.TimelineEntry(
                    ghark.models.ActivityKind.REVIEW, "APPROVED"
                ),
            ),
            review_comments=(),
        )

        self.assertEqual(state.record_poll(watch, result), ())
        self.assertEqual(state.flush_pending().events, ())

    def test_edited_review_comment_is_not_reported_again(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        snapshot = _snapshot()
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState()
        state.subscribe(pr, ghark.models.Baseline(snapshot, None, cursor))
        first = ghark.models.ReviewComment(
            1, cursor + datetime.timedelta(seconds=10), "Rename this.", "alice"
        )
        second = ghark.models.ReviewComment(
            2, cursor + datetime.timedelta(seconds=70), "And this.", "bob"
        )

        def poll(
            seconds: int, *comments: ghark.models.ReviewComment
        ) -> tuple[ghark.models.ActivityEvent, ...]:
            watch = state.watch(pr)
            assert watch is not None
            result = ghark.models.PollResult(
                snapshot=snapshot,
                timeline_cursor=None,
                review_cursor=cursor + datetime.timedelta(seconds=seconds),
                timeline_entries=(),
                review_comments=comments,
            )
            return state.record_poll(watch, result)

        reported = poll(30, first)
        self.assertEqual(poll(60), ())
        # GitHub's since filter returns the edited comment again.
        repeated = poll(90, first, second)

        self.assertEqual([event.actor for event in reported], ["alice"])
        self.assertEqual([event.actor for event in repeated], ["bob"])


class GitHubClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_and_observation_over_mock_http(self) -> None:
        calls: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path == "/graphql":
                query = request.read().decode()
                if "PullRequestEvents" in query:
                    pr_record = _pr_payload(
                        timeline_items={
                            "nodes": [
                                {
                                    "__typename": "PullRequestReview",
                                    "state": "APPROVED",
                                    "author": {"login": "reviewer"},
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "cursor-2",
                            },
                        }
                    )
                else:
                    pr_record = _pr_payload(
                        timeline_items={"pageInfo": {"endCursor": "cursor-1"}}
                    )
                return httpx.Response(
                    200,
                    json={"data": {"repository": {"pullRequest": pr_record}}},
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "created_at": "2026-01-01T00:00:10Z",
                        "body": "Please update this line.",
                        "user": {"login": "reviewer"},
                    }
                ],
            )

        client = await _mock_client(respond)
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        try:
            baseline = await client.baseline(pr)
            watch = ghark.models.WatchState(
                pr=pr,
                snapshot=baseline.snapshot,
                timeline_cursor=baseline.timeline_cursor,
                review_cursor=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                subscribed_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            )
            result = await client.observation(watch)
        finally:
            await client.close()

        self.assertEqual(baseline.timeline_cursor, "cursor-1")
        self.assertEqual(result.timeline_cursor, "cursor-2")
        self.assertEqual(
            result.timeline_entries[0].kind, ghark.models.ActivityKind.REVIEW
        )
        self.assertEqual(result.review_comments[0].comment_id, 7)
        self.assertEqual(watch.after_poll(result).seen_comment_ids, frozenset({7}))
        self.assertEqual(
            [request.url.path for request in calls],
            [
                "/graphql",
                "/graphql",
                "/repos/owner/repo/pulls/12/comments",
            ],
        )


class RecordedGitHubTests(unittest.IsolatedAsyncioTestCase):
    """Drive the poll path with responses recorded from cli/cli#9000."""

    PR = ghark.models.PullRequestRef("cli", "cli", 9000)
    SUBSCRIBED_AT = datetime.datetime(2024, 4, 29, tzinfo=datetime.UTC)

    async def test_recorded_poll_reaches_channel_message(self) -> None:
        state = ghark.session.SessionState(reviewers=frozenset({"williammartin"}))
        result = await self._observe(_fixture("cli_cli_9000_events.json"), state)

        watch = state.watch(self.PR)
        assert watch is not None
        events = state.record_poll(watch, result)
        (message,) = ghark.models.ChannelMessage.from_events(events)

        kind = ghark.models.ActivityKind
        self.assertEqual(
            [(event.kind, event.actor, event.actionable) for event in events],
            [
                (kind.COMMIT, "Andy Feller", False),
                (kind.REVIEW, "steiza", False),
                (kind.COMMENT, "andyfeller", False),
                (kind.COMMENT, "williammartin", True),
                (kind.COMMENT, "andyfeller", False),
                (kind.COMMIT, "Andy Feller", False),
                (kind.REVIEW, "williammartin", True),
                (kind.COMMIT, "Andy Feller", False),
                (kind.REVIEW, "williammartin", True),
                (kind.MERGED, "andyfeller", False),
                (kind.THREAD_COMMENT, "williammartin", True),
                (kind.THREAD_COMMENT, "williammartin", True),
            ],
        )
        self.assertEqual(
            result.timeline_cursor, "Y3Vyc29yOnYyOpPPAAABjyqkHvABqzEyNjQ1NzYyOTcx"
        )
        self.assertIsNone(state.watch(self.PR))
        self.assertEqual(message.event_count, 12)
        self.assertTrue(
            message.content.startswith(
                "cli/cli#9000: proof of concept for flag-level disable auth check\n"
                "commit: proof of concept for flag-level disable auth check "
                "(by Andy Feller); review: APPROVED: This looks like a workable "
                "alternative to #8995 to me! (by steiza);"
            )
        )
        self.assertIn("\nhttps://github.com/cli/cli/pull/9000\n", message.content)

    async def test_unknown_timeline_item_does_not_block_cursor(self) -> None:
        payload = _fixture("cli_cli_9000_events.json")
        timeline = payload["data"]["repository"]["pullRequest"]["timelineItems"]
        timeline["nodes"].insert(1, {"__typename": "HeadRefForcePushedEvent"})
        state = ghark.session.SessionState()

        with self.assertLogs("ghark", "WARNING") as logs:
            result = await self._observe(payload, state)

        self.assertEqual(len(result.timeline_entries), 11)
        self.assertEqual(result.timeline_cursor, timeline["pageInfo"]["endCursor"])
        self.assertIn("HeadRefForcePushedEvent", logs.output[0])

    async def _observe(
        self, events_payload: dict[str, Any], state: ghark.session.SessionState
    ) -> ghark.models.PollResult:
        """Subscribe to the recorded PR while it was open, then poll it once."""

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/graphql":
                return httpx.Response(200, json=events_payload)
            return httpx.Response(
                200, json=_fixture("cli_cli_9000_review_comments.json")
            )

        record = events_payload["data"]["repository"]["pullRequest"]
        open_snapshot = dataclasses.replace(
            ghark.github._snapshot(record),
            status=ghark.models.PullRequestStatus.OPEN,
        )
        state.subscribe(
            self.PR, ghark.models.Baseline(open_snapshot, None, self.SUBSCRIBED_AT)
        )
        watch = state.watch(self.PR)
        assert watch is not None
        client = await _mock_client(respond)
        try:
            return await client.observation(watch)
        finally:
            await client.close()


class ReviewerRequestTests(unittest.TestCase):
    REVIEWERS = frozenset({"reviewer"})

    def test_reviewer_comment_is_actionable(self) -> None:
        self.assertTrue(_event("COMMENT", "Rename this.", "Reviewer").actionable)

    def test_other_actor_is_not_actionable(self) -> None:
        self.assertFalse(_event("COMMENT", "Rename this.", "stranger").actionable)

    def test_signed_comment_is_not_actionable(self) -> None:
        body = "Renamed.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
        self.assertFalse(_event("THREAD_COMMENT", body, "reviewer").actionable)

    def test_bare_approval_is_not_actionable(self) -> None:
        self.assertFalse(_event("REVIEW", "APPROVED", "reviewer").actionable)
        self.assertTrue(_event("REVIEW", "CHANGES_REQUESTED", "reviewer").actionable)

    def test_status_change_is_not_actionable(self) -> None:
        self.assertFalse(_event("CHECKS", "Checks: SUCCESS → FAILURE").actionable)

    def test_pending_review_is_not_reported(self) -> None:
        entries = ghark.github._timeline_entries(
            [
                {"__typename": "PullRequestReview", "state": "PENDING", "body": ""},
                {"__typename": "PullRequestReview", "state": "COMMENTED", "body": "x"},
            ]
        )

        self.assertEqual(
            entries,
            (
                ghark.models.TimelineEntry(
                    ghark.models.ActivityKind.REVIEW, "COMMENTED: x"
                ),
            ),
        )


class PollerWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_file_gets_one_line_per_pr_with_requests(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        wake_file = pathlib.Path(directory.name) / "1.wake"
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState(reviewers=frozenset({"reviewer"}))
        state.subscribe(pr, ghark.models.Baseline(_snapshot(), None, cursor))
        entries = (
            ghark.models.TimelineEntry(
                ghark.models.ActivityKind.COMMENT, "Rename this.", "reviewer"
            ),
            ghark.models.TimelineEntry(
                ghark.models.ActivityKind.COMMENT, "Nice.", "stranger"
            ),
        )

        class GitHub:
            async def observation(
                self, watch: ghark.models.WatchState
            ) -> ghark.models.PollResult:
                return ghark.models.PollResult(_snapshot(), "next", cursor, entries, ())

        poller = ghark.watcher.PullRequestPoller(
            github=GitHub(),  # type: ignore[arg-type]
            state=state,
            channel=ghark.channel.ClaudeChannel(),
            wake_file=wake_file,
            interval=30,
            batch_window=0,
        )
        await poller.poll()

        self.assertEqual(
            wake_file.read_text(),
            "owner/repo#12: 1 review request(s); call pending_events\n",
        )
        self.assertEqual(len(state.flush_pending().events), 2)

    async def test_no_wake_without_requests(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        wake_file = pathlib.Path(directory.name) / "1.wake"
        poller = ghark.watcher.PullRequestPoller(
            github=None,  # type: ignore[arg-type]
            state=ghark.session.SessionState(),
            channel=ghark.channel.ClaudeChannel(),
            wake_file=wake_file,
            interval=30,
            batch_window=0,
        )
        await poller.poll()

        self.assertFalse(wake_file.exists())


class ClaudeChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_writes_channel_notification(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        event = ghark.models.ActivityEvent(
            pr, _snapshot(), ghark.models.ActivityKind.REVIEW, "APPROVED", "alice"
        )
        client_send, server_read = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](1)
        server_write, client_read = anyio.create_memory_object_stream[SessionMessage](1)
        options = InitializationOptions(
            server_name="ghark",
            server_version="0.1.0",
            capabilities=ServerCapabilities(),
        )
        channel = ghark.channel.ClaudeChannel()
        async with client_send, server_read, server_write, client_read:
            channel.attach(ServerSession(server_read, server_write, options))
            with self.assertNoLogs("ghark"):
                await channel.send((event,))
            sent = client_read.receive_nowait().message.root

        self.assertEqual(sent.method, "notifications/claude/channel")
        self.assertEqual(
            sent.params["meta"],
            {"repo": "owner/repo", "number": "12", "event_count": "1"},
        )
        self.assertIn("review: APPROVED (by alice)", sent.params["content"])


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_server_lists_and_serves_tools(self) -> None:
        log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(log_dir.cleanup)
        log_file = pathlib.Path(log_dir.name) / "ghark.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ghark"],
            env=os.environ
            | {"GH_TOKEN": "test-token", "GHARK_LOG_FILE": str(log_file)},
            cwd=str(pathlib.Path.cwd()),
        )
        async with asyncio.timeout(15):
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    subscriptions = await client.call_tool("subscriptions", {})
                    pending = await client.call_tool("pending_events", {})
                    unsubscribed = await client.call_tool(
                        "unsubscribe", {"pr": "owner/repo#12"}
                    )

        self.assertEqual(
            {tool.name for tool in listed.tools},
            {"subscribe", "unsubscribe", "subscriptions", "pending_events"},
        )
        self.assertIn("pr", listed.tools[0].inputSchema["properties"])
        self.assertEqual(subscriptions.structuredContent, {"subscriptions": []})
        self.assertEqual(pending.structuredContent, {"events": [], "dropped_events": 0})
        self.assertEqual(
            unsubscribed.structuredContent,
            {"status": "not_subscribed", "pr": "owner/repo#12"},
        )
        self.assertEqual(list(log_file.parent.glob("*.wake")), [])
        log = log_file.read_text()
        self.assertIn("ghark 0.1.0 started", log)
        self.assertIn("Client closed the connection; shutting down.", log)
        self.assertTrue(log.rstrip().endswith("ghark stopped."))


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


async def _mock_client(
    respond: Callable[[httpx.Request], httpx.Response],
) -> ghark.github.GitHubClient:
    client = ghark.github.GitHubClient("test-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://api.github.com",
    )
    return client


def _event(kind: str, body: str, actor: str = "") -> ghark.models.ActivityEvent:
    return ghark.models.ActivityEvent(
        ghark.models.PullRequestRef("owner", "repo", 12),
        _snapshot(),
        ghark.models.ActivityKind[kind],
        body,
        actor,
    ).for_reviewers(ReviewerRequestTests.REVIEWERS)


def _snapshot() -> ghark.models.PullRequestSnapshot:
    return ghark.models.PullRequestSnapshot(
        status=ghark.models.PullRequestStatus.OPEN,
        mergeable=ghark.models.Mergeability.MERGEABLE,
        merge_state=ghark.models.MergeState.CLEAN,
        head_oid="abc123",
        checks=ghark.models.CheckState.SUCCESS,
        title="Example change",
        url="https://github.com/owner/repo/pull/12",
    )


def _pr_payload(*, timeline_items: dict[str, object]) -> dict[str, object]:
    return {
        "url": "https://github.com/owner/repo/pull/12",
        "title": "Example change",
        "state": "OPEN",
        "merged": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "abc123",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "timelineItems": timeline_items,
    }
