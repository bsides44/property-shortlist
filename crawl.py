#!/usr/bin/env python3
"""Discover houses for sale on realestate.co.nz that meet a hard filter, in each
map region (see REGIONS: Nelson, and Wellington up to Paraparaumu), enrich each
with council hazard data where the region has it (Nelson City Council so far),
homes.co.nz age/valuation and a double-glazing / renovation description scan, and
merge the result into data.json.

Designed to run unattended in GitHub Actions (see .github/workflows/update.yml).
Only *new* listings are enriched each run (all of them, requests staggered per host); listings that
have left the market are dropped. Every outbound third-party request is logged on
one line (endpoint + status) so a failing run can be diagnosed from CI logs.

Data sources
  realestate.co.nz  -> candidate listings + description + coords + floor/land/beds
  NCC ArcGIS        -> Nelson flood / coastal / liquefaction / slope / fault hazards
  GWRC + WCC / PCC / KCDC ArcGIS -> the same for Wellington, plus tsunami zones
  homes.co.nz       -> decade built, capital/land/improvement value, construction

Nothing here needs auth. Be gentle: one request every REQUEST_DELAY seconds to the
property sites; the public ArcGIS service is queried a little faster.
"""
import json, re, time, math, os, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")

# ---------------------------------------------------------------- hard filter
# res_sale + propertyType 1 = residential houses for sale; the same filter
# applies to every region. Bedrooms and land area are deliberately NOT filtered
# here - every house in the area is kept and the viewer's sliders narrow it down.
MIN_FLOOR     = 120                       # m^2

# One entry per map region (the viewer's region toggle). Per region:
#   districts    realestate.co.nz district id -> the town name appended to addresses
#   centre/radius_km  keep the map readable, and drop the odd mis-tagged listing
#                (e.g. a Bishopdale, Christchurch address that comes back under the
#                Nelson district, or an Otaki house tagged "Paraparaumu")
#   exclude_suburbs / north_limit_lat  northern cut-off
#   hazards      which council hazard registry covers it: "ncc" (HAZARD_LAYERS) or
#                "wgtn" (WGTN_LAYERS); None = not wired up yet
REGIONS = {
    "nelson": dict(
        label="Nelson",
        districts={271: "Nelson"},
        centre=(-41.2930, 173.2440), radius_km=15,      # Tahunanui
        # nothing in Marybank or beyond it. The latitude line sits just south of
        # Marybank, so it also catches listings addressed as "Atawhai" that are level
        # with or north of it (Atawhai Downs, upper Atawhai Drive); Atawhai proper stays.
        exclude_suburbs={"marybank", "todds valley", "hira", "glenduan", "wakapuaka", "cable bay"},
        north_limit_lat=-41.2310,
        hazards="ncc",
        source_url="https://www.realestate.co.nz/residential/sale/nelson-bays/nelson/house",
    ),
    "wellington": dict(
        label="Wellington",
        # Wellington City, Porirua and the Kapiti Coast as far as Paraparaumu (no Hutt Valley)
        districts={265: "Wellington", 268: "Porirua", 261: "Kapiti Coast"},
        centre=(-41.1100, 174.8700), radius_km=34,      # Paremata, mid-corridor
        # Paraparaumu / Otaihanga are the last suburbs in; the latitude line follows
        # the Waikanae River, north of both.
        exclude_suburbs={"waikanae", "waikanae beach", "reikorangi", "peka peka", "te horo",
                         "te horo beach", "hautere", "otaki", "otaki beach", "otaki forks"},
        north_limit_lat=-40.8720,
        hazards="wgtn",
        source_url="https://www.realestate.co.nz/residential/sale/wellington/house",
    ),
}
MAX_NEW_PER_RUN = int(os.environ.get("CRAWL_MAX_NEW", "0"))    # enrichments per run; 0 = no cap (every new house, every day)
PRUNE_MIN_DISCOVERED = 20                 # only drop "gone" listings if discovery looked healthy
REQUEST_DELAY = float(os.environ.get("CRAWL_HOMES_DELAY", "1.0"))  # seconds between homes.co.nz requests
ARC_DELAY     = 0.25                      # min seconds between any two requests to the same host

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
ARC = "https://services1.arcgis.com/Y4k7lyf2XTGeQC6V/arcgis/rest/services"
# realestate.co.nz's own search API (no auth). JSON-API; page via limit/offset.
SEARCH_API = ("https://platform.realestate.co.nz/search/v1/listings"
              "?filter%5Bcategory%5D%5B%5D=res_sale"
              "&filter%5Bdistrict%5D%5B%5D={district}"
              "&filter%5BpropertyType%5D%5B%5D=1"
              "&page%5Blimit%5D={limit}&page%5Boffset%5D={offset}")

