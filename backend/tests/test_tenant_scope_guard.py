"""Guard: every tenant-model `select()` in api/services/tasks must be scoped.

zeiterfassung-0fkw (follow-up to PR #142 / #122). An architecture
review confirmed tenant-scoping is CORRECT everywhere in the current tree --
this test exists to keep it that way: it statically flags any
``select(TenantModel)`` (a model with its own ``tenant_id`` column) that
isn't visibly filtered by ``Model.tenant_id`` or built via the
``tenant_scoped()``/``get_scoped()`` helper (``zeiterfassung.db.scoping``).

Two tests:

- ``test_scanner_detects_the_patterns_it_claims_to`` -- a positive/negative
  control over small synthetic snippets, proving the AST heuristic actually
  flags an unscoped query and does NOT flag the two sanctioned filtering
  styles. Without this, a silently-broken scanner would give false
  confidence (a guard that never fires is worse than no guard).
- ``test_no_unscoped_tenant_model_queries`` -- scans the real api/ + services/
  + tasks/ trees and asserts every raw hit is either gone (fixed) or listed
  in the ``ALLOWLIST`` below with a reviewed reason. New hits fail the test;
  removing an allowlisted line without updating the entry ALSO fails it
  (stale-entry check) -- the allowlist can't silently drift from the code.
"""

from __future__ import annotations

from pathlib import Path

from tests._lint.tenant_scope_ast import discover_tenant_models, scan_paths, scan_source

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [
    BACKEND_ROOT / "src/zeiterfassung/api",
    BACKEND_ROOT / "src/zeiterfassung/services",
    BACKEND_ROOT / "src/zeiterfassung/tasks",
]

# ---------------------------------------------------------------------------
# Allowlist -- every entry is a (file, lineno) pair keyed to the CURRENT tree
# (re-grep and move the entry if the surrounding code shifts; a stale entry
# fails the test just as loudly as a new leak, see below). Grouped by the
# four legitimately-global categories the architecture review signed off on;
# do not add an entry without a category + one-line justification.
# ---------------------------------------------------------------------------

# (1) Enrichment-by-id (or by an id-list) INSIDE an already-tenant-scoped
#     parent: the id(s) being looked up were themselves produced by a query
#     that already filtered on tenant_id (or came from a JWT/session claim
#     tied to the caller's own tenant), so a second tenant_id check on the
#     enrichment lookup would be redundant, not a leak.
_ENRICHMENT_BY_ID: set[tuple[str, int]] = {
    # api/v1/absences.py:approve/reject -- req0 loaded via
    # absence_service.get_request(db, current_user.tenant_id, request_id).
    ("src/zeiterfassung/api/v1/absences.py", 505),
    # api/v1/absences.py:list_sick_mails / calendar-ics export -- emp_ids
    # collected from an already tenant_id-filtered SickMailReport/
    # ApprovalRequest result set.
    ("src/zeiterfassung/api/v1/absences.py", 1188),
    ("src/zeiterfassung/api/v1/absences.py", 1356),
    # absence_service.py: create_absence / get_vacation_balance -- employee_id
    # is a parameter of an already tenant_id-scoped service call.
    ("src/zeiterfassung/services/absence_service.py", 169),
    ("src/zeiterfassung/services/absence_service.py", 838),
    # export_service.py: emp_ids collected from an already tenant-scoped
    # PayrollBooking result set.
    ("src/zeiterfassung/services/export_service.py", 903),
    # org_service.py: list_accounts -- Role/User enrichment keyed off `a.id` /
    # `a.employee_id`, where `a` comes from an already tenant_id-filtered
    # (or explicit is_(None) cross-tenant-superadmin) Account query.
    ("src/zeiterfassung/services/org_service.py", 616),
    ("src/zeiterfassung/services/org_service.py", 629),
    # overtime_service.py: settle_month -- employee_id is a parameter of an
    # already tenant-scoped call.
    ("src/zeiterfassung/services/overtime_service.py", 779),
    # report_service.py: approval_context / list_pending -- req.employee_id /
    # employee_ids all sourced from already tenant_id-filtered
    # ApprovalRequest queries in the same function.
    ("src/zeiterfassung/services/report_service.py", 64),
    ("src/zeiterfassung/services/report_service.py", 90),
    ("src/zeiterfassung/services/report_service.py", 241),
}

