import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response

from flowweave.bootstrap.container import Container
from flowweave.modules.catalog.application import (
    agent_profiles,
    capability_collections,
    capability_imports,
    mcp_oauth_authorizations,
    mcp_oauth_secrets,
    mcp_validations,
    memory_sources,
    plugin_sources,
    plugin_validations,
    service,
)
from flowweave.runtime.base import RuntimeMCPOAuthCallbackRequest
from flowweave.shared.errors import DomainError
from flowweave.shared.http import Db, get_container, run_sync
from flowweave.shared.schemas import (
    AgentProfileCopyWrite,
    AgentProfileRetireWrite,
    AgentProfileRevisionWrite,
    CapabilityBulkDeleteWrite,
    CapabilityCollectionWrite,
    CapabilityCommitWrite,
    CapabilityMcpRevisionWrite,
    CapabilitySkillRevisionWrite,
    CapabilityValidateWrite,
    DirectoryWrite,
    MarketplaceCatalogRead,
    MarketplaceCatalogWrite,
    MarketplacePluginSourceResolveWrite,
    MCPOAuthAuthorizationCallbackWrite,
    MCPOAuthAuthorizationStartWrite,
    MCPOAuthSecretReferenceRevokeWrite,
    MCPOAuthSecretReferenceWrite,
    MCPProbeWrite,
    MemorySourceActivateWrite,
    MemorySourceCreateWrite,
    MemorySourceLifecycleWrite,
    MemorySourceReviewWrite,
    MemorySourceRevisionWrite,
    MemorySourceScanWrite,
    NodeAssetBulkDeleteWrite,
    NodeAssetWrite,
    PluginProbeWrite,
    PluginSourcePublishWrite,
    PluginSourceResolveWrite,
)

ContainerDep = Annotated[Container, Depends(get_container)]
ReviewActor = Annotated[str, Header(alias="X-Actor-ID", min_length=1, max_length=200)]

router = APIRouter()


@router.get("/agent-profiles/{version_id}")
async def agent_profile(version_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: agent_profiles.read_profile(session, version_id))


@router.get("/agent-profile-packages/{package_id}/versions")
async def agent_profile_versions(package_id: str, db: Db) -> list[dict[str, Any]]:
    return await run_sync(
        db, lambda session: agent_profiles.list_profile_versions(session, package_id)
    )


@router.post("/agent-profiles/{version_id}/versions", status_code=201)
async def revise_agent_profile(
    version_id: str, payload: AgentProfileRevisionWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: agent_profiles.revise_profile(session, version_id, payload)
    )


@router.post("/agent-profiles/{version_id}/copy", status_code=201)
async def copy_agent_profile(
    version_id: str, payload: AgentProfileCopyWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: agent_profiles.copy_profile(session, version_id, payload)
    )


@router.post("/agent-profiles/{version_id}/retire")
async def retire_agent_profile(
    version_id: str, payload: AgentProfileRetireWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: agent_profiles.retire_profile(session, version_id, payload)
    )


@router.get("/memory-sources")
async def governed_memory_sources(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, memory_sources.list_sources)


@router.post("/memory-sources", status_code=201)
async def create_governed_memory_source(payload: MemorySourceCreateWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: memory_sources.create_source(session, payload))


@router.get("/memory-sources/{source_id}")
async def governed_memory_source(source_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: memory_sources.read_source(session, source_id))


