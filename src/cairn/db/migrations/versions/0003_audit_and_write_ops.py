"""audit log and write ops"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', ID, autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('by', ID, nullable=False),
    sa.Column('op', sa.String(length=32), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index(op.f('ix_audit_log_at'), 'audit_log', ['at'], unique=False)
    op.create_table('write_ops',
    sa.Column('user_id', ID, nullable=False),
    sa.Column('op_id', sa.String(length=64), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_write_ops_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'op_id', name=op.f('pk_write_ops'))
    )
    op.create_index(op.f('ix_write_ops_at'), 'write_ops', ['at'], unique=False)
    op.create_index(op.f('ix_change_log_by_at'), 'change_log', ['by', 'at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_change_log_by_at'), table_name='change_log')
    op.drop_index(op.f('ix_write_ops_at'), table_name='write_ops')
    op.drop_table('write_ops')
    op.drop_index(op.f('ix_audit_log_at'), table_name='audit_log')
    op.drop_table('audit_log')