# (2) Globally-unique keys: the lookup column is unique across ALL tenants
#     by construction (login email, hashed device API key, refresh-token
#     jti/family_id, RFC5322 Message-ID), so a tenant filter would be
#     redundant -- the row either doesn't exist or is THE row, tenant or not.
_GLOBALLY_UNIQUE_KEY: set[tuple[str, int]] = {
    # api/deps.py:get_device -- Device.key_hash is a sha256 of the device API
    # key, globally unique; the tenant is *resolved from* this row afterwards.
    ("src/zeiterfassung/api/deps.py", 364),
    # org_service.py:create_account / user_service.py:authenticate --
    # Account.email uniqueness check / login lookup.
    ("src/zeiterfassung/services/org_service.py", 672),
    ("src/zeiterfassung/services/user_service.py", 711),
    # user_service.py:_revoke_family -- RefreshSession.family_id is a random
    # UUID minted per login, unguessable/unique like a session key.
    ("src/zeiterfassung/services/user_service.py", 775),
    # user_service.py:refresh_tokens / revoke_refresh_session --
    # RefreshSession.jti (JWT ID), globally unique per issued refresh token.
    ("src/zeiterfassung/services/user_service.py", 854),
    ("src/zeiterfassung/services/user_service.py", 948),
    # sickmail_service.py:ingest_mail -- SickMailReport.message_id is the
    # inbound email's RFC5322 Message-ID header, globally unique mailbox-wide;
    # used purely for idempotent dedup before the tenant-scoped row is created.
    ("src/zeiterfassung/services/sickmail_service.py", 46),
}

# (3) Self-scoped via principal: the row being looked up IS the
#     already-authenticated caller (or an id taken directly from their
#     verified JWT/session), not attacker-suppliable cross-tenant input --
#     login, refresh, change-own-password, auth/me.
_SELF_SCOPED_VIA_PRINCIPAL: set[tuple[str, int]] = {
    # user_service.py:refresh_tokens -- Account.id from the *signed* refresh
    # token's `sub` claim (tenant match re-checked right after, see
    # account.tenant_id comparison a few lines down).
    ("src/zeiterfassung/services/user_service.py", 831),
    # user_service.py:change_own_password -- Account.id == current_user's own
    # account_id (passed in by the /auth/change-password route).
    ("src/zeiterfassung/services/user_service.py", 1013),
    # user_service.py:_issue_token_pair -- Role enrichment / User (employee)
    # enrichment keyed off `account.id` / `account.employee_id`, where
    # `account` is the already-authenticated principal for this token issuance.
    ("src/zeiterfassung/services/user_service.py", 603),
    ("src/zeiterfassung/services/user_service.py", 633),
    # user_service.py:get_auth_me_data -- Account.id == current_user.account_id,
    # plus User/Department enrichment off that same resolved principal.
    ("src/zeiterfassung/services/user_service.py", 1147),
    ("src/zeiterfassung/services/user_service.py", 1158),
    ("src/zeiterfassung/services/user_service.py", 1173),
    # user_service.py:invalidate_account_tokens (login-hardening Task 4) --
    # RefreshSession.account_id == account.id, where `account` is passed in
    # by the caller (logout's own session lookup, change-own-password's own
    # account, admin-reset's tenant_id-scoped account, archive_user's
    # tenant_scoped account list) -- never attacker-suppliable cross-tenant
    # input at this point.
    ("src/zeiterfassung/services/user_service.py", 910),
    # user_service.py:revoke_refresh_session -- Account.id == session.account_id,
    # where `session` was just resolved from the signed refresh token's own
    # `jti` claim a few lines above (same trust level as the refresh_tokens
    # Account.id lookup above).
    ("src/zeiterfassung/services/user_service.py", 962),
    # api/deps.py:get_current_user -- Account.id from the *verified* JWT's
    # `sub` claim (login-hardening PK-lookup, Task 3): this IS the row that
    # establishes the principal itself, resolved before the tenant is known
    # from anything else -- there is no separate tenant to filter by yet.
    ("src/zeiterfassung/api/deps.py", 223),
}

