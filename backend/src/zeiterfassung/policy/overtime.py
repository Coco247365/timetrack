"""Regelschicht Überstunden / Arbeitszeitkonto — reine Funktionen ohne DB/HTTP.

Hier liegt die *fachliche* Tagesregel des Arbeitszeitkontos an EINER Stelle. Der
``overtime_service`` beschafft nur die Daten (Tagesbögen, Abwesenheiten, Feiertage,
Erfassungsbeginn) und mappt sie auf :class:`DayFacts`; die Bewertung passiert hier.

Kernregeln:
- Tages-Soll = Wochenstunden ÷ 5 (Werktag; an Wochenende/Feiertag 0).
- Abwesenheit (Urlaub/Krank/…) deckt ihren Anteil des Solls (ganz/halb).
- ``Ist − Soll`` je Arbeitstag = Beitrag zum Saldo (über/unter Soll).
- Unentschuldigtes Fehlen: Werktag ab Erfassungsbeginn ohne erfasste Zeit und ohne
  (volle) Abwesenheit → −Soll. Für Stempler (NFC/web) bei fehlendem Tagesbogen in der
  Vergangenheit; für alle bei ausdrücklich leerem Tagesbogen („nicht gearbeitet",
  heute einschließlich). Teilweise gedeckte Tage (z. B. halber Urlaub) sind KEIN
  „unentschuldigt", sondern der (halbe) Abwesenheitstag mit nur der ungedeckten Minus-Zeit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: Beschaeftigungsarten, die reine Zeiterfassung sind: es wird gestempelt und
#: angezeigt, aber es gibt kein Tagessoll — also kein Minus, kein
#: "unentschuldigt" und keinen Ueberstunden-Saldo. Sie laufen ausserdem nie in
#: den DATEV-Export (siehe services/export_service).
ERFASSUNG_OHNE_SOLL = frozenset({"werkstudent"})


def hat_tagessoll(employment_type: str | None) -> bool:
    """Hat diese Beschaeftigungsart ueberhaupt ein Tagessoll?"""
    return (employment_type or "").lower() not in ERFASSUNG_OHNE_SOLL


def daily_target_minutes(
    weekly_hours: float | None,
    *,
    weekday: int | None = None,
    weekday_minutes: list | None = None,
    employment_type: str | None = None,
) -> int:
    """Tages-Soll in Minuten.

    Ist ein wochentagsspezifischer Plan ``weekday_minutes`` (Liste Mo..So = Index
    0..6, in Minuten) gesetzt UND ein ``weekday`` (0=Mo..6=So) übergeben, gilt der
    Plan-Wert für diesen Wochentag. Sonst Fallback: Wochenstunden ÷ 5 Werktage
    (Fallback 40 h → 8 h). Die *eine* Quelle dieser Umrechnung — überall darauf
    verweisen, nicht neu rechnen.

    Beschaeftigungsarten in :data:`ERFASSUNG_OHNE_SOLL` (Werkstudenten, Aushilfen)
    haben grundsaetzlich 0 — ihre Zeit wird erfasst, aber gegen nichts gemessen.
    """
    if not hat_tagessoll(employment_type):
        return 0
    if (
        weekday_minutes is not None
        and weekday is not None
        and 0 <= weekday < len(weekday_minutes)
        and weekday_minutes[weekday] is not None
    ):
        return max(0, int(round(float(weekday_minutes[weekday]))))
    wh = float(weekly_hours) if weekly_hours else 40.0
    if wh <= 0:
        wh = 40.0
    return round(wh / 5.0 * 60.0)


def weekly_target_minutes(weekly_hours: float | None, weekday_minutes: list | None = None) -> int:
    """Wochen-Soll in Minuten — Summe des Plans (Mo..So) bzw. Wochenstunden × 60."""
    if weekday_minutes:
        return sum(max(0, int(round(float(m)))) for m in weekday_minutes if m is not None)
    wh = float(weekly_hours) if weekly_hours else 40.0
    if wh <= 0:
        wh = 40.0
    return round(wh * 60.0)


@dataclass(frozen=True)
class DayFacts:
    """Alles, was zur Bewertung eines Tages nötig ist (vom Service befüllt)."""

    day: date
    weekend: bool
    holiday: bool
    coverage: float  # Abwesenheits-Deckung 0..1 (0.5 = halber Urlaub)
    net_minutes: int  # gearbeitete Netto-Minuten (0 ohne Erfassung)
    has_timesheet: bool  # existiert ein Tagesbogen (auch ein leerer)?
    worked: bool  # echter Arbeitstag (net>0 oder Ein-Stempel)
    is_auto: bool  # Auto-Erfassung (immer gefüllt → nie „unentschuldigt" bei fehlendem Bogen)
    is_past: bool  # day < heute
    is_today_or_past: bool  # day <= heute
    within_cutoff: bool  # day >= Erfassungsbeginn (sonst zählt gar nichts)
    # Anteil der coverage, der aus URLAUB stammt (0..1). Wird am Tag trotzdem
    # gearbeitet, deckt Urlaub das Soll NICHT (Urlaub wird zurückgegeben, der Tag
    # zählt normal); Krank & Co. decken weiterhin ihren Anteil.
    vacation_coverage: float = 0.0
    # Anteil des Tages mit "Überstunden abbauen" (0..1). Zählt NICHT als
    # Deckung (Soll bleibt offen → Minus), verhindert aber die Einstufung als
    # "unentschuldigt" und lässt das Minus auch ohne Tagesbogen zählen.
    reduction: float = 0.0
    # Tages-Verbuchung von Wochenend-/Feiertagsarbeit (daily_timesheets.weekend_booking):
    # None = Standard (Überstunden → Direktvergütung, bzw. Profil-Regel),
    # "gleitzeit" = zählt ins Konto, "ueberstunden" = explizit Direktvergütung.
    weekend_booking: str | None = None


@dataclass(frozen=True)
class DayResult:
    """Beitrag eines Tages zum Konto."""

    net_minutes: int
    target_minutes: int
    delta_minutes: int  # net − target (Basis des Saldos)
    absent: bool  # unentschuldigtes Fehlen (rot; nur ohne jede Abwesenheit)
    coverage: float


def classify_day(
    f: DayFacts,
    soll_minutes: int,
    *,
    weekend_counts: bool = False,
    hat_soll: bool = True,
) -> DayResult | None:
    """Beitrag eines Tages, oder ``None`` wenn der Tag nicht zählt.

    ``soll_minutes`` = volles Tages-Soll (aus :func:`daily_target_minutes`).
    ``weekend_counts``: Profil-Regel ``wochenende_ins_konto`` — Default ``False``
    ist das bisherige Verhalten (#21: WE-/Feiertagsarbeit hat ihre eigene Lohnart
    und zählt nicht ins Konto). ``True`` kehrt das für firmenspezifische Profile
    um (z. B. Beta: Samstagsarbeit fließt ins Gleitzeitkonto).

    ``f.weekend_booking`` (Tages-Wahl am Bogen) übersteuert die Profil-Regel:
    "gleitzeit" → zählt ins Konto, "ueberstunden" → Direktvergütung,
    None → Profil-Regel bzw. Default (Überstunden).

    ``hat_soll=False`` = Beschäftigung ohne Tagessoll (:data:`ERFASSUNG_OHNE_SOLL`,
    z. B. Werkstudent): der Tag zählt gar nicht ins Konto — siehe unten.
    """
    if not f.within_cutoff:
        return None  # vor Erfassungsbeginn zählt nichts (auch keine gebuchte Zeit)

    if not hat_soll:
        # Ohne Tagessoll gibt es kein Zeitkonto: jede gearbeitete Stunde ist
        # bezahlte Arbeit, kein Plus — und ein Tag ohne Stempel ist kein Minus
        # und erst recht nicht „unentschuldigt". Der gearbeitete Tag bleibt im
        # Verlauf sichtbar (Ist), trägt aber 0 zum Saldo bei. Ohne diesen Zweig
        # liefe unten base_soll=0 → delta = volle Netto-Zeit, und die gesamte
        # Arbeitszeit landete als „Überstunden" im Konto.
        if not f.worked:
            return None
        return DayResult(
            net_minutes=f.net_minutes,
            target_minutes=0,
            delta_minutes=0,
            absent=False,
            coverage=f.coverage,
        )

    base_soll = 0 if (f.weekend or f.holiday) else soll_minutes
    eff_soll = round(base_soll * (1.0 - f.coverage))

    if f.weekend_booking == "gleitzeit":
        we_counts = True
    elif f.weekend_booking == "ueberstunden":
        we_counts = False
    else:
        we_counts = weekend_counts

    if f.worked:
        # Der HEUTIGE Arbeitstag zählt noch nicht ins Konto — er erscheint
        # erst morgen im Verlauf. Eine laufende Schicht stünde sonst als
        # grosses Minus da (bisheriges Ist gegen volles Tages-Soll), und der
        # Saldo spraenge im Tagesverlauf staendig. Ausdrücklich leere Tage
        # („nicht gearbeitet", unten) zählen heute weiterhin — das ist eine
        # bewusste Ansage, keine halbe Messung.
        if f.is_today_or_past and not f.is_past:
            return None
        if (f.weekend or f.holiday) and not we_counts:
            # Arbeit an Wochenende/Feiertag bekommt im Lohn-Export ihre eigene
            # Lohnart (Zuschläge) und zählt NICHT ins Überstundenkonto (#21).
            return DayResult(
                net_minutes=f.net_minutes,
                target_minutes=0,
                delta_minutes=0,
                absent=False,
                coverage=f.coverage,
            )
        # Wird an einem VOLL durch Urlaub gedeckten Tag trotzdem gearbeitet, ist
        # das die Anomalie „trotz Urlaub gearbeitet": der Urlaub deckt das Soll
        # NICHT, der Tag zählt normal, der Urlaubstag wird über die Urlaubsbilanz
        # zurückgegeben (#22). Geplanter halber Urlaub + halber Arbeitstag ist
        # dagegen der Normalfall und bleibt gedeckt; andere Abwesenheiten (z. B.
        # halbtags krank, #23) decken ebenfalls weiterhin ihren Anteil.
        vac_refund = f.vacation_coverage if f.vacation_coverage >= 0.999 else 0.0
        eff_worked = round(base_soll * (1.0 - max(0.0, f.coverage - vac_refund)))
        return DayResult(
            net_minutes=f.net_minutes,
            target_minutes=eff_worked,
            delta_minutes=f.net_minutes - eff_worked,
            absent=False,
            coverage=f.coverage,
        )

    if eff_soll <= 0:
        return None  # Wochenende/Feiertag/voll gedeckt → kein Beitrag

    # Kein echter Arbeitstag, Soll (teilweise) offen → Minus:
    # - ausdrücklich leerer Tagesbogen („nicht gearbeitet"), heute einschließlich, jeder Modus
    # - oder Stempler ohne Tagesbogen in der Vergangenheit (kein Stempel = unentschuldigt)
    explicit_unworked = f.has_timesheet and f.is_today_or_past
    stamper_gap = (not f.is_auto) and f.is_past
    # Geplanter Überstundenabbau zählt ab dem Tag selbst — auch bei
    # Auto-Erfassern ohne Tagesbogen (Auto-Fill überspringt Abbau-Tage).
    reduction_day = f.reduction > 0.0 and f.is_today_or_past
    if explicit_unworked or stamper_gap or reduction_day:
        return DayResult(
            net_minutes=0,
            target_minutes=eff_soll,
            delta_minutes=-eff_soll,
            # „unentschuldigt" nur ohne jede Abwesenheit UND ohne Abbau.
            absent=f.coverage <= 0.0 and f.reduction <= 0.0,
            coverage=f.coverage,
        )
    return None
