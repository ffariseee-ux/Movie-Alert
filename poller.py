#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls BookMyShow / District pages (or an internal API request you grabbed from
DevTools) and sends a Telegram message when:

  1. shows are NEWLY ADDED at the theatre for a movie/date (booking opens)
  2. more shows are added to a date that's already open     (optional: session_regex)

Multi-movie / multi-date: list movies under "movies" and set date_from/date_to
in config.json. Each (movie, date) is tracked separately in state.json.
A plain single-movie config (target_url / url_template + requested_date) still works.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

# Look like a real Chrome on Windows -- BMS rejects obvious bots.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def expand_dates(cfg):
    """dates list, or date_from..date_to (YYYYMMDD, inclusive), or requested_date."""
    if cfg.get("dates"):
        return [str(d) for d in cfg["dates"]]
    if cfg.get("date_from") and cfg.get("date_to"):
        d = datetime.strptime(str(cfg["date_from"]), "%Y%m%d")
        end = datetime.strptime(str(cfg["date_to"]), "%Y%m%d")
        out = []
        while d <= end:
            out.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return out
    return [str(cfg["requested_date"])] if cfg.get("requested_date") else []


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    # Environment variables override the file (used by GitHub Actions secrets).
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # REQUESTED_DATE=20261217 points everything at that single date (live test).
    if os.environ.get("REQUESTED_DATE"):
        cfg["dates"] = [os.environ["REQUESTED_DATE"]]
        cfg.pop("date_from", None)
        cfg.pop("date_to", None)

    for k in ("telegram_bot_token", "telegram_chat_id"):
        if not cfg.get(k):
            sys.exit(f"Missing required config: {k}")
    return cfg


def validate_target(t):
    required = ["target_url"]
    detector = t.get("detector")
    if detector in ("bms_date", "venue_date"):
        required.append("requested_date")
    else:
        required.append("theatre")
    if detector == "venue_date" and not (t.get("venue_code") or t.get("venue_codes")):
        sys.exit("venue_date detector needs 'venue_code' or 'venue_codes'")
    missing = [k for k in required if not t.get(k)]
    if missing:
        sys.exit(f"[{t.get('key')}] missing required config: {', '.join(missing)}")


def build_targets(cfg):
    """Turn config into a list of (movie, date) targets, each a flat config dict."""
    movies = cfg.get("movies")
    dates = expand_dates(cfg)
    targets = []

    if movies:
        if not dates:
            sys.exit("Multi-movie config needs dates, or date_from + date_to")
        for m in movies:
            for d in dates:
                t = {k: v for k, v in cfg.items() if k not in ("movies", "dates")}
                t.update({k: v for k, v in m.items() if k != "name"})
                t["movie"] = m["name"]
                t["requested_date"] = d
                tpl = t.get("url_template")
                if not tpl:
                    sys.exit(f"Movie '{m['name']}' needs a url_template containing {{date}}")
                t["target_url"] = tpl.format(date=d)
                t["key"] = f"{m['name']}|{d}"
                targets.append(t)
    else:
        t = dict(cfg)
        d = dates[0] if dates else None
        if d:
            t["requested_date"] = d
        if t.get("url_template") and t.get("requested_date"):
            t["target_url"] = t["url_template"].format(date=t["requested_date"])
        t["key"] = f"{t.get('movie', 'movie')}|{t.get('requested_date') or t.get('theatre')}"
        targets.append(t)

    for t in targets:
        validate_target(t)
    return targets


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    resp.raise_for_status()


def notify(t, text):
    send_telegram(t["telegram_bot_token"], t["telegram_chat_id"], text)


