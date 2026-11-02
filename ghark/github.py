"""GitHub API declarations, response projections, and client."""

import dataclasses
import datetime
import enum
import logging
import os
import pathlib
import subprocess
from typing import Any, TypeVar

import anyio
import httpx

from .models import (
    ActivityKind,
    AuthoredPullRequest,
    Baseline,
    CheckState,
    Mergeability,
    MergeState,
    PollResult,
    PullRequestRef,
    PullRequestSnapshot,
    PullRequestStatus,
    ReviewComment,
    TimelineEntry,
    WatchState,
)

__all__ = [
    "GitHubClient",
    "GitHubError",
    "git",
    "git_branch",
    "git_remote_urls",
    "github_token",
]

logger = logging.getLogger("ghark")


class GitHubError(RuntimeError):
    """A GitHub API request failed or returned invalid data."""


def github_token() -> str:
    """Read the GitHub token from the standard environment variables, or else from
    the ``gh`` CLI's login."""
    if token := os.environ.get("GH_TOKEN"):
        return token
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    if token := gh_auth_token():
        return token
    raise GitHubError("Set GH_TOKEN or GITHUB_TOKEN, or log in with gh auth login.")


@dataclasses.dataclass(slots=True)
class GitHubClient:
    """Own the HTTP client and read GitHub pull request activity."""

    token: str = dataclasses.field(repr=False)
    _client: httpx.AsyncClient = dataclasses.field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "ghark/0.1",
            },
            timeout=httpx.Timeout(20),
        )

    async def close(self) -> None:
        """Close the owned HTTP client."""
        await self._client.aclose()

    async def viewer_login(self) -> str:
        """Read the login of the token's owner."""
        data = await self._graphql("query { viewer { login } }", {})
        return data["viewer"]["login"]

    async def viewer_pull_requests(self) -> tuple[AuthoredPullRequest, ...]:
        """Read every open pull request authored by the token's owner."""
        authored: list[AuthoredPullRequest] = []
        variables: dict[str, Any] = {"after": None}
        while True:
            data = await self._graphql(_VIEWER_PULL_REQUESTS_QUERY, variables)
            connection = data["viewer"]["pullRequests"]
            authored.extend(
                _authored_pull_request(node) for node in connection["nodes"] if node
            )
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                return tuple(authored)
            variables["after"] = page_info["endCursor"]

    async def baseline(self, pr: PullRequestRef) -> Baseline:
        """Read the initial snapshot and timeline cursor for a subscription."""
        review_cursor = datetime.datetime.now(datetime.UTC)
        data = await self._graphql(_BASELINE_QUERY, _graphql_variables(pr))
        record = _pr_from_data(data, pr)
        return Baseline(
            snapshot=_snapshot(record),
            timeline_cursor=record["timelineItems"]["pageInfo"].get("endCursor"),
            review_cursor=review_cursor,
            pending_review_ids=frozenset(
                int(node["fullDatabaseId"]) for node in record["reviews"]["nodes"]
            ),
        )

    async def observation(self, watch: WatchState) -> PollResult:
        """Read new timeline activity and review comments since the last poll."""
        poll_started = datetime.datetime.now(datetime.UTC)
        variables = _graphql_variables(watch.pr) | {"after": watch.timeline_cursor}
        data = await self._graphql(_ACTIVITY_QUERY, variables)
        record = _pr_from_data(data, watch.pr)
        connection = record["timelineItems"]
        nodes = list(connection["nodes"])
        page_info = connection["pageInfo"]
        cursor = page_info.get("endCursor") or watch.timeline_cursor

        while page_info["hasNextPage"]:
            if cursor is None:
                raise GitHubError("GitHub returned a timeline page without a cursor.")
            variables["after"] = cursor
            data = await self._graphql(_ACTIVITY_QUERY, variables)
            record = _pr_from_data(data, watch.pr)
            connection = record["timelineItems"]
            nodes.extend(connection["nodes"])
            page_info = connection["pageInfo"]
            cursor = page_info.get("endCursor") or cursor

        records = await self._review_comment_records(
            watch.pr,
            watch.review_cursor - datetime.timedelta(seconds=1),
        )
        comments = tuple(_review_comment(record) for record in records)
        return PollResult(
            snapshot=_snapshot(record),
            timeline_cursor=cursor,
            review_cursor=poll_started,
            timeline_entries=_timeline_entries(nodes),
            review_comments=comments,
        )

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                _GRAPHQL_URL,
                json={"query": query, "variables": variables},
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc
        if errors := result.get("errors"):
            detail = "; ".join(
                error.get("message", "GraphQL error") for error in errors
            )
            raise GitHubError(detail)
        return result["data"]

    async def _review_comment_records(
        self, pr: PullRequestRef, since: datetime.datetime
    ) -> list[dict[str, Any]]:
        """Read all review-thread comments newer than the supplied cursor."""
        url = f"{_REST_URL}/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments"
        params = {
            "since": since.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "per_page": _REVIEW_COMMENT_PAGE_SIZE,
        }
        comments: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                response = await self._client.get(url, params=params | {"page": page})
                response.raise_for_status()
                records = response.json()
                if not isinstance(records, list):
                    raise GitHubError("GitHub returned an invalid review comment list.")
                comments.extend(records)
                if len(records) < _REVIEW_COMMENT_PAGE_SIZE:
                    return comments
                page += 1
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError(f"GitHub review comment request failed: {exc}") from exc


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True).stdout.strip()


