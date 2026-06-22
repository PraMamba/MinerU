# Copyright (c) Opendatalab. All rights reserved.
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from mineru.cli.api_request import ParseRequestOptions, parse_request_form
from mineru.cli.v4_adapter import (
    AdapterSettings,
    MineruUpstreamClient,
    SafeDownloadTarget,
    UrlDownloader,
    create_app,
    resolve_safe_download_target,
    main,
    settings_from_env,
)


AUTH = {"Authorization": "Bearer local-token"}
SETTINGS_URL = "https://cdn-mineru.openxlab.org.cn/demo/example.pdf"


def make_zip(markdown_name: str = "full.md", content: str = "# Parsed\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(markdown_name, content)
    return buffer.getvalue()


class FakeUpstream:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any] | Exception] = {}
        self.result_status = 200
        self.result_body = make_zip()
        self.result_media_type = "application/zip"
        self.submit_error: Exception | None = None
        self.status_error: Exception | None = None
        self.result_error: Exception | None = None
        self.malformed_submit = False
        self.submit_http_error = False
        self.counter = 0

    async def submit_task(
        self,
        *,
        file_path: Path,
        file_name: str,
        model_version: str,
        settings: AdapterSettings,
    ) -> str:
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_http_error:
            raise click.ClickException("Failed to submit parsing task: 500 upstream")
        if self.malformed_submit:
            raise click.ClickException("MinerU API returned an invalid task payload")
        self.counter += 1
        task_id = f"local-{self.counter}"
        self.submissions.append(
            {
                "file_path": file_path,
                "file_name": file_name,
                "model_version": model_version,
                "backend": "pipeline" if model_version == "pipeline" else "vlm-engine",
                "settings": settings,
                "exists_during_submit": file_path.exists(),
                "bytes": file_path.read_bytes(),
                "form": {
                    "backend": "pipeline" if model_version == "pipeline" else "vlm-engine",
                    "parse_method": "auto",
                    "return_md": "true",
                    "return_images": "true",
                    "response_format_zip": "true",
                    "formula_enable": str(settings.formula_enable).lower(),
                    "table_enable": str(settings.table_enable).lower(),
                    "lang_list": settings.lang,
                },
            }
        )
        self.statuses.setdefault(task_id, {"task_id": task_id, "status": "processing"})
        return task_id

    async def get_status(self, local_task_id: str) -> dict[str, Any]:
        if self.status_error is not None:
            raise self.status_error
        status = self.statuses.get(local_task_id)
        if isinstance(status, Exception):
            raise status
        if status is None:
            raise click.ClickException("Task not found")
        return status

    async def stream_result_zip(self, local_task_id: str) -> Response:
        if self.result_error is not None:
            raise self.result_error
        return Response(
            content=self.result_body,
            status_code=self.result_status,
            media_type=self.result_media_type,
            headers={"content-disposition": f'attachment; filename="{local_task_id}.zip"'},
        )


class FakeDownloader:
    def __init__(self, path: Path | None = None, file_name: str = "downloaded.pdf") -> None:
        self.path = path
        self.file_name = file_name
        self.calls: list[str] = []
        self.error: Exception | None = None

    async def download(
        self,
        url: str,
        target_dir: Path,
        target_info: SafeDownloadTarget | None = None,
    ) -> tuple[Path, str]:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if self.path is None:
            target = target_dir / self.file_name
            target.write_bytes(b"%PDF fake")
            return target, self.file_name
        return self.path, self.file_name


def app_client(
    *,
    upstream: FakeUpstream | None = None,
    downloader: FakeDownloader | None = None,
    settings: AdapterSettings | None = None,
) -> tuple[TestClient, FakeUpstream, FakeDownloader]:
    fake_upstream = upstream or FakeUpstream()
    fake_downloader = downloader or FakeDownloader()
    app = create_app(
        settings=settings or AdapterSettings(upstream_url="http://upstream.test"),
        upstream=fake_upstream,
        downloader=fake_downloader,
    )
    return TestClient(app), fake_upstream, fake_downloader


