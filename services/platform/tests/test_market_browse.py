from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from flowweave.modules.catalog.application import market_browse, market_sources, skill_market
from flowweave.shared.settings import settings_context

SHA = "a" * 40


@pytest.fixture(autouse=True)
def browse_settings(settings):
    with settings_context(settings):
        market_browse._cache.clear()
        yield
        market_browse._cache.clear()


def manifest():
    return {
        "name": "Example",
        "owner": {"name": "Team"},
        "plugins": [
            {
                "name": f"plugin-{number:02}",
                "source": f"./plugins/{number}",
                "description": f"Description {number}",
                "category": "review" if number == 22 else "tool",
            }
            for number in range(25)
        ],
    }


def test_plugin_pages_read_only_the_manifest_and_cache_one_pinned_snapshot(monkeypatch):
    pins = []
    reads = []
    monkeypatch.setattr(skill_market, "pin_commit", lambda *args: pins.append(args) or SHA)
    monkeypatch.setattr(
        market_browse, "public_json", lambda url, **kw: reads.append(url) or manifest()
    )
    source = market_sources.BUILTINS[0]
    first = market_browse.plugin_page(source, "", 1, 10, None)
    second = market_browse.plugin_page(source, "", 2, 10, first["commit"])
    search = market_browse.plugin_page(source, "review", 1, 10, first["commit"])
    assert len(first["plugins"]) == 10 and first["total"] == 25
    assert [item["name"] for item in second["plugins"]] == [f"plugin-{i:02}" for i in range(10, 20)]
    assert [item["name"] for item in search["plugins"]] == ["plugin-22"]
    assert search["total"] == 1 and search["commit"] == SHA
    assert market_browse.plugin_page(source, "", 4, 10, SHA)["plugins"] == []
    assert len(pins) == 1 and len(reads) == 1
    assert reads[0].endswith(f"/{SHA}/.plugin/marketplace.json")
    assert first["source"] == source.url
    first["plugins"][0]["name"] = "mutated"
    assert market_browse.plugin_page(source, "", 1, 10, SHA)["plugins"][0]["name"] == "plugin-00"


def test_manifest_falls_back_only_on_missing_path_and_errors_are_not_cached(monkeypatch):
    calls = []

    def load(url, **kwargs):
        calls.append(url)
        return None if "/.plugin/" in url else manifest()

    monkeypatch.setattr(market_browse, "public_json", load)
    result = market_browse.plugin_page(market_sources.BUILTINS[0], "", 1, 10, SHA)
    assert result["total"] == 25 and len(calls) == 2
    market_browse._cache.clear()
    monkeypatch.setattr(market_browse, "public_json", lambda *a, **kw: {"plugins": [{}]})
    with pytest.raises(Exception, match="插件目录暂时不可用"):
        market_browse.plugin_page(market_sources.BUILTINS[0], "", 1, 10, SHA)
    monkeypatch.setattr(market_browse, "public_json", lambda *a, **kw: manifest())
    assert market_browse.plugin_page(market_sources.BUILTINS[0], "", 1, 10, SHA)["total"] == 25


def test_simultaneous_catalog_reads_share_fetch_and_cache_has_a_bound():
    started = Event()
    finish = Event()
    calls = []

    def load():
        calls.append(True)
        started.set()
        assert finish.wait(5)
        return {"items": [1]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(market_browse.cached, ("same",), load)
        assert started.wait(5)
        second = pool.submit(market_browse.cached, ("same",), load)
        finish.set()
        assert first.result() == second.result() == {"items": [1]}
    assert len(calls) == 1
    for number in range(40):
        market_browse.cached(("key", number), lambda value=number: value)
    assert len(market_browse._cache) == 32


def test_popular_skill_page_uses_real_ranking_order_and_skips_unimportable_sources(monkeypatch):
    reads = []

    def load(url):
        reads.append(url)
        return {
            "skills": [
                {"source": "not-a-github-repo", "skillId": "invalid"},
                *[
                    {"source": "acme/skills", "skillId": f"skill-{i}", "installs": 1000 - i}
                    for i in range(30)
                ],
            ]
        }

    monkeypatch.setattr(market_browse, "public_json", load)
    source = market_sources.BUILTINS[1]
    first = skill_market.search(source, "")
    assert first["mode"] == "popular" and first["search_required"] is False
    assert len(first["skills"]) == 20
    assert first["skills"][0]["skill_id"] == "skill-0"
    assert first["skills"][-1]["installs"] == 981
    assert skill_market.search(source, "") == first
    assert reads == ["https://www.skills.sh/api/skills/all-time/0"]


def test_public_discovery_bounds_responses_and_does_not_follow_redirects(monkeypatch):
    seen = []

    def stream(method, url, **kwargs):
        seen.append(kwargs)
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
            )
        ).stream(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(ValueError, match="size limit"):
        market_browse.public_json("https://www.skills.sh/api/skills/all-time/0")
    assert seen[0]["follow_redirects"] is False


def test_market_page_route_validates_parameters_and_selected_source(client, monkeypatch):
    monkeypatch.setattr(skill_market, "pin_commit", lambda *args: SHA)
    monkeypatch.setattr(market_browse, "public_json", lambda *a, **kw: manifest())
    url = "/api/v1/market-sources/openhands/plugins"
    result = client.get(url, params={"page": 2, "page_size": 5})
    assert result.status_code == 200, result.text
    assert result.json()["page"] == 2 and len(result.json()["plugins"]) == 5
    assert result.json()["plugins"][0]["name"] == "plugin-05"
    for params in [{"page": 0}, {"page_size": 51}, {"commit": "main"}]:
        assert client.get(url, params=params).status_code == 422
    assert client.get(url.replace("openhands", "skills-sh")).status_code == 422


def test_raw_manifest_follows_only_bounded_links_in_the_same_commit(monkeypatch):
    root = f"https://raw.githubusercontent.com/acme/extensions/{SHA}/"
    urls = []

    def stream(method, url, **kwargs):
        def respond(request):
            urls.append(str(request.url))
            return (
                httpx.Response(200, content=b"../marketplaces/extensions.json")
                if "/.plugin/" in str(request.url)
                else httpx.Response(200, json=manifest())
            )

        return httpx.Client(transport=httpx.MockTransport(respond)).stream(method, url)

    monkeypatch.setattr(httpx, "stream", stream)
    result = market_browse.public_json(root + ".plugin/marketplace.json", repository_root=root)
    assert result["name"] == "Example"
    assert urls == [root + ".plugin/marketplace.json", root + "marketplaces/extensions.json"]
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda method, url, **kw: httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=b"../../escape.json")
            )
        ).stream(method, url),
    )
    with pytest.raises(ValueError, match="Invalid discovery JSON"):
        market_browse.public_json(root + ".plugin/marketplace.json", repository_root=root)
