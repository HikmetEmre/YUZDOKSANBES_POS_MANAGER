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
import sqlite3
import threading
import time
import logging

import config
import paths

log = logging.getLogger("backup")


def make_backup() -> Path | None:
    """Create one timestamped snapshot of the live DB. Returns its path, or None on failure."""
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
        return dest
    except Exception as e:
        # Never let a backup problem take down the POS.
        log.warning("Backup FAILED: %s", e)
        return None


def prune_backups() -> None:
    """
    Rolling retention:
      - keep the most recent BACKUP_KEEP_HOURLY snapshots, AND
      - keep one-per-day for the most recent BACKUP_KEEP_DAILY days.
    Anything not protected by either rule is deleted.
    """
    try:
        backup_dir = Path(paths.data_path(config.BACKUP_DIR))
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

        for p in backups:
            if p not in keep:
                try:
                    p.unlink()
                    log.info("Pruned old backup: %s", p.name)
                except Exception as e:
                    log.warning("Could not delete %s: %s", p.name, e)
    except Exception as e:
        log.warning("Prune FAILED: %s", e)


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
    log.info("Backup scheduler started (every %s min → %s/)",
             config.BACKUP_EVERY_MIN, config.BACKUP_DIR)