def log(*a):
    print("[crawl]", *a, flush=True)

_last_hit = {}

def fetch(url, tries=3, timeout=30):
    """GET a URL, returning text or None. Logs one line per call at the boundary."""
    host = urllib.parse.urlparse(url).netloc
    for i in range(tries):
        wait = _last_hit.get(host, 0) + ARC_DELAY - time.monotonic()   # stagger per host
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()
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
    "fault":       ("Fault line", "Fault rupture zone", "fault"),
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
    return layer_query(ARC + "/" + service + "/FeatureServer/" + str(layer), lon, lat)

def layer_query(layer_url, lon, lat):
    """Point-in-polygon against one ArcGIS layer (FeatureServer or MapServer).
    Returns its intersecting features, or None if the request failed."""
    geom = urllib.parse.quote(json.dumps(
        {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}))
    url = (layer_url + "/query"
           "?geometry=" + geom + "&geometryType=esriGeometryPoint&inSR=4326"
           "&spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json")
    txt = fetch(url, timeout=25)
    if txt is None:
        return None
    try:
        d = json.loads(txt)
        return None if "error" in d else d.get("features", [])
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

# ---------------------------------------------------------------- hazards: Wellington
# Four publishers cover the Wellington region. Greater Wellington's regional layers
# apply everywhere; each city / district council adds its own district-plan overlays.
# One row per layer:
#   council  gw (queried for every point) | wcc | pcc | kcdc (queried for that council's listings)
#   group    layers in a group are alternatives, listed worst first: the first hit wins
#   rank     same scale as Nelson: 0 = shown but not scored, 2 = worth checking, 3-4 = significant
#   chip     the short label shown on the listing row
#   mode     how the rank / wording is read from the feature's attributes (see wgtn_hazard)
#   future   a sea-level-rise projection rather than a present-day hazard
# The table is written into data.json ("hazardRegistry") and the viewer's "Add a
# property" reads it from there, so this is the only copy.
_GW   = "https://mapping.gw.govt.nz/arcgis/rest/services/GW/Emergencies_P/MapServer/"
_WCC  = "https://gis.wcc.govt.nz/arcgis/rest/services/2024DistrictPlan/2024DistrictPlan/MapServer/"
_PCC  = "https://services1.arcgis.com/kIwRm3UCyRHYbM8a/arcgis/rest/services/"
_KCDC = "https://maps.kapiticoast.govt.nz/server/rest/services/Public/"
def _pcc(name):
    return _PCC + name + "_PDP2025/FeatureServer/0"
def _row(council, group, key, url, name, scenario, kind, rank=0, chip=None, **extra):
    return dict(council=council, group=group, key=key, url=url, name=name,
                scenario=scenario, kind=kind, rank=rank, chip=chip or name, **extra)
