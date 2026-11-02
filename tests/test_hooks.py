"""Checks for the Claude Code hooks."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

import ghark.hooks
import ghark.models
import ghark.wake

SIGNED = "Done.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
HEREDOC = """gh pr comment 12 --body "$(cat <<'EOF'
Fixed the off-by-one. It's in `parse()`.
{tail}
EOF
)\""""


def decision(command: str, cwd: str = ".", tool_name: str = "Bash") -> str:
    """Run the hook in process and return "deny" or "allow"."""
    payload = {
        "session_id": "s",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command, "description": "test"},
    }
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    stdout = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
        ghark.hooks.signature_hook()
    if not stdout.getvalue():
        return "allow"
    output = json.loads(stdout.getvalue())["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    return output["permissionDecision"]


class SignatureHookCliTests(unittest.TestCase):
    def test_pr_comment_body_flags(self) -> None:
        for flag in ("--body", "-b"):
            with self.subTest(flag=flag):
                self.assertEqual(decision(f"gh pr comment 12 {flag} 'Fixed.'"), "deny")
                self.assertEqual(
                    decision(f"gh pr comment 12 {flag} '{SIGNED}'"), "allow"
                )
        self.assertEqual(decision('gh pr comment 12 --body="Fixed."'), "deny")
        self.assertEqual(decision("gh pr comment 12 -bFixed."), "deny")

    def test_heredoc_body(self) -> None:
        self.assertEqual(decision(HEREDOC.format(tail="Thanks.")), "deny")
        self.assertEqual(decision(HEREDOC.format(tail=SIGNED)), "allow")

    def test_review_and_issue_comment(self) -> None:
        self.assertEqual(decision("gh pr review 3 --comment -b 'Looks off'"), "deny")
        self.assertEqual(
            decision("gh pr review 3 --request-changes --body 'no'"), "deny"
        )
        self.assertEqual(decision("gh pr review 3 --approve"), "allow")
        self.assertEqual(decision("gh issue comment 9 --body 'ok'"), "deny")
        self.assertEqual(decision(f"gh issue comment 9 --body '{SIGNED}'"), "allow")

    def test_compound_command_checks_each_gh_call(self) -> None:
        command = f"cd repo && git push && gh pr comment 1 -b '{SIGNED}'\n"
        self.assertEqual(decision(command), "allow")
        self.assertEqual(decision(command + "gh pr comment 1 -b 'again'"), "deny")
        self.assertEqual(decision("git push; /usr/bin/gh pr comment 1 -b x"), "deny")

    def test_body_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "plain.md").write_text("Fixed.\n")
            pathlib.Path(tmp, "signed.md").write_text(SIGNED)
            for flag in ("--body-file ", "-F ", "--body-file="):
                with self.subTest(flag=flag):
                    plain = f"gh pr comment 1 {flag}plain.md"
                    signed = f"gh pr comment 1 {flag}signed.md"
                    self.assertEqual(decision(plain, tmp), "deny")
                    self.assertEqual(decision(signed, tmp), "allow")
            absolute = os.path.join(tmp, "plain.md")
            self.assertEqual(decision(f"gh pr review 1 -c -F {absolute}"), "deny")

    def test_denial_names_the_signature(self) -> None:
        reason = ghark.hooks._denial_reason("--body")
        self.assertIn(
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)", reason
        )


class SignatureHookApiTests(unittest.TestCase):
    def test_rest_comment_endpoints(self) -> None:
        for endpoint in (
            "repos/o/r/issues/12/comments",
            "/repos/o/r/pulls/12/comments",
            "repos/o/r/pulls/12/comments/99/replies",
            "repos/o/r/pulls/12/reviews",
            "repos/o/r/pulls/12/reviews/7/events",
        ):
            for flag in ("-f", "-F", "--field", "--raw-field"):
                with self.subTest(endpoint=endpoint, flag=flag):
                    command = f"gh api {endpoint} {flag} body='Fixed.'"
                    self.assertEqual(decision(command), "deny")
                    command = f"gh api {endpoint} {flag} body='{SIGNED}'"
                    self.assertEqual(decision(command), "allow")
        self.assertEqual(
            decision("gh api -X POST repos/o/r/issues/1/comments -fbody=x"), "deny"
        )
        self.assertEqual(
            decision("gh api --method=POST repos/o/r/issues/1/comments -f body=x"),
            "deny",
        )

    def test_review_inline_comment_bodies(self) -> None:
        command = (
            "gh api repos/o/r/pulls/1/reviews -f event=COMMENT "
            f"-f body='{SIGNED}' -f 'comments[][body]=nit'"
        )
        self.assertEqual(decision(command), "deny")
        command = "gh api repos/o/r/pulls/1/reviews -f event=APPROVE"
        self.assertEqual(decision(command), "allow")

    def test_reads_are_allowed(self) -> None:
        self.assertEqual(decision("gh api repos/o/r/issues/1/comments"), "allow")
        self.assertEqual(
            decision("gh api repos/o/r/pulls/1/comments --jq '.[].body'"), "allow"
        )
        self.assertEqual(
            decision("gh api -X PATCH repos/o/r/issues/comments/5 -f body=x"),
            "allow",
        )
        self.assertEqual(decision("gh api repos/o/r/labels -f name=bug"), "allow")

    def test_input_and_field_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "plain.json").write_text(json.dumps({"body": "Fixed."}))
            pathlib.Path(tmp, "signed.json").write_text(json.dumps({"body": SIGNED}))
            pathlib.Path(tmp, "approve.json").write_text('{"event": "APPROVE"}')
            pathlib.Path(tmp, "plain.md").write_text("Fixed.")
            endpoint = "repos/o/r/pulls/1/reviews"
            for name, expected in (
                ("plain.json", "deny"),
                ("signed.json", "allow"),
                ("approve.json", "allow"),
                ("missing.json", "allow"),
            ):
                with self.subTest(name=name):
                    command = f"gh api {endpoint} --input {name}"
                    self.assertEqual(decision(command, tmp), expected)
            command = "gh api repos/o/r/issues/1/comments -F body=@plain.md"
            self.assertEqual(decision(command, tmp), "deny")

    def test_graphql_comment_mutation(self) -> None:
        query = "mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id, body: $body}) { clientMutationId } }"  # noqa: E501
        command = f"gh api graphql -f query='{query}' -f id=X -f body='Fixed.'"
        self.assertEqual(decision(command), "deny")
        command = f"gh api graphql -f query='{query}' -f id=X -f body='{SIGNED}'"
        self.assertEqual(decision(command), "allow")
        command = "gh api graphql -f query='{ viewer { login } }'"
        self.assertEqual(decision(command), "allow")


class SignatureHookFailOpenTests(unittest.TestCase):
    def test_unknown_bodies_are_allowed(self) -> None:
        for command in (
            'gh pr comment 1 --body "$BODY"',
            'gh pr comment 1 --body "$(cat notes.md)"',
            "gh pr comment 1 --body `cat notes.md`",
            "cat notes.md | gh pr comment 1 -F -",
            "gh pr comment 1 --body 'unbalanced",
            "printf x > /tmp/c.md && gh pr comment 1 -F /tmp/c.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(decision(command), "allow")

    def test_quoted_heredoc_on_stdin(self) -> None:
        command = "gh pr comment 1 -F - <<'EOF'\nFixed\nEOF"
        self.assertEqual(decision(command), "deny")
        command = f"gh pr comment 1 -F - <<'EOF'\n{SIGNED}\nEOF"
        self.assertEqual(decision(command), "allow")

    def test_single_quoted_dollar_is_literal(self) -> None:
        self.assertEqual(decision("gh pr comment 1 --body 'costs $5'"), "deny")

    def test_unrelated_input_is_allowed(self) -> None:
        self.assertEqual(decision("git commit -m 'gh pr comment -b x'"), "allow")
        self.assertEqual(decision("gh pr view 1 --comments"), "allow")
        self.assertEqual(decision("gh pr comment 1 -b x", tool_name="Edit"), "allow")
        stdin = io.TextIOWrapper(io.BytesIO(b"not json"))
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
            ghark.hooks.signature_hook()
        self.assertEqual(stdout.getvalue(), "")


class SignatureHookEntryPointTests(unittest.TestCase):
    def test_console_script_denies(self) -> None:
        payload = {
            "cwd": ".",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr comment 1 -b x"},
        }
        script = pathlib.Path(sys.executable).with_name("ghark-hook-signature")
        if not script.exists():
            self.skipTest("console script not installed")
        result = subprocess.run(
            [str(script)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=True,
            timeout=30,
        )
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")


PR_URL = "https://github.com/owner/repo/pull/12"


class SubscribeContextTests(unittest.TestCase):
    def test_repo_flag_and_new_alias_count_as_create(self) -> None:
        for command in ("gh -R owner/repo pr create --fill", "gh pr new --fill"):
            with self.subTest(command=command):
                context = ghark.hooks.subscribe_context(_event(command=command))
                self.assertIsNotNone(context)

    def test_successful_create_names_url_and_tool(self) -> None:
        context = ghark.hooks.subscribe_context(_event())

        assert context is not None
        self.assertIn(f'"{PR_URL}"', context)
        self.assertIn("mcp__ghark__subscribe", context)
        self.assertNotIn("Monitor", context)

    def test_uses_last_url_on_stdout(self) -> None:
        stdout = f"See https://github.com/owner/repo/pull/3 for context\n{PR_URL}\n"
        context = ghark.hooks.subscribe_context(_event(stdout=stdout))

        assert context is not None
        self.assertIn(f'"{PR_URL}"', context)
        self.assertNotIn("/pull/3", context)

    def test_create_in_compound_command(self) -> None:
        command = 'git push -u origin HEAD && gh pr create --fill --title "x"'
        context = ghark.hooks.subscribe_context(_event(command=command))

        assert context is not None
        self.assertIn(PR_URL, context)

    def test_missing_url_gives_generic_reminder(self) -> None:
        context = ghark.hooks.subscribe_context(_event(stdout=""))

        assert context is not None
        self.assertIn("mcp__ghark__subscribe", context)
        self.assertNotIn("github.com", context)

    def test_ignores_other_commands(self) -> None:
        for command in ("gh pr view 12", "gh pr list", "git status", "gh issue create"):
            with self.subTest(command=command):
                self.assertIsNone(
                    ghark.hooks.subscribe_context(_event(command=command))
                )

    def test_ignores_other_tools(self) -> None:
        event = _event() | {"tool_name": "Read"}

        self.assertIsNone(ghark.hooks.subscribe_context(event))

    def test_ignores_interrupted_run(self) -> None:
        event = _event(interrupted=True)

        self.assertIsNone(ghark.hooks.subscribe_context(event))

    def test_ignores_malformed_fields(self) -> None:
        for event in (
            {},
            {"tool_name": "Bash", "tool_input": "gh pr create"},
            {"tool_name": "Bash", "tool_input": {"command": "gh pr create"}},
            _event() | {"tool_response": "https://github.com/o/r/pull/1"},
        ):
            with self.subTest(event=event):
                self.assertIsNone(ghark.hooks.subscribe_context(event))


class SubscribeHookTests(unittest.TestCase):
    def test_prints_post_tool_use_context(self) -> None:
        output = _run_main(json.dumps(_event()))

        payload = json.loads(output)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        self.assertIn(PR_URL, specific["additionalContext"])

    def test_prints_nothing_for_other_commands(self) -> None:
        self.assertEqual(_run_main(json.dumps(_event(command="ls"))), "")

    def test_prints_nothing_for_invalid_json(self) -> None:
        self.assertEqual(_run_main("not json"), "")
        self.assertEqual(_run_main("[]"), "")


class WakeDecisionTests(unittest.TestCase):
    def test_flagged_requests_name_their_prs(self) -> None:
        message = ghark.wake.wake_message("owner/repo#12", overdue=False)

        assert message is not None
        self.assertIn("owner/repo#12", message)
        self.assertIn("pending_events", message)

    def test_overdue_waiter_asks_to_re_arm(self) -> None:
        message = ghark.wake.wake_message(None, overdue=True)

        assert message is not None
        self.assertIn("re-arm", message)

    def test_nothing_flagged_keeps_waiting(self) -> None:
        self.assertIsNone(ghark.wake.wake_message(None, overdue=False))
        self.assertIsNone(ghark.wake.wake_message("", overdue=False))

    def test_margin_is_a_tenth_of_the_timeout_up_to_five_minutes(self) -> None:
        self.assertEqual(ghark.wake.wake_after(86_400), 86_100)
        self.assertEqual(ghark.wake.wake_after(600), 540)
        self.assertEqual(ghark.wake.wake_after(30), 27)

    def test_unusable_timeout_falls_back_to_the_default(self) -> None:
        default = ghark.wake.DEFAULT_TIMEOUT
        for argv in (["hook"], ["hook", "soon"], ["hook", "nan"], ["hook", "-5"]):
            with self.subTest(argv=argv):
                self.assertEqual(ghark.wake.hook_timeout(argv), default)
        self.assertEqual(ghark.wake.hook_timeout(["hook", "600"]), 600)

    def test_session_id_comes_from_the_event_then_the_environment(self) -> None:
        environ = {"CLAUDE_CODE_SESSION_ID": "from-env"}
        cases = [
            ({"session_id": "from-event"}, environ, "from-event"),
            ({"session_id": "../escape"}, environ, "from-env"),
            (None, environ, "from-env"),
            ({}, {"CLAUDE_CODE_SESSION_ID": "a/b"}, None),
            ({}, {}, None),
        ]
        for event, env, expected in cases:
            with self.subTest(event=event, env=env):
                self.assertEqual(ghark.wake.hook_session_id(event, env), expected)


class WaiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _isolated_state(self)
        for name, value in (("POLL_SECONDS", 0.01), ("SERVER_GRACE", 0.2)):
            patcher = mock.patch.object(ghark.wake, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_flag_under_a_live_server_wakes_with_its_prs(self) -> None:
        with ghark.wake.server_lease("s1"):
            ghark.wake.raise_flag("s1", [_pr(12), _pr(13)])

            message = ghark.wake.wait("s1", _never())

        assert message is not None
        self.assertIn("owner/repo#12, owner/repo#13", message)

    def test_flag_without_a_server_is_stale_and_ignored(self) -> None:
        ghark.wake.raise_flag("s1", [_pr(12)])

        self.assertIsNone(ghark.wake.wait("s1", _never()))

    def test_waiter_exits_quietly_once_the_server_is_gone(self) -> None:
        started = time.monotonic()

        self.assertIsNone(ghark.wake.wait("s1", _never()))
        self.assertGreaterEqual(time.monotonic() - started, ghark.wake.SERVER_GRACE)

    def test_overdue_waiter_wakes_to_re_arm(self) -> None:
        with ghark.wake.server_lease("s1"):
            message = ghark.wake.wait("s1", time.monotonic())

        assert message is not None
        self.assertIn("re-arm", message)

    def test_second_waiter_for_a_session_returns_at_once(self) -> None:
        with (
            ghark.wake.server_lease("s1"),
            ghark.wake._exclusive(ghark.wake._waiter_file("s1")),
        ):
            ghark.wake.raise_flag("s1", [_pr(12)])

            self.assertIsNone(ghark.wake.wait("s1", _never()))

    def test_lease_lowers_a_stale_flag_on_entry_and_on_exit(self) -> None:
        flag = ghark.wake.pending_flag("s1")
        ghark.wake.raise_flag("s1", [_pr(12)])

        with ghark.wake.server_lease("s1"):
            self.assertFalse(flag.exists())
            ghark.wake.raise_flag("s1", [_pr(12)])

        self.assertFalse(flag.exists())

    def test_raise_flag_leaves_no_staging_file(self) -> None:
        ghark.wake.raise_flag("s1", [_pr(12)])

        names = [path.name for path in ghark.wake.ghark_state_dir().iterdir()]
        self.assertEqual(names, ["s1.pending"])

    def test_sweep_deletes_only_old_unlocked_files(self) -> None:
        directory = ghark.wake.ghark_state_dir()
        directory.mkdir(parents=True)
        names = ["old.pending", "old.waiter", "held.alive", "new.pending", "ghark.log"]
        for name in names:
            (directory / name).touch()
        old = time.time() - ghark.wake.ABANDONED_AGE - 1
        for name in ("old.pending", "old.waiter", "held.alive", "ghark.log"):
            os.utime(directory / name, (old, old))

        with ghark.wake.server_lease("held"):
            ghark.wake.sweep_state(time.time())

        remaining = sorted(path.name for path in directory.iterdir())
        self.assertEqual(remaining, ["ghark.log", "held.alive", "new.pending"])


class WakeHookEntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _isolated_state(self)

    def test_hook_exits_2_with_the_message_on_stderr(self) -> None:
        with ghark.wake.server_lease("s1"):
            ghark.wake.raise_flag("s1", [_pr(12)])

            result = _run_wake_hook({"session_id": "s1"}, self.env)

        self.assertEqual(result.returncode, ghark.wake.WAKE)
        self.assertIn("owner/repo#12", result.stderr)

    def test_hook_without_a_usable_session_exits_quietly(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_SESSION_ID"}

        result = _run_wake_hook({"session_id": "../escape"}, env)

        self.assertEqual(result.returncode, ghark.wake.QUIET)
        self.assertEqual(result.stderr, "")
        self.assertFalse(ghark.wake.ghark_state_dir().exists())


def _event(
    *,
    command: str = "gh pr create --fill",
    stdout: str = f"{PR_URL}\n",
    interrupted: bool = False,
) -> dict[str, Any]:
    return {
        "session_id": "session",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "Open a PR"},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": interrupted,
            "isImage": False,
        },
    }


def _run_main(stdin: str) -> str:
    stdout = io.StringIO()
    with (
        mock.patch("sys.stdin", io.StringIO(stdin)),
        mock.patch("sys.stdout", stdout),
    ):
        ghark.hooks.subscribe_hook()
    return stdout.getvalue()


def _isolated_state(test: unittest.TestCase) -> dict[str, str]:
    """Point the state directory at a temporary one for ``test``; return the env."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    env = dict(os.environ, XDG_STATE_HOME=directory.name)
    patcher = mock.patch.dict(os.environ, env)
    patcher.start()
    test.addCleanup(patcher.stop)
    return env


def _run_wake_hook(
    event: dict[str, Any], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import ghark.wake; ghark.wake.wake_hook()"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _pr(number: int) -> ghark.models.PullRequestRef:
    return ghark.models.PullRequestRef("owner", "repo", number)


def _never() -> float:
    return time.monotonic() + 3600


if __name__ == "__main__":
    unittest.main()
