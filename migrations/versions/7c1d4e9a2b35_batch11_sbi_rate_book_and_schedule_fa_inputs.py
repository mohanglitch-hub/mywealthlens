"""batch 11: SBI rate book, per-event rate overrides, Schedule FA inputs

Revision ID: 7c1d4e9a2b35
Revises: 5b8e2c4f9a10
Create Date: 2026-10-08 12:00:00.000000

Schema changes for the International Investing Centre:

  * international_sbi_rate        - the user's SBI TT buying-rate book
  * international_rate_settings   - per-user conversion conventions
  * international_schedule_fa_input - per-holding, per-year peak/closing inputs
  * international_transaction.ttbr_override / ttbr_override_date
  * international_vesting_tranche.ttbr_override / ttbr_override_date
  * international_holding.entity_address / entity_zip / entity_nature
    (Schedule FA Table A3 wants these)

All new columns are nullable, so existing rows need no backfill and the
ALTERs succeed on a table that already has data.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c1d4e9a2b35'
down_revision = '5b8e2c4f9a10'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'international_sbi_rate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('rate_date', sa.Date(), nullable=False),
        sa.Column('rate', sa.Float(), nullable=False),
        sa.Column('note', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'currency', 'rate_date', name='uq_intl_sbi_rate'),
    )
    with op.batch_alter_table('international_sbi_rate', schema=None) as batch_op:
        batch_op.create_index('ix_intl_sbi_rate_lookup', ['user_id', 'currency', 'rate_date'], unique=False)

    op.create_table(
        'international_rate_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('fa_basis', sa.String(length=20), nullable=False),
        sa.Column('cg_method', sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )

    op.create_table(
        'international_schedule_fa_input',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('holding_id', sa.Integer(), nullable=False),
        sa.Column('calendar_year', sa.Integer(), nullable=False),
        sa.Column('peak_date', sa.Date(), nullable=True),
        sa.Column('peak_value_native', sa.Float(), nullable=True),
        sa.Column('closing_value_native', sa.Float(), nullable=True),
        sa.Column('notes', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['holding_id'], ['international_holding.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('holding_id', 'calendar_year', name='uq_intl_fa_input'),
    )

    with op.batch_alter_table('international_transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ttbr_override', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('ttbr_override_date', sa.Date(), nullable=True))

    with op.batch_alter_table('international_vesting_tranche', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ttbr_override', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('ttbr_override_date', sa.Date(), nullable=True))

    with op.batch_alter_table('international_holding', schema=None) as batch_op:
        batch_op.add_column(sa.Column('entity_address', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('entity_zip', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('entity_nature', sa.String(length=60), nullable=True))


def downgrade():
    with op.batch_alter_table('international_holding', schema=None) as batch_op:
        batch_op.drop_column('entity_nature')
        batch_op.drop_column('entity_zip')
        batch_op.drop_column('entity_address')

    with op.batch_alter_table('international_vesting_tranche', schema=None) as batch_op:
        batch_op.drop_column('ttbr_override_date')
        batch_op.drop_column('ttbr_override')

    with op.batch_alter_table('international_transaction', schema=None) as batch_op:
        batch_op.drop_column('ttbr_override_date')
        batch_op.drop_column('ttbr_override')

    op.drop_table('international_schedule_fa_input')
    op.drop_table('international_rate_settings')
    with op.batch_alter_table('international_sbi_rate', schema=None) as batch_op:
        batch_op.drop_index('ix_intl_sbi_rate_lookup')
    op.drop_table('international_sbi_rate')
