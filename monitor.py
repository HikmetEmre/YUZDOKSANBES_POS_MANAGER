# monitor.py
# =====================================================
# "Canlı durum" snapshot for the owner's phone: every MONITOR_EVERY_SEC seconds
# the POS draws ONE phone-sized picture (canli_durum.png) into the café PC's
# Google Drive folder (…\Drive'ım\pos-monitor\), so the owner can open it in the
# Google Drive app from anywhere and pull to refresh.
#
#   - Shows: active tables (open duration, bill total, remaining, QR waiting),
#     open total, today's revenue, today's cancellations.
#   - The SAME file is overwritten in place (same Drive link, no pile of old
#     pictures). Stray copies Drive may create ("canli_durum (1).png") are deleted.
#   - Google Drive is found exactly like backups (Drive'ım / My Drive, any drive
#     letter, re-checked every time). No Drive → nothing written, POS unaffected.
#   - Never crashes the POS: every failure is logged and skipped.
#
# Why a picture and not an HTML page: Google Drive does not run HTML/JavaScript
# (no live pages since 2016), but its app shows images perfectly.
# =====================================================

import io
import logging
import os
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger("monitor")

_FONT_FILES = {
    False: [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    True:  [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}
_font_cache = {}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    key = (size, bold)
    if key not in _font_cache:
        f = None
        for path in _FONT_FILES[bold]:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
        _font_cache[key] = f or ImageFont.load_default(size=size)
    return _font_cache[key]


def _dur_short(sec: int) -> str:
    """Same wording as the POS buttons ('<1 dk', '32 dk', '1 sa 7 dk'); a table
    left open past a day reads '1 gün 2 sa' instead of '26 sa 5 dk'."""
    m = max(0, int(sec or 0)) // 60
    if m < 1:
        return "<1 dk"
    h, r = divmod(m, 60)
    if not h:
        return f"{m} dk"
    if h >= 24:
        g, hh = divmod(h, 24)
        return f"{g} gün {hh} sa" if hh else f"{g} gün"
    return f"{h} sa {r} dk" if r else f"{h} sa"


def _tr_upper(s: str) -> str:
    """Turkish capitals: i → İ, ı → I (Python's upper() would turn 'iptal' into 'IPTAL')."""
    return s.replace("i", "İ").replace("ı", "I").upper()


def _tl(x) -> str:
    return f"{float(x or 0):,.0f} ₺".replace(",", ".")


# ---- colours (match the POS) ----
INK, CREAM, PAPER, MUTED, LINE = (26, 26, 26), (244, 239, 230), (255, 255, 255), (130, 124, 115), (228, 222, 212)
RED, AMBER, GREEN = (179, 0, 0), (249, 168, 37), (46, 125, 50)


def render_png(data: dict) -> bytes:
    """data = monitor_data() from order_server → PNG bytes (1080 px wide)."""
    from PIL import Image, ImageDraw
    W, PAD = 1080, 44
    tables = data["tables"]
    cancels = data["cancels"][:8]
    # height grows with the number of rows
    H = 330 + 230 + 110 + max(1, len(tables)) * 96 + 120 + max(1, len(cancels)) * 70 + 140
    img = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(img)

    # header
    d.rectangle([0, 0, W, 210], fill=INK)
    d.text((PAD, 40), f"{data['cafe']} — Canlı Durum", font=_font(54, True), fill=CREAM)
    d.text((PAD, 122), f"Güncelleme: {data['updated']}", font=_font(36), fill=(200, 196, 188))
    y = 250

    # summary tiles
    tiles = [
        ("Açık masa", str(len(tables)), INK),
        ("Açık tutar", _tl(data["open_total"]), INK),
        ("Bugün ciro", _tl(data["today_revenue"]), GREEN),
        ("Bugün iptal", f"{data['cancel_count']} · {_tl(data['cancel_amount'])}",
         RED if data["cancel_count"] else INK),
    ]
    tw = (W - 2 * PAD - 20) // 2
    for i, (label, value, colour) in enumerate(tiles):
        x0 = PAD + (i % 2) * (tw + 20)
        y0 = y + (i // 2) * 150
        d.rounded_rectangle([x0, y0, x0 + tw, y0 + 130], radius=22, fill=PAPER, outline=LINE, width=2)
        d.text((x0 + 26, y0 + 20), _tr_upper(label), font=_font(26, True), fill=MUTED)
        d.text((x0 + 26, y0 + 60), value, font=_font(46, True), fill=colour)
    y += 330

    # active tables (longest open first)
    d.text((PAD, y), "Açık Masalar", font=_font(42, True), fill=INK)
    y += 70
    if not tables:
        d.text((PAD, y + 10), "Şu an açık masa yok", font=_font(34), fill=MUTED)
        y += 96
    for t in tables:
        d.rounded_rectangle([PAD, y, W - PAD, y + 84], radius=18, fill=PAPER, outline=LINE, width=2)
        d.ellipse([PAD + 14, y + 10, PAD + 78, y + 74], fill=RED)
        # numbered table → its number in the circle; a named one ("Toplantı
        # Masası") → its initials ("TM") in the circle and the name on the title line
        name = str(t.get("name") or t["table_no"])
        named = not name.isdigit()
        no = _tr_upper("".join(w[0] for w in name.split() if w[:1].isalnum())[:2] or name[:1]) if named else name
        f = _font(30 if len(no) < 3 else 24 if len(no) < 4 else 20, True)
        tw_ = d.textlength(no, font=f)
        d.text((PAD + 46 - tw_ / 2, y + 24 if len(no) < 4 else y + 27), no, font=f, fill=PAPER)
        title = (t["area"] or "—") + (f"  ·  {name}" if named else "")
        d.text((PAD + 100, y + 10), title, font=_font(26, named), fill=INK if named else MUTED)
        d.text((PAD + 100, y + 40), _dur_short(t["open_seconds"]), font=_font(32, True), fill=INK)   # (Arial has no ⏱ glyph)
        right = _tl(t["total"])
        if t["paid"] > 0:
            right = f"{_tl(t['remaining'])} kalan"
        rw = d.textlength(right, font=_font(36, True))
        d.text((W - PAD - 24 - rw, y + 22), right, font=_font(36, True), fill=INK)
        if t["pending"]:
            d.rounded_rectangle([W - PAD - 24 - rw - 210, y + 22, W - PAD - 24 - rw - 20, y + 62],
                                radius=14, fill=AMBER)
            d.text((W - PAD - 24 - rw - 196, y + 26), "QR bekliyor", font=_font(24, True), fill=INK)
        y += 96

    # today's cancellations
    y += 30
    d.text((PAD, y), "Bugünkü İptaller", font=_font(42, True), fill=INK)
    y += 70
    if not cancels:
        d.text((PAD, y + 6), "Bugün iptal yok", font=_font(34), fill=MUTED)
        y += 70
    for c in cancels:
        reason = c["reason"] + (f": {c['note']}" if c.get("note") else "")
        line = f"{c['time']}  ·  {c.get('table_label') or 'Masa ' + str(c['table_no'])}  ·  {c['product']} ×{c['qty']:g}"
        d.text((PAD, y), line, font=_font(30, True), fill=INK)
        d.text((PAD, y + 36), f"{reason}  —  {_tl(c['amount'])}", font=_font(26), fill=RED)
        y += 70
    if data["cancel_count"] > len(cancels):
        d.text((PAD, y), f"… ve {data['cancel_count'] - len(cancels)} iptal daha (panelde)", font=_font(26), fill=MUTED)

    # footer
    d.line([PAD, H - 90, W - PAD, H - 90], fill=LINE, width=2)
    d.text((PAD, H - 70), f"Her {config.MONITOR_EVERY_SEC} saniyede güncellenir · Drive'da aşağı çekerek yenileyin · "
                          f"v{data['version']}", font=_font(24), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- writing

_last_target_state = None


def target_dir() -> Path | None:
    """Where the snapshot goes: POS_MONITOR_DIR env (empty = off), an explicit
    config.MONITOR_FOLDER, or <Google Drive>\\pos-monitor (auto-detected)."""
    global _last_target_state
    if "POS_MONITOR_DIR" in os.environ:
        v = os.environ["POS_MONITOR_DIR"].strip()
        return Path(v) if v else None
    if config.MONITOR_FOLDER:
        return Path(config.MONITOR_FOLDER)
    import backup
    gd = backup.find_google_drive()
    state = str(gd) if gd else "none"
    if state != _last_target_state:
        _last_target_state = state
        if gd:
            log.info("Live monitor → %s", gd / config.MONITOR_FOLDER_NAME / config.MONITOR_FILE)
        else:
            log.warning("Google Drive bulunamadı — canlı durum resmi yazılmıyor (live monitor off)")
    return (gd / config.MONITOR_FOLDER_NAME) if gd else None


def write_snapshot(data: dict) -> Path | None:
    """Render + write canli_durum.png (overwritten IN PLACE, so Google Drive keeps
    the same file and link), then delete stray older copies. Returns the path."""
    d = target_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    png = render_png(data)
    path = d / config.MONITOR_FILE
    with open(path, "wb") as f:          # in place — not temp+rename (that would be a NEW Drive file)
        f.write(png)
    stem, ext = os.path.splitext(config.MONITOR_FILE)
    for old in d.glob(f"{stem}*{ext}"):  # e.g. "canli_durum (1).png" left by a Drive sync conflict
        if old.name != config.MONITOR_FILE:
            try:
                old.unlink()
                log.info("Removed old monitor shot %s", old.name)
            except OSError:
                pass
    return path
