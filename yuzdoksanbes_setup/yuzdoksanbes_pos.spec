# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for YUZDOKSANBES POS (used by build.bat).
#
# ONE-DIR build (a folder with YUZDOKSANBES_POS.exe + its libraries), not
# one-file: it starts faster (nothing to unpack at every launch) and is far
# less likely to be flagged by antivirus software. The installer ships the
# whole folder to C:\Program Files\YUZDOKSANBES POS.
#
# Writable data (pos.db, backups, logs, PINs…) is NOT in here — the app keeps
# it in %ProgramData%\YUZDOKSANBES (see paths.py).
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))     # the project folder

hiddenimports = (
    collect_submodules("uvicorn")        # uvicorn picks loops/protocols by name at runtime
    + collect_submodules("zeroconf")     # mDNS (siparis.local); has compiled sub-modules
    + ["ifaddr"]
)

a = Analysis(
    [os.path.join(ROOT, "order_server.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "static"), "static"),        # web pages, JS, CSS, bell sound
        (os.path.join(ROOT, "logo.ico"), "."),
    ] + collect_data_files("fpdf"),
    hiddenimports=hiddenimports,
    # NEVER exclude "unittest": fpdf (PDF reports) imports unittest.mock at load
    # time. v1.1.0 excluded it and every report failed on installed PCs.
    # build.bat runs the exe's --selftest, which would catch this again.
    excludes=["tkinter", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YUZDOKSANBES_POS",
    icon=os.path.join(ROOT, "logo.ico"),
    console=True,          # staff see the live status window (and can close it to stop)
    upx=False,             # UPX-packed exes trigger antivirus false positives
    version=os.path.join(SPECPATH, "build", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="YUZDOKSANBES_POS",
    upx=False,
)