def fetch_url(url, cfg):
    """
    Fetch a URL from an India egress when configured.

    BookMyShow blocks non-India / datacenter IPs (e.g. GitHub's US runners),
    so a plain request from CI gets a 403. Two ways to route through India:

    * SCRAPERAPI_KEY  -- routes via ScraperAPI with country_code=in and solves
                         anti-bot. Easiest for CI. Set it as a repo secret.
    * PROXY_URL       -- a standard http(s) proxy string, e.g.
                         "http://user:pass@in-proxy-host:port".

    With neither set, it makes a direct request with browser headers plus a
    cookie warm-up -- enough only when running from an India IP.
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": url}
        )
        resp = requests.get(api_url, timeout=90)
        resp.raise_for_status()
        return resp.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    # Warm-up: hit the homepage first to pick up cookies (helps soft bot checks).
    try:
        session.get("https://in.bookmyshow.com/", timeout=30, proxies=proxies)
    except requests.RequestException:
        pass

    resp = session.get(
        url,
        timeout=30,
        proxies=proxies,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-hyderabad"},
    )
    resp.raise_for_status()
    return resp.text


def is_available_bms_date(page_text, cfg):
    """
    BookMyShow-specific detector for "a given date has opened for booking".

    BMS only renders showtimes for the date currently being displayed, and it
    silently falls back to the nearest available date when you request a date
    that hasn't opened yet. So the requested date sits at a low ~3 count (just
    the date-strip navigation) until it opens, at which point its showtimes
    render and it becomes the *dominant* date token.

    Rule: open when the requested date is the most-referenced date token on
    the page and it clears a small floor (well above strip-only noise).
    """
    requested = cfg["requested_date"]
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    requested_count = counts.get(requested, 0)

    return top_date == requested and requested_count >= floor


def is_available_venue_date(page_text, cfg):
    """
    Theatre-specific detector: is a given venue bookable on a given date?

    BMS renders a per-venue booking link like
        /cinemas/chennai/<slug>/buytickets/<venueCode>/<date>
    only when that venue has live shows for that exact date. Because the date
    is baked into the link, it can't be confused with the silent fallback.

    Set venue_code (one) or venue_codes (list). With a list, it's open when
    ANY of them is bookable for the date.
    """
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    return any("/{}/{}".format(code, date) in page_text for code in codes)


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    return is_available_generic(page_text, cfg)


def is_available_generic(page_text, cfg):
    """
    Booking is considered OPEN for the target theatre when the theatre name
    is present AND at least one 'booking is live' signal is present.
    """
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    movie = cfg.get("movie")
    if movie:
        if re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
            return False

    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(s.lower() in haystack for s in open_signals)
    only_closed = any(s.lower() in haystack for s in closed_signals) and not has_open

    return has_open and not only_closed


def pretty_date(d):
    return f"{d[6:8]}-{d[4:6]}-{d[0:4]}"


def open_message(t):
    if t.get("detector") in ("bms_date", "venue_date"):
        venue = t.get("venue_label") or t.get("venue_code") or ""
        venue_line = f"Theatre: {venue}\n" if venue else ""
        return (
            f"🎬 New shows added!\n\n"
            f"{t.get('movie', 'Movie')}\n"
            f"{venue_line}"
            f"Date: {pretty_date(t['requested_date'])}\n\n"
            f"Book here: {t['target_url']}"
        )
    return (
        f"🎬 Booking is OPEN!\n\n"
        f"{t.get('movie', 'Movie')}\n"
        f"Theatre: {t['theatre']}\n\n"
        f"Book here: {t['target_url']}"
    )


def find_sessions(page, t):
    """Show ids found by the optional session_regex (group 1 if present)."""
    rx = t.get("session_regex")
    if not rx:
        return None
    return sorted({m.group(1) if m.groups() else m.group(0) for m in re.finditer(rx, page)})


def check_shows(t, page, ts):
    """Alert on newly added shows. Returns (sessions, changed)."""
    sessions = find_sessions(page, t)
    if sessions is None:
        return None, False
    known = ts.get("sessions")
    changed = False
    if known is not None:
        new = [s for s in sessions if s not in known]
        if new:
            notify(
                t,
                f"🆕 {len(new)} new show(s) added\n\n"
                f"{t['movie']} @ {t.get('venue_label', '')}\n"
                f"Date: {pretty_date(t['requested_date'])}\n\n"
                f"{t['target_url']}",
            )
    if sessions != known:
        ts["sessions"] = sessions
        changed = True
    return sessions, changed


def process_target(t, ts):
    """Check one (movie, date). Mutates ts. Returns True if ts changed."""
    label = f"{t.get('movie', 'movie')} {t.get('requested_date', '')} @ {t.get('theatre') or t.get('venue_label') or ''}".strip()
    try:
        page = fetch_url(t["target_url"], t)
    except requests.RequestException as exc:
        # Transient network/blocking errors shouldn't crash the workflow.
        print(f"[{label}] fetch failed: {exc}")
        return False

    available = is_available(page, t)
    was = ts.get("available", False)
    print(f"[{label}] available={available} (was {was})")
    changed = False

    if available and not was:
        notify(t, open_message(t))
        print(f"[{label}] notification sent")

    if available:
        _, c1 = check_shows(t, page, ts)
        changed |= c1

    if available != was:
        ts["available"] = available
        ts["checked_at"] = int(time.time())
        changed = True
    return changed


def load_state(targets):
    raw = load_json(STATE_PATH, default={}) or {}
    if "targets" in raw:
        return raw
    # Migrate the old flat {"available": bool} state onto the first target.
    state = {"targets": {}}
    if "available" in raw and targets:
        state["targets"][targets[0]["key"]] = {
            "available": raw["available"],
            "checked_at": raw.get("checked_at"),
        }
    return state


def main():
    cfg = load_config()
    targets = build_targets(cfg)
    state = load_state(targets)
    changed = False

    for i, t in enumerate(targets):
        if i:
            time.sleep(cfg.get("delay_seconds", 2))
        ts = state["targets"].setdefault(t["key"], {})
        if process_target(t, ts):
            changed = True

    if changed or not STATE_PATH.exists():
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
