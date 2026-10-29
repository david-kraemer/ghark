"""Checks for the Claude Code hooks."""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ghark.hooks

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


if __name__ == "__main__":
    unittest.main()