@router.post("/memory-sources/{source_id}/versions", status_code=201)
async def create_governed_memory_source_revision(
    source_id: str, payload: MemorySourceRevisionWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: memory_sources.create_revision(session, source_id, payload)
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/review")
async def review_governed_memory_source_version(
    source_id: str,
    version_id: str,
    payload: MemorySourceReviewWrite,
    db: Db,
    actor: ReviewActor,
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: memory_sources.review_version(
            session, source_id, version_id, payload, actor
        ),
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/scan")
async def scan_governed_memory_source_version(
    source_id: str, version_id: str, payload: MemorySourceScanWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: memory_sources.scan_version(session, source_id, version_id, payload)
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/activate")
async def activate_governed_memory_source_version(
    source_id: str, version_id: str, payload: MemorySourceActivateWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: memory_sources.activate_version(session, source_id, version_id, payload)
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/retire")
async def retire_governed_memory_source_version(
    source_id: str, version_id: str, payload: MemorySourceLifecycleWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: memory_sources.retire_version(session, source_id, version_id, payload)
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/expire")
async def expire_governed_memory_source_version(
    source_id: str, version_id: str, payload: MemorySourceLifecycleWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: memory_sources.expire_version(session, source_id, version_id, payload)
    )


@router.post("/memory-sources/{source_id}/versions/{version_id}/delete")
async def delete_governed_memory_source_version_content(
    source_id: str, version_id: str, payload: MemorySourceLifecycleWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: memory_sources.delete_version_content(
            session, source_id, version_id, payload
        ),
    )


@router.get("/capability-collections")
async def collections(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, capability_collections.list_collections)


@router.post("/capability-collections", status_code=201)
async def create_collection(payload: CapabilityCollectionWrite, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_collections.save_collection(session, payload)
    )


@router.put("/capability-collections/{collection_id}")
async def update_collection(
    collection_id: str, payload: CapabilityCollectionWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: capability_collections.save_collection(session, payload, collection_id),
    )


@router.delete("/capability-collections/{collection_id}", status_code=204, response_class=Response)
async def delete_collection(collection_id: str, db: Db) -> Response:
    await run_sync(
        db, lambda session: capability_collections.delete_collection(session, collection_id)
    )
    return Response(status_code=204)


@router.get("/node-directories")
async def directories(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, service.list_directories)


@router.post("/node-directories", status_code=201)
async def create_directory(payload: DirectoryWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.create_directory(session, payload))


@router.get("/node-assets")
async def assets(
    db: Db, directory_id: str | None = None, q: str | None = Query(default=None, max_length=200)
) -> list[dict[str, Any]]:
    return await run_sync(db, lambda session: service.list_assets(session, directory_id, q))


@router.post("/node-assets", status_code=201)
async def create_asset(payload: NodeAssetWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_asset(session, payload))


@router.get("/node-assets/{asset_id}")
async def asset(asset_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.read_asset(session, asset_id))


@router.put("/node-assets/{asset_id}")
async def update_asset(asset_id: str, payload: NodeAssetWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.save_asset(session, payload, asset_id))


@router.delete("/node-assets/{asset_id}", status_code=204, response_class=Response)
async def delete_asset(asset_id: str, db: Db) -> Response:
    await run_sync(db, lambda session: service.delete_asset(session, asset_id))
    return Response(status_code=204)


@router.delete("/node-assets")
async def delete_assets(payload: NodeAssetBulkDeleteWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: service.delete_assets(session, payload.ids))


@router.post("/capability-imports/validate")
async def validate_capability(payload: CapabilityValidateWrite, db: Db) -> dict[str, Any]:
    await db.rollback()
    prepared = await asyncio.to_thread(capability_imports.prepare_validation, payload)
    stored = await asyncio.to_thread(capability_imports.store_validation_source, prepared)
    try:
        return await run_sync(
            db, lambda session: capability_imports.register_validation(session, stored)
        )
    except BaseException:
        await asyncio.to_thread(capability_imports.discard_validation_source, stored)
        raise


@router.post("/capability-imports", status_code=201)
async def commit_capability(payload: CapabilityCommitWrite, db: Db) -> dict[str, Any]:
    plan = await run_sync(
        db, lambda session: capability_imports.prepare_commit(session, payload.import_token)
    )
    final_key = await asyncio.to_thread(capability_imports.finalize_commit_source, plan)
    return await run_sync(
        db, lambda session: capability_imports.confirm_commit(session, plan, final_key)
    )


@router.post("/plugin-source-resolutions", status_code=202)
async def resolve_plugin_source(payload: PluginSourceResolveWrite, db: Db) -> dict[str, Any]:
    return await run_sync(db, lambda session: plugin_sources.create_resolution(session, payload))


@router.post("/plugin-source-resolutions/marketplace", status_code=202)
async def resolve_marketplace_plugin_source(
    payload: MarketplacePluginSourceResolveWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: plugin_sources.create_marketplace_resolution(session, payload)
    )


@router.post(
    "/plugin-marketplace-catalogs/preview",
    response_model=MarketplaceCatalogRead,
)
async def preview_plugin_marketplace_catalog(
    payload: MarketplaceCatalogWrite,
) -> dict[str, object]:
    return await asyncio.to_thread(plugin_sources.list_marketplace_catalog, payload)


@router.get("/plugin-source-resolutions/{resolution_id}")
async def plugin_source_resolution(resolution_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: plugin_sources.read_resolution(session, resolution_id)
    )


@router.post("/plugin-source-resolutions/{resolution_id}/publish", status_code=201)
async def publish_plugin_source(
    resolution_id: str, payload: PluginSourcePublishWrite, db: Db
) -> dict[str, Any]:
    plan = await run_sync(
        db,
        lambda session: plugin_sources.prepare_publish_resolution(
            session, resolution_id, payload.expected_state_version
        ),
    )
    if isinstance(plan, dict):
        return plan
    await db.rollback()
    await asyncio.to_thread(plugin_sources.verify_publish_source, plan)
    return await run_sync(
        db, lambda session: plugin_sources.confirm_publish_resolution(session, plan)
    )


@router.get("/capabilities")
async def capabilities(db: Db) -> list[dict[str, Any]]:
    return await run_sync(db, capability_imports.list_capabilities)


@router.post("/capabilities/{capability_id}/mcp-oauth-secret-references", status_code=201)
async def create_mcp_oauth_secret_reference(
    capability_id: str, payload: MCPOAuthSecretReferenceWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: mcp_oauth_secrets.create_reference(session, capability_id, payload)
    )


@router.get("/mcp-oauth-secret-references/{secret_reference_id}")
async def mcp_oauth_secret_reference(secret_reference_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: mcp_oauth_secrets.read_reference(session, secret_reference_id)
    )


@router.post("/mcp-oauth-secret-references/{secret_reference_id}/revoke")
async def revoke_mcp_oauth_secret_reference(
    secret_reference_id: str, payload: MCPOAuthSecretReferenceRevokeWrite, db: Db
) -> dict[str, Any]:
    return await run_sync(
        db,
        lambda session: mcp_oauth_secrets.revoke_reference(session, secret_reference_id, payload),
    )


@router.post(
    "/mcp-oauth-secret-references/{secret_reference_id}/authorizations",
    status_code=202,
)
async def start_mcp_oauth_authorization(
    secret_reference_id: str,
    payload: MCPOAuthAuthorizationStartWrite,
    db: Db,
    container: ContainerDep,
) -> dict[str, Any]:
    plan = await run_sync(
        db,
        lambda session: mcp_oauth_authorizations.begin_authorization(
            session, secret_reference_id, payload
        ),
    )
    try:
        request = await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.allocate_authorization_runtime(session, plan),
        )
        status = await asyncio.to_thread(container.runtime.start_mcp_oauth, request)
        result = await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.complete_start(
                session, plan.authorization_id, status
            ),
        )
    except BaseException as exc:
        error_code = exc.code if isinstance(exc, DomainError) else "MCP_OAUTH_AUTHORIZATION_FAILED"
        await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.fail_authorization(
                session, plan.authorization_id, error_code
            ),
        )
        await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.cleanup_terminal(
                session, plan.authorization_id
            ),
        )
        raise
    if result["state"] in {"SUCCEEDED", "FAILED", "EXPIRED"}:
        await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.cleanup_terminal(
                session, plan.authorization_id
            ),
        )
    return result


