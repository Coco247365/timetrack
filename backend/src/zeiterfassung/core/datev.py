"""DATEV LODAS export writer.

Generates the LODAS Bewegungsdaten format used by DATEV Lohn & Gehalt.
Matches a real-world LODAS export structure (BeraterNr 12345, MandantenNr 54321).

File format:
    [Allgemein]
    ZIEL=LODAS
    BeraterNr=12345
    MandantenNr=54321
    [Satzbeschreibung]
    3;u_lod_bwd_buchung_standard;abrechnung_zeitraum#bwd;bs_wert_butab#bwd;bs_nr#bwd;la_eigene#bwd;pnr#bwd
    1;u_lod_psd_fehlzeiten;datum_von_ttmmjjjj#psd;datum_bis_ttmmjjjj#psd;pnr#psd;grund_fehlzeiten#psd
    [Bewegungsdaten]
    3;01/02/2026;00154,00;1;011;13426

Reference: incoming/AJO_LODAS_Februar_2026.txt
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatevRow:
    """A single LODAS wage type booking row (record type 3)."""

    personalnummer: str
    lohnart: int
    wert: float  # Value in hours (2 decimal places)
    abrechnungszeitraum: date  # First day of the payroll month


@dataclass(frozen=True)
class DatevAbsenceRow:
    """A LODAS absence period row (record type 1)."""

    personalnummer: str
    datum_von: date
    datum_bis: date
    grund: str  # Absence reason code


@dataclass(frozen=True)
class LodasConfig:
    """LODAS export configuration from tenant settings."""

    berater_nr: str = "12345"
    mandanten_nr: str = "54321"


# Hinweis: Die früheren AJO_WAGE_TYPES/AJO_WAGE_TYPE_LABELS (hartkodierte
# DATEV-Nummern) sind entfernt. Kategorien kommen aus
# ``zeiterfassung.payroll.categories``; die Nummern pflegt der Nutzer im
# Katalog (``wage_type_mappings``). Exportiert wird nur, was eine Nummer hat.


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _format_lodas_pnr(pnr: str) -> str:
    """Personalnummer wie im echten Referenz-Export: rein numerische Nummern
    5-stellig mit fuehrenden Nullen (``27`` -> ``00027``); nicht-numerische
    Nummern unveraendert lassen."""
    p = (pnr or "").strip()
    return p.zfill(5) if p.isdigit() else p


def _format_lodas_value(value: float) -> str:
    """Format a value as LODAS expects: ``00046,50`` — 5 Stellen vor dem Komma,
    2 danach, deutsches Komma, nullengefuellt (8 Zeichen gesamt)."""
    # 08.2f = 5 Vorkommastellen + '.' + 2 Nachkommastellen; Punkt -> Komma.
    return f"{value:08.2f}".replace(".", ",")


def _format_lodas_period(d: date) -> str:
    """Format period as DD/MM/YYYY (LODAS-Format wie im echten Referenz-Export).

    Beispielzeile ``3;01/05/2026;...`` = 1. Mai 2026 (Tag/Monat/Jahr).
    """
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def write_lodas_export(
    rows: list[DatevRow],
    config: LodasConfig | None = None,
    absence_rows: list[DatevAbsenceRow] | None = None,
) -> bytes:
    """Write a complete LODAS export file.

    Returns Windows-1252 encoded bytes matching the real Referenz export format.
    """
    cfg = config or LodasConfig()
    buf = io.StringIO()

    # Header
    buf.write("[Allgemein]\r\n")
    buf.write("ZIEL=LODAS\r\n")
    buf.write(f"BeraterNr={cfg.berater_nr}\r\n")
    buf.write(f"MandantenNr={cfg.mandanten_nr}\r\n")

    # Schema description
    buf.write("[Satzbeschreibung]\r\n")
    buf.write(
        "3;u_lod_bwd_buchung_standard;abrechnung_zeitraum#bwd;bs_wert_butab#bwd;bs_nr#bwd;la_eigene#bwd;pnr#bwd\r\n"
    )
    buf.write(
        "1;u_lod_psd_fehlzeiten;datum_von_ttmmjjjj#psd;datum_bis_ttmmjjjj#psd;pnr#psd;grund_fehlzeiten#psd\r\n"
    )

    # Movement data
    buf.write("[Bewegungsdaten]\r\n")

    # Sort: by PNR, then by Lohnart for readability
    sorted_rows = sorted(rows, key=lambda r: (r.personalnummer, r.lohnart))

    for row in sorted_rows:
        if row.wert <= 0:
            continue
        period = _format_lodas_period(row.abrechnungszeitraum)
        value = _format_lodas_value(row.wert)
        la = f"{row.lohnart:03d}"
        buf.write(f"3;{period};{value};1;{la};{_format_lodas_pnr(row.personalnummer)}\r\n")

    # Absence rows (type 1) — if any
    if absence_rows:
        for ar in sorted(absence_rows, key=lambda r: (r.personalnummer, r.datum_von.isoformat())):
            von = f"{ar.datum_von.day:02d}{ar.datum_von.month:02d}{ar.datum_von.year}"
            bis = f"{ar.datum_bis.day:02d}{ar.datum_bis.month:02d}{ar.datum_bis.year}"
            buf.write(f"1;{von};{bis};{_format_lodas_pnr(ar.personalnummer)};{ar.grund}\r\n")

    return buf.getvalue().encode("windows-1252", errors="replace")


# Keep backward-compatible aliases
def write_datev_csv(rows: list[DatevRow]) -> bytes:
    """Backward-compatible wrapper — converts old DatevRow format to LODAS."""
    return write_lodas_export(rows)


def compute_checksum(data: bytes) -> str:
    """Return SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def preview_datev_export(rows: list[DatevRow]) -> list[dict[str, object]]:
    """Return rows as a list of dicts (for API preview endpoint)."""
    return [
        {
            "personalnummer": r.personalnummer,
            "lohnart": r.lohnart,
            "wert": r.wert,
            "datum_von": r.abrechnungszeitraum.isoformat(),
            "datum_bis": r.abrechnungszeitraum.isoformat(),
            "kostenstelle": "",
        }
        for r in rows
    ]
