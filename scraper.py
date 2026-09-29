#!/usr/bin/env python3
"""Daily scraper for the Germany streaming tracker.

Reads fernsehserien.de's streaming guide -- the "Vorschau" (upcoming release
dates) and "Neu verfügbar" (newly available) lists -- keeps only series whose
origin is USA/UK/Canada/Australia (or unlisted) and which started in 2016 or
later, resolves each series' IMDb link via TMDb (cached), and writes it all to
Supabase (the same project as the Poland tracker, in its own de_* tables).

One row per (series, streaming service, season label). Each run records when
that release was last seen as upcoming, its announced start date (and any
change to it), and -- once it shows up under "Neu verfügbar" -- the date it
actually became available. The dashboard derives launched / upcoming /
dropped from those fields.
"""
import os
import re
import sys
import json
import html as htmllib
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

DRY_RUN = os.environ.get("DRY_RUN") == "1"
if not DRY_RUN:
    SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
    SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
    TMDB_API_KEY = os.environ["TMDB_API_KEY"]

BASE = "https://www.fernsehserien.de"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
MAX_PAGES = 40  # safety cap; the lists currently run to ~11 (Vorschau) / ~15 (Neu) pages
MIN_START_YEAR = 2016

# The site uses German vehicle-registration-style country codes.
QUALIFYING_CODES = {"USA", "GB", "CDN", "AUS"}
CODE_TO_ISO = {
    "USA": "US", "GB": "GB", "CDN": "CA", "AUS": "AU", "D": "DE", "F": "FR",
    "J": "JP", "A": "AT", "S": "SE", "ROK": "KR", "I": "IT", "NZ": "NZ",
    "NL": "NL", "IRL": "IE", "E": "ES", "DK": "DK", "B": "BE", "CZ": "CZ",
    "MEX": "MX", "IND": "IN", "CH": "CH", "PL": "PL", "N": "NO", "IS": "IS",
    "FIN": "FI", "CO": "CO", "HK": "HK", "RC": "TW", "H": "HU", "L": "LU",
}
ISO_TO_CODE = {v: k for k, v in CODE_TO_ISO.items()}

WEEKDAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
            "freitag": 4, "samstag": 5, "sonntag": 6}

ITEM_START_RE = re.compile(r'<li(?: id="[^"]*")?><a [^>]*data-event-category=liste-[^ >]*-logo')
SEIT_HEADER_RE = re.compile(r"Verfügbar seit (\d+) Tag")
PILL_ORIGIN_RE = re.compile(r"^(?:([A-Z]+(?:/[A-Z]+)*)\s+)?(\d{4})(?:\s*–\s*(\d{4})?)?$")
BLOCK_SPLIT_RE = re.compile(r'<ul class="streaming-sendungen-block ')
ROW_SPLIT_RE = re.compile(r'<li class="streaming-sendungen-block-block-weiss[ "]')


