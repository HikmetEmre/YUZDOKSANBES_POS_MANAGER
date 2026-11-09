# daily_reports.py
# =====================================================
# Automatic end-of-day report: every day the POS saves that day's
# "Günlük Rapor" PDF by itself, with no button pressed and no one logged in.
#
#   - A day's report is made at DAILY_REPORT_AT (default 23:59, the same
#     evening). A time before 12:00 (e.g. "04:00") means "the next morning"
#     instead. Sales are dated by when they were paid, exactly like the manual
#     report.
#   - Late changes: if anything is paid, cancelled or entered for that day
#     after its PDF was saved (e.g. a bill paid at 23:59:40), the PDF is made
#     again once the day is over, so the saved report is always the final one.
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
DEFAULT_AT = dtime(23, 59)


def _cutoff() -> dtime:
    try:
        h, m = (int(x) for x in str(getattr(config, "DAILY_REPORT_AT", "23:59")).split(":"))
        return dtime(h, m)
    except (ValueError, TypeError):
        return DEFAULT_AT


def _same_evening() -> bool:
    """23:59 → the day's own evening; 04:00 → the next morning."""
    return _cutoff() >= dtime(12, 0)


def ready_at(day: date) -> datetime:
    """When the report of `day` is made."""
    return datetime.combine(day + timedelta(days=0 if _same_evening() else 1), _cutoff())


def last_finished_day(now: datetime) -> date:
    """The newest day whose report may be made at `now`."""
    back = 0 if _same_evening() else 1
    if now.time() < _cutoff():
        back += 1
    return now.date() - timedelta(days=back)


def seconds_to_next_run(now: datetime) -> float:
    """Seconds until the next report time (so 23:59 is hit on the minute)."""
    nxt = datetime.combine(now.date(), _cutoff())
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


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
    # unique temp name: the 23:59 thread and a manual report may write at once
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def run_once(now: datetime, build_pdf, has_activity, get_state, set_state,
             changed_since=None) -> list[str]:
    """Create every missing finished day's report. Injected callables keep this
    testable without a server:
        build_pdf(day_iso) -> bytes     has_activity(day_iso) -> bool
        get_state() -> 'YYYY-MM-DD'|None    set_state(day_iso)
        changed_since(day_iso, since_iso) -> bool   (optional: late-change refresh)
    Returns the days written locally in this run."""
    newest = last_finished_day(now)
    catchup = int(getattr(config, "DAILY_REPORT_CATCHUP_DAYS", 31))
    window_start = newest - timedelta(days=catchup - 1)
    oldest = window_start
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
    if changed_since:
        written += _refresh_late(now, ldir, window_start, build_pdf, changed_since)
    _sync_mirrors(ldir, window_start)
    return written


def _refresh_late(now: datetime, ldir: Path, since: date, build_pdf, changed_since) -> list[str]:
    """A PDF saved before its day ended (at 23:59) is made again once the day is
    over, if something for that day happened after it was saved."""
    redone = []
    if not ldir.is_dir():
        return redone
    for f in sorted(ldir.glob("gunluk_*.pdf")):
        try:
            day = date.fromisoformat(f.stem[len("gunluk_"):])
        except ValueError:
            continue
        end = datetime.combine(day + timedelta(days=1), dtime(0, 0))
        if day < since or now < end:
            continue                          # outside the window / day not over yet
        saved = datetime.fromtimestamp(f.stat().st_mtime)
        if saved >= end:
            continue                          # made after the day ended → already final
        if changed_since(day.isoformat(), saved.isoformat(timespec="seconds")):
            _write_atomic(f, build_pdf(day.isoformat()))
            redone.append(day.isoformat())
            log.info("Daily report updated with late changes: %s", f)
    return redone


def _sync_mirrors(ldir: Path, since: date) -> None:
    """Copy local reports of the catch-up window that a mirror doesn't have yet
    (new ones, updated ones, and earlier ones whose copy failed while Drive was
    offline)."""
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
            if dest.exists() and dest.stat().st_mtime >= f.stat().st_mtime:
                continue                      # mirror already has this version
            try:
                _write_atomic(dest, f.read_bytes())
                log.info("Daily report mirrored: %s → %s", f.name, mdir)
            except Exception as e:
                log.warning("Daily report mirror to %s FAILED (will retry): %s", mdir, e)
                break


def save_manual(day_iso: str, pdf: bytes) -> Path:
    """A daily report made by hand (dashboard) is saved as that day's report,
    so a café that closes early and makes its report itself doesn't get a
    second one at 23:59: the automatic run sees the file and skips the day.
    (If something is still paid/cancelled for that day afterwards, the usual
    late-change rule makes it again after midnight.) The Drive copy is made in
    the background so the download never waits for Drive."""
    day = date.fromisoformat(day_iso)
    target = local_dir() / file_name(day)
    _write_atomic(target, pdf)
    log.info("Daily report saved (made manually): %s", target)

    def mirror():
        try:
            _sync_mirrors(local_dir(), day)
        except Exception as e:                    # never disturb the POS
            log.warning("Daily report mirror after manual save failed: %r", e)
    threading.Thread(target=mirror, daemon=True, name="daily-report-mirror").start()
    return target


_status = {"last_run": None, "last_error": None}


def status() -> dict:
    return {**_status, "folder": str(local_dir()),
            "mirrors": [str(d) for d in mirror_dirs()]}


def start(build_pdf, has_activity, get_state, set_state, changed_since=None) -> None:
    """Run run_once now, then every DAILY_REPORT_CHECK_MIN and exactly at
    DAILY_REPORT_AT, in a daemon thread."""
    every = max(1, int(getattr(config, "DAILY_REPORT_CHECK_MIN", 10))) * 60

    def loop():
        last_err = None
        while True:
            try:
                run_once(datetime.now(), build_pdf, has_activity, get_state, set_state,
                         changed_since)
                _status["last_run"] = datetime.now().isoformat(timespec="seconds")
                _status["last_error"] = None
                last_err = None
            except Exception as e:
                _status["last_error"] = repr(e)
                if repr(e) != last_err:
                    log.warning("Daily report run failed (will retry): %r", e)
                    last_err = repr(e)
            # wake at the check interval, or right at the report time if sooner
            time.sleep(max(1.0, min(every, seconds_to_next_run(datetime.now()) + 1)))

    threading.Thread(target=loop, daemon=True, name="daily-reports").start()
    log.info("Daily reports: each day saved at %s%s → %s", _cutoff().strftime("%H:%M"),
             "" if _same_evening() else " (next morning)", local_dir())
