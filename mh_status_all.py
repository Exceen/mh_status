#!/usr/bin/env python3
"""
SOCOM and HunsterVerse Monster Hunter gathering halls in one live terminal view,
one row per server.

    python3 mh_status_all.py --me Exceen
    python3 mh_status_all.py --only fu --hide-empty
    python3 mh_status_all.py --once

If one server can't be reached, its last known halls stay visible with a
warning on its row, and the other server keeps updating.

Keys: r refresh · j/k scroll · p "PPSSPP" names / +N · q quit
"""

import threading
import time

import hunsterverse_status_mh as hunsterverse
import mh_status_ui as ui
import socom_status_mh as socom

SERVERS = [
    {"id": "socom", "brand": "SOCOM", "rgb": (242, 165, 65), "fetch": socom.fetch},
    {"id": "hv", "brand": "HunsterVerse", "rgb": (91, 186, 213), "fetch": hunsterverse.fetch},
]

_last = {}          # server id -> (snapshot, time) of the last successful fetch


def _fetch_one(server, results):
    try:
        results[server["id"]] = (server["fetch"](), None)
    except Exception as exc:
        results[server["id"]] = (None, str(exc) or exc.__class__.__name__)


def fetch(file=None):
    results = {}
    threads = [threading.Thread(target=_fetch_one, args=(s, results), daemon=True)
               for s in SERVERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    sections, failures = [], 0
    for s in SERVERS:
        snap, err = results.get(s["id"], (None, "timed out"))
        if snap is not None:
            _last[s["id"]] = (snap, time.time())
        else:
            failures += 1
            if s["id"] in _last:
                snap, when = _last[s["id"]]
                err = "%s · showing data from %s" % (err, ui.clock(when))
            else:
                snap = {"games": []}
        sections.append({"id": s["id"], "brand": s["brand"], "rgb": s["rgb"],
                         "note": snap.get("note"), "error": err,
                         "games": snap.get("games", [])})

    if failures == len(SERVERS) and not _last:
        raise RuntimeError("; ".join("%s: %s" % (s["brand"], results.get(s["id"], (None, "?"))[1])
                                     for s in SERVERS))
    return {"sections": sections}


if __name__ == "__main__":
    ui.main(fetch, brand="Gathering Halls", brand_rgb=(200, 205, 215), default_interval=60,
            source="socom.cc + hunstermonter.net",
            description="SOCOM and HunsterVerse Monster Hunter gathering halls, one row per server")
