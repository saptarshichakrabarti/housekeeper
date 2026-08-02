"""Copy honesty: every CLI command the dashboard tells a user to run must actually exist.

The wizard once told users to run ``housekeeper review new <name>`` — a command that never
existed; the real subcommand is ``review create``. A user following that copy hits an argparse
error on their first step. This guard walks the real CLI parser and asserts that every
``housekeeper …`` invocation printed in a template resolves to a real command (and, for a command
that groups subcommands or fixes its first positional to a ``choices`` set, that the next token is a
real subcommand or choice). It fails the moment help text drifts from the CLI it documents.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from housekeeper.cli import build_parser

_TEMPLATES = Path(__file__).resolve().parents[1] / "src/housekeeper/dashboard/templates"
# A `housekeeper ...` mention followed by the words that name the command path. Stops naturally at
# the first placeholder (<name>), flag (--x) or punctuation the token pattern does not match.
_INVOCATION = re.compile(r"housekeeper\s+([a-z][\w-]*(?:\s+[a-z][\w-]*)*)")


def _continuations(parser: argparse.ArgumentParser) -> tuple[dict, set[str]]:
    """This parser's subcommand name -> subparser, plus the choices of any fixed positional."""
    subs: dict = {}
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subs.update(action.choices)
        elif action.choices and not action.option_strings:
            choices |= {str(c) for c in action.choices}
    return subs, choices


def _next_tokens_by_path() -> tuple[set[str], dict[str, set[str]]]:
    """(valid top-level commands, {command-path: tokens that may legally follow it})."""
    top_subs, _ = _continuations(build_parser())
    next_tokens: dict[str, set[str]] = {}

    def walk(path: str, parser: argparse.ArgumentParser) -> None:
        subs, choices = _continuations(parser)
        next_tokens[path] = set(subs) | choices
        for name, sub in subs.items():
            walk(f"{path} {name}", sub)

    for name, sub in top_subs.items():
        walk(name, sub)
    return set(top_subs), next_tokens


def test_every_housekeeper_command_in_the_templates_is_real():
    top_level, next_tokens = _next_tokens_by_path()
    seen = 0
    for template in _TEMPLATES.rglob("*.html"):
        text = template.read_text()
        for match in _INVOCATION.finditer(text):
            tokens = match.group(1).split()
            command = tokens[0]
            assert command in top_level, (
                f"{template.name}: `housekeeper {command}` is not a real command"
            )
            # Validate the second token only where the CLI defines a fixed vocabulary for it: a
            # subcommand group, or a positional pinned to choices. Free-form args are not checked.
            allowed = next_tokens.get(command, set())
            if len(tokens) > 1 and allowed:
                assert tokens[1] in allowed, (
                    f"{template.name}: `housekeeper {command} {tokens[1]}` — "
                    f"{tokens[1]!r} is not a valid {command} subcommand/choice"
                )
            seen += 1
    assert seen, "guard found no housekeeper command references to check — did the regex break?"
