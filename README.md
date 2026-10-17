# Nelson Shortlist — property hazard tool

A self-updating map of **Nelson houses for sale** that answers four questions about
every listing: where is it, is it in a hazard zone (flood / slope / liquefaction),
how old / renovated is it, and how far is it from the things you care about.

It tracks realestate.co.nz on its own. A daily [GitHub Action](.github/workflows/update.yml)
crawls the current Nelson listings that meet a hard filter (120 m²+ floor; bedrooms and
land area are not filtered at crawl time — use the sliders), enriches each with council hazard data, homes.co.nz age/valuation and a
double-glazing / renovation scan of the listing text, rebuilds the page and commits it.
New qualifying houses appear automatically; sold ones drop off.

**Two regions, one page.** A Nelson / Wellington switch under the title swaps the
listings, map and nearby places. Wellington covers Wellington City, Porirua and the Kāpiti
Coast as far as Paraparaumu (no Hutt Valley), with the same hard filter. Its hazards come
from Greater Wellington plus the three councils' district plans. See [Regions](#regions).

Open `nelson_shortlist.html` (or the GitHub Pages `index.html`) in any browser. It is
self-contained — all data and map geometry are embedded, no server needed at view time.
Listing photos are the one thing loaded over the network; offline, rows show no photo.

---

## Files

