#!/usr/bin/env python3
"""Discover Nelson houses for sale on realestate.co.nz that meet a hard filter,
enrich each with Nelson City Council hazard data, homes.co.nz age/valuation and a
double-glazing / renovation description scan, and merge the result into data.json.

Designed to run unattended in GitHub Actions (see .github/workflows/update.yml).
Only *new* listings are enriched each run, capped for politeness; listings that
have left the market are dropped. Every outbound third-party request is logged on
one line (endpoint + status) so a failing run can be diagnosed from CI logs.

Data sources
  realestate.co.nz  -> candidate listings + description + coords + floor/land/beds
  NCC ArcGIS        -> flood / coastal / liquefaction / slope / fault hazards
  homes.co.nz       -> decade built, capital/land/improvement value, construction

Nothing here needs auth. Be gentle: one request every REQUEST_DELAY seconds to the
property sites; the public ArcGIS service is queried a little faster.
"""
import json, re, time, math, os, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")

# ---------------------------------------------------------------- hard filter
# realestate.co.nz district id (271 = Nelson) and category/type used by its
# public search API. res_sale + propertyType 1 = residential houses for sale.
RE_DISTRICT   = 271
MIN_BEDS      = 3
MIN_FLOOR     = 120                       # m^2
MIN_LAND      = 250                       # m^2
MAX_NEW_PER_RUN = int(os.environ.get("CRAWL_MAX_NEW", "40"))   # enrichments per run
PRUNE_MIN_DISCOVERED = 20                 # only drop "gone" listings if discovery looked healthy
REQUEST_DELAY = float(os.environ.get("CRAWL_HOMES_DELAY", "1.0"))  # seconds between homes.co.nz requests
ARC_DELAY     = 0.25                      # seconds between ArcGIS requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
ARC = "https://services1.arcgis.com/Y4k7lyf2XTGeQC6V/arcgis/rest/services"
# realestate.co.nz's own search API (no auth). JSON-API; page via limit/offset.
SEARCH_API = ("https://platform.realestate.co.nz/search/v1/listings"
              "?filter%5Bcategory%5D%5B%5D=res_sale"
              "&filter%5Bdistrict%5D%5B%5D=" + str(RE_DISTRICT) +
              "&filter%5BpropertyType%5D%5B%5D=1"
              "&page%5Blimit%5D={limit}&page%5Boffset%5D={offset}")

def log(*a):
    print("[crawl]", *a, flush=True)

def fetch(url, tries=3, timeout=30):
    """GET a URL, returning text or None. Logs one line per call at the boundary."""
    host = urllib.parse.urlparse(url).netloc
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                log("GET", host, r.status, url[:90])
                return body
        except Exception as e:
            msg = str(e)[:70]
            if i == tries - 1:
                log("ERR", host, msg, url[:90])
                return None
            time.sleep(2 * (i + 1))

# ---------------------------------------------------------------- hazards
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
LIQ_BENIGN = ("unlikely", "very low")

# key -> (service, [layer ids to try in order]).  River layers are per-catchment,
# so the first layer that returns a polygon is the one the point sits in.
HAZARD_LAYERS = [
    ("river2020",   "River_Flooding_Depth_2020",   [4, 5, 6, 7, 8]),
    ("river2130",   "River_Flooding_Depth_2130",   [0, 1, 2, 3, 4]),
    ("coast2020",   "Coastal_Flooding_Depth",      [7]),
    ("coast2130",   "Coastal_Flooding_Depth",      [10]),
    ("runup2020",   "Wave_Runup",                  [2]),
    ("runup2130",   "Wave_Runup",                  [20]),
    ("erosionNow",  "Coastal_Erosion_Hazards",     [1]),
    ("erosion2130", "Coastal_Erosion_Hazards",     [7]),
    ("liqB",        "Natural_Hazards_Liquefaction",[0]),
    ("slopeI",      "Slope_Instability",           [3]),
    ("slopeII",     "Slope_Instability",           [2]),
    ("slopeIII",    "Slope_Instability",           [1]),
    ("slopeRunout", "Slope_Instability",           [0]),
    ("fault",       "Fault_Deformation_Overlay",   [59]),
]

def arc_query(service, layer, lon, lat):
    geom = urllib.parse.quote(json.dumps(
        {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}))
    url = (ARC + "/" + service + "/FeatureServer/" + str(layer) + "/query"
           "?geometry=" + geom + "&geometryType=esriGeometryPoint&inSR=4326"
           "&spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json")
    txt = fetch(url, timeout=25)
    if txt is None:
        return None
    try:
        return json.loads(txt).get("features", [])
    except Exception:
        return None

