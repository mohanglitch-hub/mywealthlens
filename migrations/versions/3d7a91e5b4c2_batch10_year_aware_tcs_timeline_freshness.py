"""batch 10: year-aware TCS, holding timeline, value freshness

Revision ID: 3d7a91e5b4c2
Revises: 2c6ca6024064
Create Date: 2026-10-01 12:00:00.000000

Three schema changes for the International Investing Centre, plus one
data fix:

  1. international_remittance.education_loan_funded (Boolean, NOT NULL)
     — education funded by a loan from a financial institution has its
     own TCS rate. Existing rows get False.
  2. international_holding.value_updated_at (DateTime, nullable) — when
     the user last set the holding's value, for the "manual value is
     getting old" nudge. Existing rows are backfilled from updated_at
     (falling back to created_at), the best information available.
  3. international_timeline — new append-only audit table.
  4. DATA FIX: every existing remittance's tcs_amount_inr is recomputed
     with the corrected, date-aware rules (international_centre/
     tcs_rules.py). Batch 9.4 had stored figures built on a ₹7L
     threshold and a flat 20%, which is wrong for any remittance on or
     after 1 April 2025 (threshold ₹10L) and for education remittances.
     The recompute is deterministic and idempotent.

NOT NULL column added with a temporary server_default so the ALTER
succeeds against a table that already has rows, then the default is
dropped in a second batch (see the comment in upgrade() for why it must
be a separate batch on SQLite).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3d7a91e5b4c2'
down_revision = '2c6ca6024064'
branch_labels = None
depends_on = None


def _recompute_existing_tcs(bind):
    """Recompute tcs_amount_inr for every existing remittance, one
    financial year at a time, using the shared pure rules module."""
    from international_centre import tcs_rules

    remit = sa.table(
        'international_remittance',
        sa.column('id', sa.Integer),
        sa.column('user_id', sa.Integer),
        sa.column('date', sa.Date),
        sa.column('amount_inr', sa.Float),
        sa.column('purpose', sa.String),
        sa.column('tcs_amount_inr', sa.Float),
    )
    rows = bind.execute(
        sa.select(remit.c.id, remit.c.user_id, remit.c.date,
                  remit.c.amount_inr, remit.c.purpose)
    ).fetchall()

    # Group by (user, financial-year start year). Indian FY: 1 Apr - 31 Mar.
    groups = {}
    for rid, user_id, d, amount, purpose in rows:
        fy_start_year = d.year if d.month >= 4 else d.year - 1
        category = tcs_rules.category_for(
            purpose_is_education=(purpose == "Education abroad"),
            purpose_is_medical=(purpose == "Medical treatment abroad"),
            education_loan_funded=False,  # column is brand new: nothing flagged yet
        )
        groups.setdefault((user_id, fy_start_year), []).append(
            {"key": rid, "date": d, "amount_inr": amount, "category": category}
        )

    for entries in groups.values():
        for rid, tcs in tcs_rules.compute_fy_tcs(entries).items():
            bind.execute(
                remit.update().where(remit.c.id == rid).values(tcs_amount_inr=tcs)
            )


def upgrade():
    # Two SEPARATE batches on purpose: on SQLite, batch mode rebuilds the
    # table once per batch, and adding the column + dropping its default
    # in the SAME batch makes the rebuild copy existing rows without a
    # value for the NOT NULL column (IntegrityError on any database that
    # already has remittances). Add with the default first, drop it after.
    with op.batch_alter_table('international_remittance', schema=None) as batch_op:
        batch_op.add_column(sa.Column('education_loan_funded', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
    with op.batch_alter_table('international_remittance', schema=None) as batch_op:
        batch_op.alter_column('education_loan_funded', server_default=None)

    with op.batch_alter_table('international_holding', schema=None) as batch_op:
        batch_op.add_column(sa.Column('value_updated_at', sa.DateTime(), nullable=True))

    op.execute(
        "UPDATE international_holding "
        "SET value_updated_at = COALESCE(updated_at, created_at)"
    )

    op.create_table(
        'international_timeline',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('holding_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['holding_id'], ['international_holding.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('international_timeline', schema=None) as batch_op:
        batch_op.create_index('ix_intl_timeline_holding', ['holding_id'], unique=False)

    _recompute_existing_tcs(op.get_bind())


def downgrade():
    with op.batch_alter_table('international_timeline', schema=None) as batch_op:
        batch_op.drop_index('ix_intl_timeline_holding')
    op.drop_table('international_timeline')

    with op.batch_alter_table('international_holding', schema=None) as batch_op:
        batch_op.drop_column('value_updated_at')

    with op.batch_alter_table('international_remittance', schema=None) as batch_op:
        batch_op.drop_column('education_loan_funded')
    # Note: tcs_amount_inr values are NOT reverted to the old 7L/20%
    # figures — those were incorrect, and the column itself predates
    # this revision.
