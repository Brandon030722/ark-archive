"""Allow a separately scoped Integrated Strategies dataset.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _replace(expression):
    with op.batch_alter_table("dataset") as batch:
        batch.drop_constraint(op.f("ck_dataset_supported_module"), type_="check")
        batch.create_check_constraint(op.f("ck_dataset_supported_module"), expression)


def upgrade():
    _replace("module IN ('operator','recruitment','garrison','roguelike')")


def downgrade():
    # The database rejects narrowing this constraint while roguelike datasets
    # remain. Sealed datasets are never deleted merely to allow a downgrade.
    _replace("module IN ('operator','recruitment','garrison')")
