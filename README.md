# mh_status

Who is in which Monster Hunter gathering hall on the PSP adhoc servers
[SOCOM](https://www.socom.cc) and [HunsterVerse](https://hunstermonter.net):
Portable 3rd, Freedom Unite and Freedom 1. Python standard library only.

## Terminal

```sh
python3 mh_status_all.py --me XYZ --only fu,p3 --resize   # the usual
python3 mh_status_all.py --me Exceen           # both servers
python3 socom_status_mh.py --me Exceen         # one server
python3 hunsterverse_status_mh.py --once       # print once and exit
python3 mh_status_all.py --only fu,p3 --resize # fit the window to the halls
```

Keys: `r` refresh · `j`/`k` scroll · `p` PPSSPP names / `+N` · `q` quit.
`--help` lists all options.

Hall pips show how good a hall is to join, counting you in if `--me` is in it:
green = you'd complete a party of 4, amber = one short, red = full.

## Web page

`mh_status_web.py` serves the same view as a page for the phone's home screen:

```sh
python3 mh_status_web.py                       # http://localhost:8080
```

| Route          | What                                                              |
|----------------|-------------------------------------------------------------------|
| `/`            | the page; nickname and games are set in ⚙ and kept in the browser |
| `/status.json` | the data, `?me=NAME&only=fu,p3&hide_empty=1`                      |
| `/lite`        | plain HTML without JavaScript (e.g. the PSP browser), same params |
| `/healthz`     | `ok`                                                              |

The servers are only asked while somebody has the page open: a request
fetches both if the last fetch is older than 25 s (`CACHE_TTL`), every other
request in that time gets the same result. The page reloads every 60 s, every
30 s while you're in a hall, and right away when you come back to it.

On the iPhone: open the page in Safari → Share → *Add to Home Screen*.

### Docker

```sh
docker compose up -d --build                   # port 42426 -> 8080
```

Update after a `git pull`: the same command again.
