# paths.py
# =====================================================
# Resolve file locations correctly whether the app runs as:
#   (a) a normal Python script during development, or
#   (b) a frozen PyInstaller .exe shipped to the café.
#
# Why this matters (see ARCHITECTURE.md §1 packaging notes):
#   - WRITABLE files (pos.db, backups/, logs/…) live in %ProgramData%\YUZDOKSANBES
#     for the installed exe — you cannot write inside a frozen bundle or under
#     Program Files, and data must survive app updates and uninstalls.
#   - BUNDLED read-only files (static/) are unpacked by PyInstaller into a
#     temporary folder at runtime (sys._MEIPASS); we must read them from there.
#
# Using these two helpers everywhere means packaging "just works" later with
# zero code changes — we only add a --add-data flag for static/.
# =====================================================

from pathlib import Path
import os
import sys


def _frozen() -> bool:
    """True when running inside a PyInstaller exe."""
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """
    Folder for WRITABLE app data (database, backups, logs, secret key, PINs).
      - POS_DATA_DIR env var, if set (used by tests / special setups)
      - script mode: the project folder (where the .py files live) — as before
      - frozen exe:  %ProgramData%\\YUZDOKSANBES. The installed program lives in
        C:\\Program Files, which normal users can't write to, and data must
        survive upgrades/uninstalls — so it lives apart from the program.
    """
    override = os.environ.get("POS_DATA_DIR")
    if override:
        d = Path(override)
    elif _frozen():
        d = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "YUZDOKSANBES"
    else:
        return Path(__file__).resolve().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_path(name_or_path: str) -> str:
    """
    Resolve a writable file/folder path.
      - Absolute paths (e.g. a Google Drive folder 'G:/Drive'ım/pos-backups')
        are returned unchanged — the user chose them deliberately.
      - Relative names (e.g. 'pos.db', 'backups') are placed next to the app.
    """
    p = Path(name_or_path)
    # Absolute (incl. Windows drive letters like 'G:/...' even if checked off-Windows)
    s = str(name_or_path)
    is_win_drive = len(s) >= 2 and s[1] == ":" and s[0].isalpha()
    if p.is_absolute() or is_win_drive:
        return str(name_or_path)
    return str(app_dir() / p)


def resource_path(name: str) -> str:
    """
    Resolve a BUNDLED read-only resource (e.g. the 'static' folder).
      - frozen exe:  PyInstaller's temp extraction dir (sys._MEIPASS)
      - script mode: the project folder
    """
    if _frozen():
        base = Path(getattr(sys, "_MEIPASS", app_dir()))
    else:
        base = Path(__file__).resolve().parent
    return str(base / name)
