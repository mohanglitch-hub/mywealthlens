"""batch 10.6: reminder acknowledgements

Revision ID: 5b8e2c4f9a10
Revises: 3d7a91e5b4c2
Create Date: 2026-10-02 12:00:00.000000

Adds international_reminder_ack: which reminders (Schedule FA, Form 67,
US estate-tax note) the user has marked done. Reminders themselves are
computed on the fly and never stored; this table only remembers "dealt
with". One row per (user, reminder_key).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5b8e2c4f9a10'
down_revision = '3d7a91e5b4c2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'international_reminder_ack',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('reminder_key', sa.String(length=60), nullable=False),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'reminder_key', name='uq_intl_reminder_ack_user_key'),
    )


def downgrade():
    op.drop_table('international_reminder_ack')
