from flowweave.modules.agent_sessions.application.runtime_config import (
    FrozenSessionConfig,
    build_agent_spec,
)
from flowweave.shared.domain.openhands import FIXED_RUNTIME_TOOL_NAMES
from flowweave.shared.settings import settings_context


def test_every_agent_session_uses_the_same_complete_tool_set(tmp_path, settings):
    isolated_settings = settings.model_copy(update={"workspace_root": tmp_path})
    with settings_context(isolated_settings):
        spec = build_agent_spec(
            FrozenSessionConfig(None, None, None, None, ()),
            provider=None,
            binding_id="test-binding",
            working_directory="/runtime/workspace/project/attempt",
            host_root=tmp_path / "host",
            runtime_root=tmp_path / "runtime",
        )

    assert tuple(tool.name for tool in spec.tools) == FIXED_RUNTIME_TOOL_NAMES
    assert "task" not in FIXED_RUNTIME_TOOL_NAMES
    assert "workflow" not in FIXED_RUNTIME_TOOL_NAMES
    assert "task_tool_set" in FIXED_RUNTIME_TOOL_NAMES
    assert "workflow_tool_set" in FIXED_RUNTIME_TOOL_NAMES
    assert spec.confirmation_policy == "NEVER"
    assert spec.runtime_contract.required_tools == tuple(sorted(FIXED_RUNTIME_TOOL_NAMES))
