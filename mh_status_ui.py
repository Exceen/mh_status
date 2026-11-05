"""
Terminal frontend for Monster Hunter gathering hall status.

A data source module only has to provide a fetch function returning a snapshot:

    {
        "note":  "173 on server",                     # optional, shown in the header
        "games": [
            {
                "key": "fu", "title": "Freedom Unite",
                "rgb": (242, 165, 65),
                "users": 34,                          # hunters in this game
                "halls": [
                    {"label": "01", "count": 2, "capacity": 4,
                     "players": ["Zzzz", "ZANMATO"],
                     "note": "G-Rank Event Quests"},  # optional, e.g. the quest
                ],
            },
        ],
    }

and call main(...) with its branding. Everything else (live view, scrolling,
colours, layout, --once output) lives here. Standard library only.

Several servers at once: return {"sections": [{"id": "socom", "brand": "SOCOM",
"rgb": (r, g, b), "note": ..., "error": None or "message", "games": [...]}, ...]}
instead; each section is drawn as its own row with a divider.
"""

import argparse
import os
import re
import select
import shutil
import signal
import ssl
import sys
import threading
import time
import unicodedata
import urllib.request

USER_AGENT = "mh-status/3.0"
MIN_COL_WIDTH = 32
PIP_LIMIT = 6        # halls up to this capacity get ●●·· pips, larger ones "n/cap"
RESIZE_COL_WIDTH = 40     # --resize / --resize-once: width per game box (MIN_COL_WIDTH is the limit)
RESIZE_BUFFER_HALLS = 1   # --resize / --resize-once: spare lines for this many more halls
IN_HALL_INTERVAL = 30     # refresh this often (seconds) while --me is in a hall
RESIZE_SETTLE = 0.5       # --resize: seconds the window must be still before its height is fixed

GAME_COLORS = {
    "fu": (242, 165, 65),
    "p3": (79, 195, 199),
    "f1": (226, 87, 76),
    "f2": (156, 204, 101),
}
ERROR_RGB = (226, 87, 76)
RATING_RGB = {"best": (120, 205, 110), "good": (232, 190, 75), "full": (225, 85, 80)}
DEFAULT_NAME_RGB = (170, 150, 225)   # the "+N" count of unnamed PPSSPP players


# ----------------------------------------------------------------------------
# Networking helper for data sources
# ----------------------------------------------------------------------------

def _ssl_context():
    """Default context; python.org builds on macOS often ship without CA
    certificates, so fall back to the system bundle or certifi."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for path in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem",
                     "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(path):
                return ssl.create_default_context(cafile=path)
        try:
            import certifi  # type: ignore
            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
    return ctx


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return resp.read()


# ----------------------------------------------------------------------------
# Styling and text measurement
# ----------------------------------------------------------------------------

class Style:
    def __init__(self, enabled, truecolor):
        self.on = enabled
        self.truecolor = truecolor

    def _wrap(self, codes, text):
        if not self.on or not text:
            return text
        return "\033[%sm%s\033[0m" % (codes, text)

    def _rgb(self, rgb, bg=False):
        if self.truecolor:
            return "%d;2;%d;%d;%d" % (((48 if bg else 38),) + tuple(rgb))
        r, g, b = (round(c / 255 * 5) for c in rgb)
        return "%d;5;%d" % (48 if bg else 38, 16 + 36 * r + 6 * g + b)

    def fg(self, rgb, text, bold=False, dim=False):
        if not self.on:
            return text
        code = self._rgb(rgb)
        if bold:
            code = "1;" + code
        if dim:
            code = "2;" + code
        return self._wrap(code, text)

    def bold(self, text):
        return self._wrap("1", text)

    def muted(self, text):
        return self.fg((128, 134, 150), text)

    def faint(self, text):
        return self.fg((78, 83, 96), text)

    def badge(self, rgb, text):
        if not self.on:
            return text
        return self._wrap("1;%s;%s" % (self._rgb((20, 22, 27)), self._rgb(rgb, bg=True)), text)


_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def char_width(ch):
    if unicodedata.combining(ch) or ch in "​‍️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vis_width(text):
    return sum(char_width(c) for c in _ANSI_RE.sub("", text))


def truncate(text, width):
    """Truncate plain text to a display width, adding an ellipsis."""
    if vis_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def clip(text, width):
    """Cut a line with colour codes to `width` display columns."""
    if vis_width(text) <= width:
        return text
    out, used, i = [], 0, 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = char_width(text[i])
        if used + w > width:
            break
        out.append(text[i])
        used += w
        i += 1
    return "".join(out) + "\033[0m"


def pad(text, width):
    return text + " " * max(0, width - vis_width(text))


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

def is_default_name(name):
    """PPSSPP's out-of-the-box nickname, i.e. 'no name set'."""
    return name.strip().upper().startswith("PPSSPP")


