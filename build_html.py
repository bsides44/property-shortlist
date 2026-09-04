#!/usr/bin/env python3
"""Inject data.json + geo.json into app_template.html and write the self-contained
viewer. Produces both nelson_shortlist.html (the named deliverable) and index.html
(so GitHub Pages serves it at the site root)."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))

def build():
    tpl = open(os.path.join(HERE, "app_template.html"), encoding="utf-8").read()
    data = json.load(open(os.path.join(HERE, "data.json"), encoding="utf-8"))
    # fields the viewer never reads are dropped to keep the payload small
    for p in data.get("properties", []):
        for k in ("img", "constrCode", "councilAddress"):
            p.pop(k, None)
    geo = open(os.path.join(HERE, "geo.json"), encoding="utf-8").read().strip()
    out = (tpl.replace("__SRC__", data.get("sourceUrl", ""))
              .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
              .replace("__GEO__", geo.replace("</", "<\\/")))
    for name in ("nelson_shortlist.html", "index.html"):
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
            f.write(out)
    print("built nelson_shortlist.html + index.html:", len(out), "bytes,",
          len(data.get("properties", [])), "properties")

if __name__ == "__main__":
    build()
