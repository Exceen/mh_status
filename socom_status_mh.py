#!/usr/bin/env python3
"""
SOCOM Monster Hunter gathering halls, live in the terminal.

Data: https://www.socom.cc/status.xml (aemu / PRO adhoc server status).

    python3 socom_status_mh.py --me Exceen
    python3 socom_status_mh.py --once

Keys: r refresh · j/k scroll · p "PPSSPP" names / +N · q quit
"""

import re
import xml.etree.ElementTree as ET

import mh_status_ui as ui

STATUS_URL = "https://www.socom.cc/status.xml"
CAPACITY = 4

# "names" are matched against <game name="...">: the server shows the title from
# its product table (ULUS10391 -> "Monster Hunter 2G", regional versions are
# crosslinked to it), or the raw product code if it doesn't know the game.
GAMES = [
    {"key": "p3", "title": "Portable 3rd",
     "names": ["Monster Hunter 3", "ULJM05800"]},
    {"key": "fu", "title": "Freedom Unite",
     "names": ["Monster Hunter 2G", "ULUS10391", "ULES01213", "ULJM05500"]},
    {"key": "f1", "title": "Freedom 1",
     "names": ["Monster Hunter", "ULUS10084", "ULES00318", "ULJM05066"]},
]

# Room codes: MHP2Q000 / MHP3Q000 = gathering hall 01, MHPSP000 (Freedom) = hall 01,
# other letters (MHP3A000) are a different room type, shown as A01.
_HALL_RE = re.compile(r"^MH(?:P\dG?|PSP)([A-Z]?)(\d{3})$")


def _hall_parts(code):
    m = _HALL_RE.match(code or "")
    if not m:
        return None
    kind = m.group(1)
    return ("" if kind in ("", "Q") else kind), int(m.group(2)) + 1


def hall_sort_key(code):
    parts = _hall_parts(code)
    if parts:
        return (0 if not parts[0] else 1, parts[0], parts[1], code)
    return (2, "", 0, code or "")


def hall_label(code):
    """MHP2Q000 -> '01', MHP2Q018 -> '19', MHP3A000 -> 'A01', unknown codes unchanged."""
    parts = _hall_parts(code)
    if not parts:
        return code or "?"
    return "%s%02d" % parts


def parse(raw):
    root = ET.fromstring(raw)
    by_name = {}
    for game in root.findall("game"):
        by_name.setdefault(game.get("name", ""), []).append(game)

    games = []
    for spec in GAMES:
        halls, users = [], 0
        for name in spec["names"]:
            for game in by_name.pop(name, []):
                users += int(game.get("usercount", "0") or 0)
                for group in game.findall("group"):
                    code = group.get("name", "")
                    count = int(group.get("usercount", "0") or 0)
                    players = [(u.text or "").strip() for u in group.findall("user")]
                    if code == "Groupless" and not players:   # online, but in no hall
                        continue
                    halls.append({"code": code, "label": hall_label(code),
                                  "count": max(count, len(players)), "capacity": CAPACITY,
                                  "players": players})
        halls.sort(key=lambda h: hall_sort_key(h["code"]))
        games.append({"key": spec["key"], "title": spec["title"],
                      "rgb": ui.GAME_COLORS[spec["key"]],
                      "users": users, "halls": halls})

    return {"note": "%s on server" % root.get("usercount", "?"), "games": games}


def fetch(file=None):
    raw = open(file, "rb").read() if file else ui.http_get(STATUS_URL)
    return parse(raw)


if __name__ == "__main__":
    ui.main(fetch, brand="SOCOM", brand_rgb=(242, 165, 65), source=STATUS_URL,
            description="Live terminal view of the Monster Hunter gathering halls on socom.cc")
