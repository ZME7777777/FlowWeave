from dataclasses import replace
from time import time

import pytest

from flowweave.bootstrap.settings import Settings
from flowweave.modules.agent_workspaces.presentation.browser_content import BrowserContent
from flowweave.shared.browser_preview import BrowserPreview, deployment_path, read_preview
from flowweave.shared.errors import DomainError
from flowweave.shared.secret_redaction import redact_secret_text
from flowweave.shared.settings import settings_context

PREFIX = "/flowweave/api/v1/agent-workspaces/workspace/browser/http/5186/_preview/grant/"


def test_html_keeps_base_once_and_rewrites_vite_preamble_and_nested_resources():
    renderer = BrowserContent(PREFIX, "http", 5186, "/reports/index.html")
    content = renderer.html("""<!doctype html><HTML><HEAD class="main">
      <script type="module">import { injectIntoGlobalHook } from "/@react-refresh";</script>
      <script type="module" src="/src/main.tsx?t=1&amp;v=2"></script>
      <link rel="stylesheet" href="../style.css"><style>@import '/theme.css';</style>
      </HEAD><body><img src="image.png"><a href="#part">Part</a>
      <a href="https://example.test/report">External</a></body></HTML>""")
    assert content.count("<base ") == 1
    assert f'<base href="{PREFIX}reports/index.html">' in content
    assert f'from "{PREFIX}@react-refresh"' in content
    assert f'src="{PREFIX}src/main.tsx?t=1&amp;v=2"' in content
    assert f'href="{PREFIX}style.css"' in content
    assert f'src="{PREFIX}reports/image.png"' in content
    assert 'href="#part"' in content
    assert 'href="https://example.test/report"' in content
    assert f'@import "{PREFIX}theme.css"' in content


def test_module_imports_exports_dynamic_imports_and_vite_css_keep_proxy_scope():
    renderer = BrowserContent(PREFIX, "http", 5186, "/src/main.js")
    text = """import React from "/node_modules/.vite/deps/react.js?v=1";
    import './style.css';
    export { value } from '../value.js';
    const lazy=import("/src/lazy.js");
    const image=new URL("./image.png", import.meta.url);
    export default "/asset.svg";
    const __vite__css = "body{background:url('/image.png')}";
    // import "/comment.js";
    const message='import "/text.js"';
    const route="/users";
    import 'react';"""
    rewritten = renderer.javascript(text)
    for path in [
        "node_modules/.vite/deps/react.js?v=1",
        "src/style.css",
        "value.js",
        "src/lazy.js",
        "src/image.png",
        "asset.svg",
        "image.png",
    ]:
        assert PREFIX + path in rewritten
    assert '// import "/comment.js";' in rewritten
    assert "const message='import \"/text.js\"'" in rewritten
    assert 'const route="/users"' in rewritten
    assert "import 'react'" in rewritten


def test_css_binary_and_local_absolute_redirects():
    renderer = BrowserContent(PREFIX, "http", 5186, "/css/style.css")
    css = renderer.css(
        '@import "./theme.css"; a{src:url(../font.woff2);x:url(data:image/png;base64,AAA)}'
    )
    assert f'@import "{PREFIX}css/theme.css"' in css
    assert f'url("{PREFIX}font.woff2")' in css
    assert 'url("data:image/png;base64,AAA")' in css
    assert renderer.rewrite(b"\x00\xff", "image/png") == b"\x00\xff"
    assert renderer.url("http://127.0.0.1:5186/login?a=1#next") == PREFIX + "login?a=1#next"
    assert renderer.url("https://example.test/") == "https://example.test/"
    assert renderer.url("http://localhost:8080/") == "http://localhost:8080/"


def test_preview_grants_expire_are_scoped_and_are_redacted():
    preview = BrowserPreview(
        "user",
        "session-digest",
        "workspace",
        "http",
        5186,
        "binding",
        None,
        "/flowweave",
        int(time()) + 60,
    )
    with settings_context(Settings(_env_file=None)):
        token = preview.encode()
        path = f"/api/v1/agent-workspaces/workspace/browser/http/5186/_preview/{token}/src/app.js"
        assert read_preview(path) == preview
        assert read_preview("/api/v1/auth/me?preview_token=" + token) is None
        for changed in [
            path.replace("/5186/", "/3000/"),
            path.replace("/workspace/", "/other/"),
            path.replace("/http/", "/https/"),
            path.replace(token, token[:-5] + "AAAAA"),
        ]:
            with pytest.raises(DomainError):
                read_preview(changed)
        expired = replace(preview, expires_at=int(time()) - 1).encode()
        with pytest.raises(DomainError):
            read_preview(path.replace(token, expired))
        assert token not in redact_secret_text(path)
        assert "session-digest" not in token


@pytest.mark.parametrize("path", ["//host", "/a/../b", "https://host", "/a?b", "/a\\b"])
def test_deployment_prefix_cannot_escape_origin(path):
    with pytest.raises(DomainError):
        deployment_path(path)
