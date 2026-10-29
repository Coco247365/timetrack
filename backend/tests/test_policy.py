"""Tests für die reine Policy-Schicht (ohne DB — reine Regelfunktionen)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from zeiterfassung.policy import Severity, absence, check, permissions, statutory, timekeeping
from zeiterfassung.policy.context import AbsenceEntry, BreakInterval, DayPart

# ---------------------------------------------------------------------------
# A — Abwesenheit
# ---------------------------------------------------------------------------


def _abs(d_from, d_to, part=DayPart.FULL, typ="VACATION", status="APPROVED", rid="x"):
    return AbsenceEntry(d_from, d_to, part, typ, status, rid)


def test_overlap_fullday_blocks():
    neu = _abs(date(2026, 8, 3), date(2026, 8, 5), rid="new")
    best = _abs(date(2026, 8, 4), date(2026, 8, 4), typ="SICK_LEAVE", rid="x1")
    v = absence.check_no_overlap(neu, [best])
    assert v is not None and v.is_block
    assert v.refs["conflict_from"] == "2026-08-04"


def test_overlap_am_pm_coexist():
    am = _abs(date(2026, 8, 4), date(2026, 8, 4), DayPart.AM, rid="am")
    pm = _abs(date(2026, 8, 4), date(2026, 8, 4), DayPart.PM, rid="pm")
    assert absence.check_no_overlap(am, [pm]) is None


def test_overlap_same_halfday_blocks():
    am = _abs(date(2026, 8, 4), date(2026, 8, 4), DayPart.AM, rid="am")
    am2 = _abs(date(2026, 8, 4), date(2026, 8, 4), DayPart.AM, status="SUBMITTED", rid="am2")
    assert absence.check_no_overlap(am, [am2]).is_block


def test_overlap_ignores_dead_status():
    neu = _abs(date(2026, 8, 3), date(2026, 8, 5), rid="new")
    cancelled = _abs(date(2026, 8, 4), date(2026, 8, 4), status="CANCELLED", rid="c1")
    assert absence.check_no_overlap(neu, [cancelled]) is None


def test_overlap_ignores_self():
    e = _abs(date(2026, 8, 3), date(2026, 8, 5), rid="same")
    assert absence.check_no_overlap(e, [e]) is None


def test_balance_warns_when_exceeded():
    v = absence.check_balance_warning(-4.0)
    assert v is not None and v.severity is Severity.WARN
    assert v.refs["missing_hours"] == 4.0
    assert absence.check_balance_warning(2.0) is None


# ---------------------------------------------------------------------------
# B — Pausen
# ---------------------------------------------------------------------------

_WS = datetime(2026, 8, 3, 8, 0)
_WE = datetime(2026, 8, 3, 17, 0)


def test_break_valid():
    b = BreakInterval(datetime(2026, 8, 3, 12, 0), datetime(2026, 8, 3, 12, 30))
    assert timekeeping.check_break(b, work_start=_WS, work_end=_WE) == []


def test_break_outside_work_blocks():
    b = BreakInterval(datetime(2026, 8, 3, 7, 0), datetime(2026, 8, 3, 7, 30))
    codes = {v.code for v in timekeeping.check_break(b, work_start=_WS, work_end=_WE)}
    assert "break.outside_work" in codes


def test_break_open_shift_no_upper_cap():
    # Offene Schicht (kein Geht): nur die Untergrenze (nach dem Kommt) greift.
    # Nach oben KEIN Wanduhr-Deckel — Pause nach dem Kommt ist immer ok.
    spaet = BreakInterval(datetime(2026, 8, 3, 19, 0), datetime(2026, 8, 3, 19, 30))
    assert timekeeping.check_break(spaet, work_start=_WS, work_end=None) == []
    # Vor dem Kommt bleibt blockiert.
    frueh = BreakInterval(datetime(2026, 8, 3, 7, 0), datetime(2026, 8, 3, 7, 30))
    codes = {v.code for v in timekeeping.check_break(frueh, work_start=_WS, work_end=None)}
    assert "break.outside_work" in codes


def test_break_future_day_allowed():
    # Arbeitszeit für einen zukünftigen Tag (Kommt+Geht gesetzt): Pause im
    # Fenster ist erlaubt, egal ob der Tag in der Zukunft liegt.
    ws = datetime(2035, 1, 2, 8, 0)
    we = datetime(2035, 1, 2, 17, 0)
    b = BreakInterval(datetime(2035, 1, 2, 12, 0), datetime(2035, 1, 2, 12, 30))
    assert timekeeping.check_break(b, work_start=ws, work_end=we) == []


def test_break_overlap_blocks():
    p1 = BreakInterval(datetime(2026, 8, 3, 12, 0), datetime(2026, 8, 3, 12, 30))
    p2 = BreakInterval(datetime(2026, 8, 3, 12, 15), datetime(2026, 8, 3, 12, 45))
    codes = {
        v.code for v in timekeeping.check_break(p2, work_start=_WS, work_end=_WE, existing=[p1])
    }
    assert "break.overlap" in codes


def test_break_end_before_start_blocks():
    b = BreakInterval(datetime(2026, 8, 3, 13, 0), datetime(2026, 8, 3, 12, 0))
    vs = timekeeping.check_break(b, work_start=_WS, work_end=_WE)
    assert vs[0].code == "break.end_before_start"


def test_work_blocked_on_sick_day():
    sick = _abs(date(2026, 8, 3), date(2026, 8, 5), typ="SICK_LEAVE")
    v = timekeeping.check_work_allowed_on_date(date(2026, 8, 4), [sick])
    assert v is not None and v.is_block and v.code == "timekeeping.on_sick_day"
    # außerhalb des Krank-Zeitraums erlaubt
    assert timekeeping.check_work_allowed_on_date(date(2026, 8, 6), [sick]) is None
    # Urlaub blockt Arbeitszeit NICHT
    vac = _abs(date(2026, 8, 3), date(2026, 8, 5), typ="VACATION")
    assert timekeeping.check_work_allowed_on_date(date(2026, 8, 4), [vac]) is None
    # storniertes Krank zählt nicht
    cx = _abs(date(2026, 8, 3), date(2026, 8, 5), typ="SICK_LEAVE", status="CANCELLED")
    assert timekeeping.check_work_allowed_on_date(date(2026, 8, 4), [cx]) is None


# ---------------------------------------------------------------------------
# D — ArbZG
# ---------------------------------------------------------------------------


def test_arbzg_over_10h_warns_never_blocks():
    vs = statutory.check_arbzg(gross_work_minutes=11 * 60, break_minutes=0)
    assert vs, "erwarte ArbZG-Befunde"
    assert all(v.severity is Severity.WARN for v in vs)
    assert any(v.code.startswith("arbzg.") for v in vs)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# E — Sichtbarkeit / Berechtigung
# ---------------------------------------------------------------------------


@dataclass
class _Scope:
    is_anonymized: bool = False
    is_self_only: bool = False
    is_department: bool = False
    is_global: bool = False
    department_id: uuid.UUID | None = None
    user_id: uuid.UUID = uuid.UUID(int=1)


def test_self_approval_blocked():
    e = uuid.uuid4()
    assert permissions.check_not_self_approval(e, e).is_block
    assert permissions.check_not_self_approval(e, uuid.uuid4()) is None


def test_visibility_scopes():
    dep = uuid.uuid4()
    me = uuid.uuid4()
    other = uuid.uuid4()
    # global sieht alle, anonymisiert (BR) niemanden
    assert permissions.is_employee_visible(
        _Scope(is_global=True), target_user_id=other, target_department_id=dep
    )
    assert not permissions.is_employee_visible(
        _Scope(is_anonymized=True), target_user_id=other, target_department_id=dep
    )
    # Abteilung: nur eigene
    dept_scope = _Scope(is_department=True, department_id=dep)
    assert permissions.is_employee_visible(
        dept_scope, target_user_id=other, target_department_id=dep
    )
    assert not permissions.is_employee_visible(
        dept_scope, target_user_id=other, target_department_id=uuid.uuid4()
    )
    # self: nur man selbst
    self_scope = _Scope(is_self_only=True, user_id=me)
    assert permissions.is_employee_visible(self_scope, target_user_id=me, target_department_id=dep)
    assert not permissions.is_employee_visible(
        self_scope, target_user_id=other, target_department_id=dep
    )


def test_aggregator_collects_all():
    neu = _abs(date(2026, 8, 3), date(2026, 8, 5), rid="new")
    best = _abs(date(2026, 8, 4), date(2026, 8, 4), rid="x1")
    res = check.check_new_absence(neu, [best], remaining_hours_after=-3.0)
    codes = {v.code for v in res}
    assert codes == {"absence.overlap", "absence.balance_exceeded"}
    assert check.has_blockers(res)
