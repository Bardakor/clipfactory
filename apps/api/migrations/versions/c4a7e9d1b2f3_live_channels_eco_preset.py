"""live channels: the eco preset (chat chooses the moment, AI writes only the post)

Revision ID: c4a7e9d1b2f3
Revises: b3f1c2d4e5a6
"""
from alembic import op

revision = 'c4a7e9d1b2f3'
down_revision = 'b3f1c2d4e5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Channels were created with the old live default; nothing in the dashboard lets a channel pick another preset.
    op.execute("UPDATE live_channels SET settings = json_set(settings, '$.preset', 'eco') "
               "WHERE json_extract(settings, '$.preset') = 'balanced'")


def downgrade() -> None:
    op.execute("UPDATE live_channels SET settings = json_set(settings, '$.preset', 'balanced') "
               "WHERE json_extract(settings, '$.preset') = 'eco'")