@router.get("/mcp-oauth-authorizations/{authorization_id}")
async def mcp_oauth_authorization(
    authorization_id: str, db: Db, container: ContainerDep
) -> dict[str, Any]:
    plan = await run_sync(
        db,
        lambda session: mcp_oauth_authorizations.prepare_status(session, authorization_id),
    )
    if isinstance(plan, dict):
        result = plan
    else:
        status = await asyncio.to_thread(container.runtime.read_mcp_oauth, plan.request)
        result = await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.complete_status(session, plan, status),
        )
    if result["state"] in {"SUCCEEDED", "FAILED", "EXPIRED"}:
        await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.cleanup_terminal(session, authorization_id),
        )
    return result


@router.post("/mcp-oauth-authorizations/{authorization_id}/callback")
async def submit_mcp_oauth_authorization_callback(
    authorization_id: str,
    payload: MCPOAuthAuthorizationCallbackWrite,
    db: Db,
    container: ContainerDep,
) -> dict[str, Any]:
    plan = await run_sync(
        db,
        lambda session: mcp_oauth_authorizations.prepare_callback(
            session, authorization_id, payload
        ),
    )
    callback_request = plan.request
    if not isinstance(callback_request, RuntimeMCPOAuthCallbackRequest):
        raise DomainError(
            "MCP_OAUTH_PROTOCOL_ERROR",
            "MCP OAuth callback plan has an invalid Runtime request",
            500,
        )
    status = await asyncio.to_thread(container.runtime.submit_mcp_oauth_callback, callback_request)
    result = await run_sync(
        db,
        lambda session: mcp_oauth_authorizations.complete_callback(session, plan, status),
    )
    if result["state"] in {"SUCCEEDED", "FAILED", "EXPIRED"}:
        await run_sync(
            db,
            lambda session: mcp_oauth_authorizations.cleanup_terminal(session, authorization_id),
        )
    return result


