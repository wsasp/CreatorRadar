"""Switch workspaces still using the original ASR default to faster-whisper."""

import sqlalchemy as sa
from alembic import op

revision = "0004_asr_default"
down_revision = "0003_processing"
branch_labels = None
depends_on = None


def upgrade():
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.JSON))
    value = settings.c.value
    op.execute(settings.update().where(
        settings.c.key == "asr",
        value["engine"].as_string() == "FunASR",
        value["model"].as_string() == "paraformer-zh",
        value["language"].as_string() == "zh",
    ).values(value={"engine": "faster-whisper", "model": "small", "language": "zh"}))


def downgrade():
    # Workspaces may have explicitly selected faster-whisper after the upgrade.
    pass
