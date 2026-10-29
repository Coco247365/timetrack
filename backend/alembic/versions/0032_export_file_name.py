"""payroll_exports.file_name — Download-Dateiname mit Firmenname."""

import sqlalchemy as sa
from alembic import op

revision = "0032_export_file_name"
down_revision = "0031_department_company"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_exports", sa.Column("file_name", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("payroll_exports", "file_name")
