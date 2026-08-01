#!/usr/bin/env python3
"""Summarises what a grills.json refresh adds or removes, as markdown.

Used for both the commit message of a local update and the body of the pull
request the sync workflow opens, so that a definitions change says which models
and boards it affects rather than only that something changed.

Run with python3 -m scripts.grills_diff <old.json> <new.json>
"""

import json
import sys
from typing import Any

SECTIONS = (("grill models", "grills"), ("control boards", "control_boards"))


def _names(path: str, section: str) -> set[str]:
    """Returns the names in one section of a definitions file.

    Files predating the split into `grills` and `control_boards` are a flat map
    of models, so treat one as its own grills section and as having no boards.
    """
    with open(path) as f:
        data: dict[str, Any] = json.load(f)
    if "grills" not in data:
        return set(data) if section == "grills" else set()
    return set(data.get(section, {}))


def summarize(old_path: str, new_path: str) -> str:
    """Returns a markdown summary of the names added and removed."""
    lines: list[str] = []
    for title, section in SECTIONS:
        old, new = _names(old_path, section), _names(new_path, section)
        for heading, names in (
            (f"### Adds {title}", sorted(new - old)),
            (f"### Removes {title}", sorted(old - new)),
        ):
            if names:
                lines += [heading, ""]
                lines += [f"* {name}" for name in names]
                lines += [""]
    # No surrounding blank lines: the caller decides how this sits in a commit
    # message or a pull request body, and a trailing newline here would be
    # eaten by command substitution anyway, leaving the last bullet to run into
    # whatever follows it.
    return "\n".join(lines).strip()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <old.json> <new.json>")
    if summary := summarize(sys.argv[1], sys.argv[2]):
        print(summary)
