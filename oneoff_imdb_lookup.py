"""One-off: match Portuguese (Brazil) film titles to IMDb IDs via TMDb.
Input: PAYLOAD env (base64 gzip JSON list of {row,t,y,d,c,a}). Output printed
between markers as base64 gzip JSON. Remove after use."""
import os, json, gzip, base64, re, unicodedata, requests
KEY = os.environ["TMDB_API_KEY"]
def get(path, **p):
    r = requests.get(f"https://api.themoviedb.org/3/{path}", params=dict(p, api_key=KEY), timeout=20)
    return r.json() if r.ok else {}
norm = lambda s: re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", s or "").encode("ascii","ignore").decode().lower()).strip()
def yr(m):
    d = m.get("release_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None
items = json.loads(gzip.decompress(base64.b64decode(os.environ["PAYLOAD"])))
out = []
for it in items:
    dirs = {norm(x) for x in re.split(r",|&| e ", it["d"] or "") if x.strip()}
    actors = {norm(a) for a in it["a"]}
    cands = {}
    for q in dict.fromkeys([it["t"], re.sub(r"\s*[:–-].*$", "", it["t"])]):
        for params in ({"year": it["y"]}, {}):
            for m in get("search/movie", query=q, language="pt-BR", **params).get("results", [])[:8]:
                cands[m["id"]] = m
    scored = []
    for m in list(cands.values())[:15]:
        y = yr(m)
        det = get(f"movie/{m['id']}", append_to_response="credits,external_ids")
        crew = det.get("credits", {})
        tdirs = {norm(c["name"]) for c in crew.get("crew", []) if c.get("job") == "Director"}
        tcast = {norm(c["name"]) for c in crew.get("cast", [])[:30]}
        dmatch = bool(dirs & tdirs)
        cmatch = len(actors & tcast)
        tmatch = norm(m.get("title")) == norm(it["t"])
        ydiff = abs(y - it["y"]) if (y and it["y"]) else 9
        score = 10*dmatch + 3*min(cmatch,3) + 3*tmatch + (3 if ydiff == 0 else 2 if ydiff == 1 else 0)
        scored.append((score, dmatch, cmatch, tmatch, ydiff, m, det))
    scored.sort(key=lambda s: -s[0])
    rec = {"row": it["row"], "t": it["t"], "y": it["y"]}
    if scored:
        s, dmatch, cmatch, tmatch, ydiff, m, det = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1
        conf = ("high" if (dmatch or cmatch >= 2) and ydiff <= 1 else
                "medium" if (tmatch and ydiff <= 1 and s - second >= 3) else "low")
        rec.update(imdb=(det.get("external_ids") or {}).get("imdb_id") or det.get("imdb_id"),
                   tmdb=m["id"], title_pt=m.get("title"), original=m.get("original_title"),
                   year_tmdb=yr(m), dir_match=dmatch, cast_hits=cmatch, conf=conf,
                   directors=sorted(c["name"] for c in det.get("credits", {}).get("crew", []) if c.get("job") == "Director"))
    out.append(rec)
# Pass 2: for anything not confidently matched, go via the director's own
# filmography (exact person match + year), then via lead cast.
def via_people(it):
    names = [x.strip() for x in re.split(r",|&| e ", it["d"] or "") if x.strip()]
    hits = {}
    for n in names:
        for p in get("search/person", query=n).get("results", [])[:3]:
            if norm(p["name"]) != norm(n):
                continue
            for c in get(f"person/{p['id']}/movie_credits").get("crew", []):
                if c.get("job") == "Director":
                    hits.setdefault(c["id"], [c, 0]); hits[c["id"]][1] += 10
    if not hits:
        for n in it["a"][:4]:
            for p in get("search/person", query=n).get("results", [])[:2]:
                if norm(p["name"]) != norm(n):
                    continue
                for c in get(f"person/{p['id']}/movie_credits").get("cast", []):
                    hits.setdefault(c["id"], [c, 0]); hits[c["id"]][1] += 3
    best = []
    for mid, (m, sc) in hits.items():
        y = yr(m)
        if not y or not it["y"] or abs(y - it["y"]) > 2:
            continue
        best.append((sc - abs(y - it["y"]), m))
    best.sort(key=lambda b: -b[0])
    return best
for rec, it in zip(out, items):
    if rec.get("conf") == "high":
        continue
    best = via_people(it)
    if not best:
        rec["pass2"] = "no candidate"
        continue
    sc, m = best[0]
    det = get(f"movie/{m['id']}", append_to_response="credits,external_ids")
    rec["p2"] = {"imdb": (det.get("external_ids") or {}).get("imdb_id"), "tmdb": m["id"],
                 "original": det.get("original_title"), "title_pt": get(f"movie/{m['id']}", language="pt-BR").get("title"),
                 "year_tmdb": yr(m), "score": sc, "n_candidates": len(best),
                 "runner_up": [(b[1].get("title"), yr(b[1])) for b in best[1:3]],
                 "directors": sorted(c["name"] for c in det.get("credits", {}).get("crew", []) if c.get("job") == "Director")}
print("===RESULT===")
print(base64.b64encode(gzip.compress(json.dumps(out, ensure_ascii=False).encode())).decode())
print("===END===")
