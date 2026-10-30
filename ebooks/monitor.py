#!/usr/bin/env python3
"""Display extraction progress from the NDJSON output and, optionally, Antfly indexing progress."""

import argparse
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML = b"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rome ebook pipeline</title><style>
body{font:16px system-ui,sans-serif;color:#18212c;background:#f4f6f8;max-width:1000px;margin:3rem auto;padding:0 1.25rem}
h1{margin-bottom:.3rem}p{color:#52606e}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1rem;margin:1.5rem 0}
.card{background:white;border:1px solid #dce2e8;border-radius:10px;padding:1.1rem}
.number{font-size:2rem;font-weight:700;margin:.4rem 0}
.bar{height:10px;background:#dce2e8;border-radius:5px;overflow:hidden;margin-top:.5rem}.bar>div{height:100%;background:#2f7dd1;width:0}
table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;padding:.7rem;border-bottom:1px solid #e1e6eb}
td:last-child,th:last-child{text-align:right}#status{font-weight:600}
</style></head><body>
<h1>Rome ebook pipeline</h1><p id="status">Connecting...</p>
<h2>Extraction</h2>
<div class="cards">
<div class="card">Chunks written<div class="number" id="chunks">&#8212;</div></div>
<div class="card">Sources with chunks<div class="number" id="sources">&#8212;</div></div>
<div class="card">Output size<div class="number" id="size">&#8212;</div></div>
<div class="card">Reported errors<div class="number" id="errors">&#8212;</div></div>
</div>
<p id="current"></p>
<h2>Antfly indexing</h2>
<p id="antfly-note"></p>
<div class="cards">
<div class="card">Documents stored<div class="number" id="stored">&#8212;</div></div>
<div class="card">Searchable vectors<div class="number" id="vectors">&#8212;</div>
<div class="bar"><div id="vectorbar"></div></div></div>
<div class="card">Index state<div class="number" id="state" style="font-size:1.3rem">&#8212;</div></div>
</div>
<h2>Extracted sources</h2>
<p>Counts show complete records written so far. Sources that fail or are skipped appear in the final report, not in this list.</p>
<table><thead><tr><th>Source</th><th>Chunks</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const n=v=>v===null||v===undefined?'\\u2014':v.toLocaleString();
async function update(){try{
 const r=await fetch('/api/progress',{cache:'no-store'});if(!r.ok)throw Error(r.status);const x=await r.json();
 document.getElementById('status').textContent=x.complete?'Extraction complete; see report for failures and duplicates.':'Extraction in progress; final report not yet available.';
 document.getElementById('chunks').textContent=n(x.chunks);
 document.getElementById('sources').textContent=n(x.sources);
 document.getElementById('size').textContent=(x.bytes/1048576).toFixed(1)+' MB';
 document.getElementById('errors').textContent=x.errors===null?'Pending':x.errors;
 document.getElementById('current').textContent=x.current?'Most recently written: '+x.current:'';
 const a=x.antfly;
 document.getElementById('antfly-note').textContent=a&&a.error?'Antfly unavailable: '+a.error:(a?'':'Antfly monitoring is not configured.');
 if(a&&!a.error){
  document.getElementById('stored').textContent=n(a.documents);
  document.getElementById('vectors').textContent=n(a.searchable_vectors);
  document.getElementById('state').textContent=a.state||'\\u2014';
  document.getElementById('vectorbar').style.width=(x.chunks?Math.min(100,100*a.searchable_vectors/x.chunks):0)+'%';
 }
 const table=document.getElementById('rows');
 table.replaceChildren(...x.files.map(f=>{const tr=document.createElement('tr'),c=document.createElement('td'),d=document.createElement('td');c.textContent=f.name;d.textContent=f.chunks.toLocaleString();tr.append(c,d);return tr;}));
}catch(e){document.getElementById('status').textContent='Dashboard connection lost: '+e}}
update();setInterval(update,2000);
</script></body></html>"""


class Progress:
    def __init__(self, output, report, antfly_url, table, index):
        self.output, self.report = output, report
        self.antfly_url, self.table, self.index = antfly_url, table, index
        self.offset = 0
        self.chunks = 0
        self.sources = {}
        self.current = None
        self.lock = threading.Lock()

    def antfly(self):
        if not self.antfly_url:
            return None
        base = (
            f"{self.antfly_url.rstrip('/')}/db/v1/tables/{urllib.parse.quote(self.table, safe='')}"
            f"/indexes/"
        )
        try:

            def status(name):
                with urllib.request.urlopen(
                    base + urllib.parse.quote(name, safe=""), timeout=5
                ) as response:
                    return json.load(response)["status"]

            dense, text = status(self.index), status("full_text_index_v0")
            coverage = dense.get("source_coverage") or {}
            runtime = dense.get("enrichment_runtime") or {}
            return {
                "documents": text.get("doc_count"),
                "searchable_vectors": dense.get("searchable_vectors"),
                "failed": coverage.get("failed"),
                "retrying": bool(runtime.get("retrying")),
                "state": "retrying (rate limited?)"
                if runtime.get("retrying")
                else ("embedding" if dense.get("backfill_active") else "idle"),
            }
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
        ) as exc:
            return {"error": str(exc)}

    def snapshot(self):
        with self.lock:
            try:
                size = self.output.stat().st_size
                if size < self.offset:
                    self.offset, self.chunks, self.sources, self.current = (
                        0,
                        0,
                        {},
                        None,
                    )
                with self.output.open("rb") as stream:
                    stream.seek(self.offset)
                    while True:
                        start = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            stream.seek(start)
                            break
                        record = json.loads(line)
                        name = record["source_relative_path"]
                        self.sources[name] = self.sources.get(name, 0) + 1
                        self.current = name
                        self.chunks += 1
                    self.offset = stream.tell()
            except FileNotFoundError:
                size = 0
            complete, errors = False, None
            try:
                report = json.loads(self.report.read_text(encoding="utf-8"))
                complete, errors = True, report["errors"]
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            return {
                "chunks": self.chunks,
                "sources": len(self.sources),
                "bytes": size,
                "current": self.current,
                "complete": complete,
                "errors": errors,
                "antfly": self.antfly(),
                "files": [
                    {"name": k, "chunks": v}
                    for k, v in reversed(list(self.sources.items()))
                ],
            }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--port", type=int, default=18762)
    p.add_argument("--antfly-url", help="Antfly base URL; enables indexing progress")
    p.add_argument("--table", default="rome")
    p.add_argument("--index", default="body_dense")
    args = p.parse_args()
    progress = Progress(
        args.output, args.report, args.antfly_url, args.table, args.index
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body, mime = HTML, "text/html; charset=utf-8"
            elif self.path == "/api/progress":
                body, mime = (
                    json.dumps(progress.snapshot()).encode(),
                    "application/json",
                )
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Ebook progress: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
