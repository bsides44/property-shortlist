#!/usr/bin/env python3
"""Local server for the Nelson Shortlist app.

Serves the built viewer and relays three read-only homes.co.nz lookups for the
"Add a property" box. homes.co.nz sends no CORS headers, so a browser can't call it
directly; everything else the app fetches (realestate.co.nz, the council ArcGIS
services, Nominatim) is CORS-open and goes straight from the page.

    python3 serve.py            # then open http://localhost:8000
    python3 serve.py 8080       # another port
"""
import os, re, sys, urllib.error, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
HOMES = "https://gateway.homes.co.nz/"
# the only upstream paths that are relayed (see README "Property records")
ALLOWED = re.compile(r"^(address/search|properties|property/[0-9a-f-]{36}/timeline)$")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        if self.path.startswith("/homes/"):
            return self.relay()
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def guess_type(self, path):
        t = super().guess_type(path)
        return t + "; charset=utf-8" if t.startswith("text/") else t

    def relay(self):
        path, _, query = self.path[len("/homes/"):].partition("?")
        if not ALLOWED.match(path):
            return self.send_error(404)
        req = urllib.request.Request(HOMES + path + ("?" + query if query else ""),
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        except Exception as e:
            return self.send_error(502, str(e))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print("Nelson Shortlist: http://localhost:%d" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
