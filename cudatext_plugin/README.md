# RaggyEditor — CudaText plugin

This folder is the editor integration: it turns CudaText's **Find** into
semantic search and cited question-answering over the document you have open.

It is a **thin client**. It reads the editor buffer, POSTs it to the RaggyEditor
sidecar on localhost, and shows the results. All the retrieval lives in
`src/raggy/`; none of it runs inside CudaText's embedded Python (which has no
numpy and no torch).

## Install

1. Start the engine once so you know it works:

   ```bash
   cd /path/to/RaggyEditor
   ./run.sh install     # creates .venv with numpy
   ./run.sh demo        # offline semantic search over the sample handbook
   ```

2. Copy this folder into CudaText's plugin directory, keeping the name
   `raggy_editor`:

   | OS | Plugins folder |
   |---|---|
   | macOS | `~/Library/Application Support/CudaText/py/` |
   | Linux | `~/.config/cudatext/py/` |
   | Windows | `%APPDATA%\CudaText\py\` |

   Or, in CudaText: **Plugins → Addons Manager → Install from folder** and pick
   this `raggy_editor` folder.

3. Restart CudaText. You now have a **RaggyEditor** submenu under **Plugins**.

4. Open any text file, then **Plugins → RaggyEditor → Start sidecar**. The first
   time it asks for:
   - the **RaggyEditor folder** (the one containing `run.sh`), and
   - the **Python that has numpy** — normally `<that folder>/.venv/bin/python`.

   It saves both to `raggy_editor.ini` next to CudaText's settings, so you only
   answer once.

## The commands

| Command | What it does |
|---|---|
| **Semantic Find…** | Type a description ("how do I stop it falling over"); get a ranked list of passages. Pick one and the editor selects the exact span. |
| **Regex Find…** | The ordinary character match, but the results come back in the same list shape, so you can compare the two on one query. |
| **Ask the document…** | A grounded answer with `[n]` citations, or an honest "not in this document". Needs `OPENAI_API_KEY` (see the project `.env.example`); without it you get the ranked passages instead of a made-up answer. |
| **Start sidecar** | Launches the local engine in the background. |
| **Status / settings** | Which encoder is live (neural vs the offline LSA fallback), how many passages, whether answers are enabled. |

## Hotkeys

`Ctrl+Alt+F` semantic find, `Ctrl+Alt+A` ask. Rebind in CudaText's
**Options → Hotkeys** by searching for "RaggyEditor".

## Troubleshooting

- **"Sidecar is not running"** → run **Plugins → RaggyEditor → Start sidecar**,
  or `./run.sh serve` in a terminal. Check **Status / settings**.
- **Encoder says `lsa-tfidf-svd`** → that is the offline fallback. For the real
  encoder run `./run.sh install-neural` once (uses the local `bge-small` model).
- **Answers say "No API key is set"** → copy `.env.example` to `.env`, add your
  key, restart the sidecar. DeepSeek works: it is OpenAI-compatible for chat.
