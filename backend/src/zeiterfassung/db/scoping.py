"""Mandanten-Scoping als wiederverwendbarer Guard (#122).

45+ Endpunkte/Services schreiben den Tenant-Filter bisher von Hand aus
(``.where(Model.tenant_id == current_user.tenant_id)``). Eine vergessene
Stelle ist ein Cross-Tenant-Leak — das ist Defense-in-depth, kein Logikfehler
im Einzelfall. ``tenant_scoped``/``get_scoped`` sind der EINE Ort, an dem
dieser Filter gebaut wird; neue oder konvertierte Stellen nutzen sie statt
den Vergleich erneut von Hand zu schreiben.

Dies ist eine erste, bewusst kleine Tranche (Bead 115k): konvertiert sind
bisher nur ``user_service.get_user_by_id``, ``user_service.get_user_by_email``,
``user_service.list_users`` und die ``/admin/employees``-Query in
``api/v1/users.py``. Alle weiteren Stellen mit ``.tenant_id ==`` in
``api/v1/*.py`` sind ein offener Folge-Rollout (siehe Liste unten — Stand
2026-07-17, per ``grep -rn '\\.tenant_id =='  src/zeiterfassung/api/v1/*.py``).
Diese Liste ist ein Snapshot, kein laufend gepflegter Index — vor dem
Folge-Rollout neu greppen.

Verbleibende unkonvertierte Call-Sites (Beispiele, nicht erschoepfend):
- ``api/v1/users.py``: Zeilen um 324, 345, 365, 390, 534, 646, 726, 732, 738,
  750, 862, 879, 887, 932, 941, 965, 1028, 1061, 1088, 1094, 1098, 1158, 1195,
  1231, 1268, 1298, 1383, 1415, 1460 (Departments/Companies/Roles/Accounts/
  PunchEvent/DailyTimesheet/ApprovalRequest-Queries).
- Analoge Handschrift-Filter existieren vermutlich auch in weiteren
  ``api/v1/*.py``-Modulen (Absences, Overtime, Reports, Devices, ...) sowie
  in Services abseits von ``user_service`` — noch nicht systematisch erfasst.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from zeiterfassung.models.base import Base

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession


def tenant_scoped[ModelT: Base](
    stmt: Select[tuple[ModelT]], model: type[ModelT], tenant_id: uuid.UUID
) -> Select[tuple[ModelT]]:
    """An ein bestehendes SELECT ``.where(model.tenant_id == tenant_id)`` anhaengen.

    ``model`` muss eine ``tenant_id``-Spalte besitzen (bei allen Mandanten-
    gebundenen Modellen der Fall). Reiner Baustein — bewusst ohne weitere
    Logik, damit er sich an jeder bestehenden Query-Konstruktion (mit
    zusaetzlichen ``.where()``/``.order_by()``/Joins) anhaengen laesst.
    """
    return stmt.where(model.tenant_id == tenant_id)  # type: ignore[attr-defined]


async def get_scoped[ModelT: Base](
    db: AsyncSession, model: type[ModelT], tenant_id: uuid.UUID, id_: uuid.UUID
) -> ModelT | None:
    """Einen Datensatz per Primaerschluessel laden — NUR wenn er zum Mandanten gehoert.

    Liefert ``None`` statt einer fremden Zeile, wenn die ID zwar existiert
    aber einem anderen Mandanten gehoert — nach aussen ununterscheidbar von
    "existiert nicht", was Enumeration ueber fremde Mandanten verhindert.
    Der Aufrufer entscheidet (404 o.ae.), wie er den ``None``-Fall behandelt.
    """
    stmt = tenant_scoped(select(model), model, tenant_id).where(model.id == id_)  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
