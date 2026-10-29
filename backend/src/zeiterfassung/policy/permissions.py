"""Domäne E — Sichtbarkeit / Berechtigung.

Reine Prädikate für die Rechte-/Scope-Logik, damit die heute in mehreren
Endpunkten kopierten Checks (Abteilungs-Zugehörigkeit, Scope-Sichtbarkeit,
Self-Approval) an EINER Stelle stehen. Die Regeln sind inhaltlich unverändert
(ADR-gestützt) — hier nur entdoppelt und vereinheitlicht.

Der Scope wird über ein ``Protocol`` beschrieben, um keinen Import-Zyklus zu
``api.deps.ScopeContext`` zu erzeugen; jedes Objekt mit diesen Attributen passt.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from zeiterfassung.policy.violations import Violation


class ScopeLike(Protocol):
    is_anonymized: bool
    is_self_only: bool
    is_department: bool
    is_global: bool
    department_id: uuid.UUID | None
    user_id: uuid.UUID


def is_employee_visible(
    scope: ScopeLike,
    *,
    target_user_id: uuid.UUID,
    target_department_id: uuid.UUID | None,
) -> bool:
    """── R-P1/P3: Darf dieser Scope die Daten dieses Mitarbeiters sehen? ──

    Scope-Pyramide: anonymisiert (BR) → niemand; global → alle; Abteilung →
    nur eigene Abteilung; self → nur man selbst. EINE Wahrheit für alle
    Sichtbarkeits-Entscheidungen.
    """
    if scope.is_anonymized:
        return False
    if scope.is_global:
        return True
    if scope.is_department:
        return scope.department_id is not None and target_department_id == scope.department_id
    if scope.is_self_only:
        return target_user_id == scope.user_id
    return False


def check_not_self_approval(
    employee_id: uuid.UUID,
    approver_id: uuid.UUID,
) -> Violation | None:
    """── R-A4: Vier-Augen-Prinzip — niemand genehmigt den eigenen Antrag ──"""
    if employee_id == approver_id:
        return Violation.block(
            "approval.self",
            "Ein Antrag kann nicht von der antragstellenden Person selbst genehmigt werden.",
        )
    return None