@router.post("/capabilities/{capability_id}/mcp-probes", status_code=201)
async def probe_mcp_capability(
    capability_id: str, payload: MCPProbeWrite, db: Db, container: ContainerDep
) -> dict[str, Any]:
    plan = await run_sync(
        db, lambda session: mcp_validations.begin_probe(session, capability_id, payload)
    )
    try:
        request = await run_sync(
            db, lambda session: mcp_validations.allocate_probe_runtime(session, plan)
        )
        result = await asyncio.to_thread(container.runtime.probe_mcp, request)
        return await run_sync(
            db, lambda session: mcp_validations.complete_probe(session, plan.validation_id, result)
        )
    except BaseException as exc:
        error_code = exc.code if isinstance(exc, DomainError) else "MCP_PROBE_FAILED"
        await run_sync(
            db, lambda session: mcp_validations.fail_probe(session, plan.validation_id, error_code)
        )
        raise
    finally:
        await run_sync(
            db, lambda session: mcp_validations.cleanup_probe(session, plan.validation_id)
        )


@router.post("/capabilities/{capability_id}/plugin-probes", status_code=201)
async def probe_plugin_capability(
    capability_id: str, payload: PluginProbeWrite, db: Db, container: ContainerDep
) -> dict[str, Any]:
    plan = await run_sync(
        db, lambda session: plugin_validations.begin_probe(session, capability_id, payload)
    )
    try:
        request = await run_sync(
            db, lambda session: plugin_validations.allocate_probe_runtime(session, plan)
        )
        result = await asyncio.to_thread(container.runtime.validate_plugin, request)
        return await run_sync(
            db,
            lambda session: plugin_validations.complete_probe(session, plan.validation_id, result),
        )
    except BaseException as exc:
        error_code = exc.code if isinstance(exc, DomainError) else "PLUGIN_PROBE_FAILED"
        await run_sync(
            db,
            lambda session: plugin_validations.fail_probe(session, plan.validation_id, error_code),
        )
        raise
    finally:
        await run_sync(
            db, lambda session: plugin_validations.cleanup_probe(session, plan.validation_id)
        )


@router.get("/capabilities/{capability_id}/source")
async def capability_source(capability_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.read_skill_source(session, capability_id)
    )


@router.get("/capabilities/{capability_id}/context-source")
async def context_source(capability_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.read_context_source(session, capability_id)
    )


@router.put("/capabilities/{capability_id}/source")
async def update_capability_source(
    capability_id: str, payload: CapabilitySkillRevisionWrite, db: Db
) -> dict[str, Any]:
    update_plan = await run_sync(
        db,
        lambda session: capability_imports.prepare_skill_update(
            session, capability_id, payload.content
        ),
    )
    stored = await asyncio.to_thread(capability_imports.store_skill_update_source, update_plan)
    try:
        final_key = await asyncio.to_thread(capability_imports.finalize_skill_update_source, stored)
        return await run_sync(
            db,
            lambda session: capability_imports.confirm_skill_update(session, stored, final_key),
        )
    except BaseException:
        if stored.prepared.storage_key is not None:
            await asyncio.to_thread(capability_imports.discard_validation_source, stored.prepared)
        raise


@router.get("/capabilities/{capability_id}/mcp-source")
async def mcp_source(capability_id: str, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.read_mcp_source(session, capability_id)
    )


@router.put("/capabilities/{capability_id}/mcp-source")
async def update_mcp_source(
    capability_id: str, payload: CapabilityMcpRevisionWrite, db: Db
) -> dict[str, Any]:
    update_plan = await run_sync(
        db,
        lambda session: capability_imports.prepare_mcp_update(
            session, capability_id, payload.content, payload.mcp_scripts
        ),
    )
    stored = await asyncio.to_thread(capability_imports.store_skill_update_source, update_plan)
    try:
        final_key = await asyncio.to_thread(capability_imports.finalize_skill_update_source, stored)
        return await run_sync(
            db,
            lambda session: capability_imports.confirm_mcp_update(session, stored, final_key),
        )
    except BaseException:
        if stored.prepared.storage_key is not None:
            await asyncio.to_thread(capability_imports.discard_validation_source, stored.prepared)
        raise


@router.delete("/capabilities/{capability_id}", status_code=204, response_class=Response)
async def delete_capability(capability_id: str, db: Db) -> Response:
    result = await run_sync(
        db, lambda session: capability_imports.delete_capabilities(session, [capability_id])
    )
    if result["blocked"]:
        raise DomainError(
            "CAPABILITY_IN_USE",
            "Capability version is referenced by active nodes",
            409,
            {"blocked": result["blocked"]},
        )
    return Response(status_code=204)


@router.delete("/capabilities")
async def delete_capability_batch(payload: CapabilityBulkDeleteWrite, db: Db) -> dict[str, Any]:
    return await run_sync(
        db, lambda session: capability_imports.delete_capabilities(session, payload.ids)
    )
