"""Monats-Stundenzettel als PDF (reportlab).

Erzeugt aus dem fertigen :class:`MonthlyTimesheet` (report_service) ein
A4-Blatt: Kopf mit Firma/Name/Personalnr./Monat, Tagestabelle mit
Kommt/Geht-Segmenten und Summenblock. Bewusst schlichtes Standard-Layout —
der Optik-Feinschliff kommt spaeter mit einer Vorlage vom Kunden
(Spec 2026-08-07, Nicht-Ziele).
"""

from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from zeiterfassung.schemas.admin_reports import MonthlyTimesheet, TimesheetDay

_MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def _hmm(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    a = abs(round(minutes))
    return f"{sign}{a // 60}:{a % 60:02d}"


def _hmm_diff(minutes: int) -> str:
    return ("+" if minutes > 0 else "") + _hmm(minutes)


def _days(n: float) -> str:
    # "1" statt "1.0", "0,5" deutsch formatiert.
    r = round(n * 100) / 100
    s = f"{r:g}".replace(".", ",")
    return s


def kommt_text(seg: object | None) -> str:
    """Gewertete Kommt-Zeit fuer die Tagestabelle.

    Hat die Profil-Regel "fruehester Beginn" gekappt, steht die echte
    Stempelzeit dahinter in Klammern ("06:00 (05:55)") — so wie es die
    bisherige Abrechnung ausgewiesen hat.
    """
    if seg is None:
        return ""
    if getattr(seg, "clipped_earliest_start", False) and seg.start_punched:
        return f"{seg.start} ({seg.start_punched})"
    return str(seg.start)


def geht_text(seg: object | None) -> str:
    """Gewertete Geht-Zeit fuer die Tagestabelle.

    Hat das Stempel-Fenster eines Arbeitszeit-Profils das ENDE gekappt, steht
    die echte Stempelzeit dahinter in Klammern ("16:00 (16:30)") — analog zur
    Kommt-Spalte.
    """
    if seg is None:
        return ""
    if getattr(seg, "clipped_window_end", False) and getattr(seg, "end_punched", None):
        return f"{seg.end} ({seg.end_punched})"
    return str(seg.end)


def pause_text(day: object) -> str:
    """Angesetzte Pause; hat die gesetzliche Mindestpause angehoben, steht die
    gestempelte Pause dahinter in Klammern ("0:30 (0:15)")."""
    if not day.pause_minutes:
        return ""
    if getattr(day, "pause_raised_by_law", False):
        return f"{_hmm(day.pause_minutes)} ({_hmm(day.pause_punched_minutes or 0)})"
    return _hmm(day.pause_minutes)


def _absence_label(d: TimesheetDay) -> str:
    # Warnhinweis aus dem Tagesbogen (z. B. "Ausstempeln vergessen"): der Tag
    # zaehlt 0 h — das MUSS auf dem Blatt stehen, sonst wirkt die 0 wie Fehlzeit.
    if d.flagged_reason:
        return f"⚠ {d.flagged_reason}"
    if d.absence:
        return d.absence
    if d.holiday:
        return "Feiertag"
    return ""


def build_timesheet_pdf(sheet: MonthlyTimesheet, company_name: str | None = None) -> bytes:
    """PDF-Bytes fuer den Monats-Stundenzettel eines Mitarbeiters."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Stundenzettel {_MONTHS[sheet.month - 1]} {sheet.year} — {sheet.employee_name}",
    )

    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=14, leading=18)
    sub = ParagraphStyle(
        "sub", fontName="Helvetica", fontSize=10, leading=14, textColor=colors.HexColor("#555555")
    )

    story: list = []
    if company_name:
        story.append(Paragraph(company_name, sub))
    story.append(Paragraph(f"Stundenzettel {_MONTHS[sheet.month - 1]} {sheet.year}", h1))
    line2 = sheet.employee_name
    if sheet.employee_number:
        line2 += f" · Personalnr. {sheet.employee_number}"
    story.append(Paragraph(line2, sub))
    story.append(Spacer(1, 6 * mm))

    # --- Tagestabelle --------------------------------------------------------
    head = ["Tag", "Kommt", "Geht", "Pause", "Zeit", "Soll", "+/-", "Abwesenheit"]
    rows: list[list[str]] = [head]
    off_rows: list[int] = []  # Sa/So/Feiertag -> gedaempft
    seg_rows: list[int] = []  # Zusatzzeilen weiterer Segmente
    for d in sheet.days:
        first = d.segments[0] if d.segments else None
        rows.append(
            [
                f"{d.date.strftime('%d.')}{d.weekday}",
                kommt_text(first),
                geht_text(first),
                pause_text(d),
                _hmm(d.ist_minutes) if d.ist_minutes else "",
                _hmm(d.soll_minutes) if d.soll_minutes else "",
                _hmm_diff(d.diff_minutes) if (d.soll_minutes or d.ist_minutes) else "",
                _absence_label(d),
            ]
        )
        if d.weekday in ("Sa", "So") or d.holiday:
            off_rows.append(len(rows) - 1)
        for seg in d.segments[1:]:
            rows.append(["", kommt_text(seg), geht_text(seg), "", "", "", "", ""])
            seg_rows.append(len(rows) - 1)

    # "Kommt", "Geht" und "Pause" breiter: dort koennen "06:00 (05:55)",
    # "16:00 (16:30)" bzw. "0:30 (0:15)" stehen.
    col_w = [16 * mm, 24 * mm, 24 * mm, 20 * mm, 15 * mm, 15 * mm, 15 * mm, None]
    table = Table(rows, colWidths=col_w, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (6, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]
    for r in off_rows:
        style.append(("TEXTCOLOR", (0, r), (-1, r), colors.HexColor("#999999")))
        style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f4f4f4")))
    for r in seg_rows:
        style.append(("LINEBELOW", (0, r - 1), (-1, r - 1), 0, colors.white))
    table.setStyle(TableStyle(style))
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    # --- Summenblock ---------------------------------------------------------
    s = sheet.summary
    sum_rows = [
        # Reihenfolge wie im Alt-System-Zettel: Bruttosoll -> effektives Soll.
        ["Bruttosoll", _hmm(getattr(s, "brutto_soll_minutes", 0)) + " h"],
        ["Soll", _hmm(s.soll_minutes) + " h"],
        ["Ist", _hmm(s.ist_minutes) + " h"],
        ["Saldo", _hmm_diff(s.saldo_minutes) + " h"],
        ["Urlaub", f"{_hmm(s.urlaub_minutes)} h ({_days(s.urlaub_tage)} T)"],
        ["Krank", f"{_hmm(s.krank_minutes)} h ({_days(s.krank_tage)} T)"],
    ]
    sum_table = Table(sum_rows, colWidths=[28 * mm, 45 * mm], hAlign="LEFT")
    sum_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(sum_table)

    doc.build(story)
    return buf.getvalue()