def make_hazard(key, attrs):
    name, scenario, kind = LAYER_META[key]
    depth, rank = None, 0
    if kind in ("flood", "coastal"):
        raw = (attrs.get("Depth") or "").strip()
        aep = (attrs.get("AnnualExceedanceProbability") or "").strip()
        if aep:
            raw = (raw + " @ " + aep).strip()
        for band in sorted(DEPTH_RANK, key=len, reverse=True):
            if band in raw:
                depth, rank = band, DEPTH_RANK[band]
                break
        detail = depth or (raw or scenario)
    elif key in ("liqA", "liqB"):
        detail = (attrs.get("LiquefactionCategory") or "").strip() or scenario
        low = detail.lower()
        if any(b in low for b in LIQ_BENIGN):
            rank = 0
        elif "possible" in low or "medium" in low:
            rank = 2
        else:
            rank = 3
    elif kind == "slope":
        detail = scenario
        rank = {"slopeI": 4, "slopeII": 3, "slopeIII": 2, "slopeRunout": 3}[key]
    elif kind == "fault":
        detail = scenario
        rank = 4
    else:
        detail = scenario
    return dict(key=key, name=name, scenario=scenario, kind=kind,
                detail=detail, depth=depth, rank=rank)

def query_hazards(lat, lon):
    """Return {'outsideCoverage': bool, 'hazards': [...]}.

    Coverage probe: the regional (Level A) liquefaction layer blankets the whole
    Nelson City Council area, so a point that returns no feature there is outside
    NCC jurisdiction (e.g. Tasman District) rather than simply hazard-free.

    The ~14 layer queries per point are independent, so they run concurrently
    against the (robust, public) Esri service to keep each property fast."""
    regional = arc_query("Natural_Hazards_Liquefaction", 1, lon, lat)
    if regional is None:
        return {"outsideCoverage": False, "hazards": []}   # transient error; treat as inside/no-data
    if len(regional) == 0:
        return {"outsideCoverage": True, "hazards": []}

    tasks = [(key, idx, svc, lyr)
             for key, svc, layers in HAZARD_LAYERS
             for idx, lyr in enumerate(layers)]
    hits = {}   # key -> {layer_index: attributes}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(arc_query, svc, lyr, lon, lat): (key, idx)
                for key, idx, svc, lyr in tasks}
        for fut, (key, idx) in futs.items():
            feats = fut.result()
            if feats:
                hits.setdefault(key, {})[idx] = feats[0]["attributes"]

    hazards = [make_hazard("liqA", regional[0]["attributes"])]
    for key, svc, layers in HAZARD_LAYERS:
        got = hits.get(key)
        if got:                       # first (lowest-index) catchment/tier that intersects
            hazards.append(make_hazard(key, got[min(got)]))
    return {"outsideCoverage": False, "hazards": hazards}

# ---------------------------------------------------------------- feature scan
GLAZ_TERMS = ["double glaz", "double-glaz", "double glazed"]
# COMPLETED-tense wording only: "renovated", not the "renovate"/"renovation" stem, so
# "potential to renovate" / "renovation opportunity" (a do-up) is NOT read as renovated.
RENO_DONE = ["renovated", "refurbished", "modernised", "modernized", "redone", "re-done",
             "recently renovated", "fully renovated", "beautifully renovated",
             "tastefully renovated", "extensively renovated", "stylishly renovated",
             "beautifully updated", "tastefully updated", "recently updated", "fully updated",
             "upgraded kitchen", "new kitchen and bathroom", "new bathroom and kitchen",
             "brand new kitchen"]
# If any of these appear, the "renovated" is about work still to be done -> veto it.
RENO_VETO = ["to renovate", "renovation project", "potential to renovate", "renovator",
             "opportunity to renovate", "scope to renovate", "ready to renovate",
             "needs renovating", "requires renovation", "awaiting renovation",
             "room to renovate", "un-renovated", "unrenovated", "to be renovated",
             "could be renovated", "ripe for renovation", "renovation awaits"]

def evidence(text, terms):
    """First matched term, returned with up to two preceding words for context."""
    low = text.lower()
    best = None
    for w in terms:
        i = low.find(w)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, w)
    if not best:
        return None
    i, w = best
    start = max(0, i - 20)
    snippet = text[start:i + len(w)]
    if start > 0:
        snippet = snippet.split(" ", 1)[-1]        # drop partial leading word
    return re.sub(r"\s+", " ", snippet).strip(" ,.-")

