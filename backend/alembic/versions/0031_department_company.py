"""departments.company_id + Backfill nach Mitglieder-Mehrheit."""

import sqlalchemy as sa
from alembic import op

revision = "0031_department_company"
down_revision = "0030_user_initial_vacation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "departments",
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_departments_company_id", "departments", ["company_id"])
    # Backfill: jede Abteilung -> Firma der MEHRHEIT ihrer aktiven Mitglieder;
    # ohne Mitglieder/bei Gleichstand bleibt NULL (UI zeigt "Firma zuordnen").
    conn = op.get_bind()
    counts = conn.execute(
        sa.text(
            """
            SELECT u.department_id, u.company_id, COUNT(*) AS cnt
            FROM users u
            WHERE u.department_id IS NOT NULL AND u.company_id IS NOT NULL
              AND u.is_active = true
            GROUP BY u.department_id, u.company_id
            """
        )
    ).fetchall()
    from collections import defaultdict

    per_dept: dict = defaultdict(list)
    for dep_id, comp_id, cnt in counts:
        per_dept[dep_id].append((cnt, comp_id))
    for dep_id, lst in per_dept.items():
        lst.sort(reverse=True)
        if len(lst) > 1 and lst[0][0] == lst[1][0]:
            continue  # Gleichstand -> NULL lassen
        conn.execute(
            sa.text("UPDATE departments SET company_id = :c WHERE id = :d"),
            {"c": lst[0][1], "d": dep_id},
        )


def downgrade() -> None:
    op.drop_index("ix_departments_company_id", table_name="departments")
    op.drop_column("departments", "company_id")
