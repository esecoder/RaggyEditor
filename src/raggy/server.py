#!/usr/bin/env python3
"""
server.py — the sidecar the editor talks to.

===============================================================================
WHY A SIDECAR, AND WHY IT IS NOT FASTAPI
===============================================================================
The editor surface (CudaText, or the browser UI in `webui/`) must stay light: it
should not need numpy, a transformer, or a build step to open a text file. The
RAG engine does have those needs. Splitting them lets each be simple.

⚠️ This uses `http.server` from the standard library on purpose. FastAPI +
uvicorn is the nicer production answer and is documented in the README as the
swap; but requiring them would mean the app does not start until a web
framework is installed, and "it runs with numpy" is worth more here than
"it uses the fashionable framework". The interface is plain JSON over HTTP, so
swapping the server changes nothing above it.

    GET  /                 the web editor UI
    GET  /api/health       {ok, engine status}
    POST /api/index        {text, doc_name?, strategy?, target_chars?, overlap?}
    POST /api/search       {query, k?, mode?}
    POST /api/regex        {pattern, case_sensitive?, regex?}
    POST /api/ask          {question, k?, mode?}
    POST /api/document     -> current document text (for the editor to load)

Run:
    ./run.sh serve              # from the repo root
    python -m raggy.server --port 8791 --open
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from raggy import __version__
from raggy.engine import RaggyEngine

WEBUI_DIR = pathlib.Path(__file__).resolve().parents[2] / "webui"
MAX_BODY = 64 * 1024 * 1024            # 64 MB: a large text document has to fit

# One open document at a time — that is what a text editor *is*. The lock keeps
# concurrent requests (e.g. an index while a search is in flight) from tearing
# the index.
_ENGINE = RaggyEngine()
_LOCK = threading.Lock()
_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def _handle_api(path: str, body: dict) -> tuple[int, dict]:
    """Route one API call. Returns (http_status, payload)."""
    with _LOCK:
        if path == "/api/health":
            return 200, {"ok": True, "version": __version__, **(_ENGINE.status())}

        if path == "/api/index":
            text = body.get("text")
            if not isinstance(text, str):
                return 400, {"error": "body must include a string 'text'"}
            _ENGINE.strategy = body.get("strategy", _ENGINE.strategy)
            _ENGINE.target_chars = int(body.get("target_chars", _ENGINE.target_chars))
            _ENGINE.overlap = int(body.get("overlap", _ENGINE.overlap))
            if "threshold" in body:
                _ENGINE.threshold = float(body["threshold"])
            info = _ENGINE.index(text, body.get("doc_name", "untitled"))
            return 200, {"ok": True, **info, "status": _ENGINE.status()}

        if path == "/api/update":
            # Incremental re-index of the already-open document. Kept distinct
            # from /api/index so a client can say "this is an edit" and get the
            # reuse stats without implying a fresh document.
            text = body.get("text")
            if not isinstance(text, str):
                return 400, {"error": "body must include a string 'text'"}
            info = _ENGINE.update(text) if _ENGINE.is_indexed else _ENGINE.index(text)
            return 200, {"ok": True, **info, "status": _ENGINE.status()}

        if path == "/api/search":
            q = body.get("query", "")
            if not q:
                return 400, {"error": "missing 'query'"}
            return 200, {"ok": True, **_ENGINE.semantic_search(
                q, k=int(body.get("k", 5)), mode=body.get("mode", "hybrid"))}

        if path == "/api/regex":
            pat = body.get("pattern", "")
            return 200, {"ok": True, **_ENGINE.regex_search(
                pat, case_sensitive=bool(body.get("case_sensitive", False)),
                regex=bool(body.get("regex", True)),
                max_hits=int(body.get("max_hits", 500)))}

        if path == "/api/ask":
            q = body.get("question", "")
            if not q:
                return 400, {"error": "missing 'question'"}
            try:
                return 200, {"ok": True, **_ENGINE.ask(
                    q, k=int(body.get("k", 5)), mode=body.get("mode", "hybrid"))}
            except Exception as e:                              # noqa: BLE001
                # A failed generation must not take the search UI down with it.
                return 502, {"ok": False, "error": str(e),
                             "hint": "search and highlighting still work without a key"}

        if path == "/api/document":
            return 200, {"ok": True, "doc_name": _ENGINE.doc_name, "text": _ENGINE.text}

        if path == "/api/sample":
            sample = WEBUI_DIR.parent / "samples" / "meridian-operations-handbook.txt"
            if not sample.is_file():
                return 404, {"error": "sample document is missing"}
            return 200, {"ok": True, "doc_name": sample.name,
                         "text": sample.read_text(encoding="utf-8")}

    return 404, {"error": f"no such endpoint: {path}"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"RaggyEditor/{__version__}"

    def log_message(self, fmt, *args):        # quieter, single-line logs
        print(f"  {self.address_string()} {fmt % args}")

    # ---- helpers ----------------------------------------------------------
    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, rel: str) -> None:
        # Serve only inside webui/: prevent ../ traversal.
        target = (WEBUI_DIR / rel).resolve()
        if WEBUI_DIR.resolve() not in target.parents and target != WEBUI_DIR.resolve():
            self._send(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send(404, {"error": "not found"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---- routes -----------------------------------------------------------
    def do_GET(self):                                          # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path.startswith("/api/"):
            status, payload = _handle_api(path, {})
            self._send(status, payload)
        else:
            self._send_file(path.lstrip("/"))

    def do_POST(self):                                         # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        status, payload = _handle_api(path, self._read_json())
        self._send(status, payload)


def main() -> None:
    ap = argparse.ArgumentParser(description="RaggyEditor sidecar")
    ap.add_argument("--host", default=os.environ.get("RAGGY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("RAGGY_PORT", "8791")))
    ap.add_argument("--open", action="store_true", help="open the editor UI in a browser")
    ap.add_argument("--encoder", default=None, choices=["auto", "neural", "lsa"],
                    help="override RAGGY_ENCODER")
    args = ap.parse_args()

    if args.encoder:
        os.environ["RAGGY_ENCODER"] = args.encoder

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"RaggyEditor {__version__} sidecar listening on {url}")
    print(f"  encoder: {os.environ.get('RAGGY_ENCODER', 'auto')}   "
          f"LLM key: {'set' if os.environ.get('OPENAI_API_KEY') else 'not set (search-only)'}")
    print("  Ctrl-C to stop.")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
