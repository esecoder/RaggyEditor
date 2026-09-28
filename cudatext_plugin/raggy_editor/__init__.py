"""
RaggyEditor — CudaText plugin.

Adds three commands to CudaText's Plugins menu (under "RaggyEditor"):

    Semantic Find...     find passages by MEANING, not by characters
    Ask the document...  a grounded answer with citations, or an honest refusal
    Status / settings    which encoder is live, where the sidecar is

===============================================================================
WHY A SIDECAR AND NOT CODE IN HERE
===============================================================================
CudaText's Python is an embedded interpreter. It very likely does not have
numpy, and it certainly does not have torch. The retrieval stack needs at least
numpy. So the plugin is a THIN CLIENT: it reads the editor buffer, POSTs it to
the RaggyEditor sidecar over localhost, and renders the JSON. Installing or
upgrading the RAG engine never risks the editor's own Python.

⚠️ That also means the sidecar must be running. `RaggyEditor: Start sidecar`
launches it with the interpreter you configure (`python_path` in the settings
file), which should be the project's `.venv/bin/python`.

This file is written against the real CudaText plugin API: commands are methods
of a `Command` class and are referenced by `method=` in install.inf.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import cudatext as app
    from cudatext import ed
    IN_CUDATEXT = True
except Exception:                                   # pragma: no cover - only outside CudaText
    app = None
    ed = None
    IN_CUDATEXT = False

PLUGIN_NAME = "RaggyEditor"
DEFAULT_URL = "http://127.0.0.1:8791"


# =============================================================================
# SETTINGS — a small ini in CudaText's settings dir
# =============================================================================
def _settings_path():
    if IN_CUDATEXT:
        try:
            base = app.app_path(app.APP_DIR_SETTINGS)
        except Exception:
            base = os.path.expanduser("~")
    else:
        base = os.path.expanduser("~")
    return os.path.join(base, "raggy_editor.ini")


def load_settings():
    path = _settings_path()
    data = {"url": DEFAULT_URL, "home": "", "python_path": ""}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    return data


def save_settings(data):
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write("# RaggyEditor plugin settings\n")
        for k in ("url", "home", "python_path"):
            f.write("{0}={1}\n".format(k, data.get(k, "")))


# =============================================================================
# HTTP CLIENT — standard library, so nothing to install in CudaText
# =============================================================================
def _post(url, path, payload, timeout=120):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url, path, timeout=10):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def server_alive(url):
    try:
        _get(url, "/api/health", timeout=3)
        return True
    except Exception:
        return False


# =============================================================================
# OFFSETS -> CudaText (x, y) coordinates
# =============================================================================
def offset_to_xy(text, offset):
    """Character offset -> (col, line), both 0-based, as CudaText wants."""
    line = text.count("\n", 0, offset)
    line_start = text.rfind("\n", 0, offset) + 1
    return offset - line_start, line


def select_range(text, start, end):
    """Put the editor selection on [start, end) and scroll it into view."""
    if not IN_CUDATEXT:
        return
    x1, y1 = offset_to_xy(text, start)
    x2, y2 = offset_to_xy(text, end)
    try:
        ed.set_caret(x1, y1, x2, y2)
    except Exception:
        # Older/newer API surface: fall back rather than fail the command.
        try:
            ed.set_caret(x1, y1)
        except Exception:
            pass
    try:
        ed.set_sel_line(y1)
    except Exception:
        pass


# =============================================================================
# PLUGIN HELPERS
# =============================================================================
def _info(title, text):
    if IN_CUDATEXT:
        app.msg_box(text, title)
    else:
        print("[{0}] {1}".format(title, text))


def _ask(title, prompt, default=""):
    if IN_CUDATEXT:
        return app.dlg_input(title, prompt, default)
    return default


def _dialog_list(caption, items):
    if IN_CUDATEXT:
        return app.dlg_menu(app.DMENU_LIST, items, caption=caption)
    for i, it in enumerate(items):
        print("  [{0}] {1}".format(i, it))
    return None


class Command:

    # ---------------------------------------------------------------- status
    def cmd_status(self):
        s = load_settings()
        url = s["url"]
        if not server_alive(url):
            _info(PLUGIN_NAME, "Sidecar is NOT running at {0}\n\n"
                  "Use 'RaggyEditor\\Start sidecar' or run ./run.sh serve "
                  "in the project folder.".format(url))
            return
        h = _get(url, "/api/health")
        lines = [
            "Sidecar:   {0}".format(url),
            "Indexed:   {0}".format("yes" if h.get("indexed") else "no document"),
            "Passages:  {0}".format(h.get("n_chunks")),
            "Chunking:  {0}".format(h.get("strategy")),
            "Encoder:   {0}".format(h.get("encoder")),
            "           {0}".format(h.get("encoder_note") or ""),
            "LLM:       {0}".format(h.get("llm_model") if h.get("llm_available")
                                    else "off (search-only; set OPENAI_API_KEY for answers)"),
            "",
            "Settings:  {0}".format(_settings_path()),
        ]
        _info(PLUGIN_NAME + " — status", "\n".join(lines))

    # ----------------------------------------------------------- start server
    def cmd_start_server(self):
        s = load_settings()
        if server_alive(s["url"]):
            _info(PLUGIN_NAME, "Sidecar is already running at {0}".format(s["url"]))
            return

        home = s.get("home") or ""
        if not home or not os.path.isdir(home):
            home = _ask(PLUGIN_NAME,
                        "Path to the RaggyEditor folder (the one containing run.sh):",
                        home) or ""
            home = home.strip().strip('"')
            if not home or not os.path.isfile(os.path.join(home, "run.sh")):
                _info(PLUGIN_NAME, "That folder has no run.sh — cannot start the sidecar.")
                return
            s["home"] = home

        py = s.get("python_path") or ""
        guess = os.path.join(home, ".venv", "bin", "python")
        if not os.path.isfile(guess):
            guess_win = os.path.join(home, ".venv", "Scripts", "python.exe")
            guess = guess_win if os.path.isfile(guess_win) else guess
        if not py or not os.path.isfile(py):
            py = guess
        if not os.path.isfile(py):
            py = _ask(PLUGIN_NAME,
                      "Python that has numpy (project's .venv python):", py) or ""
            if not os.path.isfile(py):
                _info(PLUGIN_NAME, "No usable interpreter. Run ./run.sh install first.")
                return
        s["python_path"] = py
        save_settings(s)

        port = s["url"].rsplit(":", 1)[-1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(home, "src") + os.pathsep + env.get("PYTHONPATH", "")
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = 0x00000008        # DETACHED_PROCESS
            subprocess.Popen([py, "-m", "raggy.server", "--port", str(port)],
                             cwd=home, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            _info(PLUGIN_NAME, "Failed to start: {0}".format(e))
            return

        for _ in range(40):
            time.sleep(0.25)
            if server_alive(s["url"]):
                _info(PLUGIN_NAME, "Sidecar started at {0}".format(s["url"]))
                return
        _info(PLUGIN_NAME, "Started but not answering yet at {0}.\n"
              "Give the encoder a moment, then try Status again.".format(s["url"]))

    # -------------------------------------------------------------- indexing
    def _index_current(self, s):
        text = ed.get_text_all()
        name = os.path.basename(ed.get_filename() or "untitled")
        info = _post(s["url"], "/api/index", {"text": text, "doc_name": name})
        return text, info

    def _require_ready(self, s):
        """Return the document text, or None after telling the user why not."""
        if not server_alive(s["url"]):
            _info(PLUGIN_NAME, "Sidecar is not running at {0}.\n\n"
                  "Run 'RaggyEditor\\Start sidecar', or ./run.sh serve in the project."
                  .format(s["url"]))
            return None
        if not IN_CUDATEXT:
            return ""
        return ed.get_text_all()

    # ---------------------------------------------------------- semantic find
    def cmd_semantic_find(self):
        s = load_settings()
        text = self._require_ready(s)
        if text is None:
            return
        query = _ask(PLUGIN_NAME + " — semantic find",
                     "Describe what you are looking for (no need to match words):")
        if not query:
            return
        try:
            self._index_current(s)
            res = _post(s["url"], "/api/search", {"query": query, "k": 10})
        except Exception as e:
            _info(PLUGIN_NAME, "Search failed: {0}".format(e))
            return

        hits = res.get("results") or []
        if not hits:
            _info(PLUGIN_NAME, "No passages found.")
            return

        conf = res.get("confidence", 0.0)
        items = []
        for h in hits:
            snippet = " ".join(h["text"].split())[:70]
            items.append("#{0}  L{1}  conf {2:.2f}  {3}".format(
                h["rank"], h["line"], h.get("dense", 0.0) or 0.0, snippet))
        if res.get("low_confidence"):
            items.insert(0, "⚠ low confidence ({0:.2f}) — the words may not be in this document".format(conf))
            hits = [None] + hits

        choice = _dialog_list("{0} — {1} ({2})".format(PLUGIN_NAME, query, res.get("encoder", "")), items)
        if choice is None:
            return
        hit = hits[choice]
        if hit:
            select_range(text, hit["start"], hit["end"])

    # ------------------------------------------------------------- regex find
    def cmd_regex_find(self):
        s = load_settings()
        text = self._require_ready(s)
        if text is None:
            return
        pattern = _ask(PLUGIN_NAME + " — regex find", "Regular expression (RE2-ish):")
        if not pattern:
            return
        try:
            res = _post(s["url"], "/api/regex", {"pattern": pattern, "regex": True})
        except Exception as e:
            _info(PLUGIN_NAME, "Regex failed: {0}".format(e))
            return
        hits = res.get("results") or []
        if not hits:
            _info(PLUGIN_NAME, "No matches.")
            return
        items = ["L{0}: {1}".format(h["line"], h["context"].strip()[:90]) for h in hits]
        choice = _dialog_list("{0} — /{1}/ ({2} matches)".format(PLUGIN_NAME, pattern, len(hits)), items)
        if choice is not None:
            h = hits[choice]
            select_range(text, h["start"], h["end"])

    # -------------------------------------------------------------------- ask
    def cmd_ask(self):
        s = load_settings()
        text = self._require_ready(s)
        if text is None:
            return
        question = _ask(PLUGIN_NAME + " — ask the document", "Your question:")
        if not question:
            return
        try:
            self._index_current(s)
            res = _post(s["url"], "/api/ask", {"question": question, "k": 5})
        except Exception as e:
            _info(PLUGIN_NAME, "Ask failed: {0}".format(e))
            return

        mode = res.get("mode")
        if mode == "abstained":
            _info(PLUGIN_NAME, "Not answered.\n\n{0}.\n\n"
                  "RaggyEditor refuses rather than guessing — use Semantic Find "
                  "to see the closest passages.".format(res.get("reason", "")))
            return
        if mode == "retrieval_only":
            _info(PLUGIN_NAME, "No API key is set, so no answer was generated.\n\n"
                  "The passages are still ranked — use Semantic Find. To enable "
                  "answers, set OPENAI_API_KEY in the project's .env.")
            return

        answer = res.get("answer") or ""
        cites = res.get("citations") or []
        used = res.get("cited") or []
        lines = [answer, ""]
        if used:
            lines.append("Citations:")
            for i in used:
                c = cites[i]
                lines.append("  [{0}] line {1}: {2}".format(
                    i + 1, c["line"], " ".join(c["text"].split())[:60]))
        _info(PLUGIN_NAME + " — answer ({0})".format(res.get("model", "")), "\n".join(lines))

        # Offer to jump to the passages the answer actually used.
        if used:
            items = ["line {0}: {1}".format(cites[i]["line"],
                                            " ".join(cites[i]["text"].split())[:70]) for i in used]
            choice = _dialog_list("Jump to a cited passage", items)
            if choice is not None:
                c = cites[used[choice]]
                select_range(text, c["start"], c["end"])
