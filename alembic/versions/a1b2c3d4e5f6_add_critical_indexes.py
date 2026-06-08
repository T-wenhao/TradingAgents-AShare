"""add_critical_indexes

Revision ID: a1b2c3d4e5f6
Revises: 5abbf2fc8477
Create Date: 2026-06-08 16:50:00.000000

Adds performance-critical indexes that were missing from the original schema.
These indexes significantly speed up:
- Date-range report queries (reports.trade_date, reports.created_at)
- Scheduled-task scanning (scheduled_analyses.trigger_time, is_active)
- Symbol-based lookups (watchlist_items.symbol, imported_portfolio_positions.symbol)
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '5abbf2fc8477'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add missing performance-critical indexes."""
    # reports: speed up date-range and time-ordered queries
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.create_index('ix_reports_trade_date', ['trade_date'], unique=False)
        batch_op.create_index('ix_reports_created_at', ['created_at'], unique=False)

    # scheduled_analyses: speed up trigger-time scanning and active-task filtering
    with op.batch_alter_table('scheduled_analyses', schema=None) as batch_op:
        batch_op.create_index('ix_scheduled_analyses_trigger_time', ['trigger_time'], unique=False)
        batch_op.create_index('ix_scheduled_analyses_is_active', ['is_active'], unique=False)

    # watchlist_items: speed up symbol-based lookups (independent of user_id)
    with op.batch_alter_table('watchlist_items', schema=None) as batch_op:
        batch_op.create_index('ix_watchlist_items_symbol', ['symbol'], unique=False)

    # imported_portfolio_positions: speed up symbol-based portfolio queries
    with op.batch_alter_table('imported_portfolio_positions', schema=None) as batch_op:
        batch_op.create_index('ix_imported_portfolio_positions_symbol', ['symbol'], unique=False)


def downgrade() -> None:
    """Remove the indexes added in this migration."""
    with op.batch_alter_table('imported_portfolio_positions', schema=None) as batch_op:
        batch_op.drop_index('ix_imported_portfolio_positions_symbol')

    with op.batch_alter_table('watchlist_items', schema=None) as batch_op:
        batch_op.drop_index('ix_watchlist_items_symbol')

    with op.batch_alter_table('scheduled_analyses', schema=None) as batch_op:
        batch_op.drop_index('ix_scheduled_analyses_is_active')
        batch_op.drop_index('ix_scheduled_analyses_trigger_time')

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_index('ix_reports_created_at')
        batch_op.drop_index('ix_reports_trade_date')
