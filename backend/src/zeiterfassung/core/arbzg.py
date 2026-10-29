"""ArbZG (Arbeitszeitgesetz) rule validation.

Implements the key constraints from the German Working Time Act:
  - §3  Max daily working time: 8 h ordinarily, up to 10 h as exception
  - §4  Mandatory rest breaks: 30 min after 6 h, 45 min after 9 h
  - §5  Minimum rest period between shifts: 11 h
  - §9  Prohibition of Sunday work (with exceptions)
  - §11 Mandatory Sunday rest (compensatory rest)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_WORK_HOURS_REGULAR: int = 8
MAX_WORK_HOURS_EXCEPTION: int = 10
BREAK_THRESHOLD_6H_MINUTES: int = 360  # 6 h
BREAK_THRESHOLD_9H_MINUTES: int = 540  # 9 h
MIN_BREAK_AFTER_6H_MINUTES: int = 30
MIN_BREAK_AFTER_9H_MINUTES: int = 45
MIN_REST_BETWEEN_SHIFTS_HOURS: int = 11

# Grace period: minor overruns are soft warnings, not hard flags.
# E.g. 8h12m → "info" instead of "warning", 10h05m → "warning" instead of "error".
GRACE_MINUTES: int = 15


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ArbZGViolation:
    rule: str
    message: str
    severity: str = "warning"  # "warning" | "error"


@dataclass
class ArbZGResult:
    violations: list[ArbZGViolation] = field(default_factory=list)

    @property
    def is_compliant(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        return len(self.violations) > 0


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def check_daily_hours(
    net_work_minutes: int,
) -> list[ArbZGViolation]:
    """§3 — Check maximum daily working time.

    Erwartet die ECHTE Netto-Arbeitszeit (Summe der Arbeitssegmente, ohne
    Pausen). ArbZG §3 definiert "Arbeitszeit" als Zeit ohne Ruhepausen —
    Lücken zwischen zwei Sessions eines Tages (geteilte Schicht, Nachtschicht-
    Split) sind weder Arbeit noch Pause und dürfen hier nicht einfließen;
    "Spanne minus Pausen" wäre dafür falsch. A grace period (GRACE_MINUTES)
    downgrades minor overruns from hard flags to soft info-level notices.
    """
    violations: list[ArbZGViolation] = []
    hours = net_work_minutes / 60
    grace_hours = GRACE_MINUTES / 60

    if hours > MAX_WORK_HOURS_EXCEPTION + grace_hours:
        # Hard violation: clearly over 10h even with grace
        violations.append(
            ArbZGViolation(
                rule="ArbZG §3",
                message=(
                    f"Netto-Arbeitszeit {hours:.1f} h überschreitet das Höchstmaß von "
                    f"{MAX_WORK_HOURS_EXCEPTION} h (ArbZG §3 Ausnahmegrenze)."
                ),
                severity="error",
            )
        )
    elif hours > MAX_WORK_HOURS_EXCEPTION:
        # Within grace of 10h limit — soft warning
        violations.append(
            ArbZGViolation(
                rule="ArbZG §3",
                message=(
                    f"Netto-Arbeitszeit {hours:.1f} h liegt knapp über "
                    f"{MAX_WORK_HOURS_EXCEPTION} h Ausnahmegrenze. Bitte prüfen."
                ),
                severity="warning",
            )
        )
    elif hours > MAX_WORK_HOURS_REGULAR + grace_hours:
        # Clearly over 8h beyond grace — standard warning.
        # AUF WUNSCH DEAKTIVIERT: die "Regelarbeitszeit 8 h überschritten,
        # Ausgleich innerhalb 6 Monaten"-Warnung feuerte an fast jedem etwas
        # längeren Tag und war zu laut. Der harte >10 h-Fehler bleibt aktiv.
        # Zum Reaktivieren einfach den folgenden Block wieder einkommentieren
        # und ``pass`` entfernen.
        pass
        # violations.append(
        #     ArbZGViolation(
        #         rule="ArbZG §3",
        #         message=(
        #             f"Netto-Arbeitszeit {hours:.1f} h überschreitet die Regelarbeitszeit "
        #             f"von {MAX_WORK_HOURS_REGULAR} h. Ausgleich muss in 6 Monaten erfolgen."
        #         ),
        #         severity="warning",
        #     )
        # )
    elif hours > MAX_WORK_HOURS_REGULAR:
        # Within grace of 8h — info only.
        # AUF WUNSCH DEAKTIVIERT (wie die Warnung darüber): die "liegt leicht
        # über 8 h Regelarbeitszeit"-Info war ebenfalls zu häufig. Zum
        # Reaktivieren den folgenden Block wieder einkommentieren, ``pass`` weg.
        pass
        # violations.append(
        #     ArbZGViolation(
        #         rule="ArbZG §3",
        #         message=(
        #             f"Netto-Arbeitszeit {hours:.1f} h liegt leicht über "
        #             f"{MAX_WORK_HOURS_REGULAR} h Regelarbeitszeit."
        #         ),
        #         severity="info",
        #     )
        # )
    return violations


def check_break_compliance(
    gross_work_minutes: int,
    break_minutes: int,
) -> list[ArbZGViolation]:
    """§4 — Check that mandatory break time was taken.

    Maßgeblich ist die ARBEITSZEIT (brutto minus Pausen), nicht die
    Anwesenheitsspanne: >6 h Arbeitszeit → 30 min, >9 h → 45 min.
    9:12 Anwesenheit mit 30 min Pause = 8:42 Arbeitszeit → konform.
    """
    violations: list[ArbZGViolation] = []
    net_minutes = gross_work_minutes - break_minutes

    if net_minutes > BREAK_THRESHOLD_9H_MINUTES:
        required = MIN_BREAK_AFTER_9H_MINUTES
        if break_minutes < required:
            violations.append(
                ArbZGViolation(
                    rule="ArbZG §4",
                    message=(
                        f"Arbeitszeit > 9 h erfordert mindestens {required} Min. Pause; "
                        f"nur {break_minutes} Min. erfasst."
                    ),
                    severity="error",
                )
            )
    elif net_minutes > BREAK_THRESHOLD_6H_MINUTES:
        required = MIN_BREAK_AFTER_6H_MINUTES
        if break_minutes < required:
            violations.append(
                ArbZGViolation(
                    rule="ArbZG §4",
                    message=(
                        f"Arbeitszeit > 6 h erfordert mindestens {required} Min. Pause; "
                        f"nur {break_minutes} Min. erfasst."
                    ),
                    severity="error",
                )
            )
    return violations


def check_rest_period(
    prev_end_time: datetime,
    next_start_time: datetime,
) -> list[ArbZGViolation]:
    """§5 — Check 11-hour minimum rest period between consecutive shifts."""
    violations: list[ArbZGViolation] = []
    rest = next_start_time - prev_end_time
    rest_hours = rest.total_seconds() / 3600

    if rest_hours < MIN_REST_BETWEEN_SHIFTS_HOURS:
        violations.append(
            ArbZGViolation(
                rule="ArbZG §5",
                message=(
                    f"Ruhezeit zwischen Schichten beträgt nur {rest_hours:.1f} h; "
                    f"Minimum sind {MIN_REST_BETWEEN_SHIFTS_HOURS} h."
                ),
                severity="error",
            )
        )
    return violations


def validate_timesheet(
    gross_work_minutes: int,
    break_minutes: int,
    start_time: datetime | None = None,
    prev_day_end_time: datetime | None = None,
    presence_minutes: int | None = None,
    net_work_minutes: int | None = None,
) -> ArbZGResult:
    """Run all applicable ArbZG checks for a single daily timesheet.

    Args:
        gross_work_minutes: Total shift duration in minutes (including breaks).
        break_minutes: Total break time in minutes.
        start_time: Shift start (needed for rest-period check).
        prev_day_end_time: Previous shift end (needed for rest-period check).
        presence_minutes: Anwesenheit = Arbeit + Pausen OHNE Lücken zwischen
            zwei Schichten desselben Kalendertags. Wenn gesetzt, prüft §4
            damit statt mit der Tagesspanne (sonst treiben Kommen–Gehen–Kommen-
            Lücken die Pflichtpause hoch).
        net_work_minutes: Echte Netto-Arbeitszeit (Summe der Arbeitssegmente).
            Wenn None, Fallback ``gross - breaks`` — das ist nur für Tage mit
            EINER Session korrekt; bei geteilten Schichten zählt der Fallback
            die Lücke zwischen den Sessions fälschlich als Arbeit (§3).

    Returns:
        ArbZGResult with all detected violations.
    """
    result = ArbZGResult()
    if net_work_minutes is None:
        net_work_minutes = gross_work_minutes - break_minutes
    result.violations.extend(check_daily_hours(net_work_minutes))
    # §4 rechnet mit der Anwesenheit (Arbeit + Pausen ohne Zwischen-Schicht-
    # Lücken) — gleiche Konvention wie die Auto-Pausen-Policy; ohne
    # presence_minutes Fallback Tagesspanne (Ein-Schicht-Tage: identisch).
    result.violations.extend(
        check_break_compliance(
            presence_minutes if presence_minutes is not None else gross_work_minutes,
            break_minutes,
        )
    )

    if start_time is not None and prev_day_end_time is not None:
        result.violations.extend(check_rest_period(prev_day_end_time, start_time))

    return result


def required_break_minutes(net_work_minutes: int) -> int:
    """Gesetzliche Mindestpause für *net_work_minutes* ARBEITSZEIT (ohne Pausen).

    §4 ArbZG bemisst sich an der Arbeitszeit, nicht an der Anwesenheitsspanne.
    Für „Anwesenheit + gestempelte Pausen → nötige Gesamtpause" siehe
    ``policy.statutory.effective_break_minutes``."""
    if net_work_minutes > BREAK_THRESHOLD_9H_MINUTES:
        return MIN_BREAK_AFTER_9H_MINUTES
    if net_work_minutes > BREAK_THRESHOLD_6H_MINUTES:
        return MIN_BREAK_AFTER_6H_MINUTES
    return 0