_SLR_W, _SLR_P, _SLR_K = "With 1.43 m sea-level rise", "With 1 m sea-level rise", "2120, 1.25 m sea-level rise"
WGTN_LAYERS = [
    # Greater Wellington Regional Council
    _row("gw", "liq", "gwLiq", _GW + "10", "Liquefaction", "Regional liquefaction potential", "ground", mode="liq"),
    _row("gw", "slope", "gwSlope", _GW + "11", "Slope failure", "Earthquake-induced, regional", "slope", mode="slope"),
    _row("gw", "tsunami", "gwTsunami", _GW + "23", "Tsunami evacuation zone", "Regional evacuation zones", "tsunami", mode="tsunami"),
    # Wellington City Council, 2024 District Plan
    _row("wcc", "fault", "wccFaultWgtn", _WCC + "59", "Fault line", "Wellington Fault (high hazard area)", "fault", 4, "Wellington Fault"),
    _row("wcc", "fault", "wccFaultOhariu", _WCC + "58", "Fault line", "Ohariu Fault (high hazard area)", "fault", 4, "Ohariu Fault"),
    _row("wcc", "fault", "wccFaultShepherds", _WCC + "56", "Fault line", "Shepherds Gully Fault (low hazard area)", "fault", 4, "Shepherds Gully Fault"),
    _row("wcc", "fault", "wccFaultTerawhiti", _WCC + "57", "Fault line", "Terawhiti Fault (low hazard area)", "fault", 4, "Terawhiti Fault"),
    _row("wcc", "flood", "wccStream", _WCC + "63", "Flood hazard", "Stream corridor (high hazard area)", "flood", 4, "Flood: stream corridor"),
    _row("wcc", "flood", "wccOverland", _WCC + "62", "Flood hazard", "Overland flowpath (medium hazard area)", "flood", 3, "Flood: overland flowpath"),
    _row("wcc", "flood", "wccInundation", _WCC + "61", "Flood hazard", "Inundation area (low hazard area)", "flood", 2, "Flood: inundation area"),
    _row("wcc", "coast", "wccCoast", _WCC + "50", "Coastal inundation", "Present day, 1:100 year storm", "coastal", 4),
    _row("wcc", "coastF", "wccCoastSLR", _WCC + "49", "Coastal inundation", _SLR_W, "coastal", 2, future=True),
    # Porirua City Council, district plan (PDP2025 layers)
    _row("pcc", "fault", "pccFault", _pcc("Fault_Rupture_Zone"), "Fault line", "Fault rupture zone", "fault", 4, "Fault rupture zone", field="Fault_Name"),
    _row("pcc", "flood", "pccStream", _pcc("Flood_Hazard_Stream_Corridor"), "Flood hazard", "Stream corridor", "flood", 4, "Flood: stream corridor"),
    _row("pcc", "flood", "pccOverland", _pcc("Flood_Hazard_Overland_Flow"), "Flood hazard", "Overland flow", "flood", 3, "Flood: overland flow"),
    _row("pcc", "flood", "pccInundation", _pcc("Flood_Hazard_Inundation"), "Flood hazard", "Inundation", "flood", 2, "Flood: inundation"),
    _row("pcc", "coast", "pccCoast", _pcc("Coastal_Flood_Hazard_Current_Inundation_Risk"), "Coastal inundation", "Present day", "coastal", 4),
    _row("pcc", "coastF", "pccCoastSLR", _pcc("Coastal_Flood_Hazard_1in100yr_Storm_Surge_with_1m_SLR"), "Coastal inundation", _SLR_P, "coastal", 2, future=True),
    _row("pcc", "erosion", "pccErosion", _pcc("Coastal_Erosion_Hazard_Current_Erosion_Risk"), "Coastal erosion", "Present day", "coastal", 4),
    _row("pcc", "erosionF", "pccErosionSLR", _pcc("Coastal_Erosion_Hazard_Future_Erosion_with_1m_SLR"), "Coastal erosion", _SLR_P, "coastal", 2, future=True),
    # Kāpiti Coast District Council. Present-day coastal erosion is published as a
    # line, not an area, so only the 2120 shoreline range can be tested against a point.
    _row("kcdc", "fault", "kcdcFault", _KCDC + "District_Plan_Overlays/MapServer/10", "Fault line", "Fault avoidance area", "fault", 4, "Fault avoidance area", field="FAULT_NAME"),
    _row("kcdc", "flood", "kcdcFlood", _KCDC + "District_Plan_Overlays/MapServer/11", "Flood hazard", "District plan flood hazard", "flood", mode="kflood"),
    _row("kcdc", "erosionF", "kcdcErosionSLR", _KCDC + "Coastal_Erosion/MapServer/30", "Coastal erosion", _SLR_K, "coastal", 2, future=True),
]
TOWN_COUNCIL = {"Wellington": "wcc", "Porirua": "pcc", "Kapiti Coast": "kcdc"}

