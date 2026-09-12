"""FlowRun admission for immutable Runtime compatibility.

An Environment Version is immutable, so a FlowRun that was created with an
older Runtime contract cannot safely be made writable by observing that its
old container is still running.  This module is deliberately independent of
the Runtime provider: it gives every write entry point the same fail-closed
business decision before a container or OpenHands endpoint is touched.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from flowweave.shared.errors import DomainError
from flowweave.shared.models import EnvironmentVersion, FlowRun

_FROZEN_GUIDANCE = (
    "此 FlowRun 绑定的终端镜像与当前 OpenHands 运行契约不兼容，已冻结。"
    "请导出连续运行记录配置，并在兼容的新 FlowRun 中通过“新增 → 导入”恢复配置。"
)


def _runtime_manifest_compatibility(manifest: object) -> tuple[bool, str | None]:
    """Defer the environment-service import to avoid a Runtime-provider cycle."""

    from flowweave.modules.environments.application.service import runtime_manifest_compatibility

    return runtime_manifest_compatibility(manifest)


def flow_run_runtime_freeze_reason(
    db: Session,
    run: FlowRun,
    *,
    environment: EnvironmentVersion | None = None,
) -> str | None:
    """Return the user-facing reason when a frozen FlowRun is not writable."""

    if not run.environment_version_id:
        return f"{_FROZEN_GUIDANCE}（原运行未保存终端环境版本。）"
    item = (
        environment
        if environment is not None
        else db.get(EnvironmentVersion, run.environment_version_id)
    )
    if item is None:
        return f"{_FROZEN_GUIDANCE}（原终端环境版本已不可读取。）"
    compatible, detail = _runtime_manifest_compatibility(item.manifest_json)
    if compatible:
        return None
    return f"{_FROZEN_GUIDANCE}（{detail or '镜像契约校验失败'}）"


def environment_version_runtime_freeze_reason(
    db: Session, environment_version_id: str | None
) -> str | None:
    """Apply the same immutable-contract check to a schedule's frozen version."""

    if not environment_version_id:
        return f"{_FROZEN_GUIDANCE}（定时任务未保存终端环境版本。）"
    item = db.get(EnvironmentVersion, environment_version_id)
    if item is None:
        return f"{_FROZEN_GUIDANCE}（原终端环境版本已不可读取。）"
    compatible, detail = _runtime_manifest_compatibility(item.manifest_json)
    if compatible:
        return None
    return f"{_FROZEN_GUIDANCE}（{detail or '镜像契约校验失败'}）"


def require_flow_run_runtime_writable(db: Session, run: FlowRun) -> None:
    """Reject all FlowRun Runtime writes before they reach OpenHands."""

    reason = flow_run_runtime_freeze_reason(db, run)
    if reason is not None:
        raise DomainError(
            "FLOW_RUN_RUNTIME_FROZEN",
            reason,
            409,
            {"flow_run_id": run.id, "environment_version_id": run.environment_version_id},
        )


__all__ = (
    "environment_version_runtime_freeze_reason",
    "flow_run_runtime_freeze_reason",
    "require_flow_run_runtime_writable",
)
