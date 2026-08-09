"""create Lark run resources lazily"""

from alembic import op

revision = "0013_lazy_lark_run_resources"
down_revision = "0012_lark_document_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_token DROP NOT NULL")
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_url DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE flow_runs SET lark_folder_token = 'legacy' WHERE lark_folder_token IS NULL")
    op.execute(
        "UPDATE flow_runs "
        "SET lark_folder_url = 'https://example.feishu.cn/drive/folder/legacy' "
        "WHERE lark_folder_url IS NULL"
    )
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_token SET NOT NULL")
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_url SET NOT NULL")
