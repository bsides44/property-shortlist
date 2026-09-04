# Nelson Shortlist — property watchlist hazard tool

A prototype that takes a Trade Me property watchlist and answers four questions about
every listing in it: where is it, is it in a hazard zone, how old is it, and how far is
it from the things you care about.

Built against one real watchlist (the "Nelson" collection, 23 listings, snapshot
4 September 2026).

Open `nelson_shortlist.html` in any browser. It is fully self-contained — all data and
map geometry are embedded, no server and no network needed.

---

## Files

| File | What it is |
|---|---|
| `nelson_shortlist.html` | The built app. This is the deliverable. |
| `app_template.html` | App source. Contains `__DATA__`, `__GEO__` and `__SRC__` placeholders. |
| `build.py` | Merges the raw captures into `data.json`. |
| `data.json` | Merged properties + POIs, injected into the template. |
| `geo.json` | Coastline / river / arterial geometry for the map, delta-encoded. |
| `listings_raw.txt` | Trade Me watchlist cards: address, price, beds, baths, areas, listing date. |
| `homes_raw.txt` | homes.co.nz records: decade built, construction, council valuations, last sale, coordinates. |
| `hazards_raw.txt` | Nelson City Council hazard layer hits, one line per property. |
| `poi_raw.txt` | OpenStreetMap schools, cafés, supermarkets, kindergartens. |
| `coords.txt` | Coordinates scraped from Trade Me listing pages (superseded by `homes_raw.txt`). |

Rebuild after editing any raw file:

```bash
python3 build.py                 # writes data.json
# then re-inject into the template (see "Rebuilding" below)
```

---

## Where the data comes from

### 1. The watchlist — the fragile part

A shared watchlist link is a JWT-authenticated page on `trademe.co.nz`. It cannot be
fetched server-side: Trade Me is a client-rendered Angular app with bot detection that
returns **HTTP 406** to automated requests. The token also expires after about 7 days.

What worked: opening the share link in a logged-in browser session, which redirects to
`/a/my-trade-me/watchlist/<uuid>` — the `uuid` is the `sub` claim in the JWT. Listing IDs
come from `a[href*="/a/listing/"]` on that page.

**Go gently.** Loading listing pages in a rapid loop of hidden iframes got this browser
profile blocked within a few minutes. One page every 15–20 seconds is the sustainable rate.

Alternatives, if you want something less brittle:

- **Trade Me's official API** — `GET /v1/mytrademe/watchlist/{filter}.json`, OAuth, needs a
  developer key. Sanctioned and stable, but returns the whole watchlist as one list with no
  named collections, so "Nelson" and "Porirua" arrive indistinguishable.
- **Paste listing IDs manually.** Nothing to break.

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
