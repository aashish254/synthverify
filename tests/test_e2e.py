"""End-to-end tests: CLI against real files, SDK against a live uvicorn server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ADMIN_SECRET, new_database_url  # noqa: E402
from fixtures_gen import (  # noqa: E402
    AI_TEXT,
    ai_generated_photo,
    doctored_photo,
    natural_photo,
    write_fixture,
)

pytestmark = pytest.mark.e2e

PY = sys.executable
PROJECT = Path(__file__).resolve().parent.parent


class TestCLI:
    def _run(self, *args, env_overrides: dict | None = None):
        env = os.environ.copy()
        env.update(env_overrides or {})
        return subprocess.run(  # noqa: S603
            [PY, "-m", "synthverify.cli", *args],
            capture_output=True, text=True, cwd=str(PROJECT), env=env, timeout=120,
        )

    def test_list_detectors(self):
        result = self._run("list-detectors")
        assert result.returncode == 0
        for name in ("ela", "noise", "audio_spectral", "video_temporal", "text_stylometry"):
            assert name in result.stdout

    def test_analyze_human_readable(self, tmp_path):
        f = write_fixture(tmp_path, "doc.jpg", doctored_photo())
        result = self._run("analyze", str(f))
        assert result.returncode == 0
        assert "risk score" in result.stdout
        assert "MEDIUM" in result.stdout
        assert "MANUAL_REVIEW" in result.stdout
        assert "top evidence" in result.stdout.lower()

    def test_analyze_json(self, tmp_path):
        f = write_fixture(tmp_path, "ai.png", ai_generated_photo())
        import json

        result = self._run("analyze", str(f), "--json")
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["verdict"]["recommended_action"] == "BLOCK"

    def test_analyze_missing_file(self, tmp_path):
        result = self._run("analyze", str(tmp_path / "nope.jpg"))
        assert result.returncode == 2

    def test_audit_verify_clean_chain(self, database_env):
        self._run("create-key", "--name", "ci", "--role", "service", env_overrides=database_env)
        result = self._run("audit-verify", env_overrides=database_env)
        assert result.returncode == 0
        assert "VERIFIED" in result.stdout

    def test_create_key_prints_secret(self, database_env):
        result = self._run(
            "create-key", "--name", "robot", "--role", "service", env_overrides=database_env
        )
        assert result.returncode == 0
        assert "sv_live_" in result.stdout


class TestSDKLiveServer:
    @pytest.fixture()
    def live_server(self, tmp_path, monkeypatch):
        """A real uvicorn server on a free port with its own database."""
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        port_no = port.getsockname()[1]
        port.close()

        database_url, drop_database = new_database_url(tmp_path, "live")
        monkeypatch.setenv("SV_DATABASE_URL", database_url)
        monkeypatch.setenv("SV_STORAGE_DIR", str(tmp_path / "media"))
        monkeypatch.setenv("SV_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
        monkeypatch.setenv("SV_BOOTSTRAP_ADMIN_KEY", ADMIN_SECRET)
        monkeypatch.setenv("SV_EMBEDDED_WORKER", "true")
        monkeypatch.setenv("SV_WORKER_COUNT", "2")
        from synthverify.config import get_settings

        get_settings.cache_clear()
        import uvicorn

        config = uvicorn.Config("synthverify.app:app", host="127.0.0.1", port=port_no, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                import httpx

                if httpx.get(f"http://127.0.0.1:{port_no}/healthz", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port_no}"
        server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()
        drop_database()

    def test_full_workflow(self, live_server, tmp_path):
        from synthverify.client import SynthVerifyClient

        with SynthVerifyClient(live_server, api_key=ADMIN_SECRET) as sv:
            assert sv.health()["status"] == "ok"

            # sync analysis
            f = write_fixture(tmp_path, "nat.jpg", natural_photo())
            report = sv.analyze_file(f)
            assert report["verdict"]["risk_tier"] == "LOW"

            # async ingest + wait
            f2 = write_fixture(tmp_path, "ai.png", ai_generated_photo())
            full = sv.verify_and_wait(f2)
            assert full["status"] == "completed"
            assert full["result"]["verdict"]["recommended_action"] == "BLOCK"

            # text analysis
            f3 = write_fixture(tmp_path, "post.txt", AI_TEXT.encode())
            text_report = sv.analyze_file(f3)
            assert text_report["verdict"]["risk_score"] > 0.5

            # admin surface via SDK
            key_info = sv.create_key("sdk-created", role="analyst")
            assert key_info["key"].startswith("sv_live_")
            assert sv.verify_audit_chain()["verified"] is True
            stats = sv.stats()
            assert stats["jobs_by_status"]["completed"] >= 1

            # detectors catalog
            names = {d["name"] for d in sv.detectors()}
            assert "ela" in names and "text_stylometry" in names

    def test_sdk_error_surfacing(self, live_server):

        from synthverify.client import SynthVerifyClient, SynthVerifyError

        with SynthVerifyClient(live_server, api_key="sv_live_wrong") as sv:
            with pytest.raises(SynthVerifyError) as excinfo:
                sv.get_job("whatever")
            assert excinfo.value.status_code == 401
