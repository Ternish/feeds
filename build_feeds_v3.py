#!/usr/bin/env python3
"""
Chart / playlist  ->  Soundiiz JSON feed builder.

Reads a public source (JioSaavn playlist, or Shazam chart), re-inserts your own
tracks at fixed positions, and writes Soundiiz-compatible JSON.

Soundiiz then syncs from each JSON URL on a schedule, with Erase destination ON,
so the destination playlist is always clean, current, and always contains your
tracks at the positions you chose.

Usage:
    python build_feeds_v2.py
    python build_feeds_v2.py --dry-run     # print to screen, write nothing
Output:
    ./docs/<slug>.json
"""

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

OUT_DIR = "docs"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# YOUR TRACKS
# Fill in the `isrc` values from your distributor dashboard. With an ISRC,
# Soundiiz matches your track exactly on every platform instead of guessing
# from the title text - which is where these tools usually fail on Punjabi
# catalogues. This is the single highest-value 10 minutes in the whole setup.
# ---------------------------------------------------------------------------
MY_TRACKS = {
    "naare_ni":    {"title": "Naare Ni",    "artists": "Amrit Basra", "isrc": ""},
    "killa_mode":  {"title": "Killa Mode",  "artists": "Amrit Basra", "isrc": ""},
    "bro_code":    {"title": "Bro Code",    "artists": "Amrit Basra", "isrc": ""},
    "tell_me_why": {"title": "Tell Me Why", "artists": "Amrit Basra", "isrc": ""},
    "dirty_money": {"title": "Dirty Money", "artists": "Amrit Basra", "isrc": ""},
    "street_flow": {"title": "Street Flow", "artists": "Amrit Basra", "isrc": ""},
}

# ---------------------------------------------------------------------------
# FEEDS - one entry per playlist you want to exist across all platforms.
#
#   slug   : output filename -> becomes part of the public JSON URL
#   title  : playlist name shown on every streaming platform
#   desc   : playlist description
#   source : ("jiosaavn", playlist_url) | ("shazam_top", "IN") | ("shazam_viral", "IN")
#   limit  : max chart/source tracks to keep (your tracks are extra, on top)
#   inject : {position: my_track_key}   1-indexed final position
# ---------------------------------------------------------------------------
FEEDS = [
    {
        "slug": "punjabi-duets",
        "title": "Punjabi Duets 2026 - Best Punjabi Duet Songs",
        "desc": "The best Punjabi duets, refreshed weekly.",
        "source": ("jiosaavn",
                   "https://www.jiosaavn.com/featured/punjabi-duets/BRQ1j4fezTQ_"),
        "limit": 35,
        "inject": {4: "naare_ni", 8: "killa_mode"},
    },
    {
        "slug": "india-discovery",
        "title": "India Discovery 2026 - Rising Songs Before They Blow Up",
        "desc": "Tracks breaking out in India right now. Updated weekly.",
        "source": ("shazam_discovery", "india"),
        "limit": 30,
        "inject": {4: "naare_ni", 8: "bro_code"},
    },
    {
        "slug": "india-top",
        "title": "India Top Hits 2026 - Trending Now",
        "desc": "The biggest tracks in India right now. Updated weekly.",
        "source": ("shazam_top", "india"),
        "limit": 40,
        "inject": {4: "naare_ni", 8: "killa_mode"},
    },
]

# Shazam CSV endpoints use COUNTRY NAME SLUGS, not two-letter codes:
#   .../csv/top-200/india          not  .../csv/top-200/IN
#   .../csv/top-200/united-kingdom      .../csv/top-200/united-states
#   .../csv/top-200/world               (global)
# "discovery" is the rising-artists chart - usually a better fit for an
# independent artist than top-200, since the tracks are smaller and the
# playlist reads as genuine discovery rather than a chart rip.
SHAZAM_URLS = {
    "shazam_top":       "https://www.shazam.com/services/charts/csv/top-200/{slug}",
    "shazam_discovery": "https://www.shazam.com/services/charts/csv/discovery/{slug}",
}

# JioSaavn's own web API. `token` is the final segment of the playlist URL.
JIOSAAVN_API = (
    "https://www.jiosaavn.com/api.php"
    "?__call=webapi.get&token={token}&type=playlist&p=1&n={n}"
    "&includeMetaTags=0&ctx=web6dot0&api_version=4&_format=json&_marker=0"
)


BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.shazam.com/charts",
    "Connection": "keep-alive",
}


def fetch(url, as_bytes=False, headers=None):
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    return raw if as_bytes else raw.decode("utf-8-sig", errors="replace")


def clean(s):
    """JioSaavn returns HTML-escaped text, sometimes double-escaped."""
    if not s:
        return ""
    out = html.unescape(html.unescape(str(s)))
    return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------------------
# Source: JioSaavn
# ---------------------------------------------------------------------------

def jiosaavn_token(playlist_url):
    path = urllib.parse.urlparse(playlist_url).path.rstrip("/")
    token = path.split("/")[-1]
    if not token:
        raise ValueError(f"could not read playlist token from {playlist_url}")
    return token


