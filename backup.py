# backup.py
# =====================================================
# Automatic, safe SQLite backups (see ARCHITECTURE.md §8).
#
# Why a module of its own: backups are infrastructure, not POS logic.
# Keeping them separate makes the behavior easy to read, test, and tune.
#
# Safety notes:
#   - Uses sqlite3's ONLINE BACKUP API (sqlite3.Connection.backup), which
#     produces a consistent snapshot even while the DB is being written to.
#     A plain file copy of a WAL-mode DB mid-write can be corrupt — we avoid that.
#   - Rolling retention keeps recent hourly + older daily copies, so a single
#     corrupted live DB can never overwrite the last good backup.
#   - A backup failure logs a warning but NEVER crashes the POS.
# =====================================================

from datetime import datetime, timedelta
from pathlib import Path
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import logging

import config
import paths

log = logging.getLogger("backup")


def _machine_name() -> str:
    """This PC's subfolder name inside each mirror (filesystem-safe)."""
    name = getattr(config, "BACKUP_MACHINE_NAME", "") or socket.gethostname() or "pos"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "pos"


def _drive_roots() -> list[Path]:
    """Drive letters to search for Google Drive: G: first (Google Drive's
    default), then D:–Z:. Kept separate so tests can point it at fake folders."""
    letters = ["G"] + [c for c in "DEFHIJKLMNOPQRSTUVWXYZ"]
    return [Path(f"{c}:\\") for c in letters]


_last_gdrive_state = None   # log only when detection result CHANGES (not hourly)


def find_google_drive() -> Path | None:
    """The Google Drive folder on this PC — 'Drive'ım' on Turkish Windows /
    Google Drive, 'My Drive' on English — or None if Drive isn't mounted."""
    names = getattr(config, "GDRIVE_FOLDER_NAMES", ["Drive'ım", "My Drive"])
    for root in _drive_roots():
        try:
            if not root.exists():
                continue
            for name in names:
                if (root / name).is_dir():
                    return root / name
        except OSError:
            continue    # e.g. an empty card reader / disconnected network drive
    return None


def _mirror_bases() -> list[Path]:
    """The mirror folders BEFORE the per-PC subfolder."""
    global _last_gdrive_state
    configured = getattr(config, "BACKUP_MIRRORS", None)
    if configured is not None:
        return [Path(d) for d in configured]          # explicit list (or [] = off)
    gd = find_google_drive()
    state = str(gd) if gd else "none"
    if state != _last_gdrive_state:
        _last_gdrive_state = state
        if gd:
            log.info("Google Drive found: %s → backups mirror to %s", gd,
                     gd / config.GDRIVE_BACKUP_FOLDER)
        else:
            log.warning("Google Drive bulunamadı — yedek sadece bu bilgisayarda "
                        "(Google Drive not found; local backups only)")
    return [gd / getattr(config, "GDRIVE_BACKUP_FOLDER", "pos-backups")] if gd else []


def _mirror_dirs() -> list[Path]:
    """This PC's own folder inside each off-site mirror. Writing AND pruning
    only happen here, so files from other PCs or manual copies elsewhere in the
    shared mirror folder are never touched. Re-evaluated at every backup."""
    sub = _machine_name()
    return [base / sub for base in _mirror_bases()]


def _mirror_snapshot(src_file: Path) -> None:
    """Copy a finished local snapshot to every mirror folder. A missing/offline
    drive (G: not mounted, Drive app off) is skipped with a warning — the file
    is already a consistent, static copy, so a plain copy is safe.

    The copy goes to a '.tmp' name first and is renamed into place only once
    complete, so an interrupted copy never leaves a truncated pos_*.db that
    retention would treat as the newest good snapshot."""
    for d in _mirror_dirs():
        tmp = d / (src_file.name + ".tmp")   # doesn't match the pos_*.db glob
        try:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, tmp)
            os.replace(tmp, d / src_file.name)
            log.info("Backup mirrored: %s → %s", src_file.name, d)
        except Exception as e:
            log.warning("Backup mirror to %s FAILED (skipped): %s", d, e)
            try:
                tmp.unlink(missing_ok=True)   # drop the partial copy
            except Exception:
                pass


