from __future__ import annotations

import io
import zipfile
from unittest.mock import Mock

import httpx
import pytest

from flowweave.modules.catalog.application import market_sources, skill_market
from flowweave.modules.users.application.security import tenant_user
from flowweave.modules.users.infrastructure.models import User
from flowweave.shared.errors import DomainError
from flowweave.shared.market_schemas import MarketSkillImportWrite, MarketSource
from flowweave.shared.settings import settings_context


@pytest.fixture(autouse=True)
def market_settings(settings):
    with settings_context(settings):
        yield


SHA = "a" * 40


def bundle(extra: dict[str, bytes] | None = None) -> bytes:
    files = {
        "repo/skills/demo/SKILL.md": (
            b"---\nname: demo\ndescription: A complete skill\n---\nUse resources.\n"
        ),
        "repo/skills/demo/scripts/run.py": b"print('resource')\n",
        "repo/skills/demo/references/guide.md": b"Reference\n",
        "repo/skills/other/SKILL.md": b"---\nname: other\n---\nOther\n",
        **(extra or {}),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_preferences_are_persisted_isolated_and_concurrent_edits_rejected(
    client, db_session_factory
):
    original = client.get("/api/v1/market-sources").json()
    assert [item["url"] for item in original["sources"]] == [
        "https://github.com/OpenHands/extensions.git",
        "https://skills.sh",
    ]
    custom = MarketSource(
        id="custom", name="Team skills", kind="SKILL_GIT", url="https://github.com/acme/skills.git"
    ).model_dump()
    changed = {**original, "sources": [*original["sources"], custom]}
    response = client.put("/api/v1/market-sources", json=changed)
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/market-sources").json() == response.json()
    assert client.put("/api/v1/market-sources", json=changed).status_code == 409
    with db_session_factory() as db:
        user = User(id="different", username="different", role="USER")
        db.add(user)
        db.commit()
        with tenant_user("different"):
            assert len(market_sources.read_sources(db)["sources"]) == 2
            with pytest.raises(DomainError):
                market_sources.select_source(db, "custom", {"SKILL_GIT"})


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "https://example.com"},
        {"builtin": False},
        {"name": "fake"},
    ],
)
def test_builtin_identity_cannot_be_replaced(client, patch):
    snapshot = client.get("/api/v1/market-sources").json()
    snapshot["sources"][0].update(patch)
    assert client.put("/api/v1/market-sources", json=snapshot).status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com@127.0.0.1/a/b",
        "https://github.com/a/b?token=x",
        "https://github.com/a/b/tree/main",
        "https://github.com/a/../b",
        "https://github.com:443/a/b",
    ],
)
def test_unsafe_custom_skill_repository_rejected(url):
    with pytest.raises(DomainError):
        market_sources.github_repository(url)


def test_disabled_source_cannot_be_used(client):
    snapshot = client.get("/api/v1/market-sources").json()
    snapshot["sources"][1]["enabled"] = False
    assert client.put("/api/v1/market-sources", json=snapshot).status_code == 200
    assert client.get("/api/v1/market-sources/skills-sh/skills?q=demo").status_code == 422
    assert (
        client.post(
            "/api/v1/market-skill-imports/validate",
            json={"source_id": "skills-sh", "repository": "acme/skills", "skill_id": "demo"},
        ).status_code
        == 422
    )


def test_public_search_uses_unauthenticated_bounded_endpoint(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.path == "/api/search"
        assert request.url.params["limit"] == "50"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "skills": [
                    {"source": "acme/skills", "skillId": "demo", "name": "Demo", "installs": 10},
                    {"source": "../../bad", "skillId": "bad"},
                ]
            },
        )

    real_stream = httpx.stream
    # Use the same stream semantics with a real MockTransport response.
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda method, url, **kwargs: httpx.Client(transport=httpx.MockTransport(handle)).stream(
            method, url, params=kwargs.get("params")
        ),
    )
    result = skill_market.search(market_sources.BUILTINS[1], "demo")
    assert len(result["skills"]) == 1
    assert result["skills"][0]["repository"] == "acme/skills"
    assert skill_market.search(market_sources.BUILTINS[1], "x")["search_required"]
    assert len(seen) == 1
    monkeypatch.setattr(httpx, "stream", real_stream)


