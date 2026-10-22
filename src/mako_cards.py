"""Cards: frontmatter, body sections, acceptance criteria and spike summaries."""
from __future__ import annotations

import json
import re
from contextlib import suppress
from pathlib import Path

from mako_utils import MakoError


TYPES = ("feature", "bug", "spike")
COLUMNS = {"backlog": "Backlog", "in-progress": "In Progress", "done": "Done"}
STATES = {**COLUMNS, "discarded": "Discarded"}
FIELDS = ("id", "type", "title", "depends_on", "status", "created", "branch",
          "claimed_by", "claimed", "merged", "closed", "discarded", "reviews")
LIST_FIELDS = ("depends_on",)


class Card:
    def __init__(self, path: Path, meta: dict, body: str):
        self.path, self.meta, self.body = path, meta, body

    @property
    def id(self) -> str:
        return self.meta.get("id") or self.path.stem

    @property
    def status(self) -> str:
        return self.meta.get("status", "backlog")

    @property
    def title(self) -> str:
        return self.meta.get("title", "")

    @property
    def human(self) -> bool:
        """The user's own task (frontmatter `human: true`): outside the agent workflow,
        it moves freely between columns and folders and agents can't work on it."""
        return str(self.meta.get("human", "")).strip().lower() in ("true", "yes", "1")

    @property
    def approved(self) -> bool:
        """Frontmatter `approved`: cards a worker slot created stay unapproved (`false`) until the
        user ticks it. Cards from the orchestrator or the user are `true`, and a card without the
        field (older) counts as approved."""
        return str(self.meta.get("approved", "")).strip().lower() not in ("false", "no", "0")

    def render(self) -> str:
        keys = list(FIELDS) + [k for k in self.meta if k not in FIELDS]
        lines = ["---"] + [f"{k}: {_yaml_value(self.meta.get(k, ''))}" for k in keys] + ["---"]
        return "\n".join(lines) + "\n" + self.body


def _yaml_value(value) -> str:
    if isinstance(value, list):
        return f"[{', '.join(value)}]" if value else ""
    v = "" if value is None else str(value)
    if v and (re.search(r"[:#\[\]{}\"'\\,&*!|>%@`]", v) or v != v.strip()):
        return json.dumps(v, ensure_ascii=False)
    return v


def read_card(path: Path) -> Card:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return Card(path, {"id": path.stem}, text)
    meta, key = {}, None
    for line in m.group(1).splitlines():
        # Block lists, as Obsidian's Properties editor writes them: "key:" then "  - item".
        if key in LIST_FIELDS and (item := re.match(r"^\s*-\s*(.*?)\s*$", line)):
            if not isinstance(meta.get(key), list):
                meta[key] = []
            if item.group(1):
                meta[key].append(item.group(1).strip("\"'"))
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep:
            key = None
            continue
        value = value.strip()
        if key in LIST_FIELDS and value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            continue
        if value.startswith('"'):
            with suppress(ValueError):
                value = json.loads(value)
        meta[key] = value
    return Card(path, meta, m.group(2))


CARD_BODY = """
# {title}

## Description

{description}

## Acceptance criteria

{criteria}

## Notes

"""
EMPTY_CRITERIA = "- [ ]"

# The `## ` headings Mako writes in a card body: CARD_BODY's sections, and the notes
# _append adds (`## Released (agent-a, <date>)`). Only these end the Description section,
# so a description may have `## ` headings of its own, just not one of these. Like
# Markdown, a heading may be indented up to 3 spaces (4 is a code block).
CARD_SECTIONS = ("Description", "Acceptance criteria", "Notes")
NOTE_HEADINGS = ("Closing note", "Released", "Discarded", "Reopened")
MAKO_HEADING = re.compile(rf" {{0,3}}## (?:(?:{'|'.join(map(re.escape, CARD_SECTIONS))})"
                          rf"|(?:{'|'.join(map(re.escape, NOTE_HEADINGS))}) \(.*\))[ \t]*")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def mako_headings(text: str) -> list[tuple[int, str]]:
    """(offset, heading) of each Mako heading line in text, skipping fenced code blocks."""
    found, fence, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        marker = FENCE.match(bare)
        if fence:
            if (marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence)
                    and not bare[marker.end():].strip()):
                fence = None
        elif marker:
            fence = marker.group(1)
        elif MAKO_HEADING.fullmatch(bare):
            found.append((pos, bare.strip()))
        pos += len(line)
    return found


