#!/usr/bin/env python3
"""One-off OpenStreetMap pull for a map region: the basemap geometry (coastline,
rivers, arterial roads -> geo.json) and the points of interest (schools, cafés,
supermarkets -> data.json "pois"). Both are static, so this is run by hand when a
region is added, not by the daily crawl.

    python3 fetch_osm.py wellington          # basemap + POIs
    python3 fetch_osm.py wellington geo      # just one of them
    python3 fetch_osm.py wellington pois

geo.json is keyed by region; each line is delta-encoded as "lat,lon dlat,dlon ..."
in units of 1e-4 degrees (about 11 m), which is what the viewer's decode() reads.
"""
import json, os, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OVERPASS = "https://overpass-api.de/api/interpreter"
# overpass-api.de answers HTTP 406 to script-looking requests; it accepts the header
# set a browser on overpass-turbo.eu sends.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-NZ,en;q=0.9",
    "Origin": "https://overpass-turbo.eu",
    "Referer": "https://overpass-turbo.eu/",
    "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "empty",
}

# south, west, north, east
BBOX = {
    "wellington": (-41.40, 174.58, -40.82, 175.10),
}
SIMPLIFY = 0.0003          # Douglas-Peucker tolerance in degrees (~30 m)

def overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(6):
        try:
            req = urllib.request.Request(OVERPASS, data=body, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())["elements"]
        except Exception as e:
            print("overpass retry:", str(e)[:80], flush=True)
            time.sleep(15 * (attempt + 1))
    raise SystemExit("Overpass did not answer")

# ---------------------------------------------------------------- basemap
def simplify(pts, tol):
    """Douglas-Peucker on (lat, lon) pairs, iterative so long coastlines don't recurse."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ay, ax), (by, bx) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        den = dx * dx + dy * dy
        worst, at = 0.0, None
        for i in range(a + 1, b):
            py, px = pts[i]
            if den == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / den))
                d = ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5
            if d > worst:
                worst, at = d, i
        if at is not None and worst > tol:
            keep[at] = True
            stack += [(a, at), (at, b)]
    return [p for p, k in zip(pts, keep) if k]

def encode(pts):
    out, plat, plon = [], 0, 0
    for lat, lon in pts:
        ilat, ilon = round(lat * 1e4), round(lon * 1e4)
        if out and ilat == plat and ilon == plon:
            continue
        out.append("%d,%d" % (ilat - plat, ilon - plon))
        plat, plon = ilat, ilon
    return " ".join(out)

def clip_runs(pts, bbox):
    """Split a line into the runs that lie inside the bbox (ways are returned whole)."""
    s, w, n, e = bbox
    runs, cur = [], []
    for lat, lon in pts:
        if s <= lat <= n and w <= lon <= e:
            cur.append((lat, lon))
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return runs

def fetch_geo(region):
    bbox = BBOX[region]
    box = "(%s,%s,%s,%s)" % bbox
    layers = {
        "coastline": 'way["natural"="coastline"]' + box,
        "river":     'way["waterway"="river"]' + box,
        "road":      'way["highway"~"^(motorway|trunk|primary)$"]' + box,
    }
    geo = {}
    for name, sel in layers.items():
        ways = overpass("[out:json][timeout:120];" + sel + ";out geom;")
        lines = []
        for w in ways:
            pts = [(g["lat"], g["lon"]) for g in w.get("geometry", [])]
            for run in clip_runs(pts, bbox):
                enc = encode(simplify(run, SIMPLIFY))
                if " " in enc:                      # at least two distinct points
                    lines.append(enc)
        geo[name] = lines
        print(region, name, len(ways), "ways ->", len(lines), "lines", flush=True)
        time.sleep(2)
    path = os.path.join(HERE, "geo.json")
    allgeo = json.load(open(path, encoding="utf-8"))
    allgeo[region] = geo
    json.dump(allgeo, open(path, "w", encoding="utf-8"), separators=(",", ":"))
    print("geo.json:", os.path.getsize(path), "bytes")

# ---------------------------------------------------------------- points of interest
def classify(tags):
    """Same scheme as the Nelson capture: schools are classified by name, since NZ
    schools rarely carry isced:level."""
    if tags.get("shop") == "supermarket":
        return "supermarket"
    if tags.get("amenity") == "cafe":
        return "cafe"
    if tags.get("amenity") == "kindergarten":
        return "kindergarten"
    low = tags.get("name", "").lower()
    if any(w in low for w in ("kindergarten", "preschool", "pre-school", "childcare",
                              "early learning", "montessori", "playcentre", "kohanga")):
        return "kindergarten"
    if any(w in low for w in ("college", "high school", "collegiate", "grammar")):
        return "secondary"
    if "intermediate" in low:
        return "intermediate"
    return "primary"

def fetch_pois(region):
    box = "(%s,%s,%s,%s)" % BBOX[region]
    sels = ['nwr["amenity"="school"]["name"]', 'nwr["amenity"="kindergarten"]["name"]',
            'nwr["shop"="supermarket"]["name"]', 'nwr["amenity"="cafe"]["name"]']
    els = overpass("[out:json][timeout:120];(" + "".join(s + box + ";" for s in sels) + ");out center tags;")
    pois, seen = [], set()
    for e in els:
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        tags = e.get("tags", {})
        name = " ".join(tags.get("name", "").split())
        if lat is None or not name:
            continue
        key = (name.lower(), round(lat, 3), round(lon, 3))   # node + building of one place
        if key in seen:
            continue
        seen.add(key)
        pois.append(dict(n=name, t=classify(tags), lat=round(lat, 5), lon=round(lon, 5), region=region))
    path = os.path.join(HERE, "data.json")
    data = json.load(open(path, encoding="utf-8"))
    kept = [p for p in data.get("pois", []) if p.setdefault("region", "nelson") != region]
    data["pois"] = kept + sorted(pois, key=lambda p: (p["t"], p["n"]))
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)
    print(region, "pois:", len(pois), {t: sum(1 for p in pois if p["t"] == t) for t in sorted({p["t"] for p in pois})})

if __name__ == "__main__":
    region = sys.argv[1] if len(sys.argv) > 1 else ""
    what = sys.argv[2] if len(sys.argv) > 2 else "all"
    if region not in BBOX:
        raise SystemExit("usage: fetch_osm.py <%s> [geo|pois]" % "|".join(BBOX))
    if what in ("all", "geo"):
        fetch_geo(region)
    if what in ("all", "pois"):
        fetch_pois(region)