def is_me(name, me):
    """Exact (case-insensitive) match, or a name the server shortened to
    'abc...' / 'abc…' that your nickname starts with."""
    if not me or not name:
        return False
    name = name.strip().lower()
    if name == me:
        return True
    for tail in ("...", "…"):
        if name.endswith(tail) and len(name) > len(tail) + 1:
            return me.startswith(name[:-len(tail)])
    return False


def me_in_hall(snap, me):
    """True if `me` is in any hall of any server in the snapshot."""
    if not snap or not me:
        return False
    return any(is_me(p, me) for sec in sections_of(snap) for g in sec["games"]
               for h in g.get("halls", []) for p in h.get("players") or [])


def join_rating(count, capacity, has_me):
    """How good a hall is to join: the party size with you in it decides.
    'best' = makes a full party (3/4, or 4/4 with you), 'good' = one short
    (2/4, or 3/4 with you), 'full' = no room, None = nothing special.
    Halls that aren't 4-player parties only get 'full'."""
    party = count if has_me else count + 1
    if party > capacity:
        return "full"
    if capacity != 4:
        return None
    if party == capacity:
        return "best"
    if party == capacity - 1:
        return "good"
    return None


def render_game(game, width, st, me, show_default):
    """Render one game as a box of `width` display columns -> list of lines."""
    acc = game.get("rgb", (200, 200, 200))
    inner = width - 4                       # "│ " + content + " │"

    def edge(s):
        return st.fg(acc, s, dim=True)

    def plural(n, word):
        return st.bold(str(n)) + " " + word + ("" if n == 1 else "s")

    lines = []

    # ╭─ 2G ──────────╮
    title = " %s " % truncate(game["title"], max(4, width - 8))
    fill = width - 4 - vis_width(title)
    lines.append(edge("╭─") + st.fg(acc, title, bold=True) + edge("─" * max(1, fill)) + edge("─╮"))

    # summary
    halls = game.get("halls", [])
    summary = st.muted(" · ").join([plural(game.get("users", 0), "hunter"),
                                    plural(len(halls), "hall")])
    lines.append(edge("│ ") + pad(summary, inner) + edge(" │"))
    lines.append(edge("├" + "─" * (width - 2) + "┤"))

    if not halls:
        lines.append(edge("│ ") + pad(st.muted("no open halls right now"), inner) + edge(" │"))

    num_w = max([vis_width(h["label"]) for h in halls] + [2])
    pip_w = max([h.get("capacity", 4) if h.get("capacity", 4) <= PIP_LIMIT
                 else len("%d/%d" % (h["count"], h["capacity"])) for h in halls] + [4])
    star_w = 0 if st.on else 2          # room for the " ★" marker without colours
    for h in halls:
        players = h.get("players") or []
        has_me = any(is_me(p, me) for p in players)
        cap = h.get("capacity", 4)
        rating = join_rating(h["count"], cap, has_me)

        num = " %s " % h["label"].rjust(num_w)
        num = st.badge(acc, num) if has_me else st.fg(acc, num, bold=True)
        if cap <= PIP_LIMIT:
            fill_txt = "".join("●" if k < h["count"] else "·" for k in range(cap))
        else:
            fill_txt = "%d/%d" % (h["count"], cap)
        if not st.on and rating == "best":
            fill_txt += " ★"
        fill_txt = fill_txt.ljust(pip_w + star_w)
        rgb = RATING_RGB.get(rating)
        fill_txt = st.fg(rgb, fill_txt) if rgb else st.muted(fill_txt)
        head = num + " " + fill_txt + "  "
        indent = vis_width(head)
        name_w = max(6, inner - indent)

        # named players alphabetically; default "PPSSPP" nicknames last, or
        # collapsed into one "+N" entry
        named = sorted((p for p in players if not is_default_name(p)), key=str.casefold)
        defaults = [p for p in players if is_default_name(p)]
        entries = [(p, False) for p in named]
        if defaults and show_default:
            entries += [(p, False) for p in defaults]
        elif defaults:
            entries.append(("+%d" % len(defaults), True))
        if not entries:
            entries = [("(hidden)", False)]

        # wrap names into rows that fit
        rows, cur, cur_w = [], [], 0
        sep = st.fg((165, 172, 188), " · ", bold=True)
        for p, is_count in entries:
            label = truncate(p, name_w)
            w = vis_width(label)
            if is_count:
                label = st.fg(DEFAULT_NAME_RGB, label, bold=True)
            elif is_me(p, me):
                label = st.fg(acc, label, bold=True)
            elif is_default_name(p):
                label = st.faint(label)
            if cur and cur_w + 3 + w > name_w:
                rows.append(cur)
                cur, cur_w = [], 0
            cur_w += (3 if cur else 0) + w
            cur.append(label)
        rows.append(cur)

        for r, row in enumerate(rows):
            prefix = head if r == 0 else " " * indent
            lines.append(edge("│ ") + pad(prefix + sep.join(row), inner) + edge(" │"))
        if h.get("note"):
            note = st.faint("↳ ") + st.muted(truncate(h["note"], name_w - 2))
            lines.append(edge("│ ") + pad(" " * indent + note, inner) + edge(" │"))

    lines.append(edge("╰" + "─" * (width - 2) + "╯"))
    return lines


