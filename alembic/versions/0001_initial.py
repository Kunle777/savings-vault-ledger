"""Create the standalone users, vaults, and ledger tables."""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("provider_customer_code", sa.String(), nullable=True),
        sa.Column("dva_account_number", sa.String(), nullable=True),
        sa.Column("dva_bank_name", sa.String(), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "vaults",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("target_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("balance", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("lock_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), server_default="active", nullable=False),
        sa.Column("milestones_reached", sa.JSON(), nullable=False),
    )
    op.create_index("ix_vaults_user_id", "vaults", ["user_id"])
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("vault_id", sa.String(), sa.ForeignKey("vaults.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=False),
        sa.UniqueConstraint("provider_reference", name="uq_ledger_provider_reference"),
    )
    op.create_index("ix_ledger_entries_vault_id", "ledger_entries", ["vault_id"])
    op.create_index("ix_ledger_entries_user_id", "ledger_entries", ["user_id"])


def downgrade():
    op.drop_index("ix_ledger_entries_user_id", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_vault_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_vaults_user_id", table_name="vaults")
    op.drop_table("vaults")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
