"""Abrechnungsprofile: pro Mitarbeiter abweichende Lohnart-Zuordnung."""

from zeiterfassung.services.export_service import resolve_employee_lohnart

WAGE_TYPES = {
    "LA_0066": 66,
    "LA_0084": 84,
    "LA_0011": 11,
    # Standard-Zuordnung: Kategorie -> Nummer (wie _get_wage_type_mappings sie liefert)
    "UEBERSTUNDEN_AUSZAHLUNG": 66,
    "NORMALE_ARBEITSZEIT": 11,
}
PROFILES = {
    "p1": {"id": "p1", "name": "Schicht", "mapping": {"UEBERSTUNDEN_AUSZAHLUNG": "LA_0084"}},
}


def test_ohne_profil_gilt_standard():
    assert resolve_employee_lohnart(WAGE_TYPES, PROFILES, None, "UEBERSTUNDEN_AUSZAHLUNG") == 66


def test_profil_ueberschreibt_nur_die_abweichung():
    assert resolve_employee_lohnart(WAGE_TYPES, PROFILES, "p1", "UEBERSTUNDEN_AUSZAHLUNG") == 84
    # Nicht abgedeckte Kategorie faellt auf den Standard zurueck.
    assert resolve_employee_lohnart(WAGE_TYPES, PROFILES, "p1", "NORMALE_ARBEITSZEIT") == 11


def test_unbekanntes_profil_faellt_auf_standard():
    assert (
        resolve_employee_lohnart(WAGE_TYPES, PROFILES, "geloescht", "UEBERSTUNDEN_AUSZAHLUNG") == 66
    )


def test_profil_mit_ungueltigem_code_ignoriert():
    profiles = {"p2": {"id": "p2", "mapping": {"NORMALE_ARBEITSZEIT": "LA_9999"}}}
    assert resolve_employee_lohnart(WAGE_TYPES, profiles, "p2", "NORMALE_ARBEITSZEIT") == 11


def test_nicht_zugeordnete_kategorie_bleibt_none():
    assert resolve_employee_lohnart(WAGE_TYPES, PROFILES, "p1", "NACHT_ARBEIT") is None