def layout(games, cols, st, me, show_default):
    """Place game boxes in as many columns as fit -> list of lines."""
    if not games:
        return [st.muted("  no games in the data")]
    ncols = max(1, min(len(games), (cols + 1) // (MIN_COL_WIDTH + 1)))
    col_w = (cols - (ncols - 1)) // ncols
    stacks = [[] for _ in range(ncols)]
    for g in games:
        box = render_game(g, col_w, st, me, show_default)
        target = min(range(ncols), key=lambda i: len(stacks[i]))
        if stacks[target]:
            stacks[target].append("")
        stacks[target].extend(box)
    height = max(len(s) for s in stacks)
    out = []
    for row in range(height):
        cells = [pad(s[row] if row < len(s) else "", col_w) for s in stacks]
        out.append(" ".join(cells).rstrip())
    return out


def brand_line(brand, brand_rgb, st):
    badge = st.badge(brand_rgb, " %s " % brand)
    return badge if "Monster Hunter" in brand else badge + st.bold(" Monster Hunter ")


def sections_of(snap):
    """A snapshot either has "games" (one server) or "sections", a list of
    {"id", "brand", "rgb", "note", "error", "games"} (several servers)."""
    if "sections" in snap:
        return snap["sections"]
    return [{"id": "", "brand": None, "rgb": None, "note": snap.get("note"),
             "error": snap.get("error"), "games": snap.get("games", [])}]


def filter_games(games, only=None, hide_empty=False):
    if only:
        games = [g for g in games if g["key"] in only]
    if hide_empty:
        games = [g for g in games if g.get("halls")]
    return games


def _counts(games):
    return (sum(g.get("users", 0) for g in games),
            sum(len(g.get("halls", [])) for g in games))


def _divider_variants(sec, games, st, with_error=True):
    """(left, right) pairs for a server's divider, from the most to the least
    detailed."""
    mh, halls = _counts(games)
    badge = st.badge(sec["rgb"], " %s " % sec["brand"])
    stats = "%s hunters in %s halls" % (st.bold(str(mh)), st.bold(str(halls)))
    short = "%s %s" % (st.bold(str(mh)), st.muted("in %d halls" % halls))
    note = "  " + st.muted("(%s)" % sec["note"]) if sec.get("note") else ""
    err = sec.get("error") if with_error else None
    errors = ([st.fg(ERROR_RGB, " ⚠ %s" % truncate(err, 50)), st.fg(ERROR_RGB, " ⚠")]
              if err else [""])
    out = []
    for e in errors:
        out.append((badge + "  " + stats + note, e))
        out.append((badge + "  " + stats, e))
    out.append((badge + "  " + short, errors[-1]))
    out.append((badge, errors[-1]))
    return out


def section_divider(sec, games, cols, st):
    """'SOCOM  55 hunters in 21 halls  (138 on server) ───── ⚠ error', shortened
    step by step until it fits into cols."""
    variants = _divider_variants(sec, games, st)
    for left, right in variants:
        rule_w = cols - vis_width(left) - vis_width(right) - 1
        if rule_w >= 2:
            return left + " " + st.fg(sec["rgb"], "─" * rule_w, dim=True) + right
    return variants[-1][0]


def render_body(snap, cols, st, args, show_default):
    sections = sections_of(snap)
    multi = len(sections) > 1

    out = []
    for i, sec in enumerate(sections):
        games = filter_games(sec["games"], args.only, args.hide_empty)
        if multi:
            if i:
                out.append("")
            out.append(section_divider(sec, games, cols, st))
            out.append("")
        if games:
            out.extend(layout(games, cols, st, args.me, show_default))
        else:
            out.append("  " + st.muted("no matching games" if sec["games"] else
                                       ("no data yet" if sec.get("error") else "no games in the data")))
    return out


def header_line(cols, st, brand, brand_rgb, snap, right="", right_short=""):
    sections = sections_of(snap)
    if len(sections) == 1:
        mh, halls = _counts(sections[0]["games"])
        stats = "%s hunters in %s halls" % (st.bold(str(mh)), st.bold(str(halls)))
        if sections[0].get("note"):
            stats += "  " + st.muted("(%s)" % sections[0]["note"])
        left = brand_line(brand, brand_rgb, st) + "  " + stats
    else:
        # several servers: their numbers and warnings are on their own dividers
        left = brand_line(brand, brand_rgb, st)
    for r in (right, right_short):
        gap = cols - vis_width(left) - vis_width(r)
        if r and gap >= 2:
            return left + " " * gap + r
    return left


def footer_line(st, show_default):
    keys = [("r", "refresh"), ("j/k", "scroll"),
            ("p", "PPSSPP as +N" if show_default else "PPSSPP names"), ("q", "quit")]
    return "  ".join(st.bold(k) + " " + st.muted(v) for k, v in keys)


def fitted_size(snap, st, args, show_default, brand, brand_rgb, width=None):
    """Terminal size for --resize: every game of a server side by side in
    RESIZE_COL_WIDTH wide boxes (and the header and dividers fit), and the height for all
    halls plus RESIZE_BUFFER_HALLS spare lines. With `width` given, only the
    height for that width is worked out."""
    if width is not None:
        body = render_body(snap, width, st, args, show_default)
        return width, 2 + len(body) + 1 + RESIZE_BUFFER_HALLS
    ncols = max([len(filter_games(sec["games"], args.only, args.hide_empty))
                 for sec in sections_of(snap)] + [1])
    ncols = min(ncols, 3)
    width = ncols * RESIZE_COL_WIDTH + (ncols - 1)
    header = header_line(0, st, brand, brand_rgb, snap)
    width = max(width, vis_width(header) + 2 + len("↻ 120s"))   # room for the short status
    width = max(width, vis_width(footer_line(st, True)), vis_width(footer_line(st, False)))
    sections = sections_of(snap)
    if len(sections) > 1:
        for sec in sections:
            games = filter_games(sec["games"], args.only, args.hide_empty)
            left, _ = _divider_variants(sec, games, st, with_error=False)[0]
            width = max(width, vis_width(left) + 1 + 6)   # + a short rule, room to grow
    body = render_body(snap, width, st, args, show_default)
    height = 2 + len(body) + 1 + RESIZE_BUFFER_HALLS     # header, gap, body, footer
    return width, height


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------

def run_once(app, args, st):
    snap = app["fetch"](args.file)
    cols = shutil.get_terminal_size((120, 40)).columns
    print(header_line(cols, st, app["brand"], app["brand_rgb"], snap,
                      st.muted(time.strftime("%H:%M:%S"))))
    print()
    for line in render_body(snap, cols, st, args, args.show_ppsspp):
        print(line)


def run_live(app, args, st):
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    out = sys.stdout
    brand, brand_rgb = app["brand"], app["brand_rgb"]

    state = {"snap": None, "error": None, "loading": False, "updated": None,
             "next": 0.0, "scroll": 0, "dirty": True, "show_default": args.show_ppsspp}
    lock = threading.Lock()

    def worker():
        try:
            snap, err = app["fetch"](args.file), None
        except Exception as exc:
            snap, err = None, (str(exc) or exc.__class__.__name__)
        with lock:
            state["loading"] = False
            interval = args.interval
            if me_in_hall(snap if snap is not None else state["snap"], args.me):
                interval = min(interval, IN_HALL_INTERVAL)
            state["next"] = time.time() + interval
            state["error"] = err
            if snap is not None:
                state["snap"] = snap
                state["updated"] = time.strftime("%H:%M:%S")
            state["dirty"] = True

    def refresh():
        with lock:
            if state["loading"]:
                return
            state["loading"] = True
            state["dirty"] = True
        threading.Thread(target=worker, daemon=True).start()

    fit = {"done": False, "last": None, "pending": False, "winch": 0.0}

    def on_resize(*_):
        fit["pending"] = False          # the window has taken our (or your) new size
        fit["winch"] = time.time()
        state["dirty"] = True

    def fit_window(snap, show_default):
        """--resize-once: size the window after the first data; --resize: the
        same, then only the height again whenever the data or the PPSSPP
        toggle changes it (the width stays as it is, also if you changed it)."""
        if not (args.resize or args.resize_once) or (args.resize_once and fit["done"]):
            return
        if fit["pending"]:              # first resize not applied yet, don't undo its width
            return
        if time.time() - fit["winch"] < RESIZE_SETTLE:   # you're still dragging the window
            return
        if fit["done"]:
            cols = shutil.get_terminal_size((120, 40)).columns
            size = fitted_size(snap, st, args, show_default, brand, brand_rgb, width=cols)
        else:
            size = fitted_size(snap, st, args, show_default, brand, brand_rgb)
        if size != fit["last"]:
            current = tuple(shutil.get_terminal_size((120, 40)))
            fit["pending"] = not fit["done"] and size != current
            fit["last"], fit["done"] = size, True
            out.write("\033[8;%d;%dt" % (size[1], size[0]))

    def draw():
        cols, rows = shutil.get_terminal_size((120, 40))
        with lock:
            snap, err, loading = state["snap"], state["error"], state["loading"]
            updated, nxt, show_default = state["updated"], state["next"], state["show_default"]

        left_s = max(0, int(nxt - time.time()))
        if loading:
            right, right_short = st.fg(brand_rgb, "⟳ refreshing"), st.fg(brand_rgb, "⟳")
        elif err:
            right, right_short = st.fg(ERROR_RGB, "⚠ " + truncate(err, 40)), st.fg(ERROR_RGB, "⚠")
        elif updated:
            right = st.muted("updated %s · next in %2ds" % (updated, left_s))
            right_short = st.muted("↻ %2ds" % left_s)
        else:
            right = right_short = ""

        if snap is None:
            head = brand_line(brand, brand_rgb, st)
            body = ["", "  " + (st.fg(ERROR_RGB, "⚠ " + err) if err and not loading
                                else st.muted("fetching %s …" % app.get("source", "status")))]
        else:
            head = header_line(cols, st, brand, brand_rgb, snap, right, right_short)
            body = render_body(snap, cols, st, args, show_default)
            fit_window(snap, show_default)

        view_h = max(1, rows - 3)
        max_scroll = max(0, len(body) - view_h)
        state["scroll"] = min(max(0, state["scroll"]), max_scroll)
        visible = body[state["scroll"]:state["scroll"] + view_h]

        footer = footer_line(st, state["show_default"])
        if max_scroll:
            pos = "%d–%d of %d ▾" % (state["scroll"] + 1, state["scroll"] + len(visible), len(body))
            footer = pad(footer, cols - len(pos) - 1) + st.muted(pos)

        frame = [head, ""] + visible + [""] * (view_h - len(visible)) + [footer]
        frame = [clip(l, cols) for l in frame[:rows]]
        # Each line goes to its own row. Clear the rest of a row only when the
        # line is shorter: after a full-width line the cursor sits on the last
        # column and \033[K would erase that last character.
        buf = ["\033[%d;1H%s%s" % (i + 1, l, "\033[K" if vis_width(l) < cols else "")
               for i, l in enumerate(frame)]
        if len(frame) < rows:
            buf.append("\033[%d;1H\033[J" % (len(frame) + 1))
        out.write("".join(buf))
        out.flush()

    def handle_key(seq):
        _, rows = shutil.get_terminal_size((120, 40))
        page = max(1, rows - 4)
        moves = {"j": 1, "\x1b[B": 1, "k": -1, "\x1b[A": -1, " ": page, "\x1b[6~": page,
                 "b": -page, "\x1b[5~": -page, "g": -10 ** 6, "G": 10 ** 6}
        if seq in ("q", "Q", "\x03", "\x1b"):
            return False
        if seq in ("r", "R"):
            refresh()
        elif seq in ("p", "P"):
            state["show_default"] = not state["show_default"]
        elif seq in moves:
            state["scroll"] += moves[seq]
        state["dirty"] = True
        return True

    signal.signal(signal.SIGWINCH, on_resize)
    out.write("\033[?1049h\033[?25l")          # alternate screen, hide cursor
    try:
        tty.setcbreak(fd)
        refresh()
        last_tick = 0
        running = True
        while running:
            now = int(time.time())
            if now != last_tick:                # countdown ticks once per second
                last_tick = now
                state["dirty"] = True
                if not state["loading"] and time.time() >= state["next"]:
                    refresh()
            if state["dirty"]:
                state["dirty"] = False
                draw()
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except InterruptedError:            # SIGWINCH
                continue
            if ready:
                data = os.read(fd, 64).decode("utf-8", "ignore")
                for seq in re.findall(r"\x1b\[[0-9]*~|\x1b\[[A-D]|\x1b|.", data, re.S):
                    if not handle_key(seq):
                        running = False
                        break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        out.write("\033[?25h\033[?1049l")
        out.flush()


def main(fetch, brand, brand_rgb, description, default_interval=30, source=None, argv=None):
    """Entry point for a data source script.

    fetch(file_or_None) -> snapshot dict (see module docstring). The optional
    file argument is a local copy of the raw data, used with --file for testing.
    """
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--me", help="your nickname, highlighted wherever it shows up")
    ap.add_argument("-i", "--interval", type=int, default=default_interval,
                    help="refresh interval in seconds (default %d, min 5)" % default_interval)
    ap.add_argument("--once", action="store_true", help="print once and exit (also when piped)")
    ap.add_argument("--show-ppsspp", action="store_true",
                    help="list default 'PPSSPP' nicknames instead of a '+N' count")
    resize = ap.add_mutually_exclusive_group()
    resize.add_argument("--resize", action="store_true",
                        help="size the terminal window to fit all halls (+%d spare) at start, "
                        "then keep adjusting only its height"
                        % RESIZE_BUFFER_HALLS)
    resize.add_argument("--resize-once", action="store_true",
                        help="size the terminal window to fit all halls (+%d spare) once at start"
                        % RESIZE_BUFFER_HALLS)
    ap.add_argument("--only", metavar="GAMES",
                    help="comma separated games to show: fu, p3, f1 (default all)")
    ap.add_argument("--hide-empty", action="store_true",
                    help="hide games without any open hall")
    ap.add_argument("--no-color", action="store_true", help="disable colours (or set NO_COLOR)")
    ap.add_argument("--file", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.interval = max(5, args.interval)
    args.me = (args.me or "").lower() or None
    args.only = {k.strip().lower() for k in args.only.split(",") if k.strip()} if args.only else None

    interactive = sys.stdout.isatty() and sys.stdin.isatty()
    color = not args.no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()
    truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") \
        or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "vscode", "ghostty")
    st = Style(color, truecolor)
    app = {"fetch": fetch, "brand": brand, "brand_rgb": brand_rgb, "source": source}

    try:
        if args.once or not interactive:
            run_once(app, args, st)
        else:
            run_live(app, args, st)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        sys.stderr.write("%s: %s\n" % (os.path.basename(sys.argv[0]), exc))
        sys.exit(1)