def create_batch(client: TestClient, model_version: str = "pipeline") -> dict[str, Any]:
    response = client.post(
        "/api/v4/file-urls/batch",
        headers=AUTH,
        json={"files": [{"name": "paper.pdf", "data_id": "paper.pdf"}], "model_version": model_version},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    return payload["data"]


def upload_to_batch(client: TestClient, upload_url: str, body: bytes = b"%PDF") -> None:
    response = client.put(upload_url, content=body)
    assert response.status_code == 200, response.text


def test_batch_creation_requires_json_auth_and_returns_capability_upload_url() -> None:
    client, _, _ = app_client()

    missing_auth = client.post(
        "/api/v4/file-urls/batch",
        json={"files": [{"name": "paper.pdf", "data_id": "paper.pdf"}], "model_version": "pipeline"},
    )
    assert missing_auth.status_code == 401
    assert missing_auth.json()["code"] != 0

    data = create_batch(client)
    assert data["batch_id"]
    assert data["file_urls"][0].startswith("http://testserver/api/v4/uploads/")
    assert data["file_urls"][0].endswith("/0")

    upload = client.put(data["file_urls"][0], content=b"%PDF")
    assert upload.status_code == 200


def test_configured_token_rejects_wrong_bearer_token() -> None:
    client, _, _ = app_client(settings=AdapterSettings(upstream_url="http://upstream.test", token="expected"))
    response = client.post(
        "/api/v4/file-urls/batch",
        headers={"Authorization": "Bearer wrong"},
        json={"files": [{"name": "paper.pdf", "data_id": "paper.pdf"}], "model_version": "pipeline"},
    )
    assert response.status_code == 401
    assert response.json()["code"] != 0


@pytest.mark.parametrize(
    "body",
    [
        {"model_version": "pipeline"},
        {"files": [], "model_version": "pipeline"},
        {"files": [{"data_id": "paper.pdf"}], "model_version": "pipeline"},
        {"files": [{"name": "paper.pdf"}]},
        {"files": [{"name": "a.pdf"}, {"name": "b.pdf"}], "model_version": "pipeline"},
    ],
)
def test_batch_request_validation_errors_are_cloud_shaped(body: dict[str, Any]) -> None:
    client, _, _ = app_client()
    response = client.post("/api/v4/file-urls/batch", headers=AUTH, json=body)
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] != 0


def test_batch_request_invalid_json_is_client_error() -> None:
    client, _, _ = app_client()
    response = client.post(
        "/api/v4/file-urls/batch",
        headers=AUTH | {"Content-Type": "application/json"},
        content=b"{",
    )

    assert response.status_code == 400
    assert response.json()["code"] != 0


def test_unknown_upload_capability_url_returns_404() -> None:
    client, _, _ = app_client()
    response = client.put("/api/v4/uploads/missing/0", content=b"%PDF")
    assert response.status_code == 404
    assert response.json()["code"] != 0


def test_upload_submits_local_task_with_expected_options_and_cleans_temp_file() -> None:
    client, upstream, _ = app_client(
        settings=AdapterSettings(
            upstream_url="http://upstream.test",
            lang="en",
            formula_enable=False,
            table_enable=True,
        )
    )
    data = create_batch(client, model_version="vlm")
    upload_to_batch(client, data["file_urls"][0], b"pdf-bytes")

    assert len(upstream.submissions) == 1
    submission = upstream.submissions[0]
    assert submission["file_name"] == "paper.pdf"
    assert submission["model_version"] == "vlm"
    assert submission["backend"] == "vlm-engine"
    assert submission["exists_during_submit"] is True
    assert submission["bytes"] == b"pdf-bytes"
    assert submission["form"] == {
        "backend": "vlm-engine",
        "parse_method": "auto",
        "return_md": "true",
        "return_images": "true",
        "response_format_zip": "true",
        "formula_enable": "false",
        "table_enable": "true",
        "lang_list": "en",
    }
    assert not submission["file_path"].exists()


def test_upload_rejects_invalid_model_version() -> None:
    client, _, _ = app_client()
    data = create_batch(client)
    response = client.put(data["file_urls"][0].replace("/paper", "/paper"), content=b"%PDF")
    assert response.status_code == 200

    client, _, _ = app_client()
    response = client.post(
        "/api/v4/file-urls/batch",
        headers=AUTH,
        json={"files": [{"name": "paper.pdf", "data_id": "paper.pdf"}], "model_version": "bad"},
    )
    assert response.status_code == 400
    assert response.json()["code"] != 0


def test_repeated_upload_to_same_capability_url_is_rejected() -> None:
    client, upstream, _ = app_client()
    data = create_batch(client)
    upload_to_batch(client, data["file_urls"][0], b"%PDF")

    response = client.put(data["file_urls"][0], content=b"%PDF second")

    assert response.status_code == 409
    assert response.json()["code"] != 0
    assert len(upstream.submissions) == 1


