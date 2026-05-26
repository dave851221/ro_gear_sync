# PyInstaller spec for RO_GearSync.
#
# Build with:
#
#     .venv\Scripts\pyinstaller --clean --noconfirm build_app.spec
#
# Produces ``dist\RO_GearSync\RO_GearSync.exe`` plus its support folder.
# Ship the whole ``dist\RO_GearSync`` directory together with the
# (user-editable) ``config.ini`` placed next to the .exe; ``data\`` and
# ``logs\`` get created on first launch.
#
# Why one-dir (not one-file): the rapidocr ONNX models are ~400 MB on
# disk; one-file would extract the whole bundle to %TEMP% on every
# launch, adding tens of seconds and trashing the user's temp space.
# One-dir keeps the models on disk and the .exe starts in <2 s.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# IMPORTANT — Tcl/Tk detection caveat
# -----------------------------------
# PyInstaller probes tkinter availability inside an isolated subprocess
# that does NOT inherit the caller's sys.path tweaks. On this Python
# install (C:\Software\Python\Python313) Tcl/Tk lives at non-standard
# paths, so the probe fails unless ``TCL_LIBRARY`` and ``TK_LIBRARY``
# are exported BEFORE invoking PyInstaller. The build helper script
# ``scripts/build_exe.ps1`` sets them for you — use it instead of
# calling pyinstaller directly.
#
# Symptom when env vars are missing: the build "succeeds" but the
# resulting .exe dies with ``ModuleNotFoundError: No module named
# 'tkinter'`` on first launch.


# ---------------------------------------------------------------- rapidocr
# No built-in hook, and it relies on bundled data:
#   * config.yaml / default_models.yaml      (hard-coded relative path)
#   * models/*.onnx                          (looked up via the yaml)
#   * dictionary .txt files                  (ppocr keys / ppocrv5_dict)
# collect_data_files grabs all of them; collect_submodules ensures the
# backend selector (``from .onnxruntime import ...`` inside a function)
# stays reachable to PyInstaller's static analyser.
#
# Slim filter: rapidocr ships 12 .onnx models covering v4/v5 × mobile/server
# × infer/mobile/server variants — totalling ~411 MB. We only ever load the
# v5 family (see gui/app.py: primary=v5-mobile, fallback=v5-server) plus the
# cls model. Stripping v4 + the *_infer variants saves ~227 MB on disk.
# Keep BOTH .txt dictionaries (~MB level, no point filtering).
import os as _os

def _slim_rapidocr_models(datas):
    keep, dropped = [], []
    for src, dest in datas:
        name = _os.path.basename(src).lower()
        # Filenames use "pp-ocrv4" with a dash (e.g. ch_PP-OCRv4_det_mobile.onnx).
        # Also drop the *_infer variants — they duplicate the mobile models.
        if name.endswith(".onnx") and ("pp-ocrv4" in name or "_infer" in name):
            dropped.append(src)
            continue
        keep.append((src, dest))
    if dropped:
        print(f"[build_app.spec] Slim: dropped {len(dropped)} unused .onnx files")
        for p in dropped:
            print(f"  - {_os.path.basename(p)}")
    return keep

rapidocr_datas = _slim_rapidocr_models(
    collect_data_files("rapidocr", include_py_files=False)
)
rapidocr_hidden = collect_submodules("rapidocr")

# ---------------------------------------------------------------- opencc
# Pure-Python OpenCC ships its conversion configs + dictionaries as
# data files; without them, ``OpenCC("t2s")`` throws FileNotFoundError.
opencc_datas = collect_data_files("opencc")

# ---------------------------------------------------------------- matplotlib
# matplotlib's built-in hook usually catches mpl-data, but we list it
# explicitly so font lookups (esp. the CJK fallback in the chart
# dialog) don't break in obscure ways.
matplotlib_datas = collect_data_files("matplotlib")


# Hidden imports beyond what static analysis catches.
hidden_imports = rapidocr_hidden + [
    # tkinter doesn't show up in PyInstaller's module graph on this
    # Python install (custom prefix); listing it explicitly forces
    # bundling of the stdlib package. The matching _tkinter.pyd and
    # Tcl/Tk DLLs are added via ``tkinter_binaries`` above.
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.simpledialog",
    "tkinter.constants",
    # rapidocr loads its backend with a function-local ``from .onnxruntime
    # import OrtInferSession`` — collect_submodules above catches it, but
    # listing explicitly survives future rapidocr refactors.
    "rapidocr.inference_engine.onnxruntime",
    "rapidocr.inference_engine.onnxruntime.main",
    # matplotlib's Tk backend is loaded by string name in our chart dialog.
    "matplotlib.backends.backend_tkagg",
    # Pillow's Tk integration is loaded the same way.
    "PIL._tkinter_finder",
]


# Modules we deliberately exclude to keep the bundle small. None of
# these are used at runtime — rapidocr supports multiple backends but
# we only ship the onnxruntime one.
exclude_modules = [
    # Other rapidocr inference backends.
    "rapidocr.inference_engine.mnn",
    "rapidocr.inference_engine.openvino",
    "rapidocr.inference_engine.paddle",
    "rapidocr.inference_engine.pytorch",
    "rapidocr.inference_engine.tensorrt",
    # Heavy deps that those alt backends would pull in transitively.
    "torch",
    "torchvision",
    "paddle",
    "paddlepaddle",
    "openvino",
    "mnn",
    "tensorrt",
    # Qt — matplotlib supports it but we only use TkAgg.
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    # IPython / Jupyter pulled in transitively by some scientific deps.
    "IPython",
    "jupyter",
    "notebook",
    "ipykernel",
    "tornado",
    # Test infra we never need at runtime.
    "pytest",
    "_pytest",
]


block_cipher = None


a = Analysis(
    ["scripts\\run_gui.py"],
    pathex=["src"],
    binaries=[],
    datas=rapidocr_datas + opencc_datas + matplotlib_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=exclude_modules,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RO_GearSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX corrupts onnxruntime DLLs.
    console=False,                # GUI app — no terminal window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RO_GearSync",
)
