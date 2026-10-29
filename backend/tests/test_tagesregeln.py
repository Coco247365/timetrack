"""Unit-Tests der EINEN Tagesregeln-Klasse (policy.tagesregeln)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from zeiterfassung.policy.tagesregeln import Tagesregeln


@dataclass
class _U:
    employment_type: str | None = "hourly"
    weekday_target_minutes: list | None = None
    contracted_hours_per_week: float | None = 40.0
    work_time_profile_id: str | None = None


@dataclass
class _Req:
    date_from: date
    date_to: date
    day_part: str | None = "FULL"
    days_count: float | None = 1.0


MO, FR, SA = date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 6)


def test_kette_ohne_profil_plan_vor_wochenstunden():
    r = Tagesregeln.fuer(_U(weekday_target_minutes=[495, 495, 495, 495, 330, 0, 0]))
    assert r.tagessoll(MO) == 495
    assert r.tagessoll(FR) == 330
    assert Tagesregeln.fuer(_U()).tagessoll(MO) == 480  # 40h/5
    # Fallback-Stufe ist bewusst wochenend-blind (Bestandsverhalten):
    # Sa/So nullt der Konsument (classify_day bzw. Mo-Fr-Guards).
    assert Tagesregeln.fuer(_U()).tagessoll(SA) == 480
    r_plan = Tagesregeln.fuer(_U(weekday_target_minutes=[480] * 5 + [0, 0]))
    assert r_plan.tagessoll(SA) == 0  # Plan traegt seine Sa/So-Nullen selbst


def test_werkstudent_hat_nie_soll():
    r = Tagesregeln.fuer(_U(employment_type="werkstudent", weekday_target_minutes=[480] * 5))
    assert r.tagessoll(MO) == 0


def test_deckung_einzeltag_traegt_echten_anteil():
    d = date(2026, 6, 2)
    assert Tagesregeln.abwesenheits_deckung(_Req(d, d, days_count=0.75)) == 0.75
    assert Tagesregeln.abwesenheits_deckung(_Req(d, d, days_count=1.0)) == 1.0
    assert Tagesregeln.abwesenheits_deckung(_Req(d, d, days_count=None)) == 0.0
    # geklemmt: mehr als ein Tag Deckung gibt es an einem Tag nicht
    assert Tagesregeln.abwesenheits_deckung(_Req(d, d, days_count=3.0)) == 1.0


def test_deckung_mehrtaegig_halbtag_oder_ganz():
    a, b = date(2026, 6, 2), date(2026, 6, 4)
    assert Tagesregeln.abwesenheits_deckung(_Req(a, b, day_part="FULL", days_count=3.0)) == 1.0
    assert Tagesregeln.abwesenheits_deckung(_Req(a, b, day_part="AM", days_count=1.5)) == 0.5
    assert Tagesregeln.abwesenheits_deckung(_Req(a, b, day_part=None)) == 1.0
