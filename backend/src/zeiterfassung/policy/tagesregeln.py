"""DIE Stelle für die Tagesregeln eines Mitarbeiters.

Historie: die Soll-Kette und die Abwesenheits-Deckung waren je viermal
implementiert (Konto, Tagesbogen, Zettel, Export, absence_service) — und die
Kopien wichen ab: Zettel/Export kannten die Arbeitszeit-Profile nicht, und bei
Einzeltags-Abwesenheiten rechnete nur das Konto mit dem echten Anteil
(``days_count``, z. B. 0,75 bei 6 von 8 h krank) statt stumpf halb/ganz.
Spec: docs/superpowers/specs/2026-08-25-tagesregeln-design.md.

Seitdem gilt: Services bauen pro Mitarbeiter EIN :class:`Tagesregeln`-Objekt
(``Tagesregeln.fuer(user, wt_profile)``) und fragen es. Die Einzelfunktionen
(:func:`~zeiterfassung.policy.overtime.daily_target_minutes`,
:func:`~zeiterfassung.policy.worktime.auto_target_minutes`, …) sind Innereien
dieser Klasse — wer sie direkt aufruft, baut Kopie Nr. 5.

Pure Policy: kein DB-Zugriff. Das Arbeitszeit-Profil liefert der Aufrufer mit
(alle Konsumenten laden es ohnehin für die Kappung bzw. das Auto-Füllen).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from zeiterfassung.policy import worktime
from zeiterfassung.policy.overtime import daily_target_minutes, hat_tagessoll


@dataclass(frozen=True)
class Tagesregeln:
    """Tages-Soll und Abwesenheits-Deckung — eine Antwort, egal wer fragt."""

    employment_type: str | None
    weekday_minutes: list | None
    contracted_hours_per_week: float | None
    wt_profile: object | None = None

    @classmethod
    def fuer(cls, user: object, wt_profile: object | None = None) -> Tagesregeln:
        """Regeln eines Mitarbeiters (``user`` = ORM-Objekt oder Duck-Type)."""
        return cls(
            employment_type=getattr(user, "employment_type", None),
            weekday_minutes=getattr(user, "weekday_target_minutes", None),
            contracted_hours_per_week=getattr(user, "contracted_hours_per_week", None),
            wt_profile=wt_profile,
        )

    def tagessoll(self, day: date) -> int:
        """Volles Tages-Soll in Minuten.

        Kette: Beschäftigung ohne Soll (Werkstudent) → 0; sonst gewinnt ein
        aktives Arbeitszeit-Profil (erst 'auto', dann stundengebundenes 'nfc')
        KOMPLETT; sonst Wochentagsplan des Users; sonst Wochenstunden ÷ 5.

        Bewusst wochenend-BLIND (Bestandsverhalten von Konto und Tagesbogen):
        die letzte Fallback-Stufe liefert auch für Sa/So Wochenstunden ÷ 5 —
        ob ein Wochenende Soll hat, entscheidet der Konsument (classify_day
        nullt weekend/holiday; Zettel und Export fragen nur Mo–Fr). Ein
        gepflegter Wochentagsplan bzw. ein Profil traegt seine Sa/So-Nullen
        dagegen selbst.
        """
        if not hat_tagessoll(self.employment_type):
            return 0
        soll = worktime.auto_target_minutes(self.wt_profile, day.weekday())
        if soll is None:
            soll = worktime.nfc_target_minutes(self.wt_profile, day.weekday())
        if soll is not None:
            return soll
        return daily_target_minutes(
            self.contracted_hours_per_week,
            weekday=day.weekday(),
            weekday_minutes=self.weekday_minutes,
            employment_type=self.employment_type,
        )

    @staticmethod
    def abwesenheits_deckung(request: object) -> float:
        """Anteil des Tages-Solls (0..1), den eine Abwesenheit an einem Tag deckt.

        Einzeltag: ``days_count`` trägt den echten Anteil — 1,0 / 0,5 / krumme
        Werte wie 0,75 bei stundenweiser Abwesenheit (6 von 8 h krank).
        Mehrtägig: Halbtag (AM/PM) deckt die Hälfte, sonst ganz — mehrtägige
        Anträge kennen keine Stundenangabe (UI- und Schema-Regel).
        """
        if getattr(request, "date_from", None) == getattr(request, "date_to", None):
            return max(0.0, min(1.0, float(getattr(request, "days_count", 0) or 0)))
        day_part = getattr(request, "day_part", None)
        return 0.5 if str(day_part or "FULL") in ("AM", "PM") else 1.0