def scan_description(text):
    low = (text or "").lower()
    reno = any(w in low for w in RENO_DONE) and not any(v in low for v in RENO_VETO)
    glaz = any(w in low for w in GLAZ_TERMS)
    ev = []
    if reno:
        ev.append(evidence(text, RENO_DONE) or "renovated")
    if glaz:
        ev.append(evidence(text, GLAZ_TERMS) or "double glazed")
    return reno, glaz, "; ".join(ev)

# ---------------------------------------------------------------- homes.co.nz
CONSTR = {
    "WI": "Wood, iron roof", "WT": "Wood, tile roof", "WF": "Wood, flat roof",
    "BI": "Brick, iron roof", "BT": "Brick, tile roof",
    "RI": "Roughcast, iron roof", "RT": "Roughcast, tile roof",
    "FI": "Fibre cement, iron roof", "FT": "Fibre cement, tile roof",
    "CT": "Concrete, tile roof", "CI": "Concrete, iron roof",
    "XF": "Mixed materials, flat roof", "XI": "Mixed materials, iron roof",
    "XT": "Mixed materials, tile roof",
}

def enrich_homes(address, lat, lon):
    """decade built + council valuations, matched to the realestate coords so the
    fuzzy address search can't return the wrong (e.g. 200A vs 200) property."""
    out = {}
    txt = fetch("https://gateway.homes.co.nz/address/search?Address="
                + urllib.parse.quote(address))
    if not txt:
        return out
    try:
        res = json.loads(txt).get("Results", []) or []
    except Exception:
        return out
    if not res:
        return out

    def d(r):
        try:
            return (float(r["Lat"]) - lat) ** 2 + (float(r["Long"]) - lon) ** 2
        except Exception:
            return 9e9
    best = min(res, key=d)
    pid = best.get("PropertyID")
    if not pid:
        return out
    time.sleep(REQUEST_DELAY)
    txt = fetch("https://gateway.homes.co.nz/properties?property_ids=" + pid)
    if not txt:
        return out
    try:
        pd = json.loads(txt)["cards"][0]["property_details"]
    except Exception:
        return out
    dec = pd.get("decade_built")
    out["decade"] = int(dec) if str(dec).isdigit() else None
    code = (pd.get("building_construction") or "").strip() or None
    out["constrCode"] = code
    out["constr"] = CONSTR.get(code)
    out["cv"] = pd.get("capital_value")
    out["landValue"] = pd.get("land_value")
    out["improvValue"] = pd.get("improvement_value")
    return out

# ---------------------------------------------------------------- realestate.co.nz
def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def parse_api(item):
    """Turn one realestate.co.nz search-API listing into our property record."""
    a = item.get("attributes", {})
    ad = a.get("address", {}) or {}
    try:
        lat = float(ad.get("latitude")); lon = float(ad.get("longitude"))
    except (TypeError, ValueError):
        return None
    num = (ad.get("street-number") or "").strip()
    street = (ad.get("street-name") or ad.get("street") or "").strip()
    suburb = (ad.get("suburb-slug") or "").replace("-", " ").title()
    address = ", ".join(x for x in [(num + " " + street).strip(), suburb, "Nelson"] if x)
    price_txt = (a.get("price-display") or "").strip()
    ask = None
    m = re.search(r"\$([\d,]+)", price_txt)
    if m:
        ask = int(m.group(1).replace(",", ""))
    return dict(
        id=str(item.get("id")),
        address=address,
        url=a.get("website-full-url") or ("https://www.realestate.co.nz/" + str(item.get("id"))),
        lat=lat, lon=lon,
        beds=_int(a.get("bedroom-count")),
        baths=_int(a.get("bathrooms-total-count")),
        parking=_int(a.get("parking-garage-count")),
        floor=_int(a.get("floor-area")),
        land=_int(a.get("land-area")),
        priceText=price_txt,
        ask=ask,
        listed=(a.get("published-date") or "")[:10],
        _desc=strip_html(a.get("description")),
    )

def discover():
    """Page through realestate.co.nz's search API and return parsed candidate
    listings (Nelson houses for sale). One JSON call per 100 results."""
    out, offset, limit = [], 0, 100
    while True:
        txt = fetch(SEARCH_API.format(limit=limit, offset=offset))
        if not txt:
            break
        try:
            d = json.loads(txt)
        except Exception:
            break
        data = d.get("data", [])
        total = d.get("meta", {}).get("totalResults", 0)
        for item in data:
            p = parse_api(item)
            if p:
                out.append(p)
        log("discover offset", offset, "-> got", len(data), "of", total)
        offset += limit
        if offset >= total or not data:
            break
        time.sleep(1.0)
    return out

