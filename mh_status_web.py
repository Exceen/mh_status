#!/usr/bin/env python3
"""
SOCOM and HunsterVerse Monster Hunter gathering halls as a web page, made
for a phone's home screen. Standard library only.

    python3 mh_status_web.py                  # http://localhost:8080
    python3 mh_status_web.py --port 42426 --host 0.0.0.0

Routes:
    /              the page (settings are kept in the browser)
    /status.json   the halls, ?me=NAME&only=fu,p3&hide_empty=1
    /lite          plain HTML without JavaScript, for old browsers (PSP),
                   same parameters, reloads itself
    /healthz       "ok"

The servers are only asked when somebody looks: a request fetches both
servers if the last fetch is older than CACHE_TTL seconds, all other requests
share that result. Nobody looking = no traffic to socom.cc / hunstermonter.net.
"""

import argparse
import html
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import mh_status_all as all_servers
import mh_status_ui as ui

CACHE_TTL = 25            # seconds a fetch is reused for every visitor
REFRESH = 60              # the page reloads this often ...
REFRESH_IN_HALL = ui.IN_HALL_INTERVAL   # ... or this often while you're in a hall
GAME_KEYS = ("p3", "fu", "f1")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

log = logging.getLogger("mh_status_web")


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------

class Cache:
    """One fetch for everybody, at most every CACHE_TTL seconds."""

    def __init__(self, fetch, ttl):
        self.fetch, self.ttl = fetch, ttl
        self.snap, self.when, self.error = None, 0.0, None
        self.lock = threading.Lock()

    def get(self):
        with self.lock:                 # concurrent requests wait for the same fetch
            if self.snap is None or time.time() - self.when >= self.ttl:
                start = time.time()
                try:
                    self.snap, self.error = self.fetch(), None
                    log.info("fetched in %.1fs", time.time() - start)
                except Exception as exc:  # both servers down and nothing cached
                    self.error = str(exc) or exc.__class__.__name__
                    log.warning("fetch failed: %s", self.error)
                self.when = time.time()
            return self.snap, self.when, self.error


def clean_name(name):
    """Drop control characters so a nickname can't mess up the page."""
    return "".join(c for c in (name or "") if c.isprintable()).strip()


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb) if rgb else None


def build_view(snap, me=None, only=None, hide_empty=False):
    """The snapshot in the shape the page needs, with the same rating and
    name order as the terminal view."""
    me = (me or "").strip().lower() or None
    sections = []
    for sec in ui.sections_of(snap or {"sections": []}):
        games = []
        for g in ui.filter_games(sec["games"], only, hide_empty):
            halls = []
            for h in g.get("halls", []):
                players = [clean_name(p) for p in h.get("players") or []]
                players = [p for p in players if p]
                has_me = any(ui.is_me(p, me) for p in players)
                cap = h.get("capacity", 4)
                named = sorted((p for p in players if not ui.is_default_name(p)), key=str.casefold)
                halls.append({
                    "label": h["label"], "count": h["count"], "capacity": cap,
                    "rating": ui.join_rating(h["count"], cap, has_me),
                    "has_me": has_me,
                    "players": [{"name": p, "me": ui.is_me(p, me)} for p in named],
                    "defaults": [p for p in players if ui.is_default_name(p)],
                    "note": clean_name(h.get("note")) or None,
                })
            games.append({"key": g["key"], "title": g["title"], "color": rgb_hex(g.get("rgb")),
                          "users": g.get("users", 0), "halls": halls})
        sections.append({"id": sec.get("id"), "brand": sec.get("brand"),
                         "color": rgb_hex(sec.get("rgb")), "note": sec.get("note"),
                         "error": sec.get("error"),
                         "hunters": sum(g["users"] for g in games),
                         "hall_count": sum(len(g["halls"]) for g in games),
                         "games": games})
    in_hall = any(h["has_me"] for s in sections for g in s["games"] for h in g["halls"])
    return {"sections": sections, "me_in_hall": in_hall,
            "refresh": REFRESH_IN_HALL if in_hall else REFRESH}


def parse_options(query):
    q = parse_qs(query)

    def one(key):
        return (q.get(key) or [""])[0]

    only = {k.strip().lower() for k in one("only").split(",") if k.strip()} or None
    return {"me": one("me")[:40] or None, "only": only,
            "hide_empty": one("hide_empty") in ("1", "true", "yes", "on")}


# ----------------------------------------------------------------------------
# /lite: plain HTML for browsers without JavaScript (PSP)
# ----------------------------------------------------------------------------