def wgtn_hazard(e, a):
    """One registry row + the attributes of the feature it hit -> a hazard record
    (or None if the feature says nothing). Mirrored by wgtnHazard() in the viewer."""
    h = dict(key=e["key"], name=e["name"], scenario=e["scenario"], kind=e["kind"],
             detail=e["scenario"], depth=None, rank=e["rank"], chip=e["chip"])
    if e.get("future"):
        h["future"] = True
    mode = e.get("mode")
    if mode == "liq":
        v = str(a.get("Liquefaction") or "").strip()
        if not v:
            return None
        h.update(detail=v + " liquefaction potential", chip="Liquefaction: " + v,
                 rank={"Very High": 3, "High": 3, "Moderate": 2}.get(v, 0))
    elif mode == "slope":
        # SEVERITY runs "1 Low" .. "5 High"; nearly all of the city is 1-2
        n = int((re.match(r"\d", str(a.get("SEVERITY") or "")) or ["0"])[0])
        level = {3: 1, 4: 2, 5: 3}.get(n, 0)
        word = ["low", "moderate", "moderate to high", "high"][level]
        h.update(detail="Severity %d of 5" % n if n else h["detail"], level=level,
                 chip="Slope failure: " + word, rank=[0, 2, 3, 3][level])
    elif mode == "tsunami":
        c = str(a.get("Col_Code") or "").strip().lower()
        if not c:
            return None
        h.update(detail=(c.title() + " zone - " + str(a.get("Evac_Zone") or "").strip()).strip(" -"),
                 chip="Tsunami zone: " + c)
    elif mode == "kflood":
        z = str(a.get("ZONE") or "").strip()
        low = z.lower()
        h.update(detail=z or h["detail"], chip="Flood: " + (low or "hazard area"),
                 rank=4 if "corridor" in low else 3 if "overflow" in low else 2)
    elif e.get("field"):
        v = str(a.get(e["field"]) or "").strip()
        if v:
            h.update(detail=v, chip=re.sub(r"\s+(Rupture Zone|Avoidance Area)$", "", v) or e["chip"])
    return h

def query_hazards_wgtn(lat, lon, town):
    """Greater Wellington's regional layers plus the listing's own council's."""
    council = TOWN_COUNCIL.get(town)
    rows = [e for e in WGTN_LAYERS if e["council"] == "gw" or council in (None, e["council"])]
    with ThreadPoolExecutor(max_workers=8) as ex:
        feats = list(ex.map(lambda e: layer_query(e["url"], lon, lat), rows))
    if all(f is None for f in feats):
        return None                           # every request failed; try again next run
    hazards, done = [], set()
    for e, f in zip(rows, feats):
        gid = (e["council"], e["group"])
        if not f or gid in done:
            continue
        h = wgtn_hazard(e, f[0]["attributes"])
        if h:
            hazards.append(h); done.add(gid)
    return {"outsideCoverage": False, "hazards": hazards}

# ---------------------------------------------------------------- BRANZ building zones
# Per-property wind zone + coastal corrosion zone from the BRANZ Map. BRANZ has no
# public point-query API: the zones are published only as display vector tiles in a
# NZTM (EPSG:2193) tiling scheme, gzip-encoded, with the class stored as a `_symbol`
# index resolved against the layer's style. We read the tile that covers the point,
# find the containing (or nearest) polygon and map its symbol to a label. Tiles are
# cached per run; a coarse zoom means a handful of tiles cover all of Nelson.
try:
    import gzip as _gzip
    import mapbox_vector_tile as _mvt
    from shapely.geometry import shape as _shape, Point as _Point
    from pyproj import Transformer as _Transformer
    _TO_NZTM = _Transformer.from_crs(4326, 2193, always_xy=True)
    _BRANZ_OK = True
