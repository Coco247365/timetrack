"""LODAS-Ausgabeformat: muss byte-genau dem echten Referenz-Export entsprechen.

Zwei frueher falsche Stellen sind hier abgesichert:
- Zeitraum als DD/MM/YYYY (Tag/Monat/Jahr), nicht MM/DD.
- Wert mit 5 Stellen vor dem Komma (00046,50), nicht 6.
Referenzzeile aus LODAS-REF.txt: ``3;01/05/2026;00046,50;1;011;13426``
"""

from __future__ import annotations

from datetime import date

from zeiterfassung.core.datev import (
    DatevRow,
    LodasConfig,
    _format_lodas_period,
    _format_lodas_value,
    write_lodas_export,
)


def test_period_is_day_month_year():
    assert _format_lodas_period(date(2026, 5, 1)) == "01/05/2026"
    assert _format_lodas_period(date(2026, 7, 1)) == "01/07/2026"
    assert _format_lodas_period(date(2026, 12, 1)) == "01/12/2026"


def test_value_has_five_integer_digits():
    assert _format_lodas_value(46.5) == "00046,50"
    assert _format_lodas_value(4.5) == "00004,50"
    assert _format_lodas_value(154.0) == "00154,00"
    assert _format_lodas_value(160.5) == "00160,50"


def test_full_row_matches_reference_line():
    rows = [
        DatevRow(
            personalnummer="13426",
            lohnart=11,
            wert=46.5,
            abrechnungszeitraum=date(2026, 5, 1),
        )
    ]
    out = write_lodas_export(rows, LodasConfig(berater_nr="12345", mandanten_nr="54321"))
    text = out.decode("windows-1252")
    # Exakt die Referenzzeile aus LODAS-REF.txt.
    assert "3;01/05/2026;00046,50;1;011;13426\r\n" in text
    assert "BeraterNr=12345" in text
    assert "MandantenNr=54321" in text


def test_pnr_wird_fuenfstellig_aufgefuellt():
    """Wie im echten Export: 27 -> 00027, 41001 bleibt 41001, Nicht-Numerisches unveraendert."""
    from zeiterfassung.core.datev import _format_lodas_pnr

    assert _format_lodas_pnr("27") == "00027"
    assert _format_lodas_pnr("5") == "00005"
    assert _format_lodas_pnr("41001") == "41001"
    assert _format_lodas_pnr("123456") == "123456"  # laenger als 5: nicht kuerzen
    assert _format_lodas_pnr("DEMO-MA") == "DEMO-MA"


def test_lodas_dateiname_wie_altsystem():
    """2026-05_BETA_LODAS.txt — Firma gross, Umlaute transkribiert, Fallback ohne Firma."""
    from zeiterfassung.services.export_service import build_lodas_filename

    assert build_lodas_filename(2026, 5, "beta") == "2026-05_BETA_LODAS.txt"
    assert build_lodas_filename(2026, 6, "Alpha") == "2026-06_ALPHA_LODAS.txt"
    assert build_lodas_filename(2026, 5, "Müller & Söhne") == "2026-05_MUELLER___SOEHNE_LODAS.txt"
    assert build_lodas_filename(2026, 5, None) == "LODAS_2026-05.txt"
    assert build_lodas_filename(2026, 5, "  ") == "LODAS_2026-05.txt"
