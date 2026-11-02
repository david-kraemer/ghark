"""Find the open pull requests that a restarted session most plausibly owns.

Subscriptions live in process memory, so a resumed session starts with none.
The project's git checkout is the only durable record of what the session
worked on: a pull request is claimed when the token's owner authored it, its
repository is one of the checkout's GitHub remotes, and its head branch is the
branch checked out there. Matching every local branch instead would hand each
session in a busy repository every open PR, stale ones included, and each
review comment would wake all of them.
"""

import dataclasses
import pathlib
import re

from .github import GitHubClient, git_branch, git_remote_urls
from .models import AuthoredPullRequest, PullRequestRef

__all__ = [
    "LocalCheckout",
    "github_repository",
    "local_checkout",
    "owned_pull_requests",
]


@dataclasses.dataclass(frozen=True, slots=True)
class LocalCheckout:
    """The GitHub repositories a checkout's remotes name, and its branch."""

    #: Lowercase ``owner/repo`` names.
    repositories: frozenset[str]
    #: Empty when HEAD is detached.
    branch: str

    def owns(self, authored: AuthoredPullRequest) -> bool:
        pr = authored.pr
        return (
            f"{pr.owner}/{pr.repo}".lower() in self.repositories
            and authored.head_branch == self.branch
        )


async def owned_pull_requests(
    directory: pathlib.Path, github: GitHubClient
) -> tuple[PullRequestRef, ...]:
    """Open PRs by the viewer from the checked-out branch of a local remote.

    Raises when ``directory`` is not a git checkout or GitHub cannot be read;
    the caller decides how to fail.
    """
    checkout = await local_checkout(directory)
    if not checkout.repositories or not checkout.branch:
        return ()
    authored = await github.viewer_pull_requests()
    owned = {pr.pr for pr in authored if checkout.owns(pr)}
    return tuple(sorted(owned, key=str))


async def local_checkout(directory: pathlib.Path) -> LocalCheckout:
    """Read the GitHub remotes and checked-out branch of a git checkout."""
    urls = await git_remote_urls(directory)
    return LocalCheckout(
        repositories=frozenset(
            repository
            for url in urls
            if (repository := github_repository(url)) is not None
        ),
        branch=await git_branch(directory),
    )


def github_repository(url: str) -> str | None:
    """The lowercase ``owner/repo`` of a github.com remote URL, else None."""
    match = _GITHUB_REMOTE.search(url.strip())
    return f"{match['owner']}/{match['repo']}".lower() if match else None


# git@github.com:o/r.git, ssh://git@github.com/o/r, https://github.com/o/r.git
_GITHUB_REMOTE = re.compile(
    r"github\.com[:/](?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
