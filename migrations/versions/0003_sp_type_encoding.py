"""Preserve string and numeric client SP encodings without invented enum names.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "skill_level",
        "sp_type",
        existing_type=sa.String(40),
        type_=postgresql.JSONB(),
        nullable=True,
        postgresql_using="to_jsonb(sp_type)",
    )


def downgrade():
    # Refuse a lossy downgrade for published numeric or unknown encodings.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM skill_level WHERE sp_type IS NULL
        OR jsonb_typeof(sp_type) <> 'string') THEN
        RAISE EXCEPTION 'cannot downgrade non-string SP encodings without data loss';
      END IF;
    END $$""")
    op.alter_column(
        "skill_level",
        "sp_type",
        existing_type=postgresql.JSONB(),
        type_=sa.String(40),
        nullable=False,
        postgresql_using="sp_type #>> '{}'",
    )
