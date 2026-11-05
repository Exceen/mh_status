#!/usr/bin/env python3
"""
HunsterVerse Monster Hunter gathering halls, live in the terminal.

Data: the fragment behind https://hunstermonter.net/info.php ("GH Status"):
new_gh_getter.php for the halls, ghstats_info.php for the player stats.

    python3 hunsterverse_status_mh.py --me Exceen
    python3 hunsterverse_status_mh.py --once

Keys: r refresh · j/k scroll · p "PPSSPP" names / +N · q quit
"""

import json
import re
from html.parser import HTMLParser

import mh_status_ui as ui

BASE = "https://hunstermonter.net/"
GH_URL = BASE + "new_gh_getter.php"
STATS_URL = BASE + "ghstats_info.php"

# GameName on the page -> display info, in display order
GAMES = [
    {"key": "p3", "match": r"^MHP3", "title": "Portable 3rd"},
    {"key": "fu", "match": r"^MHFU", "title": "Freedom Unite"},
    {"key": "f1", "match": r"^F1\b", "title": "Freedom 1"},
]

_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr"}


class _GHParser(HTMLParser):
    """Walks the GH fragment: EntireGame > GameName, FullGH > GHName / GHPop /
    Hunter spans / QuestInfoShow, TotName."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, set(classes))]
        self.games = []
        self.game = None
        self.hall = None

    def _in(self, cls):
        return any(cls in classes for _, classes in self.stack)

    def handle_starttag(self, tag, attrs):
        classes = set((dict(attrs).get("class") or "").split())
        if "EntireGame" in classes:
            self.game = {"name": "", "total": None, "halls": []}
            self.games.append(self.game)
            self.hall = None
        elif "FullGH" in classes and self.game is not None:
            self.hall = {"name": "", "pop": "", "players": [], "quest": ""}
            self.game["halls"].append(self.hall)
        elif "Hunter" in classes and self.hall is not None:
            self.hall["players"].append("")
        if tag not in _VOID:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.game is None:
            return
        if self._in("GameName"):
            self.game["name"] += data
        elif self._in("TotName"):
            m = re.search(r"(\d+)", data)
            if m:
                self.game["total"] = int(m.group(1))
        elif self.hall is None:
            return
        elif self._in("GHName"):
            self.hall["name"] += data
        elif self._in("GHPop"):
            self.hall["pop"] += data
        elif self._in("Hunter") and self.stack and self.stack[-1][0] == "span":
            self.hall["players"][-1] += data
        elif self._in("QuestInfoShow") and self.stack and self.stack[-1][0] == "b":
            self.hall["quest"] += data


def hall_label(name):
    """'GH01A' -> '01', 'GH5000A' -> '5000', 'GHA lA' -> 'A'."""
    name = name.replace("\xa0", " ").strip()
    m = re.match(r"^GH\s*(\d+)\s*A?$", name)
    if m:
        return m.group(1)
    name = re.sub(r"^GH\s*", "", name)
    name = re.sub(r"\s*l?A$", "", name)
    return name or "?"


def parse(html, stats=None):
    p = _GHParser()
    p.feed(html)

    games = []
    for spec in GAMES:
        found = [g for g in p.games if re.search(spec["match"], g["name"].strip())]
        if not found:
            continue
        halls, in_halls, total = [], 0, 0
        for g in found:
            for h in g["halls"]:
                players = [n.strip().rstrip(",").strip() for n in h["players"] if n.strip()]
                m = re.search(r"(\d+)\s*/\s*(\d+)", h["pop"])
                count, cap = (int(m.group(1)), int(m.group(2))) if m else (len(players), 4)
                count = max(count, len(players))
                if count == 0:
                    continue
                in_halls += count
                halls.append({"label": hall_label(h["name"]), "count": count,
                              "capacity": cap or 4, "players": players,
                              "note": h["quest"].strip() or None})
            total += g["total"] if g["total"] is not None else 0
        users = max(total, in_halls)
        games.append({"key": spec["key"], "title": spec["title"],
                      "rgb": ui.GAME_COLORS[spec["key"]], "users": users,
                      "halls": halls})

    note = None
    if stats:
        m = re.search(r"Max Today:\s*(\d+)", stats.get("stat_page", ""))
        if m:
            note = "max today %s" % m.group(1)
    return {"note": note, "games": games}


def fetch(file=None):
    if file:
        return parse(open(file, "rb").read().decode("utf-8", "replace"))
    html = ui.http_get(GH_URL).decode("utf-8", "replace")
    try:
        stats = json.loads(ui.http_get(STATS_URL, timeout=8))
    except Exception:
        stats = None
    return parse(html, stats)


if __name__ == "__main__":
    ui.main(fetch, brand="HunsterVerse", brand_rgb=(91, 186, 213), source=GH_URL,
            default_interval=60,
            description="Live terminal view of the HunsterVerse Monster Hunter gathering halls")
