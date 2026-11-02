"""Behavior checks for pure projections, session state, and GitHub reads."""

import asyncio
import dataclasses
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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
import ghark.config
import ghark.discovery
import ghark.github
import ghark.models
import ghark.server
import ghark.session
import ghark.wake
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

    def test_comment_from_review_pending_at_subscription_is_reported(self) -> None:
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        snapshot = _snapshot()
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState()
        state.subscribe(
            pr,
            ghark.models.Baseline(
                snapshot, None, cursor, pending_review_ids=frozenset({50})
            ),
        )
        watch = state.watch(pr)
        assert watch is not None
        before = cursor - datetime.timedelta(seconds=10)
        result = ghark.models.PollResult(
            snapshot=snapshot,
            timeline_cursor=None,
            review_cursor=cursor + datetime.timedelta(seconds=30),
            timeline_entries=(),
            review_comments=(
                # Drafted in a review that was pending at subscription.
                ghark.models.ReviewComment(1, before, "Drafted.", "alice", 50),
                # An edit to a comment from a review submitted earlier.
                ghark.models.ReviewComment(2, before, "Edited.", "alice", 40),
            ),
        )

        events = state.record_poll(watch, result)

        self.assertEqual([event.summary for event in events], ["Drafted."])


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


class RecordedPendingReviewTests(unittest.IsolatedAsyncioTestCase):
    """Drive a review recorded on david-kraemer/ghark#1.

    The review was started, leaving it pending, before the subscription and
    submitted after it. Its inline comment predates the subscription.
    """

    PR = ghark.models.PullRequestRef("david-kraemer", "ghark", 1)

    async def test_review_pending_at_subscription_reports_its_comments(
        self,
    ) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/graphql":
                if "PullRequestEvents" in request.read().decode():
                    return httpx.Response(
                        200, json=_fixture("david_kraemer_ghark_1_events.json")
                    )
                return httpx.Response(
                    200, json=_fixture("david_kraemer_ghark_1_baseline.json")
                )
            return httpx.Response(
                200, json=_fixture("david_kraemer_ghark_1_review_comments.json")
            )

        state = ghark.session.SessionState(reviewers=frozenset({"david-kraemer"}))
        client = await _mock_client(respond)
        try:
            baseline = await client.baseline(self.PR)
            state.subscribe(self.PR, baseline)
            watch = state.watch(self.PR)
            assert watch is not None
            result = await client.observation(watch)
        finally:
            await client.close()
        events = state.record_poll(watch, result)

        self.assertEqual(baseline.pending_review_ids, frozenset({5697166279}))
        self.assertEqual(
            [(event.kind, event.summary, event.actionable) for event in events],
            [
                (
                    ghark.models.ActivityKind.REVIEW,
                    "COMMENTED: Review started before subscription.",
                    True,
                ),
                (
                    ghark.models.ActivityKind.THREAD_COMMENT,
                    "Inline comment made before subscription.",
                    True,
                ),
            ],
        )


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
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = unittest.mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": directory.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.flag = ghark.wake.pending_flag("s1")

    def poller(
        self, state: ghark.session.SessionState, session: str | None = "s1"
    ) -> ghark.watcher.PullRequestPoller:
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
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

        return ghark.watcher.PullRequestPoller(
            github=GitHub(),  # type: ignore[arg-type]
            state=state,
            channel=ghark.channel.ClaudeChannel(),
            claude_session=session,
            interval=30,
            batch_window=0,
        )

    def subscribed_state(self) -> ghark.session.SessionState:
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        state = ghark.session.SessionState(reviewers=frozenset({"reviewer"}))
        pr = ghark.models.PullRequestRef("owner", "repo", 12)
        state.subscribe(pr, ghark.models.Baseline(_snapshot(), None, cursor))
        return state

    async def test_review_request_raises_the_flag_with_its_pr(self) -> None:
        state = self.subscribed_state()

        await self.poller(state).poll()

        self.assertEqual(self.flag.read_text(), "owner/repo#12\n")
        self.assertEqual(len(state.flush_pending().events), 2)

    async def test_pending_events_lowers_the_flag(self) -> None:
        state = self.subscribed_state()
        await self.poller(state).poll()

        drained = ghark.server._pending_events(state, "s1")

        self.assertEqual(len(drained["events"]), 2)
        self.assertFalse(self.flag.exists())

    async def test_no_flag_when_drained_during_the_batch_window(self) -> None:
        state = self.subscribed_state()
        poller = self.poller(state)
        poller.batch_window = 0.05

        poll = asyncio.create_task(poller.poll())
        await asyncio.sleep(0.01)
        ghark.server._pending_events(state, "s1")
        await poll

        self.assertFalse(self.flag.exists())

    async def test_failed_flag_write_is_retried_on_the_next_poll(self) -> None:
        state = self.subscribed_state()
        poller = self.poller(state)
        with (
            unittest.mock.patch("ghark.watcher.raise_flag", side_effect=OSError),
            self.assertLogs("ghark", "ERROR"),
        ):
            await poller.poll()
        self.assertFalse(self.flag.exists())

        poller.github = _QuietGitHub()  # type: ignore[assignment]
        await poller.poll()

        self.assertEqual(self.flag.read_text(), "owner/repo#12\n")

    async def test_no_flag_without_requests(self) -> None:
        await self.poller(ghark.session.SessionState()).poll()

        self.assertFalse(self.flag.exists())

    async def test_no_flag_outside_claude_code(self) -> None:
        await self.poller(self.subscribed_state(), session=None).poll()

        self.assertFalse(ghark.wake.ghark_state_dir().exists())