# (4) Background jobs with no tenant context: scheduled loops (main.py
#     lifespan) that must sweep ALL tenants by design; each iterates its own
#     per-row tenant_id afterward where it matters (e.g. settle_through reads
#     `tenant.settings` per user.tenant_id before deciding to act).
_BACKGROUND_JOB_NO_TENANT: set[tuple[str, int]] = {
    # user_service.py:archive_due_terminations -- daily sweep, all tenants.
    ("src/zeiterfassung/services/user_service.py", 544),
    # overtime_service.py:settle_through -- daily sweep, all tenants (only
    # `export_basis == "soll"` tenants act, checked per-user afterward).
    ("src/zeiterfassung/services/overtime_service.py", 969),
}

ALLOWLIST: set[tuple[str, int]] = (
    _ENRICHMENT_BY_ID
    | _GLOBALLY_UNIQUE_KEY
    | _SELF_SCOPED_VIA_PRINCIPAL
    | _BACKGROUND_JOB_NO_TENANT
)


def test_scanner_detects_the_patterns_it_claims_to() -> None:
    """Positive/negative control for the AST heuristic itself.

    Guards against a silently-broken scanner (e.g. a refactor that makes
    `_is_filtered`/`_find_query_roots` always return empty) giving false
    confidence in the real-tree test below.
    """
    tenant_models = {"User"}

    leaky = """
from sqlalchemy import select
from zeiterfassung.models.user import User

async def leaky(db, tenant_id):
    return (await db.execute(select(User))).scalars().all()
"""
    hits = scan_source(leaky, "synthetic_leaky.py", tenant_models)
    assert len(hits) == 1
    assert hits[0].model == "User"

    filtered_direct = """
from sqlalchemy import select
from zeiterfassung.models.user import User

async def ok_direct(db, tenant_id):
    stmt = select(User).where(User.tenant_id == tenant_id)
    return (await db.execute(stmt)).scalars().all()
"""
    assert scan_source(filtered_direct, "synthetic_ok_direct.py", tenant_models) == []

    filtered_via_helper = """
from sqlalchemy import select
from zeiterfassung.db.scoping import tenant_scoped
from zeiterfassung.models.user import User

async def ok_helper(db, tid):
    stmt = tenant_scoped(select(User), User, tid)
    stmt = stmt.where(User.is_active == True)
    return (await db.execute(stmt)).scalars().all()
"""
    assert scan_source(filtered_via_helper, "synthetic_ok_helper.py", tenant_models) == []


def test_no_unscoped_tenant_model_queries() -> None:
    tenant_models = discover_tenant_models()
    assert tenant_models, "sanity: discover_tenant_models() must find at least one model"

    violations = scan_paths(SCAN_DIRS, tenant_models, root=BACKEND_ROOT)
    found = {(v.file, v.lineno): v for v in violations}
    found_keys = set(found)

    new_leaks = found_keys - ALLOWLIST
    stale_entries = ALLOWLIST - found_keys

    problems: list[str] = []
    if new_leaks:
        problems.append("Unscoped tenant-model queries (not in ALLOWLIST):")
        for file, lineno in sorted(new_leaks):
            v = found[(file, lineno)]
            problems.append(f"  {file}:{lineno}  {v.model}  {v.snippet}")
        problems.append(
            "  -> Either scope the query with `Model.tenant_id ==` / "
            "tenant_scoped()/get_scoped(), or, if it is a legitimately "
            "global read, add a (file, lineno) entry to the matching "
            "category set in this test with a one-line justification."
        )
    if stale_entries:
        problems.append(
            "Stale ALLOWLIST entries (no longer match a detected query -- "
            "the code moved/changed; re-grep and update the line number, "
            "or remove the entry if the query was converted to scoped):"
        )
        for file, lineno in sorted(stale_entries):
            problems.append(f"  {file}:{lineno}")

    assert not problems, "\n" + "\n".join(problems)


def test_allowlist_has_no_duplicate_categorization() -> None:
    """Every (file, lineno) appears in exactly one category set."""
    categories = [
        _ENRICHMENT_BY_ID,
        _GLOBALLY_UNIQUE_KEY,
        _SELF_SCOPED_VIA_PRINCIPAL,
        _BACKGROUND_JOB_NO_TENANT,
    ]
    seen: dict[tuple[str, int], int] = {}
    for cat in categories:
        for key in cat:
            seen[key] = seen.get(key, 0) + 1
    duplicates = {k: n for k, n in seen.items() if n > 1}
    assert not duplicates, f"entries listed in more than one category: {duplicates}"