def gh_auth_token() -> str:
    return gh("auth", "token")


async def git(directory: pathlib.Path, *args: str) -> str:
    """Run git in ``directory`` and return its stdout; raise if git fails."""
    # stdin is the MCP transport; the child must not inherit it.
    with anyio.fail_after(_GIT_TIMEOUT_SECONDS):
        result = await anyio.run_process(
            ["git", "-C", str(directory), *args],
            stdin=subprocess.DEVNULL,
            check=False,
        )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {args[0]} failed in {directory}: {detail}")
    return result.stdout.decode(errors="replace")


async def git_remote_urls(directory: pathlib.Path) -> tuple[str, ...]:
    """The fetch and push URLs of every remote, each once, in git's order."""
    lines = (await git(directory, "remote", "-v")).splitlines()
    return tuple(dict.fromkeys(line.split()[1] for line in lines if line.strip()))


async def git_branch(directory: pathlib.Path) -> str:
    """The checked-out branch; empty when HEAD is detached."""
    return (await git(directory, "branch", "--show-current")).strip()


_GIT_TIMEOUT_SECONDS = 10
_GRAPHQL_URL = "https://api.github.com/graphql"
_REST_URL = "https://api.github.com"
_REVIEW_COMMENT_PAGE_SIZE = 100

_PULL_REQUEST_FIELDS = """
      url
      title
      state
      merged
      mergeable
      mergeStateStatus
      headRefOid
      commits(last: 1) {
        nodes { commit { statusCheckRollup { state } } }
      }
"""

_BASELINE_QUERY = f"""
query PullRequest($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
{_PULL_REQUEST_FIELDS}
      timelineItems(last: 1) {{ pageInfo {{ endCursor }} }}
      reviews(states: [PENDING], first: 10) {{ nodes {{ fullDatabaseId }} }}
    }}
  }}
}}
"""

_ACTIVITY_QUERY = f"""
query PullRequestEvents(
  $owner: String!, $name: String!, $number: Int!, $after: String
) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
{_PULL_REQUEST_FIELDS}
      timelineItems(
        first: 100
        after: $after
        itemTypes: [
          PULL_REQUEST_COMMIT,
          PULL_REQUEST_REVIEW,
          ISSUE_COMMENT,
          CLOSED_EVENT,
          REOPENED_EVENT,
          MERGED_EVENT
        ]
      ) {{
        nodes {{
          __typename
          ... on PullRequestCommit {{
            commit {{ messageHeadline author {{ name }} }}
          }}
          ... on PullRequestReview {{ state body author {{ login }} }}
          ... on IssueComment {{ body author {{ login }} }}
          ... on ClosedEvent {{ actor {{ login }} }}
          ... on ReopenedEvent {{ actor {{ login }} }}
          ... on MergedEvent {{ actor {{ login }} }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
  }}
}}
"""


EnumValue = TypeVar("EnumValue", bound=enum.StrEnum)


def _graphql_variables(pr: PullRequestRef) -> dict[str, str | int]:
    return {"owner": pr.owner, "name": pr.repo, "number": pr.number}


def _known(
    enum_type: type[EnumValue], value: str | None, unknown: EnumValue
) -> EnumValue:
    try:
        return enum_type(value) if value else unknown
    except ValueError:
        return unknown