def make_backup() -> Path | None:
    """Create one timestamped snapshot of the live DB, then copy it to any
    off-site mirrors. Returns the local snapshot path, or None on failure."""
    try:
        backup_dir = Path(paths.data_path(config.BACKUP_DIR))
        backup_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        dest = backup_dir / f"pos_{stamp}.db"

        # Online backup: open source (live) and dest, copy via the C backup API.
        src = sqlite3.connect(paths.data_path(config.DB_PATH), timeout=10.0)
        try:
            out = sqlite3.connect(str(dest))
            try:
                src.backup(out)        # consistent snapshot, safe during writes
            finally:
                out.close()
        finally:
            src.close()

        log.info("Backup written: %s", dest.name)
        _mirror_snapshot(dest)         # copy to Google Drive / other mirrors
        return dest
    except Exception as e:
        # Never let a backup problem take down the POS.
        log.warning("Backup FAILED: %s", e)
        return None


def _prune_dir(backup_dir: Path) -> None:
    """Apply the rolling retention to a single backup folder:
      - keep the most recent BACKUP_KEEP_HOURLY snapshots, AND
      - keep one-per-day for the most recent BACKUP_KEEP_DAILY days.
    Anything not protected by either rule is deleted."""
    try:
        if not backup_dir.exists():
            return

        backups = sorted(backup_dir.glob("pos_*.db"), reverse=True)  # newest first
        if not backups:
            return

        keep = set()

        # Rule 1: the N most recent snapshots (covers the "hourly" window).
        for p in backups[: config.BACKUP_KEEP_HOURLY]:
            keep.add(p)

        # Rule 2: the first (newest) snapshot seen for each of the last N days.
        cutoff = datetime.now() - timedelta(days=config.BACKUP_KEEP_DAILY)
        seen_days = set()
        for p in backups:
            day = _date_from_name(p)
            if day is None:
                continue
            if day < cutoff.date():
                continue
            if day not in seen_days:
                seen_days.add(day)
                keep.add(p)

        # Rule 3 (hard age cap): drop anything older than BACKUP_MAX_AGE_DAYS
        # from the keep set, overriding rules 1-2 (e.g. an idle PC whose 24
        # "newest" snapshots are actually weeks old). The single newest snapshot
        # is always retained so we never delete the last remaining backup.
        max_age = getattr(config, "BACKUP_MAX_AGE_DAYS", 0)
        if max_age:
            hard_cutoff = (datetime.now() - timedelta(days=max_age)).date()
            newest = backups[0]
            for p in list(keep):
                if p == newest:
                    continue
                day = _date_from_name(p)
                if day is not None and day < hard_cutoff:
                    keep.discard(p)

        for p in backups:
            if p not in keep:
                try:
                    p.unlink()
                    log.info("Pruned old backup: %s (%s)", p.name, backup_dir)
                except Exception as e:
                    log.warning("Could not delete %s: %s", p.name, e)
    except Exception as e:
        log.warning("Prune FAILED for %s (skipped): %s", backup_dir, e)


def prune_backups() -> None:
    """Prune the local folder and this PC's subfolder in every off-site mirror
    with the same retention (other PCs' files are left alone)."""
    _prune_dir(Path(paths.data_path(config.BACKUP_DIR)))
    for d in _mirror_dirs():
        _prune_dir(d)


def _date_from_name(path: Path):
    """Extract the date from 'pos_YYYY-MM-DD_HHMM.db'. Returns a date or None."""
    try:
        stem = path.stem            # pos_2026-06-16_1400
        datepart = stem.split("_")[1]  # 2026-06-16
        return datetime.strptime(datepart, "%Y-%m-%d").date()
    except Exception:
        return None


def _loop():
    """Background thread: snapshot + prune every BACKUP_EVERY_MIN minutes."""
    interval = max(1, config.BACKUP_EVERY_MIN) * 60
    while True:
        make_backup()
        prune_backups()
        time.sleep(interval)


def start_backup_scheduler() -> None:
    """
    Start the backup loop in a daemon thread (dies with the server).
    Takes one immediate backup at startup so there's always a fresh copy,
    then runs on the configured interval.
    """
    t = threading.Thread(target=_loop, daemon=True, name="backup-scheduler")
    t.start()
    mirrors = getattr(config, "BACKUP_MIRRORS", None)
    log.info("Backup scheduler started (every %s min → %s/%s)",
             config.BACKUP_EVERY_MIN, config.BACKUP_DIR,
             " + Google Drive (auto-detected)" if mirrors is None
             else (" + mirrors: " + ", ".join(mirrors)) if mirrors else "")