except Exception as _e:
    _BRANZ_OK = False

_BR_OX, _BR_OY, _BR_TS = -4020900, 19998100, 512      # NZTM tiling origin + tile size
_BR_RES = [78271.51696399967]
for _i in range(1, 20):
    _BR_RES.append(_BR_RES[-1] / 2)
BRANZ_LEVEL = 12                                       # one level-12 tile ~9.8km; a few cover Nelson
WIND_LABEL = {0: "Low", 1: "Medium", 2: "High", 3: "Very High",
              4: "Extra High", 5: "Specific Engineering Design"}
CORR_LABEL = {0: "Zone B", 1: "Zone C", 2: "Zone D"}
_branz_tiles = {}

def _branz_tile(svc, row, col):
    key = (svc, row, col)
    if key in _branz_tiles:
        return _branz_tiles[key]
    url = ("https://tiles.arcgis.com/tiles/vkPf8weODt71Prmb/arcgis/rest/services/"
           + svc + "/VectorTileServer/tile/" + str(BRANZ_LEVEL) + "/" + str(row) + "/" + str(col) + ".pbf")
    dec = None
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25).read()
        if raw[:2] == b"\x1f\x8b":
            raw = _gzip.decompress(raw)
        if raw:
            dec = _mvt.decode(raw)
        log("GET tiles.arcgis.com branz", svc, BRANZ_LEVEL, row, col)
    except Exception as e:
        log("ERR tiles.arcgis.com branz", str(e)[:60])
    _branz_tiles[key] = dec
    return dec

def _branz_symbol(svc, lat, lon):
    res = _BR_RES[BRANZ_LEVEL]; span = res * _BR_TS
    X, Y = _TO_NZTM.transform(lon, lat)
    col = int((X - _BR_OX) // span); row = int((_BR_OY - Y) // span)
    dec = _branz_tile(svc, row, col)
    if not dec:
        return None
    tminx = _BR_OX + col * span; tmaxy = _BR_OY - row * span
    fx = (X - tminx) / span; fy = (tmaxy - Y) / span
    best, bestd = None, 1e18
    for ln, lay in dec.items():
        ext = lay["extent"]; pt = _Point(fx * ext, (1 - fy) * ext)
        for f in lay["features"]:
            try:
                g = _shape(f["geometry"])
                if g.contains(pt):
                    return f["properties"].get("_symbol")   # BRANZ zones tile all land,
                d = g.distance(pt)                           # so nearest polygon is the zone
                if d < bestd:
                    bestd, best = d, f["properties"].get("_symbol")
            except Exception:
                pass
    return best

def branz_zones(lat, lon):
    if not _BRANZ_OK:
        return {"windZone": None, "corrosionZone": None}
    try:
        return {"windZone": WIND_LABEL.get(_branz_symbol("Wind_Zones", lat, lon)),
                "corrosionZone": CORR_LABEL.get(_branz_symbol("Corrosion_Zones", lat, lon))}
    except Exception:
        return {"windZone": None, "corrosionZone": None}

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

IMG_HOST = "https://mediaserver.realestate.co.nz"

def lead_photo(a):
    """URL of the listing's first photo, or None. The image server crops to any
    size named in the URL; the viewer swaps in a smaller crop for desktop rows."""
    for ph in a.get("photos") or []:
        base = ph.get("base-url") or ""
        if re.fullmatch(r"/listings/[\w/-]+", base):
            return IMG_HOST + base + ".crop.504x336.jpg"
    return None

def parse_api(item, region, town):
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
    address = ", ".join(x for x in [(num + " " + street).strip(), suburb, town] if x)
    price_txt = (a.get("price-display") or "").strip()
    ask = None
    m = re.search(r"\$([\d,]+)", price_txt)
    if m:
        ask = int(m.group(1).replace(",", ""))
    return dict(
        id=str(item.get("id")),
        region=region,
        address=address,
        url=a.get("website-full-url") or ("https://www.realestate.co.nz/" + str(item.get("id"))),
        lat=lat, lon=lon,
        beds=_int(a.get("bedroom-count")),
        baths=_int(a.get("bathrooms-total-count")),
        parking=_int(a.get("parking-garage-count")),
        floor=_int(a.get("floor-area")),
        land=(_int(a.get("land-area")) or 0) > 1 and _int(a.get("land-area")) or None,  # 0 / 1 = not given
        priceText=price_txt,
        ask=ask,
        listed=(a.get("published-date") or "")[:10],
        img=lead_photo(a),
        _desc=strip_html(a.get("description")),
    )

def discover(region):
    """Page through realestate.co.nz's search API and return (candidates, complete)
    for one region: its parsed houses for sale, and whether every district was read
    to the end. One JSON call per 100 results."""
    out, complete = [], True
    for district, town in REGIONS[region]["districts"].items():
        offset, limit = 0, 100
        while True:
            txt = fetch(SEARCH_API.format(district=district, limit=limit, offset=offset))
            try:
                d = json.loads(txt)
            except Exception:
                complete = False
                break
            data = d.get("data", [])
            total = d.get("meta", {}).get("totalResults", 0)
            for item in data:
                p = parse_api(item, region, town)
                if p:
                    out.append(p)
            log("discover", town, "offset", offset, "-> got", len(data), "of", total)
            offset += limit
            if offset >= total or not data:
                break
            time.sleep(1.0)
    return out, complete

def km_from(lat, lon, centre):
    R = 6371.0
    dlat = math.radians(lat - centre[0]); dlon = math.radians(lon - centre[1])
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(centre[0])) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))

