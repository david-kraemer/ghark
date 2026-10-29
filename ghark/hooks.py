"""Claude Code hooks that keep Claude's GitHub comments recognizable."""

import json
import os
import re
import shlex
import sys
from typing import Any

from .models import CLAUDE_SIGNATURE

__all__ = ["signature_hook", "unsigned_comment"]

# --- PreToolUse: refuse unsigned GitHub comments -----------------------------
#
# ghark treats every unsigned comment by the reviewer's login as a review
# request. Claude posts with that login, so an unsigned comment from Claude
# wakes its own session. This hook denies Bash commands that would post one.
#
# The hook fails open. A body it cannot read with confidence -- a shell
# variable, a command substitution, stdin from a pipe, a missing file -- is
# allowed, because a false block stalls legitimate work while a rare miss costs
# one extra wake-up.

SIGNATURE_LINE = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


def signature_hook() -> None:
    """Run the PreToolUse hook: read the tool call on stdin, deny if unsigned.

    A denial is printed as ``hookSpecificOutput`` JSON with exit status 0;
    Claude Code shows ``permissionDecisionReason`` to Claude. Malformed input
    and every other tool are allowed silently.
    """
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        command = payload["tool_input"]["command"]
        cwd = payload.get("cwd") or os.getcwd()
    except (ValueError, KeyError, TypeError):
        return
    if payload.get("tool_name") != "Bash" or not isinstance(command, str):
        return
    problem = unsigned_comment(command, cwd)
    if problem is None:
        return
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _denial_reason(problem),
        }
    }
    print(json.dumps(decision))


def unsigned_comment(command: str, cwd: str) -> str | None:
    """Describe the first unsigned GitHub comment body ``command`` would post.

    :param command: The Bash command Claude is about to run.
    :param cwd: The directory relative file paths are resolved against.
    :returns: Where the unsigned body comes from, or ``None`` to allow.
    """
    if not _MAYBE_COMMENT.search(command):
        return None
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        # Unbalanced quotes, or a stdin heredoc whose text the lexer cannot
        # read; the body is not recoverable, so allow.
        return None
    for args in _gh_invocations(tokens):
        match args:
            case ["pr", "comment" | "review", *rest] | ["issue", "comment", *rest]:
                bodies = _cli_bodies(rest)
            case ["api", *rest]:
                bodies = _api_bodies(rest)
            case _:
                continue
        for source, body in bodies:
            if _is_unsigned(body, source, command, cwd):
                return source
    return None


# Body sources: ("--body", text), ("file", path), ("field body", text),
# ("field-file body", path), ("--input", path), ("graphql", texts joined).
_Body = tuple[str, str]

_MAYBE_COMMENT = re.compile(r"\bgh\b.*\b(?:comment|review|api)\b", re.DOTALL)
_SEPARATORS = frozenset("();<>|&\n")
_EXPANSION = re.compile(r"\$[({\w@*#?!-]|`")
_HEREDOC = re.compile(
    r"\$\(\s*cat\s*<<-?\s*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)[ \t]*\n"
    r"(?P<text>.*)\n[ \t]*(?P=tag)\s*\)",
    re.DOTALL,
)
_COMMENT_ENDPOINT = re.compile(r"/(?:comments|reviews|replies)$|/reviews/\d+/events$")
_GRAPHQL_COMMENT = re.compile(
    r"\b(?:addComment|addPullRequestReview|addPullRequestReviewComment"
    r"|addPullRequestReviewThreadReply|submitPullRequestReview)\b"
)
# gh api flags that consume the next argument, so it is not the endpoint.
_API_VALUE_FLAGS = frozenset(
    {"-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header"}
    | {"--input", "-q", "--jq", "-t", "--template", "--hostname", "--cache"}
    | {"-p", "--preview"}
)


def _denial_reason(source: str) -> str:
    where = (
        "the end of that file"
        if source.startswith(("file ", "--input ", "field-file "))
        else "the end of the body"
    )
    return (
        f"This command posts a GitHub comment without the Claude Code signature "
        f"(unsigned body from {source}). ghark treats unsigned comments from the "
        f"reviewer's account as new review requests, so this session would wake "
        f"on its own reply. Append this line to {where} and run it again:\n\n"
        f"{SIGNATURE_LINE}"
    )


def _shell_tokens(command: str) -> list[str]:
    """Split ``command`` into words and one-token operators, newlines included."""
    lexer = shlex.shlex(
        command.replace("\\\n", " "), posix=True, punctuation_chars="();<>|&\n"
    )
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    return list(lexer)


def _gh_invocations(tokens: list[str]) -> list[list[str]]:
    """Return the arguments of each ``gh`` call, up to the next shell operator."""
    calls = []
    for i, token in enumerate(tokens):
        if token == "gh" or token.endswith("/gh"):
            args = []
            for arg in tokens[i + 1 :]:
                if set(arg) <= _SEPARATORS:
                    break
                args.append(arg)
            calls.append(args)
    return calls