def test_full_skill_directory_frozen_and_provenance_persisted(
    client, monkeypatch, db_session_factory
):
    monkeypatch.setattr(skill_market, "pin_commit", lambda *_: SHA)
    monkeypatch.setattr(skill_market, "_download", lambda *_: bundle())
    body = {"source_id": "skills-sh", "repository": "acme/skills", "skill_id": "demo"}
    validated = client.post("/api/v1/market-skill-imports/validate", json=body)
    assert validated.status_code == 200, validated.text
    data = validated.json()
    provenance = data["preview"]["source"]
    assert provenance["commit"] == SHA
    assert provenance["repository_url"] == "https://github.com/acme/skills.git"
    assert provenance["skill_path"] == "skills/demo"
    prepared = skill_market.prepare_import(
        market_sources.BUILTINS[1], MarketSkillImportWrite(**body)
    )
    with zipfile.ZipFile(io.BytesIO(prepared.content)) as archive:
        assert set(archive.namelist()) == {"SKILL.md", "scripts/run.py", "references/guide.md"}
    assert prepared.content_hash == data["content_hash"]
    published = client.post(
        "/api/v1/capability-imports", json={"import_token": data["import_token"]}
    )
    assert published.status_code == 201, published.text
    from sqlalchemy import select

    from flowweave.shared.models import CapabilityImport

    with db_session_factory() as db:
        imported = db.scalar(select(CapabilityImport))
        assert imported.preview_json["source"] == provenance
    again = client.post("/api/v1/market-skill-imports/validate", json=body).json()
    assert again["content_hash"] == data["content_hash"]


def test_ambiguous_skill_rejected_and_explicit_path_supported(monkeypatch):
    monkeypatch.setattr(
        skill_market,
        "_download",
        lambda *_: bundle({"repo/alternate/SKILL.md": b"---\nname: demo\n---\nDuplicate\n"}),
    )
    source = market_sources.BUILTINS[1]
    payload = MarketSkillImportWrite(
        source_id=source.id, repository="acme/skills", skill_id="demo", commit=SHA
    )
    with pytest.raises(DomainError, match="无法唯一定位"):
        skill_market.prepare_import(source, payload)
    prepared = skill_market.prepare_import(
        source, payload.model_copy(update={"path": "skills/demo"})
    )
    assert prepared.preview["source"]["skill_path"] == "skills/demo"


def test_archive_traversal_and_selected_skill_symlinks_rejected(monkeypatch):
    with pytest.raises(DomainError):
        skill_market._files(bundle({"repo/../../escape": b"x"}))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("repo/skills/demo/SKILL.md", "---\nname: demo\n---\nSkill\n")
        info = zipfile.ZipInfo("repo/skills/demo/link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    monkeypatch.setattr(skill_market, "_download", lambda *_: output.getvalue())
    with pytest.raises(DomainError):
        skill_market.prepare_import(
            market_sources.BUILTINS[1],
            MarketSkillImportWrite(
                source_id="skills-sh", repository="acme/skills", skill_id="demo", commit=SHA
            ),
        )
    # A repository's unrelated symlink is never followed or included in this Skill.
    outside = io.BytesIO()
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("repo/skills/demo/SKILL.md", "---\nname: demo\n---\nSkill\n")
        info = zipfile.ZipInfo("repo/unrelated-link")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "skills/demo")
    monkeypatch.setattr(skill_market, "_download", lambda *_: outside.getvalue())
    assert (
        skill_market.prepare_import(
            market_sources.BUILTINS[1],
            MarketSkillImportWrite(
                source_id="skills-sh", repository="acme/skills", skill_id="demo", commit=SHA
            ),
        ).preview["source"]["skill_path"]
        == "skills/demo"
    )


def test_custom_git_skill_catalog_is_pinned_and_confined(monkeypatch):
    monkeypatch.setattr(skill_market, "pin_commit", lambda *_: SHA)
    monkeypatch.setattr(skill_market, "_download", lambda *_: bundle())
    source = MarketSource(
        id="custom",
        name="Team",
        kind="SKILL_GIT",
        url="https://github.com/acme/skills.git",
        repo_path="skills/demo",
    )
    result = skill_market.search(source, "")
    assert len(result["skills"]) == 1
    assert result["skills"][0]["commit"] == SHA
    with pytest.raises(DomainError):
        skill_market.prepare_import(
            source,
            MarketSkillImportWrite(
                source_id=source.id, repository="another/repo", skill_id="demo", commit=SHA
            ),
        )


def test_annotated_tag_resolves_peeled_commit(monkeypatch):
    monkeypatch.setattr(
        skill_market.subprocess,
        "run",
        lambda *_, **__: Mock(
            returncode=0, stdout=f"{'b' * 40}\trefs/tags/v1\n{SHA}\trefs/tags/v1^{{}}\n"
        ),
    )
    assert skill_market.pin_commit("https://github.com/acme/skills.git", "v1") == SHA