def too_far_north(p):
    cfg = REGIONS[p["region"]]
    parts = [x.strip().lower() for x in p.get("address", "").split(",")]
    suburb = parts[-2] if len(parts) >= 2 else ""
    return suburb in cfg["exclude_suburbs"] or p["lat"] > cfg["north_limit_lat"]

def qualifies(p):
    if p["floor"] is None or p["floor"] < MIN_FLOOR: return False
    cfg = REGIONS[p["region"]]
    if km_from(p["lat"], p["lon"], cfg["centre"]) > cfg["radius_km"]: return False
    if too_far_north(p):                            return False
    return True

HAZ_V = 1      # bump to make the next run re-query hazards for a registry's listings

def set_hazards(p):
    """Query the region's hazard registry for one property. Returns False if the
    council services could not be reached (the record is then left unversioned,
    so the next run tries again)."""
    kind = REGIONS[p["region"]]["hazards"]
    if kind == "ncc":
        hz = query_hazards(p["lat"], p["lon"])
    elif kind == "wgtn":
        hz = query_hazards_wgtn(p["lat"], p["lon"], p["address"].rsplit(", ", 1)[-1])
    else:                                   # no council registry for this region yet
        hz = {"outsideCoverage": True, "hazards": []}
    ok = hz is not None
    if not ok:
        hz = {"outsideCoverage": True, "hazards": []}
    p["outsideCoverage"] = hz["outsideCoverage"]
    p["hazards"] = hz["hazards"]
    flagged = [h for h in p["hazards"] if h["rank"] > 0]
    p["hazardScore"] = sum(h["rank"] for h in flagged)
    p["hazardCount"] = len(flagged)
    if ok and kind == "wgtn":
        p["hazV"] = HAZ_V
    return ok

def enrich(p):
    """Fill hazards, homes.co.nz records and the description scan onto a parsed listing."""
    set_hazards(p)

    time.sleep(REQUEST_DELAY)
    p.update(enrich_homes(p["address"], p["lat"], p["lon"]))
    p.setdefault("decade", None); p.setdefault("constr", None)
    p.setdefault("cv", None); p.setdefault("landValue", None); p.setdefault("improvValue", None)
    p.setdefault("lastSale", None); p.setdefault("lastSaleDate", None)

    p.update(branz_zones(p["lat"], p["lon"]))   # BRANZ wind + corrosion zones

    reno, glaz, ev = scan_description(p.pop("_desc", ""))
    p["reno"], p["dblGlaz"], p["featEvidence"] = reno, glaz, ev
    p["descScanned"] = True
    return p