| File | What it is |
|---|---|
| `crawl.py` | **The engine.** Discovers each region's listings via realestate.co.nz's search API and enriches each (hazards + homes.co.nz + description scan) into `data.json`. Regions are configured in `REGIONS` at the top. |
| `fetch_osm.py` | One-off OpenStreetMap pull for a region: basemap geometry into `geo.json` and schools / cafés / supermarkets into `data.json`. Run by hand when a region is added. |
| `build_html.py` | Injects `data.json` + `geo.json` into the template → `nelson_shortlist.html` + `index.html`. |
| `.github/workflows/update.yml` | Daily cron that runs the two scripts and commits the result. |
| `serve.py` | Optional local server (`python3 serve.py`, then http://localhost:8000). Serves the viewer and relays homes.co.nz lookups so "Add a property" can fill in age and valuation. |
| `requirements.txt` | Python deps for the BRANZ zone lookup only (`mapbox-vector-tile`, `shapely`, `pyproj`); the rest of the crawl is stdlib. |
| `app_template.html` | App source. Contains `__DATA__`, `__GEO__` and `__SRC__` placeholders. |
| `nelson_shortlist.html` / `index.html` | The built, self-contained app (identical; `index.html` is for Pages). |
| `data.json` | Live properties + POIs. Rewritten by `crawl.py` each run. |
| `geo.json` | Coastline / river / arterial geometry for the map, keyed by region, delta-encoded. |
| `poi_raw.txt` | OpenStreetMap schools, cafés, supermarkets (POIs are static; not re-crawled). |
| `build.py` | **Legacy** one-off merger for the original 23-listing Trade Me snapshot. Superseded by `crawl.py`; kept for reference. |
| `*_raw.txt`, `coords.txt` | Original one-off captures behind `build.py`. Historical. |

Run the pipeline locally:

```bash
python3 crawl.py           # update data.json from realestate.co.nz (respects CRAWL_MAX_NEW)
python3 build_html.py      # rebuild nelson_shortlist.html + index.html
```

---

## Automated updates (GitHub Actions)

`.github/workflows/update.yml` runs once a day (`0 18 * * *`, ≈ 6am NZ) plus a manual
**Run workflow** button. Each run: `crawl.py` → `build_html.py` → commit `data.json` +
`index.html` if anything changed. It needs no secrets — `GITHUB_TOKEN` (granted
`contents: write` in the workflow) does the commit.

- **Free tier.** One ~5–15 min job a day. Public repos: Actions minutes are unmetered.
  Private repos: 2,000 min/month free, so this uses roughly a quarter of it at most.
- **Politeness.** Discovery is a handful of API calls. Every *new* listing is enriched each
  run (no cap; set `CRAWL_MAX_NEW` to impose one), and requests are staggered: at least
  0.25 s between calls to the same host, 1 s between homes.co.nz and discovery pages.
  While a run is in progress the page shows a loading bar (it polls the public
  GitHub Actions API) and offers a reload once the new houses are published.
- **First run.** `data.json` was seeded locally with the full backfill, so day one is
  already complete; the Action just maintains it.

### Publishing the page

To view the always-current page, turn on **GitHub Pages** (Settings → Pages → deploy from
branch, root) and open the `index.html` URL — the daily commit republishes it. If you'd
rather not host it, just `git pull` and open `nelson_shortlist.html` locally.

### Tuning

The filter and cadence live at the top of `crawl.py` (`MIN_FLOOR`,
`MAX_NEW_PER_RUN`) and in the workflow's `cron`; each region's districts, centre / radius and
northern cut-off live in `REGIONS`.

**Northern cut-off (Nelson).** Marybank and everything beyond it is left out: `exclude_suburbs`
(Marybank, Todds Valley, Hira, Glenduan, Wakapuaka, Cable Bay) plus `north_limit_lat`, a
latitude line just south of Marybank that also catches listings addressed as "Atawhai" but
level with or north of it (Atawhai Downs, upper Atawhai Drive). Atawhai proper stays.

**Northern cut-off (Wellington).** Paraparaumu, Paraparaumu Beach and Otaihanga are the last
suburbs in. Waikanae and everything north is excluded by suburb, and `north_limit_lat` follows
the Waikanae River, which also catches Ōtaki houses that the site tags "Paraparaumu".

### Adding one listing by hand

The ➕ button beside the search box adds a property to *your browser only* (localStorage).
Give it an address or a realestate.co.nz link and it:

1. finds the listing through realestate.co.nz's search API (`filter[address][]=<number and
   street>`, or `/listings/<id>` from a pasted link) and fills in price, listed date, beds,
   baths, floor and land area, photo, and the renovation / double-glazing scan. Anything
   typed into the form wins over what it finds;
2. runs every council hazard layer against the point;
3. works out the nearest schools, cafés and supermarkets from the coordinates;
4. pulls decade built, construction, council valuation and last sale from homes.co.nz.

Step 4 needs `python3 serve.py`: homes.co.nz sends no CORS headers, so a browser can't call
it and `serve.py` relays it at `/homes/…`. On the published GitHub Pages site there is no
relay, so those fields stay blank for hand-added properties (crawled ones are unaffected —
the crawler fetches them server-side). BRANZ wind / corrosion zones are also crawler-only.

Matching is strict: street number (with any unit or letter) and street name must match
exactly, since both sites' address searches are fuzzy; "St" for "Street" is fine. A listing
the crawler already tracks is opened rather than added twice.

---

## Where the data comes from

### 1. The listings — realestate.co.nz search API

Discovery uses realestate.co.nz's own (unauthenticated) search API — the JSON that its
site hydrates from — so there is no HTML scraping and nothing brittle to break:

```
GET https://platform.realestate.co.nz/search/v1/listings
    ?filter[category][]=res_sale         # residential, for sale
    &filter[district][]=271              # Nelson  (Tasman is a different id)
    &filter[propertyType][]=1            # house
    &page[limit]=100&page[offset]=0      # meta.totalResults drives paging
```

Each `data[]` item's `attributes` already carry everything we filter and display —
`bedroom-count`, `floor-area`, `land-area`, `price-display`, `published-date`,
`description`, `photos` (the first one becomes the row thumbnail) and
`address.latitude/longitude`. The hard filter (120 m²+ floor;
no bedroom or land prefilter) is applied in `crawl.py`; the coordinates feed the hazard queries directly,
so homes.co.nz's fuzzy address search never has to guess which house we mean.

Notes for anyone extending it:
- District ids: Nelson = **271**; Wellington City = **265**, Porirua = **268**, Kāpiti Coast
  = **261** (Lower Hutt 263 and Upper Hutt 266 are not crawled). The full list is at
  `https://platform.realestate.co.nz/search/v1/locations`.
- Trade Me is deliberately *not* used — its watchlist is
  login-gated, bot-detected (HTTP 406 to scripts) and its token expires ~weekly.
- The public site's HTML is served in inconsistent variants (sometimes SSR anchors,
  sometimes a hydration blob), which is exactly why the API is the stable source.