def _snapshot(record: dict[str, Any]) -> PullRequestSnapshot:
    status = (
        PullRequestStatus.MERGED
        if record.get("merged")
        else _known(PullRequestStatus, record.get("state"), PullRequestStatus.UNKNOWN)
    )
    commits = record.get("commits", {}).get("nodes", [])
    latest = commits[-1] if commits else {}
    rollup = latest.get("commit", {}).get("statusCheckRollup") or {}
    return PullRequestSnapshot(
        status=status,
        mergeable=_known(Mergeability, record.get("mergeable"), Mergeability.UNKNOWN),
        merge_state=_known(
            MergeState, record.get("mergeStateStatus"), MergeState.UNKNOWN
        ),
        head_oid=record.get("headRefOid", ""),
        checks=_known(CheckState, rollup.get("state"), CheckState.UNKNOWN),
        title=record.get("title", "(untitled)"),
        url=record.get("url", ""),
    )


def _timeline_entries(nodes: list[dict[str, Any]]) -> tuple[TimelineEntry, ...]:
    """Project timeline nodes, skipping any this version cannot read.

    A node that cannot be read is logged and skipped rather than failing the
    poll; otherwise the cursor never advances and the PR fails on every poll.

    A pending review is skipped, and the cursor may pass it. GitHub orders a
    review by its submission time, so the submitted review appears again after
    the cursor.
    """
    entries: list[TimelineEntry] = []
    for node in nodes:
        if node.get("state") == "PENDING":
            continue  # The viewer's unsubmitted draft review.
        entry = _timeline_entry(node)
        if entry is None:
            logger.warning("Skipped GitHub timeline item: %s", node.get("__typename"))
        else:
            entries.append(entry)
    return tuple(entries)


def _timeline_entry(node: dict[str, Any]) -> TimelineEntry | None:
    match node.get("__typename"):
        case "PullRequestCommit":
            commit = node.get("commit") or {}
            author = commit.get("author") or {}
            return TimelineEntry(
                ActivityKind.COMMIT,
                commit.get("messageHeadline") or "New commit",
                author.get("name") or "",
            )
        case "PullRequestReview":
            author = node.get("author") or {}
            state = node.get("state") or "REVIEWED"
            body = node.get("body") or ""
            return TimelineEntry(
                ActivityKind.REVIEW,
                f"{state}: {body}" if body else state,
                author.get("login") or "",
            )
        case "IssueComment":
            author = node.get("author") or {}
            return TimelineEntry(
                ActivityKind.COMMENT,
                node.get("body", ""),
                author.get("login", ""),
            )
        case "ClosedEvent" | "ReopenedEvent" | "MergedEvent" as typename:
            kind, verb = {
                "ClosedEvent": (ActivityKind.CLOSED, "closed"),
                "ReopenedEvent": (ActivityKind.REOPENED, "reopened"),
                "MergedEvent": (ActivityKind.MERGED, "merged"),
            }[typename]
            actor = (node.get("actor") or {}).get("login", "")
            return TimelineEntry(kind, f"Pull request {verb}", actor)
        case _:
            return None


def _review_comment(record: dict[str, Any]) -> ReviewComment:
    return ReviewComment(
        comment_id=record["id"],
        created_at=datetime.datetime.fromisoformat(
            record["created_at"].replace("Z", "+00:00")
        ),
        body=record.get("body", ""),
        actor=(record.get("user") or {}).get("login", ""),
        review_id=record.get("pull_request_review_id"),
    )


def _pr_from_data(data: dict[str, Any], pr: PullRequestRef) -> dict[str, Any]:
    record = (data.get("repository") or {}).get("pullRequest")
    if record is None:
        raise GitHubError(f"Pull request not found: {pr}")
    return record


_VIEWER_PULL_REQUESTS_QUERY = """
query ViewerPullRequests($after: String) {
  viewer {
    pullRequests(states: OPEN, first: 100, after: $after) {
      nodes {
        number
        headRefName
        repository { name owner { login } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _authored_pull_request(node: dict[str, Any]) -> AuthoredPullRequest:
    repository = node["repository"]
    return AuthoredPullRequest(
        pr=PullRequestRef(
            repository["owner"]["login"], repository["name"], node["number"]
        ),
        head_branch=node["headRefName"],
    )