# ---------------------------------------------------------------- main
def save_state(state, props):
    state["properties"] = sorted(props.values(), key=lambda x: x.get("address", ""))
    state["collection"] = "Nelson"
    state["sourceUrl"] = REGIONS["nelson"]["source_url"]
    state["regions"] = {k: {"label": c["label"], "sourceUrl": c["source_url"],
                            "hazards": bool(c["hazards"])} for k, c in REGIONS.items()}
    state["hazardRegistry"] = {"wellington": WGTN_LAYERS}
    state["generated"] = time.strftime("%Y-%m-%d")
    state["criteria"] = {"floor_min": MIN_FLOOR}
    tmp = DATA + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, DATA)   # atomic; a checkpoint is never a half-written file

def main():
    state = json.load(open(DATA, encoding="utf-8"))
    props = {p["id"]: p for p in state.get("properties", [])}
    for p in props.values():
        p.setdefault("region", "nelson")       # records from before regions existed

    # discover each region in turn; a region is only pruned if its own discovery
    # was complete and looked healthy, so one failed region can't empty another
    candidates, qualifying, prunable = [], [], set()
    for region, cfg in REGIONS.items():
        cands, complete = discover(region)
        qual = [p for p in cands if qualifies(p)]
        candidates += cands; qualifying += qual
        if complete and len(qual) >= PRUNE_MIN_DISCOVERED:
            prunable.add(region)
        else:
            log("%s discovery incomplete or too small (%d) - skipping prune this run"
                % (cfg["label"], len(qual)))
        log("discovered %d %s houses, %d meet the filter (%dm2+ floor)"
            % (len(cands), cfg["label"], len(qual), MIN_FLOOR))
    disc_set = {p["id"] for p in qualifying}

    # enrich + add every new qualifying listing (requests are staggered). We checkpoint
    # data.json every few adds so an interrupted run keeps its progress and the
    # next run simply resumes with the listings it hasn't reached yet.
    new = [p for p in qualifying if p["id"] not in props]
    added = failed = 0
    for p in new:
        if MAX_NEW_PER_RUN and added >= MAX_NEW_PER_RUN:
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
            p["img"] = live[lid].get("img")          # lead photo can change; free with discovery
            reno, glaz, ev = scan_description(live[lid].get("_desc", ""))
            p["reno"], p["dblGlaz"], p["featEvidence"], p["descScanned"] = reno, glaz, ev, True

    removed = 0
    for lid in list(props):
        if lid not in disc_set and props[lid]["region"] in prunable:
            log("REMOVED (off market / no longer qualifies)", props[lid].get("address", lid))
            del props[lid]; removed += 1
    skipped = len(candidates) - len(qualifying)

    # (re)query hazards for Wellington listings enriched before the registry existed
    # or under an older version of it
    stale = [p for p in props.values()
             if REGIONS[p["region"]]["hazards"] == "wgtn" and p.get("hazV") != HAZ_V]
    for i, p in enumerate(stale):
        set_hazards(p)
        if (i + 1) % 50 == 0:
            save_state(state, props)
    if stale:
        log("hazards refreshed for", len(stale), "Wellington listings")

    # backfill BRANZ zones for any property that lacks them (cheap: tiles are cached)
    branz_filled = 0
    for p in props.values():
        if p.get("windZone") is None and p.get("corrosionZone") is None:
            p.update(branz_zones(p["lat"], p["lon"]))
            if p.get("windZone") or p.get("corrosionZone"):
                branz_filled += 1
    if branz_filled:
        log("BRANZ zones filled for", branz_filled, "properties")

    save_state(state, props)
    log("DONE. added=%d removed=%d skipped=%d failed=%d total=%d"
        % (added, removed, skipped, failed, len(props)))

if __name__ == "__main__":
    main()
