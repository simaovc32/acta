"""obsidian.py — read an Obsidian Kanban-plugin board from the vault.

Optional: when ACTA_OBSIDIAN_VAULT is set, the dashboard shows one Obsidian
board (ACTA_OBSIDIAN_BOARD, relative to the vault) alongside the native boards
in acta.db. It is **read-only**: the vault is also written by Obsidian (synced
with git), and a two-way sync into a file both sides edit is exactly how cards
get lost to merge conflicts. Writes stay in Obsidian; the dashboard just
renders what the last sync brought over.

The Kanban-plugin markdown shape:

    ---
    <frontmatter>
    kanban-plugin: board
    ---

    ## Lane name

    - [ ] a card
    - [x] a done card
      indented lines are the card's body

    %% kanban:settings
    ```
    {"kanban-plugin":"board","list-collapse":[false,null,true]}
    ```
    %%

Parsing is deliberately forgiving: anything that is not a `## ` header or a
top-level `- [ ] ` / `- [x] ` list item is either attached to the current card
(if indented) or ignored, so an unexpected Kanban feature degrades to "not
shown" rather than a crash.
"""

import json
import re

from acta import config

VAULT = config.OBSIDIAN_VAULT
DEFAULT_REL = config.OBSIDIAN_BOARD

_CARD_RE = re.compile(r"^- \[([ xX])\] (.*)$")
_LANE_RE = re.compile(r"^## (.+?)\s*$", re.M)
_SETTINGS_RE = re.compile(r"\n%%\s*kanban:settings.*?```\n(\{.*?\})\n```\s*\n?%%", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def parse_text(text: str) -> dict:
    """Parse Kanban markdown into {frontmatter, lanes:[{name, collapsed, cards}]}."""
    fm, body = _parse_frontmatter(text)

    collapse: list = []
    sm = _SETTINGS_RE.search(body)
    if sm:
        try:
            collapse = json.loads(sm.group(1)).get("list-collapse") or []
        except Exception:
            collapse = []
        body = body[: sm.start()]

    lanes: list[dict] = []
    parts = _LANE_RE.split(body)
    # parts[0] is the preamble before the first "## "; then (name, content) pairs
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        content = parts[i + 1] if i + 1 < len(parts) else ""
        cards: list[dict] = []
        cur = None
        for line in content.splitlines():
            cm = _CARD_RE.match(line)
            if cm:
                cur = {
                    "text": cm.group(2).strip(),
                    "checked": cm.group(1).lower() == "x",
                    "body": "",
                }
                cards.append(cur)
            elif cur is not None and (line.startswith("  ") or line.startswith("\t")):
                cur["body"] += line.lstrip() + "\n"
        for c in cards:
            c["body"] = c["body"].strip()
        lanes.append({"name": name, "cards": cards})

    for idx, lane in enumerate(lanes):
        lane["collapsed"] = bool(collapse[idx]) if idx < len(collapse) else False

    return {"frontmatter": fm, "lanes": lanes}


def parse_board(rel_path: str = DEFAULT_REL) -> dict:
    """Read + parse the vault Kanban file. Never raises — returns an `error`
    key instead so the API can serve an empty board with a note."""
    if VAULT is None:
        return {"frontmatter": {}, "lanes": [], "error": "no Obsidian vault configured"}
    path = VAULT / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"frontmatter": {}, "lanes": [], "error": f"not found: {rel_path}"}
    except OSError as exc:
        return {"frontmatter": {}, "lanes": [], "error": f"{type(exc).__name__}: {exc}"}

    if "kanban-plugin" not in text:
        return {"frontmatter": {}, "lanes": [],
                "error": "not a Kanban board (no kanban-plugin marker)"}
    try:
        return parse_board_from_text(text)
    except Exception as exc:                       # forgiving: never crash a read
        return {"frontmatter": {}, "lanes": [], "error": f"parse failed: {exc}"}


def parse_board_from_text(text: str) -> dict:
    return parse_text(text)


if __name__ == "__main__":
    import sys
    b = parse_board(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL)
    if b.get("error"):
        print("ERROR:", b["error"])
    for lane in b["lanes"]:
        mark = " [collapsed]" if lane["collapsed"] else ""
        print(f"\n## {lane['name']}{mark}  ({len(lane['cards'])} cards)")
        for c in lane["cards"]:
            print(f"  [{'x' if c['checked'] else ' '}] {c['text']}")
            if c["body"]:
                print(f"      body: {c['body']!r}")
