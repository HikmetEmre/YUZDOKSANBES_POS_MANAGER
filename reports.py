# reports.py
# =====================================================
# PDF report generation (see ARCHITECTURE.md — Reports).
# Daily and monthly reports for the café, with optional logo.
#
# Font note: fpdf2's built-in fonts are Latin-1 only and can't render ₺ or
# Turkish characters (ş, ğ, İ...). We load a Unicode TrueType font from the
# first location that exists across platforms (Windows Arial, Linux DejaVu,
# or a font bundled next to the app). If none is found we fall back to a
# Latin-1-safe transliteration so a report is always produced.
# =====================================================

from datetime import datetime
from pathlib import Path
import os

from fpdf import FPDF

import config
import paths

CURRENCY = config.CURRENCY

# Candidate Unicode fonts, in priority order.
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
]
_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _find(cands, bundled=()):
    # Check next to the app first (a bundled font), but only bundled files that
    # match the requested WEIGHT — otherwise the bold lookup would return the
    # regular face and "bold" text would render non-bold.
    for name in bundled:
        p = Path(paths.app_dir()) / name
        if p.exists():
            return str(p)
    for c in cands:
        if os.path.exists(c):
            return c
    return None


class _Report(FPDF):
    def __init__(self):
        super().__init__()
        self.uni = False
        reg = _find(_FONT_CANDIDATES, ("font.ttf", "DejaVuSans.ttf"))
        bold = _find(_BOLD_CANDIDATES, ("font-bold.ttf", "DejaVuSans-Bold.ttf"))
        if reg:
            self.add_font("body", "", reg)
            self.add_font("body", "B", bold or reg)
            self.uni = True
            self.base = "body"
        else:
            self.base = "Helvetica"   # Latin-1 fallback

    def _t(self, s):
        """Make text safe if we had to fall back to a non-Unicode font."""
        if self.uni:
            return str(s)
        repl = {"₺": "TL", "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G", "ı": "i",
                "İ": "I", "ç": "c", "Ç": "C", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U"}
        out = str(s)
        for k, v in repl.items():
            out = out.replace(k, v)
        return out.encode("latin-1", "replace").decode("latin-1")

    def header_block(self, subtitle):
        # Logo centered at top if present, else café name as text.
        logo = config.LOGO_PATH
        logo_path = logo if os.path.isabs(logo) else str(Path(paths.app_dir()) / logo)
        if os.path.exists(logo_path):
            try:
                w = 40
                self.image(logo_path, x=(self.w - w) / 2, y=10, w=w)
                self.ln(34)
            except Exception:
                self._name_title()
        else:
            self._name_title()
        self.set_font(self.base, "B", 14)
        self.cell(0, 8, self._t(subtitle), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def _name_title(self):
        self.set_font(self.base, "B", 22)
        self.cell(0, 12, self._t(config.CAFE_NAME), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def section(self, title):
        self.ln(3)
        self.set_font(self.base, "B", 12)
        self.set_fill_color(26, 26, 26)
        self.set_text_color(244, 239, 230)
        self.cell(0, 8, "  " + self._t(title), new_x="LMARGIN", new_y="NEXT", fill=True)
        self.set_text_color(26, 26, 26)
        self.ln(2)

    def kv(self, label, value, bold=False):
        self.set_font(self.base, "B" if bold else "", 11)
        self.cell(120, 7, self._t(label))
        self.cell(0, 7, self._t(value), align="R", new_x="LMARGIN", new_y="NEXT")

    def table_header(self, cols, widths):
        self.set_font(self.base, "B", 10)
        self.set_fill_color(235, 230, 220)
        for c, w in zip(cols, widths):
            self.cell(w, 7, self._t(c), border=1, fill=True, align="C")
        self.ln()

    def table_row(self, cells, widths, aligns=None):
        self.set_font(self.base, "", 10)
        aligns = aligns or ["L"] * len(cells)
        for cell, w, a in zip(cells, widths, aligns):
            self.cell(w, 6.5, self._t(cell), border=1, align=a)
        self.ln()


# ---------- SHARED SECTIONS ----------

def _manual_income_section(pdf, manual_incomes) -> None:
    """'Ek Gelirler' table (description + amount); skipped when empty.
    Used by both the daily and the monthly report."""
    if not manual_incomes:
        return
    pdf.section("Ek Gelirler")
    pdf.table_header(["Açıklama", "Tutar"], [140, 50])
    for mi in manual_incomes:
        pdf.table_row([mi["description"], f'{mi["amount"]:.0f} {CURRENCY}'],
                      [140, 50], ["L", "R"])


# ---------- DAILY ----------

def build_daily_pdf(date_str, sold_items, totals,
                    expenses=None, manual_incomes=None, tasks=None) -> bytes:
    """
    date_str       : 'YYYY-MM-DD'
    sold_items     : [{name, qty, total}]
    totals         : {cash, card, count, sales_total, manual,
                      income_total, expense_total, net}
    expenses       : [{description, amount}]     (optional — that day's Gider)
    manual_incomes : [{description, amount}]     (optional — that day's Ek Gelir)
    tasks          : [{title, category, done}]   (optional — that day's Görevler)
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    pretty = d.strftime("%d-%m-%Y")
    pdf = _Report()
    pdf.add_page()
    pdf.header_block(f"Günlük Rapor — {pretty}")

    # ---- Sold items ----
    pdf.section("Satılan Ürünler")
    if sold_items:
        pdf.table_header(["Ürün", "Adet", "Tutar"], [110, 30, 50])
        for it in sold_items:
            pdf.table_row([it["name"], f'{it["qty"]:g}', f'{it["total"]:.0f} {CURRENCY}'],
                          [110, 30, 50], ["L", "C", "R"])
    else:
        pdf.set_font(pdf.base, "", 11)
        pdf.cell(0, 7, pdf._t("Bu gün satış yok."), new_x="LMARGIN", new_y="NEXT")

    # ---- Income summary ----
    manual_total = totals.get("manual", 0)
    pdf.section("Gelir Özeti")
    pdf.kv("Nakit", f'{totals["cash"]:.0f} {CURRENCY}')
    pdf.kv("Kart",  f'{totals["card"]:.0f} {CURRENCY}')
    pdf.kv("Satış Cirosu", f'{totals["sales_total"]:.0f} {CURRENCY}')
    pdf.kv("Satış adedi", f'{totals["count"]}')
    if manual_total:
        pdf.kv("Ek Gelir", f'{manual_total:.0f} {CURRENCY}')
    pdf.kv("TOPLAM GELİR", f'{totals["income_total"]:.0f} {CURRENCY}', bold=True)

    # ---- Manual income list (Ek Gelir) ----
    _manual_income_section(pdf, manual_incomes)

    # ---- Expenses (Gider) ----
    pdf.section("Giderler")
    if expenses:
        pdf.table_header(["Açıklama", "Tutar"], [140, 50])
        for e in expenses:
            pdf.table_row([e["description"], f'{e["amount"]:.0f} {CURRENCY}'],
                          [140, 50], ["L", "R"])
        pdf.kv("TOPLAM GİDER", f'{totals["expense_total"]:.0f} {CURRENCY}', bold=True)
    else:
        pdf.set_font(pdf.base, "", 11)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 7, pdf._t("Bu gün gider kaydı yok."), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(26, 26, 26)

    # ---- Net profit ----
    pdf.section("Net Kâr")
    pdf.kv("NET KÂR (Gelir - Gider)", f'{totals["net"]:.0f} {CURRENCY}', bold=True)

    # ---- Tasks of the day (Görevler) ----
    pdf.section("Günün Görevleri")
    if tasks:
        pdf.table_header(["İş", "Kategori", "Durum"], [110, 45, 35])
        for t in tasks:
            durum = "Tamam" if t.get("done") else "Bekliyor"
            pdf.table_row([t["title"], t.get("category", ""), durum], [110, 45, 35], ["L", "C", "C"])
    else:
        pdf.set_font(pdf.base, "", 11)
        pdf.cell(0, 7, pdf._t("Bu güne görev yok."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)
    pdf.set_font(pdf.base, "", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.cell(0, 5, pdf._t(f'Oluşturuldu: {datetime.now().strftime("%d-%m-%Y %H:%M")}'),
             align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---------- MONTHLY ----------

def build_monthly_pdf(year, month, daily_rows, totals, done_tasks,
                      expenses=None, manual_incomes=None) -> bytes:
    """
    daily_rows     : [{date, total}]   per-day revenue
    totals         : {cash, card, grand, count, manual}
    done_tasks     : [{date, title, category}]
    expenses       : [{description, amount}]   (optional)
    manual_incomes : [{description, amount}]   (optional — the month's Ek Gelir)
    """
    TR_MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                 "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    pdf = _Report()
    pdf.add_page()
    pdf.header_block(f"Aylık Rapor — {TR_MONTHS[month]} {year}")

    pdf.section("Aylık Gelir")
    pdf.kv("Nakit", f'{totals["cash"]:.0f} {CURRENCY}')
    pdf.kv("Kart",  f'{totals["card"]:.0f} {CURRENCY}')
    if totals.get("manual", 0) > 0:
        pdf.kv("Ek Gelir", f'{totals["manual"]:.0f} {CURRENCY}')
    pdf.kv("Satış adedi", f'{totals["count"]}')
    pdf.kv("TOPLAM GELİR", f'{totals["grand"]:.0f} {CURRENCY}', bold=True)

    # Itemized manual income (Ek Gelir) for the month.
    _manual_income_section(pdf, manual_incomes)

    pdf.section("Günlük Döküm")
    if daily_rows:
        pdf.table_header(["Tarih", "Ciro"], [60, 60])
        for r in daily_rows:
            dd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d-%m-%Y")
            pdf.table_row([dd, f'{r["total"]:.0f} {CURRENCY}'], [60, 60], ["L", "R"])
    else:
        pdf.set_font(pdf.base, "", 11)
        pdf.cell(0, 7, pdf._t("Bu ay satış yok."), new_x="LMARGIN", new_y="NEXT")

    pdf.section("Giderler")
    pdf.set_font(pdf.base, "", 11)
    expense_total = 0.0
    if expenses:
        for e in expenses:
            expense_total += e["amount"]
            pdf.kv(e["description"], f'{e["amount"]:.0f} {CURRENCY}')
        pdf.kv("TOPLAM GİDER", f'{expense_total:.0f} {CURRENCY}', bold=True)
    else:
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 7, pdf._t("Bu ay gider kaydı yok."),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(26, 26, 26)

    # Net profit = income - expenses (correct accounting; income line stays as income).
    net = totals["grand"] - expense_total
    pdf.section("Net Kâr")
    pdf.kv("NET KÂR (Gelir - Gider)", f'{net:.0f} {CURRENCY}', bold=True)

    pdf.section("Tamamlanan İşler (Takvim)")
    if done_tasks:
        pdf.table_header(["Tarih", "İş", "Kategori"], [40, 100, 50])
        for t in done_tasks:
            dd = datetime.strptime(t["date"], "%Y-%m-%d").strftime("%d-%m-%Y")
            pdf.table_row([dd, t["title"], t["category"]], [40, 100, 50], ["L", "L", "C"])
    else:
        pdf.set_font(pdf.base, "", 11)
        pdf.cell(0, 7, pdf._t("Tamamlanan iş yok."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)
    pdf.set_font(pdf.base, "", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.cell(0, 5, pdf._t(f'Oluşturuldu: {datetime.now().strftime("%d-%m-%Y %H:%M")}'),
             align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---------- DATE RANGE (any period, e.g. 07-09 → 15-11) ----------

def _empty_note(pdf, text):
    pdf.set_font(pdf.base, "", 11)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 7, pdf._t(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(26, 26, 26)


def build_range_pdf(start_str, end_str, sold_items, daily_rows, totals,
                    expenses=None, manual_incomes=None, done_tasks=None) -> bytes:
    """
    start_str/end_str : 'YYYY-MM-DD' (inclusive)
    sold_items        : [{name, qty, total}]       products sold in the period
    daily_rows        : [{date, total, count}]     days that had sales
    totals            : {cash, card, count, sales_total, manual,
                         income_total, expense_total, net}
    expenses          : [{description, amount}]    (period's Gider)
    manual_incomes    : [{description, amount}]    (period's Ek Gelir)
    done_tasks        : [{date, title, category}]  completed calendar tasks
    """
    s = datetime.strptime(start_str, "%Y-%m-%d")
    e = datetime.strptime(end_str, "%Y-%m-%d")
    days = (e - s).days + 1
    pdf = _Report()
    pdf.add_page()
    pdf.header_block(f"Dönem Raporu — {s:%d-%m-%Y} – {e:%d-%m-%Y} ({days} gün)")

    # ---- Income summary ----
    pdf.section("Gelir Özeti")
    pdf.kv("Nakit", f'{totals["cash"]:.0f} {CURRENCY}')
    pdf.kv("Kart",  f'{totals["card"]:.0f} {CURRENCY}')
    pdf.kv("Satış Cirosu", f'{totals["sales_total"]:.0f} {CURRENCY}')
    pdf.kv("Satış adedi", f'{totals["count"]}')
    if totals.get("manual", 0):
        pdf.kv("Ek Gelir", f'{totals["manual"]:.0f} {CURRENCY}')
    pdf.kv("TOPLAM GELİR", f'{totals["income_total"]:.0f} {CURRENCY}', bold=True)

    # ---- Expenses ----
    pdf.section("Giderler")
    if expenses:
        pdf.table_header(["Açıklama", "Tutar"], [140, 50])
        for x in expenses:
            pdf.table_row([x["description"], f'{x["amount"]:.0f} {CURRENCY}'], [140, 50], ["L", "R"])
        pdf.kv("TOPLAM GİDER", f'{totals["expense_total"]:.0f} {CURRENCY}', bold=True)
    else:
        _empty_note(pdf, "Bu dönemde gider kaydı yok.")

    # ---- Net ----
    pdf.section("Net Kâr")
    pdf.kv("NET KÂR (Gelir - Gider)", f'{totals["net"]:.0f} {CURRENCY}', bold=True)

    # ---- Products sold ----
    pdf.section("Satılan Ürünler")
    if sold_items:
        pdf.table_header(["Ürün", "Adet", "Tutar"], [110, 30, 50])
        for it in sold_items:
            pdf.table_row([it["name"], f'{it["qty"]:g}', f'{it["total"]:.0f} {CURRENCY}'],
                          [110, 30, 50], ["L", "C", "R"])
    else:
        _empty_note(pdf, "Bu dönemde satış yok.")

    # ---- Per-day breakdown (only days with sales) ----
    pdf.section("Günlük Döküm")
    if daily_rows:
        pdf.table_header(["Tarih", "Satış", "Ciro"], [60, 40, 60])
        for r in daily_rows:
            dd = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d-%m-%Y")
            pdf.table_row([dd, f'{r["count"]}', f'{r["total"]:.0f} {CURRENCY}'], [60, 40, 60], ["L", "C", "R"])
        pdf.table_row(["TOPLAM", f'{totals["count"]}', f'{totals["sales_total"]:.0f} {CURRENCY}'],
                      [60, 40, 60], ["L", "C", "R"])
    else:
        _empty_note(pdf, "Bu dönemde satış yok.")

    # ---- Manual income ----
    _manual_income_section(pdf, manual_incomes)

    # ---- Completed tasks ----
    pdf.section("Tamamlanan İşler (Takvim)")
    if done_tasks:
        pdf.table_header(["Tarih", "İş", "Kategori"], [40, 100, 50])
        for t in done_tasks:
            dd = datetime.strptime(t["date"], "%Y-%m-%d").strftime("%d-%m-%Y")
            pdf.table_row([dd, t["title"], t["category"]], [40, 100, 50], ["L", "L", "C"])
    else:
        _empty_note(pdf, "Bu dönemde tamamlanan iş yok.")

    pdf.ln(8)
    pdf.set_font(pdf.base, "", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.cell(0, 5, pdf._t(f'Oluşturuldu: {datetime.now().strftime("%d-%m-%Y %H:%M")}'),
             align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())