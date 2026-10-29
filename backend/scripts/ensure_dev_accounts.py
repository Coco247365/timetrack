"""Dev-only: stellt loginfähige Accounts sicher.

Hintergrund: Der Login geht gegen die ``accounts``-Tabelle (getrennt von
``users``). ``seed_demo`` befüllt aber nur ``users`` — nach frischem Seed ist
``accounts`` leer und niemand kann sich einloggen.

Dieses Skript ist idempotent und deckt beide Startpfade ab:
  * Nach *seed_demo*: legt für die Demo-Logins (gf@/hr@/... ) Account-Zeilen an,
    verknüpft sie mit User (employee_id) und Rolle (account_roles).
  * Nach *Dump-Restore*: setzt für alle bereits vorhandenen Accounts das
    bekannte Dev-Passwort (die Original-Hashes vom Pi sind unbekannt).

Usage:  PYTHONPATH=src .venv/bin/python scripts/ensure_dev_accounts.py [passwort]
        (Standard-Passwort: Test1234!)
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from zeiterfassung.config import get_settings
from zeiterfassung.core.security import hash_password
from zeiterfassung.models import Role, User, UserRole  # noqa: F401 (Role via UserRole)
from zeiterfassung.models.account import Account, AccountRole

# Demo-Logins, die als Account existieren sollen: email -> is_superadmin.
# Passend zu den "Dedicated demo accounts" in seed_demo.py.
DEMO_LOGINS: dict[str, bool] = {
    "gf@example.com": True,
    "hr@example.com": False,
    "teamleiter@example.com": False,
    "teamleiter2@example.com": False,
    "mitarbeiter@example.com": False,
    "mitarbeiter2@example.com": False,
    "buchhaltung@example.com": False,
    "betriebsrat@example.com": False,
}


async def main() -> None:
    pw = sys.argv[1] if len(sys.argv) > 1 else "Test1234!"
    h = hash_password(pw)
    engine = create_async_engine(get_settings().database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    created: list[str] = []
    reset: list[str] = []
    async with Session() as s:
        # 1) Vorhandene Accounts -> Dev-Passwort (Dump-Fall).
        existing = (await s.execute(select(Account))).scalars().all()
        existing_emails = {a.email for a in existing}
        for a in existing:
            a.hashed_password = h
            reset.append(a.email)

        # 2) Fehlende Demo-Logins anlegen (Seed-Fall), falls passender User da ist.
        for email, is_super in DEMO_LOGINS.items():
            if email in existing_emails:
                continue
            user = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if user is None:
                continue  # kein passender Demo-User (z.B. Dump ohne diese Mails)
            role_id = (
                await s.execute(
                    select(UserRole.role_id).where(UserRole.user_id == user.id).limit(1)
                )
            ).scalar_one_or_none()
            acc = Account(
                tenant_id=user.tenant_id,
                email=email,
                hashed_password=h,
                is_superadmin=is_super,
                employee_id=user.id,
                is_active=True,
            )
            s.add(acc)
            await s.flush()
            if role_id is not None:
                s.add(AccountRole(account_id=acc.id, role_id=role_id))
            created.append(email)

        await s.commit()
    await engine.dispose()

    print(f"Accounts: {len(created)} neu angelegt, {len(reset)} Passwörter gesetzt (pw='{pw}')")
    for e in created:
        print("  +", e)
    for e in sorted(reset):
        print("  ~", e)
    if not created and not reset:
        print("  (keine Accounts und keine passenden Demo-User gefunden)")


if __name__ == "__main__":
    asyncio.run(main())
