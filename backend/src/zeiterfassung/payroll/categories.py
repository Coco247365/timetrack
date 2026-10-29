"""Registry der Export-Kategorien für den Lohn-Export.

Jede Kategorie hat einen stabilen internen Code, ein deutsches Label, eine
Einheit und eine Kurzbeschreibung der Regel. Es gibt hier bewusst KEINE
Lohnart-Nummern — die hinterlegt der Nutzer selbst in den Einstellungen
(Mapping Code → Nummer), damit jede Kanzlei/jeder Lohnkreis frei bleibt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Unit = Literal["hours", "amount", "days"]


@dataclass(frozen=True)
class Category:
    """Eine Export-Kategorie: was gezählt wird, nicht wohin es gebucht wird."""

    code: str
    label: str
    unit: Unit
    description: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        code="NORMALE_ARBEITSZEIT",
        label="Normale Arbeitszeit",
        unit="hours",
        description="Gearbeitete Zeit Mo–Fr außerhalb gesetzlicher Feiertage "
        "(inkl. Sa/So-Arbeit vor der konfigurierten Zuschlagsschwelle).",
    ),
    Category(
        code="SAMSTAG_ARBEIT",
        label="Samstagsarbeit",
        unit="hours",
        description="Gearbeitete Zeit am Samstag ab der konfigurierbaren "
        "Uhrzeit (saturday_from); davor zählt sie als normale Arbeitszeit.",
    ),
    Category(
        code="SONNTAG_ARBEIT",
        label="Sonntagsarbeit",
        unit="hours",
        description="Gearbeitete Zeit am Sonntag ab der konfigurierbaren "
        "Uhrzeit (sunday_from); davor zählt sie als normale Arbeitszeit.",
    ),
    Category(
        code="FEIERTAG_ARBEIT",
        label="Feiertagsarbeit",
        unit="hours",
        description="Gearbeitete Zeit an gesetzlichen Feiertagen; Feiertag "
        "schlägt Samstag/Sonntag.",
    ),
    Category(
        code="NACHT_ARBEIT",
        label="Nachtarbeit",
        unit="hours",
        description="Gearbeitete Zeit im konfigurierbaren Nachtfenster "
        "(z.B. 23–6 Uhr), minutengenau und ZUSÄTZLICH zur Basis-Kategorie "
        "(Zuschlag überlappt).",
    ),
    Category(
        code="FEIERTAG_BEZAHLT",
        label="Feiertag bezahlt (ohne Arbeit)",
        unit="hours",
        description="Je gesetzlichem Feiertag auf Mo–Fr wird das Tagessoll "
        "gutgeschrieben — nur wenn an dem Tag weder gearbeitet noch "
        "Urlaub/Krank erfasst wurde.",
    ),
    Category(
        code="URLAUB_STD",
        label="Urlaubsstunden",
        unit="hours",
        description="Genehmigte Urlaubstage im Monat × Tagessoll (Halbtage zählen mit 0,5).",
    ),
    Category(
        code="SONDERURLAUB_STD",
        label="Sonderurlaub (bezahlt)",
        unit="hours",
        description="Genehmigte Sonderurlaubstage (z.B. Hochzeit, Todesfall) × Tagessoll. "
        "Ohne zugeordnete Lohnart bleiben die Tage bezahlt in der normalen "
        "Sollzeit (keine eigene Zeile).",
    ),
    Category(
        code="KRANK_STD",
        label="Krankheitsstunden",
        unit="hours",
        description="Genehmigte Kranktage im Monat × Tagessoll (Halbtage zählen mit 0,5). "
        "Sammel-Kategorie: Kranktage ohne AU-Status — oder deren AU-Status-Kategorie "
        "keine eigene Lohnart zugeordnet hat — landen hier.",
    ),
    Category(
        code="KRANK_EAU_TELEFON",
        label="Krank: telefonisch gemeldet (eAU)",
        unit="hours",
        description="Kranktage mit AU-Status 'telefonische Krankmeldung (eAU folgt)'. "
        "Ohne Zuordnung fallen die Stunden auf 'Krankheitsstunden' zurück.",
    ),
    Category(
        code="KRANK_MIT_AU",
        label="Krank: mit AU (mit LFZ)",
        unit="hours",
        description="Kranktage mit AU-Status 'mit AU, mit Lohnfortzahlung'. "
        "Ohne Zuordnung fallen die Stunden auf 'Krankheitsstunden' zurück.",
    ),
    Category(
        code="KRANK_OHNE_AU_MIT_LFZ",
        label="Krank: ohne AU, mit LFZ",
        unit="hours",
        description="Kranktage mit AU-Status 'ohne AU, mit Lohnfortzahlung'. "
        "Ohne Zuordnung fallen die Stunden auf 'Krankheitsstunden' zurück.",
    ),
    Category(
        code="KRANK_OHNE_AU_OHNE_LFZ",
        label="Krank: ohne AU, ohne LFZ",
        unit="hours",
        description="Kranktage mit AU-Status 'ohne AU, ohne Lohnfortzahlung'. "
        "Ohne Zuordnung fallen die Stunden auf 'Krankheitsstunden' zurück.",
    ),
    Category(
        code="MEHRARBEIT_0",
        label="Mehrarbeit ohne Zuschlag",
        unit="hours",
        description="Nur im Export-Modus 'Sollzeit + Mehrarbeit': Stunden über "
        "dem Tagessoll bis zum wöchentlichen Freikontingent (Einstellungen → "
        "Zuschläge, z.B. 1,5 h = Zone 38,5→40 Std).",
    ),
    Category(
        code="MEHRARBEIT_ZUSCHLAG",
        label="Mehrarbeit mit Zuschlag",
        unit="hours",
        description="Nur im Export-Modus 'Sollzeit + Mehrarbeit': Mehrarbeit "
        "über dem wöchentlichen Freikontingent. Über eine Folgelohnart am "
        "Katalog-Eintrag wird der Zuschlag (z.B. 30%) automatisch als zweite "
        "Zeile gebucht (066 → 067).",
    ),
    Category(
        code="MINUSSTUNDEN",
        label="Minusstunden (Abzug)",
        unit="hours",
        description="Nur im Export-Modus 'Sollzeit + Mehrarbeit': Summe der "
        "Tage unter Tagessoll (positiv gebucht auf die Abzugs-Lohnart, "
        "z.B. 013).",
    ),
    Category(
        code="UEBERSTUNDEN_AUSZAHLUNG",
        label="Überstunden-Auszahlung",
        unit="hours",
        description="Auszuzahlende Stunden aus dem Überstunden-Konto; der "
        "Wert kommt vom Aufrufer, die Regel-Schicht definiert nur die "
        "Kategorie.",
    ),
)

_BY_CODE: dict[str, Category] = {c.code: c for c in CATEGORIES}


def all_categories() -> tuple[Category, ...]:
    """Alle Export-Kategorien in definierter Reihenfolge."""
    return CATEGORIES


def get_category(code: str) -> Category:
    """Kategorie zu einem Code; ``KeyError`` bei unbekanntem Code."""
    return _BY_CODE[code]