RATING_HEX = {k: rgb_hex(v) for k, v in ui.RATING_RGB.items()}
DEFAULT_HEX = rgb_hex(ui.DEFAULT_NAME_RGB)


def render_lite(view, when, query):
    e = html.escape
    out = ['<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">',
           '<meta http-equiv="refresh" content="%d">' % view["refresh"],
           '<title>MH Gathering Halls</title></head>',
           '<body bgcolor="#14161b" text="#d7dae0" link="#8ab4f8" vlink="#8ab4f8">',
           '<font face="Arial">',
           '<b>Gathering Halls</b> <font color="#808696">%s &middot; <a href="/lite?%s">reload</a></font><br>'
           % (ui.clock(when), e(query))]
    for sec in view["sections"]:
        out.append('<br><font color="%s"><b>%s</b></font> <font color="#808696">%d hunters, %d halls</font>'
                   % (sec["color"] or "#ffffff", e(sec["brand"] or ""), sec["hunters"], sec["hall_count"]))
        if sec["error"]:
            out.append(' <font color="%s">! %s</font>' % (rgb_hex(ui.ERROR_RGB), e(sec["error"][:60])))
        out.append("<br>")
        for g in sec["games"]:
            out.append('<font color="%s"><b>%s</b></font> <font color="#808696">%d</font><br>'
                       % (g["color"], e(g["title"]), g["users"]))
            if not g["halls"]:
                out.append('<font color="#808696">&nbsp; no open halls</font><br>')
            for h in g["halls"]:
                names = [('<b><font color="%s">%s</font></b>' % (g["color"], e(p["name"])))
                         if p["me"] else e(p["name"]) for p in h["players"]]
                if h["defaults"]:
                    names.append('<font color="%s">+%d</font>' % (DEFAULT_HEX, len(h["defaults"])))
                fill = "%d/%d" % (h["count"], h["capacity"])
                color = RATING_HEX.get(h["rating"], "#808696")
                label = e(h["label"])
                if h["has_me"]:
                    label = "[%s]" % label
                out.append('&nbsp; <font color="%s"><b>%s</b></font> <font color="%s">%s</font> %s<br>'
                           % (g["color"], label, color, fill, " &middot; ".join(names) or "(hidden)"))
    out.append("</font></body></html>")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Icons (files in web/)
# ----------------------------------------------------------------------------

ICONS = {                 # URL path -> file in web/
    "/favicon.png": "favicon.png",
    "/favicon.ico": "favicon.png",            # browsers that ask for it anyway
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/icon-512.png": "icon-512.png",
}


MANIFEST = {
    "name": "MH Halls", "short_name": "MH Halls", "start_url": "/", "display": "standalone",
    "background_color": "#14161b", "theme_color": "#14161b",
    "icons": [{"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "mh-status-web"
    cache = None

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/":
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8", "no-cache")
        elif path == "/status.json":
            opts = parse_options(url.query)
            snap, when, err = self.cache.get()
            if snap is None:
                self.send(502, json.dumps({"error": err}), "application/json")
                return
            view = build_view(snap, **opts)
            view["updated"] = datetime.fromtimestamp(when, timezone.utc).isoformat()
            self.send(200, json.dumps(view, ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/lite":
            snap, when, err = self.cache.get()
            if snap is None:
                self.send(502, "<html><body>Servers not reachable: %s</body></html>" % html.escape(err or ""),
                          "text/html; charset=utf-8")
                return
            view = build_view(snap, **parse_options(url.query))
            self.send(200, render_lite(view, when, url.query), "text/html; charset=utf-8")
        elif path in ICONS:
            with open(os.path.join(WEB_DIR, ICONS[path]), "rb") as f:
                self.send(200, f.read(), "image/png", "public, max-age=86400")
        elif path == "/manifest.webmanifest":
            self.send(200, json.dumps(MANIFEST), "application/manifest+json", "public, max-age=86400")
        elif path == "/healthz":
            self.send(200, "ok\n", "text/plain")
        else:
            self.send(404, "not found\n", "text/plain")


def main():
    ap = argparse.ArgumentParser(description="Monster Hunter gathering halls as a web page")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="address to listen on (default 127.0.0.1, 0.0.0.0 for everyone)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--file", help=argparse.SUPPRESS)   # testing: a saved combined snapshot (JSON)
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.file:
        def fetch():
            with open(args.file) as f:
                return json.load(f)
    else:
        def fetch():
            return all_servers.fetch()

    Handler.cache = Cache(fetch, CACHE_TTL)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log.info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