@pytest.mark.parametrize("error, expected_status", [(click.ClickException("upstream 500"), 502), (RuntimeError("down"), 503)])
def test_upload_submission_errors_are_cloud_shaped_and_cleanup_temp(error: Exception, expected_status: int) -> None:
    upstream = FakeUpstream()
    upstream.submit_error = error
    client, _, _ = app_client(upstream=upstream)
    data = create_batch(client)

    response = client.put(data["file_urls"][0], content=b"%PDF")

    assert response.status_code == expected_status
    assert response.json()["code"] != 0
    assert len(upstream.submissions) == 0


def test_upload_submission_malformed_payload_error_is_502() -> None:
    upstream = FakeUpstream()
    upstream.malformed_submit = True
    client, _, _ = app_client(upstream=upstream)
    data = create_batch(client)

    response = client.put(data["file_urls"][0], content=b"%PDF")

    assert response.status_code == 502
    assert response.json()["code"] != 0


def test_batch_poll_maps_states_and_returns_absolute_zip_url() -> None:
    client, upstream, _ = app_client()
    data = create_batch(client)
    upload_to_batch(client, data["file_urls"][0])
    local_task_id = upstream.submissions[0]["settings"].upstream_url and "local-1"

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "processing"}
    processing = client.get(f"/api/v4/extract-results/batch/{data['batch_id']}", headers=AUTH)
    assert processing.status_code == 200
    result = processing.json()["data"]["extract_result"][0]
    assert result["state"] == "running"
    assert "full_zip_url" not in result

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "completed"}
    done = client.get(f"/api/v4/extract-results/batch/{data['batch_id']}", headers=AUTH)
    result = done.json()["data"]["extract_result"][0]
    assert result["state"] == "done"
    assert result["full_zip_url"].startswith("http://testserver/api/v4/results/")

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "failed", "error": "boom"}
    failed = client.get(f"/api/v4/extract-results/batch/{data['batch_id']}", headers=AUTH)
    result = failed.json()["data"]["extract_result"][0]
    assert result["state"] == "failed"
    assert result["err_msg"] == "boom"


def test_unknown_batch_poll_returns_404() -> None:
    client, _, _ = app_client()
    response = client.get("/api/v4/extract-results/batch/missing", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["code"] != 0


@pytest.mark.parametrize("error, expected_status", [(click.ClickException("bad status"), 502), (ValueError("bad json"), 502), (RuntimeError("down"), 503)])
def test_poll_upstream_errors_are_cloud_shaped(error: Exception, expected_status: int) -> None:
    client, upstream, _ = app_client()
    data = create_batch(client)
    upload_to_batch(client, data["file_urls"][0])
    upstream.status_error = error

    response = client.get(f"/api/v4/extract-results/batch/{data['batch_id']}", headers=AUTH)

    assert response.status_code == expected_status
    assert response.json()["code"] != 0


def test_result_zip_capability_url_handles_ready_pending_failed_and_unknown() -> None:
    client, upstream, _ = app_client()
    data = create_batch(client)
    upload_to_batch(client, data["file_urls"][0])
    local_task_id = "local-1"
    result_url = f"/api/v4/results/{data['batch_id']}.zip"

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "processing"}
    pending = client.get(result_url)
    assert pending.status_code == 202

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "failed", "error": "boom"}
    failed = client.get(result_url)
    assert failed.status_code == 409

    unknown = client.get("/api/v4/results/missing.zip")
    assert unknown.status_code == 404

    upstream.statuses[local_task_id] = {"task_id": local_task_id, "status": "completed"}
    ready = client.get(result_url)
    assert ready.status_code == 200
    assert ready.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(ready.content)) as zf:
        assert any(name.endswith(".md") for name in zf.namelist())


@pytest.mark.parametrize("error, expected_status", [(RuntimeError("down"), 503), (click.ClickException("bad"), 502)])
def test_result_upstream_errors_are_mapped(error: Exception, expected_status: int) -> None:
    client, upstream, _ = app_client()
    data = create_batch(client)
    upload_to_batch(client, data["file_urls"][0])
    upstream.statuses["local-1"] = {"task_id": "local-1", "status": "completed"}
    upstream.result_error = error

    response = client.get(f"/api/v4/results/{data['batch_id']}.zip")

    assert response.status_code == expected_status


