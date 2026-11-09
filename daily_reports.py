# daily_reports.py
# =====================================================
# Automatic end-of-day report: once a day is over, the POS saves that day's
# "Günlük Rapor" PDF by itself, with no button pressed and no one logged in.
#
#   - A day counts as over at DAILY_REPORT_AT the next morning (default 04:00),
#     so a café still paying bills after midnight is finished first. Sales are
#     still dated by when they were paid, exactly like the manual report.
#   - Saved to <data dir>\reports\gunluk\gunluk_YYYY-MM-DD.pdf (ProgramData for
#     the installed app) and mirrored to <Google Drive>\pos-reports\<PC>\.
#   - Catch-up: if the PC was off (weekend, power cut) every missed day of the
#     last DAILY_REPORT_CATCHUP_DAYS is created at the next start. Days with no
#     activity at all are skipped, so a closed Sunday makes no empty PDF.
#   - The PDF is the same one the dashboard downloads (_build_daily_pdf).
#   - Like backup.py: a daemon thread that NEVER crashes the POS. A failure is
#     logged and retried at the next check; a Drive copy that failed is retried
#     until it exists.
# =====================================================
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
import logging
import os
import threading
import time

import config
import paths

log = logging.getLogger("daily_reports")

STATE_KEY = "last_daily_report"      # app_settings: newest day saved locally


def _cutoff() -> dtime:
    try:
        h, m = (int(x) for x in str(getattr(config, "DAILY_REPORT_AT", "04:00")).split(":"))
        return dtime(h, m)
    except (ValueError, TypeError):
        return dtime(4, 0)


def last_finished_day(now: datetime) -> date:
    """The newest day whose report may be made at `now` (yesterday after the
    cut-off time, the day before yesterday before it)."""
    back = 1 if now.time() >= _cutoff() else 2
    return now.date() - timedelta(days=back)


def local_dir() -> Path:
    return Path(paths.data_path(os.path.join("reports", "gunluk")))


def mirror_dirs() -> list[Path]:
    """This PC's folder in each report mirror. POS_REPORT_MIRRORS overrides
    (';'-separated, empty = no mirror); otherwise <Google Drive>\\pos-reports\\<PC>."""
    import backup
    if "POS_REPORT_MIRRORS" in os.environ:
        bases = [Path(p.strip()) for p in os.environ["POS_REPORT_MIRRORS"].split(";") if p.strip()]
    else:
        gd = backup.find_google_drive()
        bases = [gd / getattr(config, "DAILY_REPORT_FOLDER_NAME", "pos-reports")] if gd else []
    return [b / backup._machine_name() for b in bases]


def file_name(day: date) -> str:
    return f"gunluk_{day.isoformat()}.pdf"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def run_once(now: datetime, build_pdf, has_activity, get_state, set_state) -> list[str]:
    """Create every missing finished day's report. Injected callables keep this
    testable without a server:
        build_pdf(day_iso) -> bytes     has_activity(day_iso) -> bool
        get_state() -> 'YYYY-MM-DD'|None    set_state(day_iso)
    Returns the days written locally in this run."""
    newest = last_finished_day(now)
    catchup = int(getattr(config, "DAILY_REPORT_CATCHUP_DAYS", 31))
    oldest = newest - timedelta(days=catchup - 1)
    last = get_state()
    if last:
        try:
            oldest = max(oldest, date.fromisoformat(last) + timedelta(days=1))
        except ValueError:
            pass
    written = []
    ldir = local_dir()
    day = oldest
    while day <= newest:
        iso = day.isoformat()
        target = ldir / file_name(day)
        if not target.exists() and has_activity(iso):
            _write_atomic(target, build_pdf(iso))
            written.append(iso)
            log.info("Daily report saved: %s", target)
        set_state(iso)                       # done (or nothing to report) → never redo
        day += timedelta(days=1)
    _sync_mirrors(ldir, newest - timedelta(days=catchup - 1))
    return written


def _sync_mirrors(ldir: Path, since: date) -> None:
    """Copy local reports of the catch-up window that a mirror doesn't have yet
    (new ones, and earlier ones whose copy failed while Drive was offline)."""
    if not ldir.is_dir():
        return
    for mdir in mirror_dirs():
        for f in sorted(ldir.glob("gunluk_*.pdf")):
            try:
                if date.fromisoformat(f.stem[len("gunluk_"):]) < since:
                    continue
            except ValueError:
                continue
            dest = mdir / f.name
            if dest.exists():
                continue
            try:
                _write_atomic(dest, f.read_bytes())
                log.info("Daily report mirrored: %s → %s", f.name, mdir)
            except Exception as e:
                log.warning("Daily report mirror to %s FAILED (will retry): %s", mdir, e)
                break


_status = {"last_run": None, "last_error": None}


def status() -> dict:
    return {**_status, "folder": str(local_dir()),
            "mirrors": [str(d) for d in mirror_dirs()]}


def start(build_pdf, has_activity, get_state, set_state) -> None:
    """Run run_once now and then every DAILY_REPORT_CHECK_MIN, in a daemon thread."""
    every = max(1, int(getattr(config, "DAILY_REPORT_CHECK_MIN", 10))) * 60

    def loop():
        last_err = None
        while True:
            try:
                run_once(datetime.now(), build_pdf, has_activity, get_state, set_state)
                _status["last_run"] = datetime.now().isoformat(timespec="seconds")
                _status["last_error"] = None
                last_err = None
            except Exception as e:
                _status["last_error"] = repr(e)
                if repr(e) != last_err:
                    log.warning("Daily report run failed (will retry): %r", e)
                    last_err = repr(e)
            time.sleep(every)

    threading.Thread(target=loop, daemon=True, name="daily-reports").start()
    log.info("Daily reports: each day saved after %s next morning → %s",
             _cutoff().strftime("%H:%M"), local_dir())