### 2. Property records — homes.co.nz

Trade Me sources its "Past sales & timeline" panel from homes.co.nz, so going to that
source directly is both easier and richer. Two unauthenticated endpoints:

```
GET https://gateway.homes.co.nz/address/search?Address=<address>
    -> Results[0].{ Title, Lat, Long, PropertyID }

GET https://gateway.homes.co.nz/properties?property_ids=<uuid>
    -> cards[0].property_details.{ decade_built, building_construction,
                                   land_area, floor_area, num_bedrooms }
    -> cards[0].tm_ids   # Trade Me listing IDs — use these to confirm the match

GET https://gateway.homes.co.nz/property/<uuid>/timeline
    -> events[] with key "valuation" (capital / land / improvement value)
                     and "property_sale" (price, date)
```

The parameter is capitalised `Address`; lowercase `address` returns
`"Address: cannot be blank"`.

**Always check `tm_ids`.** Address search is fuzzy — "5 Orakei Street" first resolved to
"5A Orakei Street", a different house built fifty years later. The `tm_ids` field caught it.

### 3. Hazards — Nelson City Council ArcGIS

Public, unauthenticated, CORS-enabled feature services. Point-in-polygon:

```
GET https://services1.arcgis.com/Y4k7lyf2XTGeQC6V/arcgis/rest/services/
      <SERVICE>/FeatureServer/<LAYER>/query
      ?geometry={"x":<lon>,"y":<lat>,"spatialReference":{"wkid":4326}}
      &geometryType=esriGeometryPoint&inSR=4326
      &spatialRel=esriSpatialRelIntersects&outFields=*&returnGeometry=false&f=json
```

An empty `features` array means the point is outside that hazard polygon.

The 23 layers queried (see `HAZARD_LAYERS` in `build.py` for the severity mapping):

| Service | Layers | Key attributes |
|---|---|---|
| `River_Flooding_Depth_2020` | 4,5,6,7,8 (catchments) | `Depth`, `AnnualExceedanceProbability`, `RiverName` |
| `River_Flooding_Depth_2130` | 0,1,2,3,4 | `Depth`, `Year` |
| `Coastal_Flooding_Depth` | 7 (present), 10 (2130 SSP5-8.5M) | `Depth`, `Scenario`, `InundationCellName` |
| `Wave_Runup` | 2, 20 | `Timeframe`, `SSP`, `AEP` |
| `Coastal_Erosion_Hazards` | 1 (current), 7 (2130) | `Year`, `ClimateScenario` |
| `Natural_Hazards_Liquefaction` | 0 (Tāhunanui Level B), 1 (regional Level A) | `LiquefactionCategory` |
| `Slope_Instability` | 3,2,1,0 (Tier I–III, run-out) | `TechnicalReportLayerName` |
| `Fault_Deformation_Overlay` | 59 | `FaultOverlayName` |

Catchment layer IDs are **not** contiguous and differ between the 2020 and 2130 services —
enumerate them from `<SERVICE>/FeatureServer?f=json` rather than assuming.

Full polygon geometry is available but large (~590 KB for one simplified catchment), which
is why the app ships precomputed per-property results rather than drawing the flood extents.

### 4. Points of interest — OpenStreetMap

