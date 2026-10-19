"""accounts and links"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('links',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('from_kind', sa.String(length=1), nullable=False),
    sa.Column('from_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('to_kind', sa.String(length=1), nullable=False),
    sa.Column('to_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('created_by', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_links_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_links')),
    sa.UniqueConstraint('from_kind', 'from_id', 'to_kind', 'to_id', name=op.f('uq_links_from_kind_from_id_to_kind_to_id'))
    )
    with op.batch_alter_table('links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_links_to_kind_to_id'), ['to_kind', 'to_id'], unique=False)

    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=200), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('password_hash')

    with op.batch_alter_table('credentials', schema=None) as batch_op:
        batch_op.drop_column('expires_at')

    with op.batch_alter_table('links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_links_to_kind_to_id'))

    op.drop_table('links')
