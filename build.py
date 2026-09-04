#!/usr/bin/env python3
"""Merge Trade Me listing data, homes.co.nz property records, Nelson City Council
hazard queries and OSM POIs into a single JSON payload for the web app."""
import json, re, math, unicodedata

D = "/home/claude/watchlist/"

# ---------- listings (Trade Me watchlist cards) ----------
listings = {}
for line in open(D + "listings_raw.txt", encoding="utf-8"):
    line = line.rstrip("\n")
    if not line:
        continue
    p = line.split("|")
    tmid = p[0]
    listed = p[2].replace("Listed", "").strip()
    address = p[3].strip()
    price_txt = p[4].strip()
    # trailing fields: [beds, baths, (parking)?, floor m², land m²], then image
    img = p[-1].strip()
    mid = [x.strip() for x in p[5:-1] if x.strip()]
    areas = [x for x in mid if "m²" in x]
    nums = [x for x in mid if "m²" not in x]
    def toi(s):
        s = s.replace("+", "").replace(",", "")
        return int(s) if s.isdigit() else None
    beds = toi(nums[0]) if len(nums) > 0 else None
    baths = toi(nums[1]) if len(nums) > 1 else None
    parking = toi(nums[2]) if len(nums) > 2 else None
    def toa(s):
        return int(s.replace("m²", "").replace(",", "").strip())
    floor = toa(areas[0]) if len(areas) > 0 else None
    land = toa(areas[1]) if len(areas) > 1 else None
    # asking price -> numeric where the listing states one
    ask = None
    m = re.search(r"\$([\d,]+)", price_txt)
    if m:
        ask = int(m.group(1).replace(",", ""))
    listings[tmid] = dict(
        id=tmid, address=address, listed=listed, priceText=price_txt, ask=ask,
        beds=beds, baths=baths, parking=parking, floor=floor, land=land, img=img or None,
        url="https://www.trademe.co.nz/a/listing/" + tmid,
    )

# ---------- homes.co.nz property records ----------
CONSTR = {
    "WI": "Wood, iron roof", "WT": "Wood, tile roof", "WF": "Wood, flat roof",
    "BI": "Brick, iron roof", "BT": "Brick, tile roof",
    "RI": "Roughcast, iron roof", "RT": "Roughcast, tile roof",
    "FI": "Fibre cement, iron roof", "FT": "Fibre cement, tile roof",
    "CT": "Concrete, tile roof", "CI": "Concrete, iron roof",
    "XF": "Mixed materials, flat roof", "XI": "Mixed materials, iron roof",
    "XT": "Mixed materials, tile roof",
}
for line in open(D + "homes_raw.txt", encoding="utf-8"):
    line = line.rstrip("\n")
    if not line or line.startswith("tmid|"):
        continue
    p = line.split("|")
    tmid = p[0]
    if tmid not in listings:
        continue
    def num(x):
        x = x.strip()
        return int(x) if x.isdigit() else None
    L = listings[tmid]
    L["decade"] = num(p[2])
    L["constrCode"] = p[3].strip() or None
    L["constr"] = CONSTR.get(p[3].strip())
    L["cv"] = num(p[6]); L["landValue"] = num(p[7]); L["improvValue"] = num(p[8])
    L["lastSale"] = num(p[9]); L["lastSaleDate"] = p[10].strip() or None
    L["lat"] = float(p[11]); L["lon"] = float(p[12])
    L["councilAddress"] = p[1].strip()

# ---------- hazards ----------
# Severity ordering for the depth bands the council publishes.
DEPTH_RANK = {"0 - 20cm": 1, ">5 - 20cm": 1, ">20 - 50cm": 2, ">50cm - 1m": 3,
              ">1 - 2m": 4, ">2m": 5}
LAYER_META = {
    "river2020":   ("River flooding", "Present day, 1% AEP", "flood"),
    "river2130":   ("River flooding", "2130 projection", "flood"),
    "coast2020":   ("Coastal flooding", "Present day", "coastal"),
    "coast2130":   ("Coastal flooding", "2130, SSP5-8.5M", "coastal"),
    "runup2020":   ("Wave run-up", "Present day", "coastal"),
    "runup2130":   ("Wave run-up", "2130, SSP5-8.5M", "coastal"),
    "erosionNow":  ("Coastal erosion", "Current", "coastal"),
    "erosion2130": ("Coastal erosion", "2130, 8.5M", "coastal"),
    "liqA":        ("Liquefaction", "Regional assessment (Level A)", "ground"),
    "liqB":        ("Liquefaction", "Tāhunanui assessment (Level B)", "ground"),
    "slopeI":      ("Slope instability", "Tier I (highest)", "slope"),
    "slopeII":     ("Slope instability", "Tier II", "slope"),
    "slopeIII":    ("Slope instability", "Tier III", "slope"),
    "slopeRunout": ("Slope instability", "Run-out zone", "slope"),
    "fault":       ("Fault deformation", "Fault overlay", "fault"),
}
# Liquefaction categories that are not really a flag
LIQ_BENIGN = ("unlikely", "very low")

