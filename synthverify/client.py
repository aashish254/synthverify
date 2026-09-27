"""Official Python SDK client for the SynthVerify API.

    from synthverify.client import SynthVerifyClient

    sv = SynthVerifyClient("http://localhost:8080", api_key="sv_live_...")
    report = sv.verify_and_wait("invoice_scan.jpg")      # ingest + poll + report
    print(report["verdict"]["recommended_action"])
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx


class SynthVerifyError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SynthVerifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )
        self.poll_interval = poll_interval

    # ------------------------------------------------------------- primitive

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") or payload.get("error") or resp.text
            except Exception:  # noqa: BLE001
                payload, detail = None, resp.text
            raise SynthVerifyError(f"{method} {path} -> {resp.status_code}: {detail}", resp.status_code, payload)
        return resp

    # ---------------------------------------------------------------- meta

    def health(self) -> dict:
        return self._request("GET", "/healthz").json()

    def meta(self) -> dict:
        return self._request("GET", "/api/v1/meta").json()

    def detectors(self) -> list[dict]:
        return self._request("GET", "/api/v1/admin/detectors").json()["items"]

    # ------------------------------------------------------------ analysis

    def analyze_file(self, path: str | Path, detectors: list[str] | None = None) -> dict:
        """Synchronous inline analysis (files up to the server's inline limit)."""
        path = Path(path)
        data = {"requested_detectors": f"{list(detectors)}"} if detectors else None
        with path.open("rb") as fh:
            resp = self._request(
                "POST",
                "/api/v1/media/analyze",
                files={"file": (path.name, fh)},
                data=data,
            )
        return resp.json()

    def ingest_file(
        self,
        path: str | Path,
        detectors: list[str] | None = None,
        priority: int = 5,
        idempotency_key: str | None = None,
    ) -> str:
        """Asynchronous ingestion; returns the job_id."""
        path = Path(path)
        data = {"priority": str(priority)}
        if detectors:
            data["requested_detectors"] = f"{list(detectors)}"
        if idempotency_key:
            data["idempotency_key"] = idempotency_key
        with path.open("rb") as fh:
            resp = self._request(
                "POST",
                "/api/v1/media/ingest",
                files={"file": (path.name, fh)},
                data=data,
            )
        return resp.json()["job_id"]

    def get_job(self, job_id: str, include_report: bool = True) -> dict:
        return self._request("GET", f"/api/v1/jobs/{job_id}", params={"include_report": include_report}).json()

    def wait_for_job(self, job_id: str, timeout: float = 300.0) -> dict:
        """Poll until the job completes; raises SynthVerifyError on failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id, include_report=False)
            if job["status"] == "completed":
                return self.get_job(job_id, include_report=True)
            if job["status"] == "failed":
                raise SynthVerifyError(f"Job {job_id} failed: {job.get('error')}")
            time.sleep(self.poll_interval)
        raise SynthVerifyError(f"Timed out waiting for job {job_id}")

    def verify_and_wait(self, path: str | Path, **kwargs) -> dict:
        """One-call convenience: ingest, wait, return the full XAI report."""
        return self.wait_for_job(self.ingest_file(path, **kwargs))

    # --------------------------------------------------------------- admin

    def create_key(self, name: str, role: str = "service", organisation: str = "default") -> dict:
        return self._request(
            "POST",
            "/api/v1/admin/keys",
            json={"name": name, "role": role, "organisation": organisation},
        ).json()

    def create_webhook(self, url: str, events: list[str], description: str = "") -> dict:
        return self._request(
            "POST",
            "/api/v1/admin/webhooks",
            json={"url": url, "events": events, "description": description},
        ).json()

    def verify_audit_chain(self) -> dict:
        return self._request("GET", "/api/v1/admin/audit/verify").json()

    def stats(self) -> dict:
        return self._request("GET", "/api/v1/admin/stats").json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SynthVerifyClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