def text(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def berlin_today():
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def resolve_date_text(raw, today):
    """'ab morgen' / 'ab Donnerstag' / 'ab 14.10.' -> a real date, relative to
    the day the page was read. Returns None for wording we don't recognise
    (raw text is stored regardless, so nothing is lost)."""
    t = (raw or "").strip().lower()
    t = re.sub(r"^(ab|seit)\s+", "", t)
    if t == "heute":
        return today
    if t == "morgen":
        return today + timedelta(days=1)
    if t in WEEKDAYS:
        delta = (WEEKDAYS[t] - today.weekday()) % 7 or 7
        return today + timedelta(days=delta)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            return date(y, mo, d)
        # No year given: it's the next occurrence (a date well in the past
        # can only mean next year -- the list runs ~9 months ahead).
        cand = date(today.year, mo, d)
        if cand < today - timedelta(days=30):
            cand = date(today.year + 1, mo, d)
        return cand
    return None


def parse_page(page_html, list_kind, today):
    """Return one dict per (series, provider, season-label) row on the page.
    list_kind: 'vorschau' or 'neu'."""
    starts = [m.start() for m in ITEM_START_RE.finditer(page_html)]
    headers = [(m.start(), int(m.group(1))) for m in SEIT_HEADER_RE.finditer(page_html)]
    rows = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        chunk = page_html[start:end]

        # On "Neu verfügbar" items sit under "Verfügbar seit N Tagen" headings
        # (none above the first group = available today). A heading can fall
        # between two items, so look it up by absolute position.
        days_ago = None
        if list_kind == "neu":
            prior = [n for pos, n in headers if pos < start]
            days_ago = prior[-1] if prior else PAGE_CARRY.get("days_ago", 0)

        slug_m = re.search(r'href="/([^"/]+)"', chunk)
        title_m = re.search(r"streaming-sendungen-titel>([^<]*)", chunk)
        if not slug_m or not title_m:
            continue
        orig_m = re.search(r"streaming-sendungen-originaltitel>\(([^<]*)\)", chunk)

        countries, start_year, end_year, genres = None, None, None, []
        pills_m = re.search(r"<ul class=genrepillen>(.*?)</ul>", chunk, re.S)
        if pills_m:
            pills = [text(p) for p in pills_m.group(1).split("<li>") if text(p)]
            if pills:
                om = PILL_ORIGIN_RE.match(pills[0])
                if om:
                    countries = om.group(1)
                    start_year = int(om.group(2))
                    end_year = int(om.group(3)) if om.group(3) else None
                    pills = pills[1:]
            genres = pills

        for block in BLOCK_SPLIT_RE.split(chunk)[1:]:
            prov_m = re.search(r'href="/streaming/([^"]+)"', block)
            name_m = re.search(r'data-alt="([^"]+)"', block)
            if not prov_m:
                continue
            # Third-party add-on channels (sold inside Prime Video or MagentaTV)
            # carry a caption naming the host platform.
            cap_m = re.search(r"<figcaption>([^<]+)</figcaption>", block.split("block-rest")[0])
            channel_via = text(cap_m.group(1)) if cap_m else None
            for seg in ROW_SPLIT_RE.split(block)[1:]:
                label_m = re.search(r"block-weiss-staffel>([^<]*)", seg)
                date_m = re.search(r'block-weiss-text">([^<]*)', seg)
                # Miniseries / one-off specials have no season label at all.
                label = text(label_m.group(1)) if label_m else "—"
                date_text = text(date_m.group(1)) if date_m else None
                row = {
                    "series_slug": slug_m.group(1),
                    "title": text(title_m.group(1)),
                    "original_title": text(orig_m.group(1)) if orig_m else None,
                    "countries": countries,
                    "start_year": start_year,
                    "end_year": end_year,
                    "genres": ", ".join(genres) or None,
                    "provider_slug": prov_m.group(1),
                    "provider_name": htmllib.unescape(name_m.group(1)) if name_m else prov_m.group(1),
                    "channel_via": channel_via,
                    "season_label": label,
                    "is_premiere": "block-weiss-premiere" in seg,
                    "date_text": date_text,
                }
                if list_kind == "vorschau":
                    row["expected_date"] = resolve_date_text(date_text, today)
                else:
                    row["available_date"] = today - timedelta(days=days_ago or 0)
                rows.append(row)

    if list_kind == "neu":
        # The current "seit N Tagen" group can continue onto the next page.
        if headers:
            PAGE_CARRY["days_ago"] = headers[-1][1]
    return rows


PAGE_CARRY = {}


def passes_filter(row):
    if not row["start_year"] or row["start_year"] < MIN_START_YEAR:
        return False
    if row["countries"] and not (set(row["countries"].split("/")) & QUALIFYING_CODES):
        return False
    return True


def fetch_list(list_kind, today, local_dir=None):
    path = "streaming/vorschau" if list_kind == "vorschau" else "streaming/neu"
    PAGE_CARRY.clear()
    rows, pages = [], 0
    for p in range(1, MAX_PAGES + 1):
        if local_dir:
            fn = os.path.join(local_dir, f"{list_kind[0]}{p}.html")
            if not os.path.exists(fn):
                break
            page_html = open(fn, encoding="utf-8").read()
        else:
            url = f"{BASE}/{path}" + (f"/{p}" if p > 1 else "")
            resp = None
            for attempt in range(3):
                try:
                    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
                    break
                except requests.RequestException as e:
                    print(f"  {url}: {e} (attempt {attempt + 1})")
                    time.sleep(5)
            if resp is None:
                raise RuntimeError(f"could not fetch {url}")
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            page_html = resp.text
            time.sleep(1)  # be polite
        page_rows = parse_page(page_html, list_kind, today)
        if not page_rows:
            break
        rows.extend(page_rows)
        pages += 1
    # The same release can be listed twice (main block + "weitere" block).
    dedup = {}
    for r in rows:
        dedup.setdefault((r["series_slug"], r["provider_slug"], r["season_label"]), r)
    print(f"  {list_kind}: {pages} pages, {len(rows)} rows, {len(dedup)} unique")
    return list(dedup.values())


# ---------------------------------------------------------------------------
# Supabase REST helpers
# ---------------------------------------------------------------------------

def sb_headers(prefer=None):
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_get(table, params):
    out, offset = [], 0
    while True:
        p = dict(params, limit=1000, offset=offset)
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=sb_headers(), params=p, timeout=30)
        if not r.ok:
            print(f"  Supabase GET {table} failed: {r.status_code} {r.text}")
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < 1000:
            return out
        offset += 1000