for line in open(D + "hazards_raw.txt", encoding="utf-8"):
    line = line.rstrip("\n")
    if not line:
        continue
    tmid, rest = line.split(" :: ", 1)
    if tmid not in listings:
        continue
    L = listings[tmid]
    L["hazards"] = []
    L["outsideCoverage"] = False
    if rest == "OUTSIDE_COVERAGE":
        L["outsideCoverage"] = True
        continue
    if rest == "NONE":
        continue
    for part in rest.split(" || "):
        k, _, val = part.partition("=")
        if val == "ERR" or k not in LAYER_META:
            continue
        name, scenario, kind = LAYER_META[k]
        depth = None
        rank = 0
        detail = val
        if kind in ("flood", "coastal"):
            for band in sorted(DEPTH_RANK, key=len, reverse=True):
                if band in val:
                    depth = band
                    rank = DEPTH_RANK[band]
                    break
            detail = depth or val
        elif k in ("liqA", "liqB"):
            detail = val
            low = val.lower()
            if any(b in low for b in LIQ_BENIGN):
                rank = 0
            elif "possible" in low or "medium" in low:
                rank = 2
            else:
                rank = 3
        elif kind == "slope":
            detail = scenario
            rank = {"slopeI": 4, "slopeII": 3, "slopeIII": 2, "slopeRunout": 3}[k]
        elif kind == "fault":
            rank = 4
        L["hazards"].append(dict(key=k, name=name, scenario=scenario, kind=kind,
                                 detail=detail, depth=depth, rank=rank))

# ---------- POIs ----------
SCHOOL_WORDS = ("school", "college", "kura", "academy", "oneschool")
def classify(name, code):
    if code == "c":
        return "cafe"
    if code == "k":
        return "kindergarten"
    low = name.lower()
    if any(w in low for w in SCHOOL_WORDS):
        # NZ conventions: College = secondary, Intermediate = years 7-8, rest = primary
        if "college" in low:
            return "secondary"
        if "intermediate" in low:
            return "intermediate"
        return "primary"
    return "supermarket"

pois = []
for line in open(D + "poi_raw.txt", encoding="utf-8"):
    line = line.rstrip("\n")
    if not line:
        continue
    name, code, lat, lon = line.split("|")
    pois.append(dict(n=name, t=classify(name, code), lat=float(lat), lon=float(lon)))

# a couple of NZ-specific fixes OSM name heuristics can't catch
FIXES = {"Salisbury School": "secondary", "Maitai School": "primary",
         "Te Kura Kaupapa Maori o Tuia te Matangi": "primary",
         "OneSchool Global Nelson": "secondary"}
for p in pois:
    if p["n"] in FIXES:
        p["t"] = FIXES[p["n"]]

# ---------- overall hazard score ----------
for L in listings.values():
    hz = L.get("hazards", [])
    flagged = [h for h in hz if h["rank"] > 0]
    L["hazardScore"] = sum(h["rank"] for h in flagged)
    L["hazardCount"] = len(flagged)

# ---------- listing-description feature scan ----------
# Each listing's marketing description was fetched from its realestate.co.nz /
# OneRoof / agency page (Trade Me blocks automated requests) and scanned for
# double-glazing and renovation wording. Descriptions are copyright and live on
# third-party sites, so only the derived flags and the short matched phrase are
# stored here. Six listings had no public description to scan (descScanned=False).
# tmid -> (renovated, double_glazing, matched phrase)
FEATURE_SCAN = {
    "5750958235": (True,  False, "thoughtfully renovated"),
    "5532971914": (True,  False, "extensively renovated"),
    "6010957811": (True,  False, "new bathroom and kitchen"),
    "5766783417": (True,  False, "beautifully renovated family bathroom"),
    "5969731326": (True,  False, "modernised kitchen"),
    "5788310112": (True,  False, "thoughtfully modernised"),
    "5959828752": (True,  False, "beautifully renovated throughout"),
    "6064875981": (True,  False, "beautifully renovated"),
    "5941032933": (True,  True,  "lovingly renovated; double-glazed studio"),
    "6064828779": (False, True,  "double glazing"),
}
# listings where no public listing description could be found to scan
FEATURE_UNSCANNED = {"6101420908", "6114065395", "5930848760",
                     "6077521522", "6012638172", "6104877044"}
for tmid, L in listings.items():
    reno, glaz, ev = FEATURE_SCAN.get(tmid, (False, False, ""))
    L["reno"] = reno
    L["dblGlaz"] = glaz
    L["featEvidence"] = ev
    L["descScanned"] = tmid not in FEATURE_UNSCANNED

out = dict(
    collection="Nelson",
    sourceUrl="https://www.trademe.co.nz/a/my-trade-me/watchlist/6c84172e-ec87-4420-8852-a7ce3d7ff433",
    generated="2026-09-04",
    properties=sorted(listings.values(), key=lambda x: x["address"]),
    pois=pois,
)
json.dump(out, open(D + "data.json", "w", encoding="utf-8"), ensure_ascii=False)

print("properties:", len(out["properties"]))
print("with decade:", sum(1 for p in out["properties"] if p.get("decade")))
print("with coords:", sum(1 for p in out["properties"] if p.get("lat")))
print("with hazards flagged:", sum(1 for p in out["properties"] if p.get("hazardCount")))
print("outside coverage:", [p["address"] for p in out["properties"] if p.get("outsideCoverage")])
print("pois:", len(pois), {t: sum(1 for p in pois if p["t"] == t) for t in set(p["t"] for p in pois)})
print("missing land:", [p["address"] for p in out["properties"] if not p.get("land")])
