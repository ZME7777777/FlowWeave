"""Apply FlowWeave's governed OpenHands source fixes.

OpenHands 1.44.0 deep-copies the source Agent when forking. That is correct
for general callers, but FlowWeave needs a fork to retain the selected event
history while adopting the currently governed condenser policy. The upstream
SDK already supports LocalConversation.fork(agent=...); this patch exposes
only the condenser override through Agent Server's fork request.

The native auto-title helper is also made protocol-aware: providers configured
for the Responses API (notably Codex OAuth) must use ``LLM.responses()``,
whereas Chat Completions providers retain ``LLM.completion()``.  Auto-title is
non-critical metadata, so its failure is logged upstream but never published as
a ConversationErrorEvent for a successful user turn.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _replace(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(
            f"unexpected OpenHands 1.44.0 source shape: {path}: {old[:80]!r}"
        )
    path.write_text(source.replace(old, new), encoding="utf-8")


def apply(source_root: Path) -> None:
    server = source_root / "openhands-agent-server" / "openhands" / "agent_server"

    models = server / "models.py"
    _replace(
        models,
        "from openhands.sdk.conversation.conversation_stats import ConversationStats\n",
        "from openhands.sdk.context.condenser import CondenserBase\n"
        "from openhands.sdk.conversation.conversation_stats import ConversationStats\n",
    )
    _replace(
        models,
        "    from_event_id: str | None = Field(\n"
        "        default=None,\n"
        "        description=(\n"
        "            \"If set, fork only the branch up to and including this event and \"\n",
        "    condenser: CondenserBase | None = Field(\n"
        "        default=None,\n"
        "        description=(\n"
        "            \"Optional governed condenser for the forked Agent. All other Agent \"\n"
        "            \"configuration remains copied from the source conversation.\"\n"
        "        ),\n"
        "    )\n"
        "    from_event_id: str | None = Field(\n"
        "        default=None,\n"
        "        description=(\n"
        "            \"If set, fork only the branch up to and including this event and \"\n",
    )

    router = server / "conversation_router.py"
    _replace(
        router,
        "            reset_metrics=request.reset_metrics,\n"
        "            from_event_id=request.from_event_id,\n",
        "            reset_metrics=request.reset_metrics,\n"
        "            from_event_id=request.from_event_id,\n"
        "            condenser=request.condenser,\n",
    )

    service = server / "conversation_service.py"
    _replace(
        service,
        "from openhands.sdk.conversation.impl.local_conversation import LocalConversation\n",
        "from openhands.sdk.context.condenser import CondenserBase\n"
        "from openhands.sdk.conversation.impl.local_conversation import LocalConversation\n",
    )
    _replace(
        service,
        "        reset_metrics: bool = True,\n"
        "        from_event_id: str | None = None,\n"
        "    ) -> ConversationInfo | None:\n",
        "        reset_metrics: bool = True,\n"
        "        from_event_id: str | None = None,\n"
        "        condenser: CondenserBase | None = None,\n"
        "    ) -> ConversationInfo | None:\n",
    )
    _replace(
        service,
        "        source_conversation = source_service.get_conversation()\n\n"
        "        # fork() deep-copies events, state, and writes to a new persistence dir.\n"
        "        fork_conv = await asyncio.to_thread(\n"
        "            source_conversation.fork,\n",
        "        source_conversation = source_service.get_conversation()\n"
        "        fork_agent = (\n"
        "            source_conversation.agent.model_copy(update={\"condenser\": condenser})\n"
        "            if condenser is not None\n"
        "            else None\n"
        "        )\n\n"
        "        # fork() deep-copies events, state, and writes to a new persistence dir.\n"
        "        fork_conv = await asyncio.to_thread(\n"
        "            source_conversation.fork,\n"
        "            agent=fork_agent,\n",
    )

    title_utils = source_root / "openhands-sdk" / "openhands" / "sdk" / "conversation" / "title_utils.py"
    _replace(
        title_utils,
        "        response = llm.completion(messages)\n",
        "        # Respect the provider's frozen protocol contract. Codex OAuth, for\n"
        "        # example, exposes only the native Responses endpoint; forcing it\n"
        "        # through Chat Completions turns a harmless title request into 404.\n"
        "        response = (\n"
        "            llm.responses(messages) if llm.api_mode == \"responses\" else llm.completion(messages)\n"
        "        )\n",
    )

    _replace(
        service,
        "        # Surface an LLM failure during auto-titling to the UI (issue #16686);\n"
        "        # generation itself stays non-fatal and falls back to truncation.\n"
        "        def _on_title_error(exc: Exception) -> None:\n"
        "            self.service._publish_error_event_sync(exc)\n\n",
        "        # Auto-title is non-critical metadata. Its fallback is the first user\n"
        "        # message, so never publish a title-provider failure as a conversation\n"
        "        # error for an otherwise successful turn.\n",
    )
    _replace(
        service,
        "                    50,\n"
        "                    _on_title_error,\n"
        "                )\n",
        "                    50,\n"
        "                )\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    apply(args.source_root)


if __name__ == "__main__":
    main()
