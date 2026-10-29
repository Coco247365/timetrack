"""Schemas fuer Arbeitszeit-Profile (Spec 2026-08-14).

Alle UI-Beschraenkungen sind hier SERVERSEITIG gespiegelt
(.claude/rules/ui-limits-serverseitig-spiegeln.md):

- HH:MM-Format fuer alle Zeitfelder (00:00–23:59, zweistellig).
- kind 'nfc': kein ``weekday_times``; ``clock_end < clock_start`` ist ERLAUBT
  (Nachtschicht), nur Gleichheit nicht.
- kind 'auto': keine ``clock_*``-Felder; je Wochentag ``start < end`` Pflicht
  (Ueber-Mitternacht ist bei Auto-Profilen nicht vorgesehen); ``break_start``/
  ``break_end`` nur beide zusammen, ``break_start < break_end`` und beide
  innerhalb [start, end].
- ``weekday_times``-Keys nur "0".."6" (0=Mo); null-Tag = frei.

Nachtrag 14.08. (Vertragsstunden + Tagessoll im Profil):

- kind 'nfc': ``contracted_hours_per_week`` (> 0, max 80) und
  ``weekday_target_minutes`` (Keys "0".."6", Werte 0..1440, fehlender Tag = 0)
  sind bei Neuanlage Pflicht und nur GEMEINSAM setzbar; die Summe der
  Tagessolls muss exakt ``contracted_hours_per_week * 60`` ergeben (422 nennt
  die Differenz). Bestandsprofile (beide Felder NULL) bleiben beim
  PATCH-Merge erlaubt — dafuer validiert der Endpoint mit
  ``context={"bestand_ohne_stunden_ok": True}``.
- kind 'auto': beide Felder duerfen NICHT gesetzt werden; die Response
  liefert ``contracted_hours_per_week`` BERECHNET aus ``weekday_times``
  (policy.worktime.auto_contracted_hours_per_week).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from zeiterfassung.policy.worktime import fmt_hours

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

WEEKDAY_KEYS = {"0", "1", "2", "3", "4", "5", "6"}


def _fmt_hhmm(minutes: int) -> str:
    """Minuten -> "H:MM" fuer Fehlermeldungen (2370 -> "39:30")."""
    return f"{minutes // 60}:{minutes % 60:02d}"


class WeekdayTime(BaseModel):
    """Zeiten eines Wochentags fuer kind='auto'."""

    start: str = Field(pattern=HHMM_RE.pattern)
    end: str = Field(pattern=HHMM_RE.pattern)
    break_start: str | None = Field(default=None, pattern=HHMM_RE.pattern)
    break_end: str | None = Field(default=None, pattern=HHMM_RE.pattern)

    @model_validator(mode="after")
    def _check_windows(self) -> WeekdayTime:
        # HH:MM ist lexikografisch == chronologisch vergleichbar.
        if not self.start < self.end:
            raise ValueError("start muss vor end liegen (Auto-Profile ohne Über-Mitternacht)")
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError("break_start und break_end nur gemeinsam setzen")
        if self.break_start is not None and self.break_end is not None:
            if not self.break_start < self.break_end:
                raise ValueError("break_start muss vor break_end liegen")
            if self.break_start < self.start or self.break_end > self.end:
                raise ValueError("Pause muss innerhalb von start–end liegen")

        # Betriebliche Pausenregel (2026-08-24): > 4 h Arbeitszeit → mind.
        # 45 min Pause. Ein Auto-Profil, dessen Tagesfenster mehr als 4 h
        # Arbeit ergibt, braucht ein entsprechendes Pausenfenster — sonst
        # zöge der abgeleitete Tagesbogen 45 min ab, während das Profil-Soll
        # ohne Pause rechnet, und jeder Tag liefe auf ein Dauer-Minus.
        def _min(hhmm: str) -> int:
            return int(hhmm[:2]) * 60 + int(hhmm[3:])

        span = _min(self.end) - _min(self.start)
        brk = _min(self.break_end) - _min(self.break_start) if self.break_start else 0
        if span - brk > 4 * 60 and brk < 45:
            raise ValueError(
                "Pausenregel: bei mehr als 4 h Arbeitszeit pro Tag braucht das "
                "Profil eine Pause von mindestens 45 min "
                f"(Fenster {self.start}–{self.end}, Pause {brk} min)"
            )
        return self


class WorkTimeProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["nfc", "auto"]
    is_active: bool = True
    clock_start: str | None = Field(default=None, pattern=HHMM_RE.pattern)
    clock_end: str | None = Field(default=None, pattern=HHMM_RE.pattern)
    weekday_times: dict[str, WeekdayTime | None] | None = None
    # Nachtrag 14.08. — nur kind='nfc', siehe Modul-Docstring.
    contracted_hours_per_week: Decimal | None = Field(
        default=None, gt=0, le=80, max_digits=5, decimal_places=2
    )
    weekday_target_minutes: dict[str, Annotated[int, Field(ge=0, le=1440)]] | None = None

    @field_validator("weekday_times")
    @classmethod
    def _check_weekday_keys(
        cls, v: dict[str, WeekdayTime | None] | None
    ) -> dict[str, WeekdayTime | None] | None:
        if v is not None:
            bad = sorted(set(v) - WEEKDAY_KEYS)
            if bad:
                raise ValueError(f"weekday_times-Keys müssen '0'..'6' sein (0=Mo), nicht {bad}")
        return v

    @field_validator("weekday_target_minutes")
    @classmethod
    def _check_target_keys(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        if v is not None:
            bad = sorted(set(v) - WEEKDAY_KEYS)
            if bad:
                raise ValueError(
                    f"weekday_target_minutes-Keys müssen '0'..'6' sein (0=Mo), nicht {bad}"
                )
        return v

    @model_validator(mode="after")
    def _check_kind_fields(self, info: ValidationInfo) -> WorkTimeProfileCreate:
        if self.kind == "nfc":
            if self.weekday_times is not None:
                raise ValueError("weekday_times ist nur bei kind='auto' erlaubt")
            # Nachtschicht (clock_end < clock_start) ist erlaubt, nur ein
            # leeres Fenster (start == end) nicht.
            if (
                self.clock_start is not None
                and self.clock_end is not None
                and self.clock_start == self.clock_end
            ):
                raise ValueError("clock_start und clock_end dürfen nicht gleich sein")
            self._check_nfc_hours(info)
        else:  # auto
            if self.clock_start is not None or self.clock_end is not None:
                raise ValueError("clock_start/clock_end sind nur bei kind='nfc' erlaubt")
            if self.contracted_hours_per_week is not None:
                raise ValueError(
                    "contracted_hours_per_week wird bei kind='auto' aus den "
                    "Wochentags-Zeiten berechnet und darf nicht gesetzt werden"
                )
            if self.weekday_target_minutes is not None:
                raise ValueError("weekday_target_minutes ist nur bei kind='nfc' erlaubt")
        return self

    def _check_nfc_hours(self, info: ValidationInfo) -> None:
        """Vertragsstunden-Regeln fuer kind='nfc' (Nachtrag 14.08.).

        Beide Felder None ist nur beim PATCH-Merge eines Bestandsprofils
        erlaubt (``context={"bestand_ohne_stunden_ok": True}``) — bei einer
        Neuanlage sind sie Pflicht.
        """
        bestand_ok = bool((info.context or {}).get("bestand_ohne_stunden_ok"))
        if self.contracted_hours_per_week is None and self.weekday_target_minutes is None:
            if not bestand_ok:
                raise ValueError(
                    "contracted_hours_per_week und weekday_target_minutes "
                    "sind bei kind='nfc' Pflicht"
                )
            return
        if self.contracted_hours_per_week is None or self.weekday_target_minutes is None:
            raise ValueError(
                "contracted_hours_per_week und weekday_target_minutes nur gemeinsam setzen"
            )
        # HARTE Regel: Summe der Tagessolls == Vertragsstunden × 60 — sonst
        # driftet das Wochen-Soll still von den Vertragsstunden weg.
        total = sum(self.weekday_target_minutes.get(str(wd), 0) for wd in range(7))
        expected = self.contracted_hours_per_week * 60
        if Decimal(total) != expected:
            diff = expected - Decimal(total)
            diff_min = int(diff.copy_abs().to_integral_value(rounding=ROUND_HALF_UP))
            hint = f"fehlen {_fmt_hhmm(diff_min)}" if diff > 0 else f"{_fmt_hhmm(diff_min)} zu viel"
            raise ValueError(
                f"Tagessoll-Summe {_fmt_hhmm(total)} h passt nicht zu "
                f"{fmt_hours(self.contracted_hours_per_week)} h Vertragsstunden ({hint})"
            )


class WorkTimeProfileUpdate(BaseModel):
    """Partielles Update — die Feld-Kombination wird nach dem Mergen mit dem
    Bestand erneut durch :class:`WorkTimeProfileCreate` validiert (Endpoint)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: Literal["nfc", "auto"] | None = None
    is_active: bool | None = None
    clock_start: str | None = Field(default=None, pattern=HHMM_RE.pattern)
    clock_end: str | None = Field(default=None, pattern=HHMM_RE.pattern)
    weekday_times: dict[str, WeekdayTime | None] | None = None
    contracted_hours_per_week: Decimal | None = Field(
        default=None, gt=0, le=80, max_digits=5, decimal_places=2
    )
    weekday_target_minutes: dict[str, Annotated[int, Field(ge=0, le=1440)]] | None = None


class WorkTimeProfileResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    kind: str
    is_active: bool
    clock_start: str | None = None
    clock_end: str | None = None
    weekday_times: dict[str, dict | None] | None = None
    # nfc: gespeicherter Wert (NULL = Bestandsprofil ohne Stundenbindung);
    # auto: vom Endpoint BERECHNET aus weekday_times (DB-Spalte bleibt NULL).
    # JSON-serialisiert als String mit 2 Nachkommastellen, z. B. "38.50".
    contracted_hours_per_week: Decimal | None = None
    weekday_target_minutes: dict[str, int] | None = None
    assigned_count: int = 0
    created_at: datetime
    updated_at: datetime