def jiosaavn_via_api(playlist_url, limit):
    token = jiosaavn_token(playlist_url)
    data = json.loads(fetch(JIOSAAVN_API.format(token=token, n=max(limit, 100))))

    songs = data.get("list") or data.get("songs") or []
    if not songs:
        raise ValueError("API returned no songs")

    tracks = []
    for s in songs:
        title = clean(s.get("title") or s.get("song"))
        more = s.get("more_info") or {}
        artists = ""
        amap = more.get("artistMap") or {}
        primary = amap.get("primary_artists") or []
        if isinstance(primary, list) and primary:
            artists = ", ".join(clean(a.get("name")) for a in primary if a.get("name"))
        if not artists:
            artists = clean(more.get("music") or s.get("subtitle") or "")
        # subtitle often looks like "Artist A, Artist B - Album" -> keep the left side
        if " - " in artists:
            artists = artists.split(" - ")[0].strip()
        if title:
            tracks.append({"title": title, "artists": artists})
    return tracks


def jiosaavn_via_html(playlist_url, limit):
    """Fallback: the playlist page server-renders every track."""
    page = fetch(playlist_url)
    # Each song row links to /song/<slug>/<id> and its anchor text is the title.
    pattern = re.compile(
        r'href="(?:https://www\.jiosaavn\.com)?/song/[^"]+"[^>]*>([^<]{1,200})</a>')
    seen, tracks = set(), []
    for m in pattern.finditer(page):
        title = clean(m.group(1))
        if not title or title.startswith("/song/"):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        tracks.append({"title": title, "artists": ""})
    return tracks


def load_jiosaavn(playlist_url, limit):
    try:
        tracks = jiosaavn_via_api(playlist_url, limit)
        if tracks:
            return tracks
        raise ValueError("empty API result")
    except Exception as e:
        print(f"      api failed ({e}); falling back to HTML", file=sys.stderr)
        return jiosaavn_via_html(playlist_url, limit)


# ---------------------------------------------------------------------------
# Source: Shazam
# ---------------------------------------------------------------------------

def load_shazam(kind, slug):
    # If a CSV with this name sits next to the script, use it instead of the
    # website. Lets you download the file by hand if Shazam blocks us.
    local = f"shazam-{kind.replace('shazam_', '')}-{slug}.csv"
    if os.path.exists(local):
        print(f"      using local file {local}")
        with open(local, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    else:
        page = "discovery" if "discovery" in kind else "top-200"
        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = f"https://www.shazam.com/charts/{page}/{slug}"
        text = fetch(SHAZAM_URLS[kind].format(slug=slug), headers=headers)
    rows = list(csv.reader(io.StringIO(text)))
    tracks, started = [], False
    for row in rows:
        cells = [c.strip() for c in row] if row else []
        if not started:
            if len(cells) >= 3 and cells[0].lower() == "rank":
                started = True
            continue
        if len(cells) < 3:
            continue
        artist, title = cells[1], cells[2]
        if artist and title:
            tracks.append({"title": title, "artists": artist})
    if not tracks:
        raise ValueError("no tracks parsed - Shazam CSV format may have changed")
    return tracks


def load_source(feed):
    kind = feed["source"][0]
    if kind == "jiosaavn":
        return load_jiosaavn(feed["source"][1], feed["limit"])
    if kind in SHAZAM_URLS:
        return load_shazam(kind, feed["source"][1])
    raise ValueError(f"unknown source type: {kind}")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_tracklist(source_tracks, limit, inject):
    pool = source_tracks[:limit]
    out, i, pos = [], 0, 1
    total = len(pool) + len(inject)
    while pos <= total:
        if pos in inject:
            t = dict(MY_TRACKS[inject[pos]])
            if not t.get("isrc"):
                t.pop("isrc", None)
            out.append(t)
        elif i < len(pool):
            out.append(pool[i])
            i += 1
        pos += 1
    return out


def build(feed):
    source = load_source(feed)
    return {
        "title": feed["title"],
        "description": feed["desc"],
        "tracklist": build_tracklist(source, feed["limit"], feed["inject"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the result instead of writing files")
    args = ap.parse_args()

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)

    failures = 0
    for feed in FEEDS:
        print(f"-> {feed['slug']}")
        try:
            payload = build(feed)
        except Exception as e:
            # Leave the previous good file alone. A stale feed is survivable;
            # a broken one would let Soundiiz wipe the destination playlist.
            print(f"   FAIL: {e}", file=sys.stderr)
            failures += 1
            continue

        if args.dry_run:
            for n, t in enumerate(payload["tracklist"], 1):
                mine = " <-- YOURS" if t.get("artists") == "Amrit Basra" else ""
                print(f"   {n:>3}. {t['title']} — {t.get('artists','')}{mine}")
        else:
            path = os.path.join(OUT_DIR, feed["slug"] + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"   wrote {path} ({len(payload['tracklist'])} tracks)")

    if failures:
        print(f"\n{failures} feed(s) failed - previous versions kept.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
