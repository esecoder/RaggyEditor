# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for RaggyEditor.

Design decisions worth knowing:

* EXCLUDES. The whole point of the ONNX path is that torch is not shipped.
  `sentence-transformers`, `transformers` and `torch` are excluded EXPLICITLY
  rather than relied on to be absent — if they happen to be installed in the
  build environment, PyInstaller would happily bundle several hundred megabytes
  of them.

* The model is NOT bundled. Weights are fetched on first use into a user cache,
  so the installer stays small and works offline immediately via the LSA
  fallback.

* `defaults` keeps Qt from dragging in WebEngine. QScintilla is the only Qt
  extra we need, and it must be listed as a hidden import because it is a
  compiled module that is imported indirectly.
"""

import pathlib
import sys

ROOT = pathlib.Path(SPECPATH).resolve().parent          # noqa: F821 (PyInstaller injects SPECPATH)

datas = [
    (str(ROOT / "samples"), "samples"),
    (str(ROOT / "LICENSE"), "."),
]

excludes = [
    # The torch stack — the thing ONNX exists to avoid.
    "torch", "torchvision", "torchaudio", "transformers", "sentence_transformers",
    "safetensors", "huggingface_hub", "datasets",
    # Scientific/plotting extras that leak in through transitive imports.
    "matplotlib", "scipy", "pandas", "sympy", "notebook", "IPython",
    # GUI toolkit we do not use.
    "tkinter", "PyQt5", "PySide6",
    # Server path not used by the desktop app.
    "uvicorn", "fastapi", "starlette",
]

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["PyQt6.Qsci", "certifi"],
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)                                       # noqa: F821

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RaggyEditor",
    debug=False,
    strip=False,
    upx=False,
    console=False,                                      # GUI app: no terminal window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RaggyEditor",
)

if sys.platform == "darwin":
    # ⚠️ Without CFBundleDocumentTypes macOS does not know RaggyEditor can open a
    # text file. Double-clicking a .txt does nothing, and the app is missing from
    # Finder's "Open With" — which for a downloadable editor is the difference
    # between a real Mac app and a program you have to launch first.
    #
    # macOS hands an opened document to the app as argv[1]; raggy.app:main already
    # takes a path there, so this declaration is the whole wiring.
    _TEXT_EXTENSIONS = ["txt", "text", "md", "markdown", "log", "csv", "tsv", "json"]
    app = BUNDLE(
        coll,
        name="RaggyEditor.app",
        icon=None,
        bundle_identifier="org.raggyeditor.app",
        info_plist={
            "CFBundleName": "RaggyEditor",
            "CFBundleDisplayName": "RaggyEditor",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Plain Text Document",
                    "CFBundleTypeRole": "Editor",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": ["public.plain-text", "public.utf8-plain-text"],
                    "CFBundleTypeExtensions": _TEXT_EXTENSIONS,
                },
            ],
            # Being able to "Open With" the app is the point of the above.
            "CFBundleTypeRole": "Editor",
        },
    )