def qualifies(p):
    if p["beds"] is None or p["beds"] < MIN_BEDS:   return False
    if p["floor"] is None or p["floor"] < MIN_FLOOR: return False
    if p["land"] is None or p["land"] < MIN_LAND:   return False
    return True

def enrich(p):
    """Fill hazards, homes.co.nz records and the description scan onto a parsed listing."""
    hz = query_hazards(p["lat"], p["lon"])
    p["outsideCoverage"] = hz["outsideCoverage"]
    p["hazards"] = hz["hazards"]
    flagged = [h for h in p["hazards"] if h["rank"] > 0]
    p["hazardScore"] = sum(h["rank"] for h in flagged)
    p["hazardCount"] = len(flagged)

    time.sleep(REQUEST_DELAY)
    p.update(enrich_homes(p["address"], p["lat"], p["lon"]))
    p.setdefault("decade", None); p.setdefault("constr", None)
    p.setdefault("cv", None); p.setdefault("landValue", None); p.setdefault("improvValue", None)
    p.setdefault("lastSale", None); p.setdefault("lastSaleDate", None)

    reno, glaz, ev = scan_description(p.pop("_desc", ""))
    p["reno"], p["dblGlaz"], p["featEvidence"] = reno, glaz, ev
    p["descScanned"] = True
    return p

# ---------------------------------------------------------------- main
def save_state(state, props):
    state["properties"] = sorted(props.values(), key=lambda x: x.get("address", ""))
    state["collection"] = "Nelson"
    state["sourceUrl"] = "https://www.realestate.co.nz/residential/sale/nelson-bays/nelson/house"
    state["generated"] = time.strftime("%Y-%m-%d")
    state["criteria"] = {"beds_min": MIN_BEDS, "floor_min": MIN_FLOOR, "land_min": MIN_LAND}
    tmp = DATA + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, DATA)   # atomic; a checkpoint is never a half-written file

def main():
    state = json.load(open(DATA, encoding="utf-8"))
    props = {p["id"]: p for p in state.get("properties", [])}

    candidates = discover()
    qualifying = [p for p in candidates if qualifies(p)]
    disc_set = {p["id"] for p in qualifying}
    log("discovered %d Nelson houses, %d meet the filter (%d+ bed, %dm2+ floor, %dm2+ land)"
        % (len(candidates), len(qualifying), MIN_BEDS, MIN_FLOOR, MIN_LAND))

    # enrich + add new qualifying listings (capped for politeness). We checkpoint
    # data.json every few adds so an interrupted run keeps its progress and the
    # next run simply resumes with the listings it hasn't reached yet.
    new = [p for p in qualifying if p["id"] not in props]
    added = failed = 0
    for p in new:
        if added >= MAX_NEW_PER_RUN:
            log("hit MAX_NEW_PER_RUN =", MAX_NEW_PER_RUN, "- %d remain for next run" % (len(new) - added - failed))
            break
        try:
            enrich(p)
        except Exception as e:
            log("enrich failed", p["address"], str(e)[:70]); failed += 1; continue
        props[p["id"]] = p
        added += 1
        log("ADDED", p["address"], "| flags=%d outside=%s reno=%s glaz=%s"
            % (p["hazardCount"], p["outsideCoverage"], p["reno"], p["dblGlaz"]))
        if added % 10 == 0:
            save_state(state, props)

    # refresh price + re-scan the description on listings we already hold (the
    # description comes free with discovery), then drop ones off the market
    live = {p["id"]: p for p in qualifying}
    for lid, p in list(props.items()):
        if lid in live:
            p["priceText"] = live[lid]["priceText"] or p.get("priceText", "")
            p["ask"] = live[lid]["ask"] if live[lid]["ask"] is not None else p.get("ask")
            reno, glaz, ev = scan_description(live[lid].get("_desc", ""))
            p["reno"], p["dblGlaz"], p["featEvidence"], p["descScanned"] = reno, glaz, ev, True

    removed = 0
    if len(disc_set) >= PRUNE_MIN_DISCOVERED:
        for lid in list(props):
            if lid not in disc_set:
                log("REMOVED (off market / no longer qualifies)", props[lid].get("address", lid))
                del props[lid]; removed += 1
    else:
        log("discovery too small (%d) - skipping prune this run" % len(disc_set))
    skipped = len(candidates) - len(qualifying)

    save_state(state, props)
    log("DONE. added=%d removed=%d skipped=%d failed=%d total=%d"
        % (added, removed, skipped, failed, len(props)))

if __name__ == "__main__":
    main()
