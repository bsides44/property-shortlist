# Nelson Shortlist — property hazard tool

A self-updating map of **Nelson houses for sale** that answers four questions about
every listing: where is it, is it in a hazard zone (flood / slope / liquefaction),
how old / renovated is it, and how far is it from the things you care about.

It tracks realestate.co.nz on its own. A daily [GitHub Action](.github/workflows/update.yml)
crawls the current Nelson listings that meet a hard filter (3+ bed, 120 m²+ floor,
250 m²+ land), enriches each with council hazard data, homes.co.nz age/valuation and a
double-glazing / renovation scan of the listing text, rebuilds the page and commits it.
New qualifying houses appear automatically; sold ones drop off.

Open `nelson_shortlist.html` (or the GitHub Pages `index.html`) in any browser. It is
fully self-contained — all data and map geometry are embedded, no server needed at view time.

---

## Files

| File | What it is |
|---|---|
| `crawl.py` | **The engine.** Discovers Nelson listings via realestate.co.nz's search API and enriches each (hazards + homes.co.nz + description scan) into `data.json`. |
| `build_html.py` | Injects `data.json` + `geo.json` into the template → `nelson_shortlist.html` + `index.html`. |
| `.github/workflows/update.yml` | Daily cron that runs the two scripts and commits the result. |
| `app_template.html` | App source. Contains `__DATA__`, `__GEO__` and `__SRC__` placeholders. |
| `nelson_shortlist.html` / `index.html` | The built, self-contained app (identical; `index.html` is for Pages). |
| `data.json` | Live properties + POIs. Rewritten by `crawl.py` each run. |
| `geo.json` | Coastline / river / arterial geometry for the map, delta-encoded. |
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
- **Politeness.** Discovery is 2–3 API calls. Only *new* listings are enriched, capped at
  `CRAWL_MAX_NEW` (40) per run — so a first run backfills 40 and the rest arrive over the
  next few days, then it's just whatever came on the market. Hazard queries run against
  the public council Esri service; homes.co.nz calls are spaced out.
- **First run.** `data.json` was seeded locally with the full backfill, so day one is
  already complete; the Action just maintains it.

### Publishing the page

To view the always-current page, turn on **GitHub Pages** (Settings → Pages → deploy from
branch, root) and open the `index.html` URL — the daily commit republishes it. If you'd
rather not host it, just `git pull` and open `nelson_shortlist.html` locally.

### Tuning

The filter and cadence live at the top of `crawl.py` (`MIN_BEDS`, `MIN_FLOOR`, `MIN_LAND`,
`RE_DISTRICT`, `MAX_NEW_PER_RUN`) and in the workflow's `cron`. A manual "add this one
listing" path isn't wired up, but a listing outside the filter can be included by loosening
these constants.

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
`description`, and `address.latitude/longitude`. The hard filter (3+ bed, 120 m²+ floor,
250 m²+ land) is applied in `crawl.py`; the coordinates feed the hazard queries directly,
so homes.co.nz's fuzzy address search never has to guess which house we mean.

Notes for anyone extending it:
- District ids: Nelson = **271**. Trade Me is deliberately *not* used — its watchlist is
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

Overpass API, `POST https://overpass-api.de/api/interpreter`. Schools are classified by
name, since NZ schools rarely carry `isced:level`: "College" → secondary, "Intermediate" →
intermediate, otherwise primary. Exceptions are hardcoded in `build.py` (`FIXES`).

---

## Adding another region

Coverage stops at the Nelson City Council boundary — 139 Barnett Avenue is in Tasman
District and correctly reports "no council data" rather than "no hazards".

To add a region:

1. Find the council's ArcGIS org id. The Experience Builder app id from the public viewer
   URL gives it: `https://www.arcgis.com/sharing/rest/content/items/<appId>?f=json` returns
   `orgId`, and `https://services1.arcgis.com/<orgId>/arcgis/rest/services?f=json` lists
   every service.
2. Enumerate each service's layers and fields.
3. Add the layers to the registry with a severity rank, matching the existing scale:
   0 = present but benign, 2 = worth checking, 3–4 = significant.

For the Wellington lists that means Greater Wellington Regional Council plus the relevant
city council; for Richmond and Best Island, Tasman District Council.

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
- Listing photos are referenced but not displayed — they live on Trade Me's CDN.

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