Overpass API, `POST https://overpass-api.de/api/interpreter` (`fetch_osm.py` for Wellington;
Nelson's is the original `poi_raw.txt` capture). In Wellington, cafés count towards "Nearest
café" but are left out of the "A specific place" list — there are several hundred. Schools are classified by
name, since NZ schools rarely carry `isced:level`: "College" → secondary, "Intermediate" →
intermediate, otherwise primary. Exceptions are hardcoded in `build.py` (`FIXES`).

### 5. Wind + corrosion zones — BRANZ Map (vector tiles)

Each property's **wind zone** (NZS 3604: Low → Extra High) and **coastal corrosion zone**
(B / C / D) come from the [BRANZ Map](https://experience.arcgis.com/experience/6e83b0bb19d14a0db411aebdc301cf49).
BRANZ offers no public point-query API — its "Property Information" layer is access-restricted,
and the zones are published only as **display vector tiles**. So `crawl.py` reads them the hard way:

- The tiles use a **NZTM (EPSG:2193)** tiling scheme (origin `-4020900, 19998100`, 512px tiles),
  not Web Mercator — so the point is projected to 2193 and the tile row/col computed from the
  service's own LOD resolutions (`?f=json` → `tileInfo`).
- Tiles are **gzip-encoded** `.pbf`; decode with `gzip` then `mapbox_vector_tile`.
- Each feature carries only a `_symbol` index; the label comes from the layer's
  `resources/styles/root.json` (e.g. Wind `_symbol` 0–5 → Low…Extra High; Corrosion 0–2 → Zone B/C/D).
- The zones tile all land contiguously, so the property's zone is the polygon that **contains**
  the point, or the **nearest** polygon if simplification leaves a gap. A coarse level (12) means a
  handful of cached tiles cover all of Nelson.

This is the only part of the pipeline needing third-party libraries (`requirements.txt`:
`mapbox-vector-tile`, `shapely`, `pyproj`). If they're absent the crawl still runs and leaves the
zones blank.

---

## Regions

A region is one entry in `REGIONS` in `crawl.py` (which districts to crawl, where to cut
off) plus one in `REGIONS` in `app_template.html` (title, map labels, personal places).
Every property and POI in `data.json` carries a `region`; records without one are Nelson.
The viewer shows one region at a time and remembers the last one chosen; `#wellington` /
`#nelson` on the URL selects one directly. Pruning is per region: listings are only dropped
when that region's own discovery ran to completion, so a failed Wellington crawl cannot empty
Wellington (or Nelson).

To add a region:

1. Add it to both `REGIONS` tables and to `BBOX` in `fetch_osm.py`.
2. `python3 fetch_osm.py <region>` for the basemap and POIs. (overpass-api.de answers
   HTTP 406 to script-looking requests; the script sends overpass-turbo's browser headers.)
3. `CRAWL_MAX_NEW=1000 python3 crawl.py` once locally to backfill, rather than waiting for
   the daily cap of 40.

### Wellington hazards

Nelson's hazard coverage stops at the Nelson City Council boundary — 139 Barnett Avenue is
in Tasman District and correctly reports "no council data" rather than "no hazards".

Wellington uses four public ArcGIS servers (all CORS-open). The layer table is
`WGTN_LAYERS` in `crawl.py`; it is written into `data.json` as `hazardRegistry` and the
viewer's "Add a property" reads it from there, so there is one copy.

| Publisher | Server | Layers used |
|---|---|---|
| Greater Wellington (every listing) | `mapping.gw.govt.nz/.../GW/Emergencies_P/MapServer` | 10 liquefaction potential, 11 earthquake-induced slope failure, 23 tsunami evacuation zones |
| Wellington City | `gis.wcc.govt.nz/.../2024DistrictPlan/2024DistrictPlan/MapServer` | 56–59 fault overlays, 61–63 flood (inundation / overland flowpath / stream corridor), 50 and 49 coastal inundation (present, +1.43 m) |
| Porirua | `services1.arcgis.com/kIwRm3UCyRHYbM8a` (`*_PDP2025` services) | fault rupture zone, flood (inundation / overland flow / stream corridor), coastal inundation and erosion (present, +1 m) |
| Kāpiti Coast | `maps.kapiticoast.govt.nz/server/rest/services/Public` | `District_Plan_Overlays` 10 fault avoidance areas and 11 flood hazard (by `ZONE`), `Coastal_Erosion` 30 (2120 shoreline range) |

Scoring, on the same 0–4 scale as Nelson:

- **Fault zones** — 4, every named fault.
- **Flood** — stream / river corridor 4, overland flow / overflow path 3, inundation /
  ponding 2. These are district-plan hazard *areas*; there are no depth bands as in Nelson.
- **Coastal inundation / erosion** — present day 4, sea-level-rise scenarios 2 (shown as
  projections, like Nelson's 2130 layers).
- **Liquefaction** — High / Very High 3, Moderate 2, Low 0.
- **Slope failure** — regional severity 5 or 4 scores 3, severity 3 scores 2, 1–2 score 0.
- **Tsunami evacuation zone** — shown as a chip (red / orange / yellow), never scored.

Known gaps: Kāpiti's present-day coastal erosion hazard is published as a line, so it can't
be point-tested; Wellington City's and Porirua's own tsunami overlays are not used (the
regional evacuation zones cover all three councils consistently).

Listings are matched to a council by the town on their address, and only Greater
Wellington's layers plus that council's are queried. `HAZ_V` in `crawl.py` versions the
registry: bump it and the next run re-queries every Wellington listing.

### Hazards for a new region

To wire one up:

1. Find the council's ArcGIS org id or server. For ArcGIS Online, the Experience Builder
   app id from the public viewer URL gives it:
   `https://www.arcgis.com/sharing/rest/content/items/<appId>?f=json` returns `orgId`, and
   `https://services1.arcgis.com/<orgId>/arcgis/rest/services?f=json` lists every service.
2. Enumerate each service's layers and fields.
3. Add the layers to the registry with a severity rank, matching the existing scale:
   0 = present but benign, 2 = worth checking, 3–4 = significant. Nelson's registry is
   mirrored by hand in `app_template.html` for "Add a property"; Wellington's is not (see above).

---

## Rebuilding

```bash
python3 build.py     # raw captures -> data.json
```

Then inject into the template:

```python
import json
tpl  = open('app_template.html', encoding='utf-8').read()
data = json.load(open('data.json', encoding='utf-8'))
for p in data['properties']:
    p.pop('img', None); p.pop('constrCode', None); p.pop('councilAddress', None)
geo = open('geo.json', encoding='utf-8').read().strip()
out = (tpl.replace('__SRC__', data['sourceUrl'])
          .replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))
          .replace('__GEO__', geo.replace('</', '<\\/')))
open('nelson_shortlist.html', 'w', encoding='utf-8').write(out)
```

---

## Caveats

- Trade Me plots listings to the street address, so a point can sit a few metres off its
  true position. On a hazard boundary, check the council viewer before concluding anything.
- Distances are straight-line, not walking or driving time.
- 2130 layers are projections under a stated climate scenario, not forecasts.
- Outside every hazard polygon means outside *these* polygons. It is not a geotech report,
  and says nothing about overland flow or stormwater capacity.
- Listing thumbnails are hotlinked from realestate.co.nz's image server, not embedded. If a
  photo is missing or stops loading, the row shows no image.

## Verification performed

The flood layer discriminates between sites rather than blanketing the valley:

| Point | Result |
|---|---|
| 221 Nile Street East | `>1 - 2m @ 1AEP` |
| ~120 m north of it | outside flood extent |
| ~250 m south, uphill | outside flood extent |
| 16 Gorrie Street | `>20 - 50cm @ 1AEP` |
| ~200 m west of it | outside flood extent |
| 38 Mount Street | outside flood extent |
