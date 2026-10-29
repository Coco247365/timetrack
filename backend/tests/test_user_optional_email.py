"""E-Mail beim Anlegen eines Mitarbeiters ist optional (User-Wunsch 31.07.):
nicht jeder Gewerbliche im Lager hat eine Firmenadresse.
"""

from __future__ import annotations

from zeiterfassung.schemas.user import UserCreate
from zeiterfassung.services.user_service import create_user, get_user_by_email


async def test_create_user_without_email(db, tenant) -> None:
    user = await create_user(
        db,
        tenant.id,
        UserCreate(first_name="Ohne", last_name="Adresse"),
    )
    assert user.email is None


async def test_two_users_without_email_do_not_collide(db, tenant) -> None:
    # Ohne E-Mail entfaellt die Dublettenpruefung -- zwei leere Adressen
    # duerfen nicht als "gleiche E-Mail" gelten.
    a = await create_user(db, tenant.id, UserCreate(first_name="Erst", last_name="Ohne"))
    b = await create_user(db, tenant.id, UserCreate(first_name="Zweit", last_name="Ohne"))
    assert a.id != b.id
    assert a.email is None
    assert b.email is None


async def test_create_user_with_email_still_dedupes(db, tenant) -> None:
    await create_user(
        db, tenant.id, UserCreate(email="dup@test.de", first_name="Erst", last_name="Mit")
    )
    found = await get_user_by_email(db, tenant.id, "dup@test.de")
    assert found is not None
    assert found.first_name == "Erst"
