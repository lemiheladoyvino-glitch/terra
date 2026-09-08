from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import pytest

import server.app as app_module

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_configs() -> None:
    yaml = pytest.importorskip("yaml", reason="PyYAML from uvicorn[standard] is unavailable")
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    service, = blueprint["services"]
    assert service["type"] == "web" and service["runtime"] == "python"
    assert "--workers 1" in service["startCommand"]
    assert "--log-config log_config.yaml" in service["startCommand"]
    assert service["numInstances"] == 1
    assert service["healthCheckPath"] == "/healthz"
    assert service["plan"] == "free"
    assert "disk" not in service
    env = {entry["key"]: entry["value"] for entry in service["envVars"]}
    assert env["TERRA_DB"] == "/tmp/terra.db"
    assert (ROOT / "runtime.txt").read_text().strip() == f'python-{env["PYTHON_VERSION"]}'
    config = yaml.safe_load((ROOT / "log_config.yaml").read_text())
    assert config["loggers"]["server"]["level"] == "INFO"
    assert config["root"]["level"] == "INFO"
    assert config["handlers"]["stdout"]["stream"] == "ext://sys.stdout"


def test_shutdown_save_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TERRA_DB", str(tmp_path / "deadline.db"))
    monkeypatch.setattr(app_module, "SHUTDOWN_SAVE_TIMEOUT", 0.02)
    release = threading.Event()

    def slow_write(path: Path, blob: bytes) -> None:
        assert release.wait(5)

    monkeypatch.setattr(app_module, "write_snapshot", slow_write)

    async def exercise() -> None:
        context = app_module.lifespan(app_module.app)
        await context.__aenter__()
        try:
            await asyncio.wait_for(context.__aexit__(None, None, None), 1)
            assert not app_module.app.state.shutdown_save_task.done()
        finally:
            release.set()
            await app_module.app.state.shutdown_save_task

    with caplog.at_level(logging.ERROR):
        asyncio.run(exercise())
    assert "Shutdown save did not finish" in caplog.text