def sb_upsert(table, rows, on_conflict):
    for i in range(0, len(rows), 500):
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=sb_headers(prefer="resolution=merge-duplicates,return=minimal"),
            data=json.dumps(rows[i:i + 500], default=str),
            timeout=60,
        )
        if not r.ok:
            print(f"  Supabase upsert into {table} failed: {r.status_code} {r.text}")
        r.raise_for_status()


def sb_insert(table, rows):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=sb_headers(prefer="return=minimal"),
        data=json.dumps(rows, default=str),
        timeout=60,
    )
    if not r.ok:
        print(f"  Supabase insert into {table} failed: {r.status_code} {r.text}")
    r.raise_for_status()


# ---------------------------------------------------------------------------
# TMDb resolution (cached per series in de_title_links)
# ---------------------------------------------------------------------------

def tmdb_get(path, params):
    r = requests.get(f"https://api.themoviedb.org/3/{path}",
                     params=dict(params, api_key=TMDB_API_KEY), timeout=20)
    return r.json() if r.ok else {}


def resolve_series(row, cache):
    """Match a series to TMDb by title + series start year (+ country when
    the page states one). Unlike the Poland TV guide, the year here is the
    series' own first-air year, so it's a precise key: only candidates within
    a year of it are accepted, and no match beats a wrong match."""
    slug = row["series_slug"]
    if slug in cache:
        return cache[slug]

    kinds = ["tv", "movie"] if row["season_label"] == "Filmreihe" else ["tv"]
    isos = {CODE_TO_ISO.get(c) for c in (row["countries"] or "").split("/")} - {None}
    queries = [q for q in (row["original_title"], row["title"]) if q]

    best = None
    for q in dict.fromkeys(queries):
        cands = []
        for kind in kinds:
            for r in tmdb_get(f"search/{kind}", {"query": q}).get("results", []):
                d = r.get("first_air_date") or r.get("release_date") or ""
                y = int(d[:4]) if d[:4].isdigit() else None
                if y is None or abs(y - row["start_year"]) > 1:
                    continue
                country_ok = not isos or bool(isos & set(r.get("origin_country") or []))
                cands.append((not country_ok, abs(y - row["start_year"]), r, kind))
        if cands:
            cands.sort(key=lambda t: (t[0], t[1]))
            best = cands[0]
            break

    link = {"series_slug": slug, "tmdb_id": None, "tmdb_type": None, "imdb_id": None,
            "imdb_url": None, "matched_original_name": None, "origin_iso": None}
    if best:
        _, _, r, kind = best
        details = tmdb_get(f"{kind}/{r['id']}", {"append_to_response": "external_ids"})
        imdb_id = (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")
        oc = r.get("origin_country") or [c["iso_3166_1"] for c in details.get("production_countries") or []]
        link.update({
            "tmdb_id": r["id"], "tmdb_type": kind, "imdb_id": imdb_id,
            "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
            "matched_original_name": r.get("original_name") or r.get("original_title"),
            "origin_iso": oc[0] if oc else None,
        })
    sb_upsert("de_title_links", [link], on_conflict="series_slug")
    cache[slug] = link
    return link


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else berlin_today()
    print(f"Run date (Berlin): {today.isoformat()}")

    if DRY_RUN:
        local = os.environ.get("LOCAL_PAGES")
        for kind in ("vorschau", "neu"):
            rows = fetch_list(kind, today, local)
            kept = [r for r in rows if passes_filter(r)]
            print(f"  {kind}: {len(kept)} kept after filter")
            for r in kept[:400]:
                print("   ", r["title"], "|", r["original_title"], "|", r["countries"], r["start_year"],
                      "|", r["provider_name"], f"(via {r['channel_via']})" if r["channel_via"] else "", "|", r["season_label"],
                      "| P" if r["is_premiere"] else "|", r["date_text"], "->",
                      r.get("expected_date") or r.get("available_date"))
            unresolved = sorted({r["date_text"] for r in rows if kind == "vorschau" and r["expected_date"] is None})
            print("  unresolved date texts:", unresolved)
        return 0

    if not os.environ.get("FORCE") and sb_get("de_scrape_log", {"run_date": f"eq.{today}", "select": "run_date"}):
        print("Already scraped today -- nothing to do.")
        return 0

    upcoming = fetch_list("vorschau", today)
    available = fetch_list("neu", today)
    if not upcoming:
        raise RuntimeError("Vorschau list came back empty -- page layout may have changed")

    up_kept = [r for r in upcoming if passes_filter(r)]
    av_kept = [r for r in available if passes_filter(r)]

    existing = {(r["series_slug"], r["provider_slug"], r["season_label"]): r
                for r in sb_get("de_releases", {"select": "*"})}
    cache = {r["series_slug"]: r for r in sb_get("de_title_links", {"select": "*"})}

    merged, date_changes = {}, []
    for r in up_kept + av_kept:
        key = (r["series_slug"], r["provider_slug"], r["season_label"])
        base = merged.get(key) or dict(existing.get(key) or {})
        is_up = "expected_date" in r
        out = dict(base)
        out.update({k: v for k, v in r.items() if k not in ("expected_date", "available_date", "date_text")})
        if is_up:
            old = base.get("expected_date")
            new = r["expected_date"].isoformat() if r["expected_date"] else None
            if old and new and old != new:
                date_changes.append({"series_slug": key[0], "provider_slug": key[1], "season_label": key[2],
                                     "old_date": old, "new_date": new, "seen_on": today.isoformat()})
            out["expected_date"] = new
            out["date_text"] = r["date_text"]
            out["first_seen_upcoming"] = base.get("first_seen_upcoming") or today.isoformat()
            out["last_seen_upcoming"] = today.isoformat()
        else:
            # Keep the earliest availability date we've ever recorded.
            new = r["available_date"].isoformat()
            out["available_date"] = min(filter(None, [base.get("available_date"), new]))
            out["last_seen_available"] = today.isoformat()
        out["first_seen"] = base.get("first_seen") or today.isoformat()
        merged[key] = out

    for out in merged.values():
        link = resolve_series(out, cache)
        out["imdb_url"] = link.get("imdb_url")
        out["matched_original_name"] = link.get("matched_original_name")
        if not out.get("countries") and link.get("origin_iso"):
            out["countries_tmdb"] = ISO_TO_CODE.get(link["origin_iso"], link["origin_iso"])

    cols = ["series_slug", "provider_slug", "season_label", "title", "original_title", "countries",
            "countries_tmdb", "start_year", "end_year", "genres", "provider_name", "channel_via",
            "is_premiere", "date_text", "expected_date", "available_date", "first_seen",
            "first_seen_upcoming", "last_seen_upcoming", "last_seen_available", "imdb_url",
            "matched_original_name"]
    rows = [{c: m.get(c) for c in cols} for m in merged.values()]
    sb_upsert("de_releases", rows, on_conflict="series_slug,provider_slug,season_label")
    sb_insert("de_date_changes", date_changes)
    sb_upsert("de_scrape_log", [{
        "run_date": today.isoformat(),
        "upcoming_rows": len(upcoming), "available_rows": len(available),
        "kept_rows": len(rows), "date_changes": len(date_changes),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }], on_conflict="run_date")

    print(f"\nDone. {len(upcoming)} upcoming + {len(available)} newly-available rows read; "
          f"{len(rows)} kept after filter; {len(date_changes)} release-date changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