def check_description(text: str) -> None:
    """Refuse a description that would split the card: one with a Mako heading in it."""
    for _, heading in mako_headings(text):
        name = heading[3:]
        kind = "a card section" if name in CARD_SECTIONS else "a heading Mako uses for notes"
        hint = (", or fill in the card's own section" if name in CARD_SECTIONS[1:] else "")
        if name == "Acceptance criteria":
            hint = ", or pass the criteria as acceptance_criteria (--criteria) so they fill the card's own section"
        raise MakoError(f"'{heading}' is {kind}; use ### inside the description{hint}")


CRITERIA_HEADING = "## Acceptance criteria"
CHECKBOX = re.compile(r"\s*[-*+] \[[ xX]\](?: |$)")
BULLET = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+")


def criteria_lines(lines) -> str:
    """Acceptance-criteria text as checkbox lines: existing `- [ ]`/`- [x]` lines stay as they
    are, any other non-empty line (bulleted or not) becomes `- [ ] <text>`."""
    out = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        out.append(line.lstrip() if CHECKBOX.match(line) else f"- [ ] {BULLET.sub('', line, count=1).strip()}")
    return "\n".join(out) or EMPTY_CRITERIA


def split_criteria(description: str) -> tuple[str, str | None]:
    """Split a `## Acceptance criteria` section out of a description: (the description without
    it, the criteria as checkbox lines), or (description, None) when it has none. The section
    runs to the end of the text; any other Mako heading is still refused."""
    found = [pos for pos, heading in mako_headings(description) if heading == CRITERIA_HEADING]
    if not found:
        check_description(description)
        return description, None
    before, section = description[:found[0]], description[found[0]:]
    check_description(before)
    after = section.split("\n", 1)[1] if "\n" in section else ""
    check_description(after)
    return before, criteria_lines(after.splitlines())


def set_section(body: str, cid: str, name: str, text: str) -> str:
    """body with the text under its `## <name>` heading replaced (up to the next Mako heading)."""
    heading = f"## {name}"
    headings = mako_headings(body)
    starts = [i for i, (_, found) in enumerate(headings) if found == heading]
    if not starts:
        raise MakoError(f"{cid} has no '{heading}' section to replace; "
                        "fix the card by hand, or use --append")
    if starts[0] + 1 == len(headings):
        raise MakoError(f"{cid}'s {name} section has no Mako heading after it "
                        f"({', '.join('## ' + s for s in CARD_SECTIONS[1:])}), so "
                        "its end is unclear; fix the card by hand, or use --append")
    begin = body.index("\n", headings[starts[0]][0]) + 1
    return f"{body[:begin]}\n{text}\n\n{body[headings[starts[0] + 1][0]:]}"

# A spike's summary: on its branch at .mako_spikes/spike_<ID>_<slug>.md, written by Mako
# at claim time (frontmatter filled in), the body by the agent.
SPIKE_DIR = ".mako_spikes"
SUMMARY_FIELDS = ("id", "title", "agent", "created", "branch", "closed")
SUMMARY_BODY = """
# {title}

## Summary

_TBD_

## Findings

## Recommendation
"""


def cycle_from(graph: dict, start: str) -> list[str] | None:
    """A path start -> ... -> start through graph (id -> dependency ids), if there is one."""
    seen, stack = set(), [(start, [start])]
    while stack:
        node, path = stack.pop()
        for d in graph.get(node, []):
            if d == start:
                return path + [d]
            if d not in seen:
                seen.add(d)
                stack.append((d, path + [d]))
    return None


def review_count(card: Card) -> int:
    """Reviews held on the issue; a missing or blank field (older cards) is 0."""
    try:
        return max(0, int(str(card.meta.get("reviews", "")).strip() or 0))
    except ValueError:
        return 0
