"""Small DataHarbor API client for worker tasks (standard library only).

Requests go through a `Transport`, so tests can drive the real FastAPI app
in-process while Airflow uses `UrllibTransport` over HTTP. The bearer token
is only ever sent in the `Authorization` header and never logged.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


class ApiError(Exception):
    """A non-2xx answer, carrying the API's error envelope code and message."""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class Transport(Protocol):
    def __call__(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]: ...


@dataclass
class UrllibTransport:
    base_url: str
    timeout_seconds: float = 30.0

    def __call__(self, method, path, *, params=None, json_body=None, content=None, headers=None):
        url = self.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = dict(headers or {})
        data = content
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read())


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


class ApiClient:
    """Calls scoped to one workspace with one bearer token."""

    def __init__(self, transport: Transport, token: str, workspace_id: str):
        self._transport = transport
        self._auth = {"Authorization": f"Bearer {token}"}
        self.workspace_id = workspace_id

    def _call(self, method: str, path: str, **kwargs) -> tuple[int, Any]:
        headers = {**self._auth, **kwargs.pop("headers", {})}
        status, body = self._transport(
            method, f"/v1/workspaces/{self.workspace_id}{path}", headers=headers, **kwargs
        )
        if status >= 400:
            error = (body or {}).get("error") or {} if isinstance(body, dict) else {}
            raise ApiError(
                status,
                error.get("code", "error"),
                error.get("message", "Unexpected response"),
                error.get("details"),
            )
        return status, body

    def pipeline_by_name(self, name: str) -> dict:
        _, body = self._call("GET", "/pipelines")
        for pipeline in body["items"]:
            if pipeline["name"] == name:
                return pipeline
        raise LookupError(f"Pipeline {name!r} is not registered in this workspace")

    def raw_datasets(self) -> dict[str, dict]:
        """Raw datasets by name."""
        found: dict[str, dict] = {}
        params = {"layer": "raw", "limit": "200"}
        while True:
            _, body = self._call("GET", "/datasets", params=params)
            found.update({d["name"]: d for d in body["items"]})
            if not body["next_cursor"]:
                return found
            params = {**params, "cursor": body["next_cursor"]}

    def start_run(self, pipeline_id: str, external_run_id: str, business_date: date,
                  trigger: str) -> dict:
        _, body = self._call(
            "POST",
            f"/pipelines/{pipeline_id}/runs",
            json_body={
                "external_run_id": external_run_id,
                "business_date": business_date.isoformat(),
                "trigger": trigger,
            },
        )
        return body

    def get_run(self, pipeline_id: str, run_id: str) -> dict:
        return self._call("GET", f"/pipelines/{pipeline_id}/runs/{run_id}")[1]

    def record_task(self, pipeline_id: str, run_id: str, **report: Any) -> dict:
        body = {k: v for k, v in report.items() if v is not None}
        return self._call("POST", f"/pipelines/{pipeline_id}/runs/{run_id}/tasks",
                          json_body=body)[1]

    def finish_run(self, pipeline_id: str, run_id: str, status: str,
                   error_summary: str | None = None) -> dict:
        body = {"status": status}
        if error_summary is not None:
            body["error_summary"] = error_summary
        return self._call("POST", f"/pipelines/{pipeline_id}/runs/{run_id}/finish",
                          json_body=body)[1]

    def land_file(self, dataset_id: str, data: bytes, business_date: date, filename: str,
                  sha256: str) -> dict:
        _, body = self._call(
            "POST",
            f"/datasets/{dataset_id}/files",
            params={"business_date": business_date.isoformat(), "filename": filename},
            content=data,
            headers={"X-Content-SHA256": sha256, "Content-Type": "application/octet-stream"},
        )
        return body

    def landed_files(self, dataset_id: str, business_date: date) -> list[dict]:
        """Files landed for a dataset and business date, newest landing first."""
        _, body = self._call(
            "GET",
            f"/datasets/{dataset_id}/files",
            params={"business_date": business_date.isoformat(), "limit": "200"},
        )
        return body["items"]
