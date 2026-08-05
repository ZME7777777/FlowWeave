"""Run both hardened Docker sandbox images through the production adapter."""

from __future__ import annotations

from flowweave.bootstrap.settings import Settings
from flowweave.shared.infrastructure.sandbox import DockerSandbox


def main() -> None:
    settings = Settings()
    if settings.sandbox_backend != "docker":
        raise SystemExit("SANDBOX_BACKEND must be docker")
    sandbox = DockerSandbox(
        settings.sandbox_image_python,
        settings.sandbox_image_javascript,
    )
    python = sandbox.execute(
        "PYTHON",
        "result = {'decision': 'PASS', 'summary': 'python-docker-smoke'}",
        {},
        10,
    )
    javascript = sandbox.execute(
        "JAVASCRIPT",
        "return {decision: 'PASS', summary: 'javascript-docker-smoke'};",
        {},
        10,
    )
    expected = (
        (python, "python-docker-smoke"),
        (javascript, "javascript-docker-smoke"),
    )
    for execution, summary in expected:
        if execution.status != "COMPLETED" or not isinstance(execution.result, dict):
            raise SystemExit(f"Sandbox smoke failed: {execution}")
        if execution.result.get("decision") != "PASS" or execution.result.get("summary") != summary:
            raise SystemExit(f"Unexpected sandbox result: {execution.result}")
    print("docker-sandbox-smoke: python=PASS javascript=PASS")


if __name__ == "__main__":
    main()