def _cli_bodies(args: list[str]) -> list[_Body]:
    """Body sources of ``gh pr comment``, ``gh pr review`` or ``gh issue comment``."""
    bodies = []
    i = 0
    while i < len(args):
        arg = args[i]
        value = args[i + 1] if i + 1 < len(args) else None
        if arg in ("-b", "--body") and value is not None:
            bodies.append(("--body", value))
            i += 1
        elif arg in ("-F", "--body-file") and value is not None:
            bodies.append((f"file {value}", value))
            i += 1
        elif arg.startswith("--body="):
            bodies.append(("--body", arg.removeprefix("--body=")))
        elif arg.startswith("--body-file="):
            path = arg.removeprefix("--body-file=")
            bodies.append((f"file {path}", path))
        elif arg.startswith("-b") and len(arg) > 2:
            bodies.append(("--body", arg[2:]))
        elif arg.startswith("-F") and len(arg) > 2:
            bodies.append((f"file {arg[2:]}", arg[2:]))
        i += 1
    return bodies


def _api_bodies(args: list[str]) -> list[_Body]:
    """Body sources of a ``gh api`` call that creates a comment or review."""
    endpoint = None
    method = None
    fields: list[tuple[str, str]] = []  # (flag, "key=value")
    inputs: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        flag, _, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            flag, inline = arg[:2], arg[2:]
        if flag in _API_VALUE_FLAGS:
            value = inline
            if not value and i + 1 < len(args):
                i += 1
                value = args[i]
            if flag in ("-X", "--method"):
                method = value.upper()
            elif flag in ("-f", "--raw-field", "-F", "--field"):
                fields.append((flag, value))
            elif flag == "--input":
                inputs.append(value)
        elif not arg.startswith("-") and endpoint is None:
            endpoint = arg
        i += 1
    if endpoint is None:
        return []
    if method is None:
        method = "POST" if fields or inputs else "GET"
    if method != "POST":
        return []
    path = endpoint.split("?", 1)[0].rstrip("/")
    if path == "graphql":
        return _graphql_bodies(fields, inputs)
    if not _COMMENT_ENDPOINT.search(path):
        return []
    bodies = [(f"--input {path}", path) for path in inputs]
    for flag, field in fields:
        key, _, value = field.partition("=")
        if key != "body" and not key.endswith("[body]"):
            continue
        if flag in ("-F", "--field") and value.startswith("@"):
            bodies.append((f"field-file {key}", value[1:]))
        else:
            bodies.append((f"field {key}", value))
    return bodies


def _graphql_bodies(fields: list[tuple[str, str]], inputs: list[str]) -> list[_Body]:
    """Treat a comment mutation's fields as one body: signed if any field is."""
    texts = [field.partition("=")[2] for _, field in fields]
    if inputs or any(text.startswith("@") for text in texts):
        return []  # Query or variables in a file; rare enough to allow.
    if not any(_GRAPHQL_COMMENT.search(text) for text in texts):
        return []
    if not any("body" in text for text in texts):
        return []  # A bare approval carries no comment text.
    return [("graphql", "\x00".join(texts))]


def _is_unsigned(body: str, source: str, command: str, cwd: str) -> bool:
    """Whether ``body`` certainly lacks the signature once the shell runs."""
    if source.startswith(("file ", "--input ", "field-file ")):
        return _is_unsigned_file(body, source, command, cwd)
    if source == "graphql":
        texts = body.split("\x00")
        if any(_has_signature(text) for text in texts):
            return False
        return all(_literal_text(text, command) is not None for text in texts)
    text = _literal_text(body, command)
    return text is not None and not _has_signature(text)


def _is_unsigned_file(path: str, source: str, command: str, cwd: str) -> bool:
    if path == "-":
        # Stdin: only a quoted heredoc in the command is readable, and then
        # the signature, if present, is somewhere in the command text.
        return bool(re.search(r"<<-?\s*['\"]", command)) and not _has_signature(command)
    if command.count(path) > 1:
        return False  # The command itself writes or copies this file first.
    try:
        with open(os.path.join(cwd, os.path.expanduser(path)), encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    if source.startswith("--input "):
        try:
            bodies = list(_json_bodies(json.loads(text)))
        except ValueError:
            return False
        return any(not _has_signature(body) for body in bodies)
    return not _has_signature(text)


def _json_bodies(value: Any) -> Any:
    """Yield every string under a ``body`` key in a JSON request payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "body" and isinstance(item, str):
                yield item
            else:
                yield from _json_bodies(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_bodies(item)


def _literal_text(value: str, command: str) -> str | None:
    """Return the text the shell passes for ``value``, or ``None`` if unknown.

    The lexer drops quoting, so a ``$`` or backtick may be literal (single
    quotes, quoted heredoc) or an expansion. Text that appears single-quoted in
    the command is literal; a ``$(cat <<'EOF' ...)`` body is literal; anything
    else with an expansion may receive the signature at run time.
    """
    _, eq, field_value = value.partition("=")
    if f"'{value}'" in command or (eq and f"'{field_value}'" in command):
        return value
    heredoc = _HEREDOC.fullmatch(value.strip())
    if heredoc is not None:
        if heredoc["q"]:
            return heredoc["text"]
        value = heredoc["text"]
    return None if _EXPANSION.search(value) else value


def _has_signature(text: str) -> bool:
    return CLAUDE_SIGNATURE.lower() in text.lower()