def test_url_task_flow_uses_downloader_and_polls_result() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "source.pdf"
        source.write_bytes(b"%PDF")
        downloader = FakeDownloader(source, "source.pdf")
        client, upstream, _ = app_client(
            downloader=downloader,
            settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True),
        )

        submit = client.post(
            "/api/v4/extract/task",
            headers=AUTH,
            json={"url": "https://93.184.216.34/source.pdf", "model_version": "pipeline"},
        )
        assert submit.status_code == 200
        task_id = submit.json()["data"]["task_id"]
        assert downloader.calls == ["https://93.184.216.34/source.pdf"]
        assert upstream.submissions[0]["file_name"] == "source.pdf"

        upstream.statuses["local-1"] = {"task_id": "local-1", "status": "completed"}
        status = client.get(f"/api/v4/extract/task/{task_id}", headers=AUTH)
        assert status.status_code == 200
        assert status.json()["data"]["state"] == "done"
        assert status.json()["data"]["full_zip_url"].startswith("http://testserver/api/v4/results/")


def test_settings_page_url_uses_local_sample_without_network() -> None:
    client, upstream, downloader = app_client()
    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH,
        json={"url": SETTINGS_URL, "model_version": "pipeline"},
    )
    assert response.status_code == 200
    assert downloader.calls == []
    assert upstream.submissions[0]["file_name"].endswith(".pdf")


@pytest.mark.parametrize(
    "body",
    [
        {"model_version": "pipeline"},
        {"url": 123, "model_version": "pipeline"},
        {"url": "https://example.com/a.pdf"},
        {"url": "https://example.com/a.pdf", "model_version": "bad"},
    ],
)
def test_url_task_request_validation_errors(body: dict[str, Any]) -> None:
    client, _, _ = app_client(settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True))
    response = client.post("/api/v4/extract/task", headers=AUTH, json=body)
    assert response.status_code == 400
    assert response.json()["code"] != 0


def test_url_task_invalid_json_is_client_error() -> None:
    client, _, _ = app_client(settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True))
    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH | {"Content-Type": "application/json"},
        content=b"{",
    )

    assert response.status_code == 400
    assert response.json()["code"] != 0


def test_arbitrary_url_fetch_is_disabled_by_default() -> None:
    client, _, _ = app_client()
    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH,
        json={"url": "https://93.184.216.34/source.pdf", "model_version": "pipeline"},
    )
    assert response.status_code == 400
    assert response.json()["code"] != 0


@pytest.mark.parametrize("url", ["ftp://example.com/a.pdf", "http://127.0.0.1/a.pdf", "http://10.0.0.1/a.pdf"])
def test_unsafe_url_targets_are_rejected_when_fetch_enabled(url: str) -> None:
    client, _, _ = app_client(settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True))
    response = client.post("/api/v4/extract/task", headers=AUTH, json={"url": url, "model_version": "pipeline"})
    assert response.status_code == 400
    assert response.json()["code"] != 0


def test_safe_download_target_for_ip_literal_does_not_set_sni() -> None:
    target = resolve_safe_download_target("https://93.184.216.34/source.pdf")

    assert target.request_url == "https://93.184.216.34/source.pdf"
    assert target.host_header == "93.184.216.34"
    assert target.extensions == {}


def test_redirects_are_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import socket

    captured: dict[str, Any] = {}

    class DummyResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1/secret"}

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError("redirect", request=httpx.Request("GET", "https://example.com/a.pdf"), response=httpx.Response(302))

    class DummyClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "DummyClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, *args: Any, **kwargs: Any) -> DummyResponse:
            captured["request_url"] = args[0]
            captured["request_kwargs"] = kwargs
            return DummyResponse()

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))])
    client, _, _ = app_client(
        downloader=UrlDownloader(),
        settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True),
    )

    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH,
        json={"url": "https://example.com/a.pdf", "model_version": "pipeline"},
    )

    assert response.status_code == 502
    assert captured["request_url"] == "https://93.184.216.34/a.pdf"
    assert captured["request_kwargs"]["headers"] == {"host": "example.com"}
    assert captured["request_kwargs"]["extensions"] == {"sni_hostname": "example.com"}
    assert captured["follow_redirects"] is False


def test_downloader_failure_and_upstream_failure_cleanup_url_task() -> None:
    downloader = FakeDownloader()
    downloader.error = RuntimeError("download failed")
    client, _, _ = app_client(
        downloader=downloader,
        settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True),
    )
    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH,
        json={"url": "https://93.184.216.34/source.pdf", "model_version": "pipeline"},
    )
    assert response.status_code == 503
    assert response.json()["code"] != 0

    upstream = FakeUpstream()
    upstream.submit_error = click.ClickException("upstream")
    client, _, _ = app_client(
        upstream=upstream,
        downloader=FakeDownloader(),
        settings=AdapterSettings(upstream_url="http://upstream.test", allow_url_fetch=True),
    )
    response = client.post(
        "/api/v4/extract/task",
        headers=AUTH,
        json={"url": "https://example.com/source.pdf", "model_version": "pipeline"},
    )
    assert response.status_code == 502
    assert response.json()["code"] != 0


