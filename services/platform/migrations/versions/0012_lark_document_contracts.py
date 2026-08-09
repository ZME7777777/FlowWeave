"""make node contracts Lark-document-only"""

from alembic import op

revision = "0012_lark_document_contracts"
down_revision = "0011_oauth_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE node_io_fields ADD COLUMN IF NOT EXISTS template_url TEXT")
    op.execute("UPDATE node_io_fields SET data_type = 'URL'")
    op.execute(
        "UPDATE node_io_fields SET template_url = "
        "'https://example.feishu.cn/docx/legacy-template' WHERE template_url IS NULL"
    )
    op.execute("ALTER TABLE node_io_fields ALTER COLUMN template_url SET NOT NULL")
    op.execute("ALTER TABLE flow_definitions ADD COLUMN IF NOT EXISTS lark_root_folder_url TEXT")
    op.execute(
        "UPDATE flow_definitions SET lark_root_folder_url = "
        "'https://example.feishu.cn/drive/folder/legacy-root' "
        "WHERE lark_root_folder_url IS NULL"
    )
    op.execute("ALTER TABLE flow_definitions ALTER COLUMN lark_root_folder_url SET NOT NULL")
    op.execute("ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS lark_folder_token VARCHAR(200)")
    op.execute("ALTER TABLE flow_runs ADD COLUMN IF NOT EXISTS lark_folder_url TEXT")
    op.execute(
        "UPDATE flow_runs SET lark_folder_token = 'legacy', "
        "lark_folder_url = 'https://example.feishu.cn/drive/folder/legacy' "
        "WHERE lark_folder_token IS NULL"
    )
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_token SET NOT NULL")
    op.execute("ALTER TABLE flow_runs ALTER COLUMN lark_folder_url SET NOT NULL")
    op.execute(
        "ALTER TABLE node_attempts ADD COLUMN IF NOT EXISTS output_targets_json JSON NOT NULL "
        "DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE node_attempts DROP COLUMN IF EXISTS output_targets_json")
    op.execute("ALTER TABLE flow_runs DROP COLUMN IF EXISTS lark_folder_url")
    op.execute("ALTER TABLE flow_runs DROP COLUMN IF EXISTS lark_folder_token")
    op.execute("ALTER TABLE flow_definitions DROP COLUMN IF EXISTS lark_root_folder_url")
    op.execute("ALTER TABLE node_io_fields DROP COLUMN IF EXISTS template_url")