class _QuietGitHub:
    async def observation(
        self, watch: ghark.models.WatchState
    ) -> ghark.models.PollResult:
        cursor = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        return ghark.models.PollResult(_snapshot(), "next", cursor, (), ())


class DiscoveryConfigTests(unittest.TestCase):
    def test_discover_is_on_unless_disabled(self) -> None:
        for value, expected in [("", True), ("1", True), ("0", False), ("Off", False)]:
            with unittest.mock.patch.dict(os.environ, {"GHARK_DISCOVER": value}):
                self.assertIs(ghark.config.ghark_discover(), expected, value)

    def test_project_dir_prefers_claude_then_pwd(self) -> None:
        env = {"CLAUDE_PROJECT_DIR": "/claude", "PWD": "/pwd"}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(ghark.config.ghark_project_dir(), pathlib.Path("/claude"))
            del os.environ["CLAUDE_PROJECT_DIR"]
            self.assertEqual(ghark.config.ghark_project_dir(), pathlib.Path("/pwd"))
            del os.environ["PWD"]
            self.assertEqual(ghark.config.ghark_project_dir(), pathlib.Path.cwd())

    def test_github_repository_from_remote_urls(self) -> None:
        repository = ghark.discovery.github_repository
        self.assertEqual(repository("git@github.com:Owner/repo.git"), "owner/repo")
        self.assertEqual(repository("https://github.com/owner/re.po"), "owner/re.po")
        self.assertEqual(repository("ssh://git@github.com/owner/repo/"), "owner/repo")
        self.assertIsNone(repository("https://gitlab.com/owner/repo.git"))


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Discover PRs from a real git checkout and a mocked GitHub."""

    AUTHORED = [
        ("owner", "repo", 1, "feature-a"),  # Checked out, origin remote.
        ("owner", "repo", 2, "gone"),  # No local branch.
        ("other", "repo", 3, "feature-a"),  # Not a remote of the checkout.
        ("upstream", "repo", 4, "feature-a"),  # Checked out, upstream remote.
        ("owner", "repo", 5, "feature-b"),  # Local but not checked out.
    ]

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = pathlib.Path(directory.name)
        _git(self.repo, "init", "--quiet", "--initial-branch=main")
        _git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "init")
        _git(self.repo, "branch", "feature-a")
        _git(self.repo, "branch", "feature-b")
        _git(self.repo, "checkout", "--quiet", "feature-a")
        _git(self.repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
        _git(self.repo, "remote", "add", "upstream", "https://github.com/Upstream/repo")
        _git(self.repo, "remote", "add", "mirror", "https://gitlab.com/owner/repo")

    async def test_owned_pull_requests_match_remote_and_branch(self) -> None:
        github = await _mock_client(_discovery_responder(self.AUTHORED))
        self.addAsyncCleanup(github.close)

        owned = await ghark.discovery.owned_pull_requests(self.repo, github)

        self.assertEqual([str(pr) for pr in owned], ["owner/repo#1", "upstream/repo#4"])

    async def test_discover_subscribes_with_baseline(self) -> None:
        requests: list[str] = []
        github = await _mock_client(_discovery_responder(self.AUTHORED, requests))
        self.addAsyncCleanup(github.close)
        state = ghark.session.SessionState()

        await ghark.server._discover(self.repo, state, github)
        listed = ghark.server._subscriptions(state)

        self.assertEqual(
            [watch.pr for watch in state.subscriptions()],
            [
                ghark.models.PullRequestRef("owner", "repo", 1),
                ghark.models.PullRequestRef("upstream", "repo", 4),
            ],
        )
        self.assertEqual(state.subscriptions()[0].timeline_cursor, "cursor-1")
        self.assertEqual(state.reviewers, frozenset({"me"}))
        self.assertEqual(len(listed["subscriptions"]), 2)
        self.assertEqual(requests.count("ViewerPullRequests"), 1)
        self.assertEqual(state.flush_pending().events, ())

    async def test_discover_logs_and_continues_outside_a_checkout(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        github = await _mock_client(_discovery_responder(self.AUTHORED))
        self.addAsyncCleanup(github.close)
        state = ghark.session.SessionState()

        with self.assertLogs("ghark", "WARNING") as logs:
            await ghark.server._discover(pathlib.Path(directory.name), state, github)

        self.assertEqual(state.subscriptions(), ())
        self.assertIn("Discovery skipped", logs.output[0])

    async def test_discover_logs_and_continues_on_github_failure(self) -> None:
        github = await _mock_client(lambda _: httpx.Response(502))
        self.addAsyncCleanup(github.close)
        state = ghark.session.SessionState()

        with self.assertLogs("ghark", "WARNING") as logs:
            await ghark.server._discover(self.repo, state, github)

        self.assertEqual(state.subscriptions(), ())
        self.assertIn("502", logs.output[0])

    async def test_one_failed_baseline_does_not_stop_the_others(self) -> None:
        responder = _discovery_responder(self.AUTHORED)

        def respond(request: httpx.Request) -> httpx.Response:
            if json.loads(request.read())["variables"].get("number") == 1:
                return httpx.Response(500)
            return responder(request)

        github = await _mock_client(respond)
        self.addAsyncCleanup(github.close)
        state = ghark.session.SessionState()

        with self.assertLogs("ghark", "ERROR"):
            await ghark.server._discover(self.repo, state, github)

        self.assertEqual(
            [str(watch.pr) for watch in state.subscriptions()], ["upstream/repo#4"]
        )


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
        # Discovery runs in a directory that is not a checkout: it must fail
        # quietly, before any GitHub request, and leave the tools serving.
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ghark"],
            env=os.environ
            | {
                "GH_TOKEN": "test-token",
                "GHARK_LOG_FILE": str(log_file),
                "CLAUDE_PROJECT_DIR": log_dir.name,
                "CLAUDE_CODE_SESSION_ID": "s1",
                "XDG_STATE_HOME": log_dir.name,
            },
            cwd=str(pathlib.Path.cwd()),
        )
        stale = pathlib.Path(log_dir.name) / "ghark" / "s1.pending"
        stale.parent.mkdir()
        stale.write_text("owner/repo#1\n")
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
        self.assertFalse(stale.exists())
        log = log_file.read_text()
        self.assertIn("ghark 0.1.0 started", log)
        self.assertIn(f"Discovery skipped in {log_dir.name}", log)
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


def _git(directory: pathlib.Path, *args: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "-C", str(directory), *identity, *args], check=True)


def _discovery_responder(
    authored: list[tuple[str, str, int, str]], requests: list[str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the viewer, viewer-PR and baseline queries."""

    def respond(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.read())["query"]
        if "ViewerPullRequests" in query:
            name = "ViewerPullRequests"
            nodes = [
                {
                    "number": number,
                    "headRefName": branch,
                    "repository": {"name": repo, "owner": {"login": owner}},
                }
                for owner, repo, number, branch in authored
            ]
            page = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
            data: dict[str, Any] = {"viewer": {"pullRequests": page}}
        elif "viewer" in query:
            name = "viewer"
            data = {"viewer": {"login": "Me"}}
        else:
            name = "PullRequest"
            pr_record = _pr_payload(
                timeline_items={"pageInfo": {"endCursor": "cursor-1"}}
            )
            data = {"repository": {"pullRequest": pr_record}}
        if requests is not None:
            requests.append(name)
        return httpx.Response(200, json={"data": data})

    return respond


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
        "reviews": {"nodes": []},
    }