def test_unknown_url_task_poll_returns_404() -> None:
    client, _, _ = app_client()
    response = client.get("/api/v4/extract/task/missing", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["code"] != 0


def test_settings_from_env_and_cli_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_V4_ADAPTER_UPSTREAM_URL", "http://example.test:8000")
    monkeypatch.setenv("MINERU_V4_ADAPTER_TOKEN", "secret")
    monkeypatch.setenv("MINERU_V4_ADAPTER_LANG", "en")
    monkeypatch.setenv("MINERU_V4_ADAPTER_FORMULA_ENABLE", "0")
    monkeypatch.setenv("MINERU_V4_ADAPTER_TABLE_ENABLE", "1")
    monkeypatch.setenv("MINERU_V4_ADAPTER_ALLOW_URL_FETCH", "true")
    monkeypatch.setenv("MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS", "123")

    settings = settings_from_env()

    assert settings.upstream_url == "http://example.test:8000"
    assert settings.token == "secret"
    assert settings.lang == "en"
    assert settings.formula_enable is False
    assert settings.table_enable is True
    assert settings.allow_url_fetch is True
    assert settings.task_retention_seconds == 123

    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--upstream-url" in result.output


def test_route_surface_contains_llm_wiki_endpoints() -> None:
    app = create_app(settings=AdapterSettings(upstream_url="http://upstream.test"))
    paths = {route.path for route in app.routes}
    assert {
        "/api/v4/extract/task",
        "/api/v4/file-urls/batch",
        "/api/v4/extract/task/{task_id}",
        "/api/v4/extract-results/batch/{batch_id}",
        "/api/v4/uploads/{batch_id}/{file_index}",
        "/api/v4/results/{task_id}.zip",
    }.issubset(paths)


def test_end_to_end_llm_wiki_batch_flow() -> None:
    client, upstream, _ = app_client()
    batch = create_batch(client)
    upload_to_batch(client, batch["file_urls"][0], b"%PDF")
    upstream.statuses["local-1"] = {"task_id": "local-1", "status": "completed"}

    poll = client.get(f"/api/v4/extract-results/batch/{batch['batch_id']}", headers=AUTH)
    assert poll.status_code == 200
    zip_url = poll.json()["data"]["extract_result"][0]["full_zip_url"]

    download = client.get(zip_url)
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        assert any(name.endswith(".md") for name in zf.namelist())


def test_real_upstream_client_contract_uses_mineru_parse_request_form(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    upstream_app = FastAPI()

    @upstream_app.post("/tasks", status_code=202)
    async def tasks(options: ParseRequestOptions = Depends(parse_request_form)) -> dict[str, str]:
        seen["backend"] = options.backend
        seen["parse_method"] = options.parse_method
        seen["return_md"] = options.return_md
        seen["return_images"] = options.return_images
        seen["response_format_zip"] = options.response_format_zip
        seen["lang_list"] = options.lang_list
        return {
            "task_id": "real-local-1",
            "status_url": "http://upstream.test/tasks/real-local-1",
            "result_url": "http://upstream.test/tasks/real-local-1/result",
        }

    with TestClient(upstream_app) as upstream_client:
        class ContractClient(MineruUpstreamClient):
            async def submit_task(self, *, file_path: Path, file_name: str, model_version: str, settings: AdapterSettings) -> str:
                form_data = self.build_form_data(model_version, settings)
                with file_path.open("rb") as handle:
                    response = upstream_client.post(
                        "/tasks",
                        data=form_data,
                        files={"files": (file_name, handle, "application/pdf")},
                    )
                response.raise_for_status()
                return response.json()["task_id"]

        client = ContractClient("http://upstream.test")
        file_path = tmp_path / "paper.pdf"
        file_path.write_bytes(b"%PDF")
        task_id = __import__("anyio").run(
            lambda: client.submit_task(
                file_path=file_path,
                file_name="paper.pdf",
                model_version="pipeline",
                settings=AdapterSettings(upstream_url="http://upstream.test", lang="korean"),
            )
        )

    assert task_id == "real-local-1"
    assert seen == {
        "backend": "pipeline",
        "parse_method": "auto",
        "return_md": True,
        "return_images": True,
        "response_format_zip": True,
        "lang_list": ["korean"],
    }
