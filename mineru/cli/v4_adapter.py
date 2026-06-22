# Copyright (c) Opendatalab. All rights reserved.
import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import click
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mineru.cli import api_client

API_PREFIX = "/api/v4"
DEFAULT_UPSTREAM_URL = "http://127.0.0.1:8000"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8888
DEFAULT_TASK_RETENTION_SECONDS = 86400
SETTINGS_TEST_URL = "https://cdn-mineru.openxlab.org.cn/demo/example.pdf"
TASK_WAITING_FILE = "waiting-file"
TASK_UPLOADING = "uploading"
TASK_SUBMITTED = "submitted"


@dataclass(frozen=True)
class AdapterSettings:
    upstream_url: str = DEFAULT_UPSTREAM_URL
    token: str = ""
    lang: str = "ch"
    formula_enable: bool = True
    table_enable: bool = True
    tmp_dir: str | None = None
    allow_url_fetch: bool = False
    task_retention_seconds: int = DEFAULT_TASK_RETENTION_SECONDS


@dataclass
class TaskRecord:
    task_id: str
    file_name: str
    model_version: str
    created_at: float
    local_task_id: str | None = None
    state: str = TASK_WAITING_FILE
    error: str | None = None
    completed_at: float | None = None


@dataclass
class BatchRecord:
    batch_id: str
    file_name: str
    model_version: str
    task_id: str
    created_at: float


@dataclass(frozen=True)
class SafeDownloadTarget:
    request_url: str
    host_header: str
    extensions: dict[str, str]


class AdapterHTTPError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AdapterRegistry:
    def __init__(self, retention_seconds: int = DEFAULT_TASK_RETENTION_SECONDS):
        self.retention_seconds = retention_seconds
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._batches: dict[str, BatchRecord] = {}

    async def create_batch(self, file_name: str, model_version: str) -> BatchRecord:
        now = time.time()
        batch_id = str(uuid.uuid4())
        task = TaskRecord(
            task_id=batch_id,
            file_name=file_name,
            model_version=model_version,
            created_at=now,
        )
        batch = BatchRecord(
            batch_id=batch_id,
            file_name=file_name,
            model_version=model_version,
            task_id=task.task_id,
            created_at=now,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
            self._batches[batch.batch_id] = batch
        return batch

    async def create_url_task(self, file_name: str, model_version: str) -> TaskRecord:
        task = TaskRecord(
            task_id=str(uuid.uuid4()),
            file_name=file_name,
            model_version=model_version,
            created_at=time.time(),
        )
        async with self._lock:
            self._tasks[task.task_id] = task
        return task

    async def get_batch(self, batch_id: str) -> BatchRecord | None:
        async with self._lock:
            return self._batches.get(batch_id)

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def mark_submitted(self, task_id: str, local_task_id: str) -> TaskRecord:
        async with self._lock:
            task = self._tasks[task_id]
            task.local_task_id = local_task_id
            task.state = TASK_SUBMITTED
            task.error = None
            return task

    async def claim_upload(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != TASK_WAITING_FILE or task.local_task_id is not None:
                return False
            task.state = TASK_UPLOADING
            return True

    async def mark_failed(self, task_id: str, error: str) -> TaskRecord | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.state = "failed"
            task.error = error
            task.completed_at = time.time()
            return task

    async def cleanup_expired(self) -> None:
        if self.retention_seconds <= 0:
            return
        now = time.time()
        async with self._lock:
            expired_tasks = {
                task_id
                for task_id, task in self._tasks.items()
                if task.completed_at is not None
                and now - task.completed_at >= self.retention_seconds
            }
            for task_id in expired_tasks:
                self._tasks.pop(task_id, None)
            expired_batches = [
                batch_id
                for batch_id, batch in self._batches.items()
                if batch.task_id in expired_tasks
            ]
            for batch_id in expired_batches:
                self._batches.pop(batch_id, None)

    async def cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self.cleanup_expired()
        except asyncio.CancelledError:
            raise


class MineruUpstreamClient:
    def __init__(self, upstream_url: str):
        self.upstream_url = normalize_base_url(upstream_url)

    def build_form_data(
        self,
        model_version: str,
        settings: AdapterSettings,
    ) -> dict[str, str | list[str]]:
        backend = model_version_to_backend(model_version)
        return api_client.build_parse_request_form_data(
            lang_list=[settings.lang],
            backend=backend,
            effort="medium",
            parse_method="auto",
            formula_enable=settings.formula_enable,
            table_enable=settings.table_enable,
            image_analysis=True,
            server_url=None,
            start_page_id=0,
            end_page_id=None,
            return_md=True,
            return_middle_json=False,
            return_model_output=False,
            return_content_list=False,
            return_images=True,
            response_format_zip=True,
            return_original_file=False,
            client_side_output_generation=False,
        )

    async def submit_task(
        self,
        *,
        file_path: Path,
        file_name: str,
        model_version: str,
        settings: AdapterSettings,
    ) -> str:
        form_data = self.build_form_data(model_version, settings)
        response = await api_client.submit_parse_task(
            base_url=self.upstream_url,
            upload_assets=[api_client.UploadAsset(path=file_path, upload_name=file_name)],
            form_data=form_data,
        )
        return response.task_id

    async def get_status(self, local_task_id: str) -> dict[str, Any]:
        url = f"{self.upstream_url}/tasks/{local_task_id}"
        try:
            async with httpx.AsyncClient(timeout=api_client.build_http_timeout(), follow_redirects=True) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to query MinerU task status: {exc}") from exc
        if response.status_code != 200:
            raise click.ClickException(
                f"Failed to query MinerU task status: {response.status_code} {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("MinerU API returned malformed task status JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise ValueError("MinerU API returned an invalid task status payload")
        return payload

    async def stream_result_zip(self, local_task_id: str) -> Response:
        url = f"{self.upstream_url}/tasks/{local_task_id}/result"
        client = httpx.AsyncClient(timeout=api_client.build_http_timeout(), follow_redirects=True)
        stream_cm = client.stream("GET", url)
        try:
            upstream_response = await stream_cm.__aenter__()
        except httpx.HTTPError as exc:
            await client.aclose()
            raise RuntimeError(f"Failed to download MinerU result zip: {exc}") from exc

        if upstream_response.status_code != 200:
            await stream_cm.__aexit__(None, None, None)
            await client.aclose()
            raise click.ClickException(
                f"Failed to download MinerU result zip: {upstream_response.status_code}"
            )

        headers = {}
        for header_name in ("content-disposition", "content-type"):
            value = upstream_response.headers.get(header_name)
            if value:
                headers[header_name] = value

        async def iter_bytes():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await stream_cm.__aexit__(None, None, None)
                await client.aclose()

        return StreamingResponse(
            iter_bytes(),
            status_code=200,
            media_type=upstream_response.headers.get("content-type", "application/zip"),
            headers=headers,
        )


class UrlDownloader:
    async def download(
        self,
        url: str,
        target_dir: Path,
        target_info: SafeDownloadTarget | None = None,
    ) -> tuple[Path, str]:
        target_info = target_info or resolve_safe_download_target(url)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(
                timeout=api_client.build_http_timeout(),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    target_info.request_url,
                    headers={"host": target_info.host_header},
                    extensions=target_info.extensions,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to download URL: {exc}") from exc

        if 300 <= response.status_code < 400:
            raise click.ClickException("URL redirects are not allowed")
        if response.status_code != 200:
            raise click.ClickException(f"Failed to download URL: HTTP {response.status_code}")

        file_name = infer_download_filename(url, response.headers)
        target = target_dir / file_name
        target.write_bytes(response.content)
        return target, file_name


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        resolved = int(value)
    except ValueError:
        return default
    return max(0, resolved)


def settings_from_env() -> AdapterSettings:
    return AdapterSettings(
        upstream_url=os.getenv("MINERU_V4_ADAPTER_UPSTREAM_URL", DEFAULT_UPSTREAM_URL),
        token=os.getenv("MINERU_V4_ADAPTER_TOKEN", ""),
        lang=os.getenv("MINERU_V4_ADAPTER_LANG", "ch"),
        formula_enable=parse_bool_env("MINERU_V4_ADAPTER_FORMULA_ENABLE", True),
        table_enable=parse_bool_env("MINERU_V4_ADAPTER_TABLE_ENABLE", True),
        tmp_dir=os.getenv("MINERU_V4_ADAPTER_TMP_DIR") or None,
        allow_url_fetch=parse_bool_env("MINERU_V4_ADAPTER_ALLOW_URL_FETCH", False),
        task_retention_seconds=parse_int_env(
            "MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS",
            DEFAULT_TASK_RETENTION_SECONDS,
        ),
    )


def model_version_to_backend(model_version: str) -> str:
    if model_version == "pipeline":
        return "pipeline"
    if model_version == "vlm":
        return "vlm-engine"
    raise AdapterHTTPError(400, "Unsupported model_version. Expected pipeline or vlm.")


def cloud_success(data: dict[str, Any], msg: str = "success") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data}


def cloud_error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": -1, "msg": message, "data": {}})


def exception_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, AdapterHTTPError):
        return cloud_error_response(exc.status_code, exc.message)
    if isinstance(exc, click.ClickException):
        return cloud_error_response(502, exc.message)
    if isinstance(exc, ValueError):
        return cloud_error_response(502, str(exc))
    return cloud_error_response(503, str(exc))


def require_bearer_token(request: Request, settings: AdapterSettings) -> None:
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise AdapterHTTPError(401, "Missing MinerU bearer token")
    token = header[len(prefix) :].strip()
    if not token:
        raise AdapterHTTPError(401, "Missing MinerU bearer token")
    if settings.token and token != settings.token:
        raise AdapterHTTPError(401, "Invalid MinerU bearer token")


def parse_json_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AdapterHTTPError(400, "Request body must be a JSON object")
    return payload


async def read_json_object(request: Request) -> dict[str, Any]:
    try:
        return parse_json_object(await request.json())
    except StarletteHTTPException as exc:
        raise AdapterHTTPError(400, "Request body must be valid JSON") from exc
    except ValueError as exc:
        raise AdapterHTTPError(400, "Request body must be valid JSON") from exc


def sanitize_file_name(name: str) -> str:
    basename = Path(str(name).replace("\\", "/")).name
    cleaned = re.sub(r"[\x00-\x1f<>:\"|?*]+", "_", basename).strip(" .")
    return cleaned or "document.pdf"


def parse_batch_request(payload: dict[str, Any]) -> tuple[str, str]:
    files = payload.get("files")
    model_version = payload.get("model_version")
    if not isinstance(model_version, str):
        raise AdapterHTTPError(400, "model_version is required")
    model_version_to_backend(model_version)
    if not isinstance(files, list) or not files:
        raise AdapterHTTPError(400, "files must contain exactly one file")
    if len(files) != 1:
        raise AdapterHTTPError(400, "Only one file per batch is supported")
    first = files[0]
    if not isinstance(first, dict) or not isinstance(first.get("name"), str):
        raise AdapterHTTPError(400, "files[0].name is required")
    return sanitize_file_name(first["name"]), model_version


def parse_url_task_request(payload: dict[str, Any]) -> tuple[str, str]:
    url = payload.get("url")
    model_version = payload.get("model_version")
    if not isinstance(url, str) or not url.strip():
        raise AdapterHTTPError(400, "url is required")
    if not isinstance(model_version, str):
        raise AdapterHTTPError(400, "model_version is required")
    model_version_to_backend(model_version)
    return url.strip(), model_version


def result_url_for(request: Request, task_id: str) -> str:
    return str(request.url_for("get_adapter_result_zip", task_id=task_id))


def cloud_state(local_status: str) -> str:
    return {
        TASK_WAITING_FILE: "waiting-file",
        TASK_UPLOADING: "running",
        TASK_SUBMITTED: "pending",
        "pending": "pending",
        "processing": "running",
        "completed": "done",
        "failed": "failed",
    }.get(local_status, "running")


def is_terminal_cloud_state(state: str) -> bool:
    return state in {"done", "failed"}


def make_temp_dir(settings: AdapterSettings) -> Path:
    return Path(tempfile.mkdtemp(prefix="mineru-v4-adapter-", dir=settings.tmp_dir))


def cleanup_path(path: Path | None) -> None:
    if path is None:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def forbidden_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def default_port_for_scheme(scheme: str) -> int:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    raise AdapterHTTPError(400, "Only http and https URLs are supported")


def format_ip_url_host(address: str) -> str:
    if ":" in address:
        return f"[{address}]"
    return address


def format_host_header(hostname: str, port: int | None, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != default_port_for_scheme(scheme):
        return f"{host}:{port}"
    return host


def resolve_safe_download_target(url: str) -> SafeDownloadTarget:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise AdapterHTTPError(400, "Only http and https URLs are supported")
    if not parsed.hostname:
        raise AdapterHTTPError(400, "URL host is required")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise AdapterHTTPError(400, "URL port is invalid") from exc
    effective_port = explicit_port or default_port_for_scheme(parsed.scheme)
    hostname_is_ip = False
    try:
        ipaddress.ip_address(parsed.hostname)
        addresses = [parsed.hostname]
        hostname_is_ip = True
    except ValueError:
        try:
            infos = socket.getaddrinfo(parsed.hostname, effective_port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AdapterHTTPError(400, f"Could not resolve URL host: {parsed.hostname}") from exc
        addresses = [info[4][0] for info in infos]
    if any(forbidden_ip(address) for address in addresses):
        raise AdapterHTTPError(400, "URL host resolves to a forbidden network address")
    if not addresses:
        raise AdapterHTTPError(400, f"Could not resolve URL host: {parsed.hostname}")

    pinned_address = addresses[0]
    pinned_netloc = format_ip_url_host(pinned_address)
    if explicit_port is not None:
        pinned_netloc = f"{pinned_netloc}:{explicit_port}"
    request_url = parsed._replace(netloc=pinned_netloc).geturl()
    extensions = (
        {"sni_hostname": parsed.hostname}
        if parsed.scheme == "https" and not hostname_is_ip
        else {}
    )
    return SafeDownloadTarget(
        request_url=request_url,
        host_header=format_host_header(parsed.hostname, explicit_port, parsed.scheme),
        extensions=extensions,
    )


def validate_safe_url(url: str) -> None:
    resolve_safe_download_target(url)


def infer_download_filename(url: str, headers: httpx.Headers | dict[str, str]) -> str:
    disposition = headers.get("content-disposition") if headers else None
    if disposition:
        match = re.search(r'filename="?([^";]+)"?', disposition)
        if match:
            return sanitize_file_name(unquote(match.group(1)))
    parsed_name = Path(unquote(urlparse(url).path)).name
    if parsed_name:
        return sanitize_file_name(parsed_name)
    return "document.pdf"


def bundled_settings_sample() -> Path:
    sample = resources.files("mineru").joinpath("resources/v4_adapter/demo1.pdf")
    if not sample.is_file():
        raise AdapterHTTPError(503, "Bundled MinerU settings sample is unavailable")
    return Path(str(sample))


async def submit_source_file(
    *,
    upstream: MineruUpstreamClient,
    registry: AdapterRegistry,
    task: TaskRecord,
    file_path: Path,
    file_name: str,
    settings: AdapterSettings,
    cleanup_root: Path | None,
) -> str:
    try:
        local_task_id = await upstream.submit_task(
            file_path=file_path,
            file_name=file_name,
            model_version=task.model_version,
            settings=settings,
        )
        await registry.mark_submitted(task.task_id, local_task_id)
        return local_task_id
    except Exception as exc:
        await registry.mark_failed(task.task_id, str(exc))
        raise
    finally:
        cleanup_path(cleanup_root)


async def task_status_payload(
    *,
    upstream: MineruUpstreamClient,
    registry: AdapterRegistry,
    task: TaskRecord,
    request: Request,
) -> tuple[str, str | None, str | None]:
    if task.local_task_id is None:
        return cloud_state(task.state), None, task.error
    try:
        status_payload = await upstream.get_status(task.local_task_id)
    except Exception as exc:
        raise exc
    state = cloud_state(status_payload["status"])
    err_msg = status_payload.get("error") if isinstance(status_payload.get("error"), str) else task.error
    if is_terminal_cloud_state(state):
        async with registry._lock:
            task.completed_at = task.completed_at or time.time()
            task.state = state
            task.error = err_msg
    full_zip_url = result_url_for(request, task.task_id) if state == "done" else None
    return state, full_zip_url, err_msg


def create_app(
    settings: AdapterSettings | None = None,
    upstream: MineruUpstreamClient | None = None,
    downloader: UrlDownloader | None = None,
) -> FastAPI:
    resolved_settings = settings or settings_from_env()
    registry = AdapterRegistry(retention_seconds=resolved_settings.task_retention_seconds)
    resolved_upstream = upstream or MineruUpstreamClient(resolved_settings.upstream_url)
    resolved_downloader = downloader or UrlDownloader()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_task = asyncio.create_task(registry.cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.registry = registry
    app.state.upstream = resolved_upstream
    app.state.downloader = resolved_downloader

    @app.post(f"{API_PREFIX}/file-urls/batch")
    async def create_file_urls_batch(request: Request):
        try:
            require_bearer_token(request, resolved_settings)
            payload = await read_json_object(request)
            file_name, model_version = parse_batch_request(payload)
            batch = await registry.create_batch(file_name, model_version)
            upload_url = str(
                request.url_for(
                    "upload_adapter_file",
                    batch_id=batch.batch_id,
                    file_index=0,
                )
            )
            return cloud_success({"batch_id": batch.batch_id, "file_urls": [upload_url]})
        except Exception as exc:
            return exception_response(exc)

    @app.put(f"{API_PREFIX}/uploads/{{batch_id}}/{{file_index}}", name="upload_adapter_file")
    async def upload_adapter_file(batch_id: str, file_index: int, request: Request):
        if file_index != 0:
            return cloud_error_response(404, "Upload URL not found")
        temp_dir: Path | None = None
        task: TaskRecord | None = None
        try:
            batch = await registry.get_batch(batch_id)
            if batch is None:
                raise AdapterHTTPError(404, "Batch not found")
            task = await registry.get_task(batch.task_id)
            if task is None:
                raise AdapterHTTPError(404, "Task not found")
            if not await registry.claim_upload(task.task_id):
                raise AdapterHTTPError(409, "Upload URL has already been consumed")
            temp_dir = make_temp_dir(resolved_settings)
            file_path = temp_dir / batch.file_name
            file_path.write_bytes(await request.body())
            await submit_source_file(
                upstream=resolved_upstream,
                registry=registry,
                task=task,
                file_path=file_path,
                file_name=batch.file_name,
                settings=resolved_settings,
                cleanup_root=temp_dir,
            )
            temp_dir = None
            return cloud_success({"batch_id": batch.batch_id})
        except Exception as exc:
            if task is not None and task.state == TASK_UPLOADING:
                await registry.mark_failed(task.task_id, str(exc))
            cleanup_path(temp_dir)
            return exception_response(exc)

    @app.get(f"{API_PREFIX}/extract-results/batch/{{batch_id}}")
    async def get_batch_result(batch_id: str, request: Request):
        try:
            require_bearer_token(request, resolved_settings)
            batch = await registry.get_batch(batch_id)
            if batch is None:
                raise AdapterHTTPError(404, "Batch not found")
            task = await registry.get_task(batch.task_id)
            if task is None:
                raise AdapterHTTPError(404, "Task not found")
            state, full_zip_url, err_msg = await task_status_payload(
                upstream=resolved_upstream,
                registry=registry,
                task=task,
                request=request,
            )
            result: dict[str, Any] = {"file_name": batch.file_name, "state": state}
            if full_zip_url:
                result["full_zip_url"] = full_zip_url
            if err_msg:
                result["err_msg"] = err_msg
            return cloud_success({"batch_id": batch.batch_id, "extract_result": [result]})
        except Exception as exc:
            return exception_response(exc)

    @app.get(f"{API_PREFIX}/results/{{task_id}}.zip", name="get_adapter_result_zip")
    async def get_adapter_result_zip(task_id: str, request: Request):
        try:
            task = await registry.get_task(task_id)
            if task is None:
                raise AdapterHTTPError(404, "Task not found")
            state, _, err_msg = await task_status_payload(
                upstream=resolved_upstream,
                registry=registry,
                task=task,
                request=request,
            )
            if state in {"waiting-file", "pending", "running", "converting"}:
                return cloud_error_response(202, "Task result is not ready yet")
            if state == "failed":
                return cloud_error_response(409, err_msg or "Task execution failed")
            if task.local_task_id is None:
                raise AdapterHTTPError(404, "Task not found")
            return await resolved_upstream.stream_result_zip(task.local_task_id)
        except Exception as exc:
            return exception_response(exc)

    @app.post(f"{API_PREFIX}/extract/task")
    async def create_url_task(request: Request):
        temp_dir: Path | None = None
        try:
            require_bearer_token(request, resolved_settings)
            payload = await read_json_object(request)
            url, model_version = parse_url_task_request(payload)

            if url == SETTINGS_TEST_URL and not resolved_settings.allow_url_fetch:
                file_path = bundled_settings_sample()
                file_name = file_path.name
                cleanup_root = None
            else:
                if not resolved_settings.allow_url_fetch:
                    raise AdapterHTTPError(400, "URL fetching is disabled")
                target_info = resolve_safe_download_target(url)
                temp_dir = make_temp_dir(resolved_settings)
                file_path, file_name = await resolved_downloader.download(url, temp_dir, target_info)
                cleanup_root = temp_dir

            task = await registry.create_url_task(file_name, model_version)
            await submit_source_file(
                upstream=resolved_upstream,
                registry=registry,
                task=task,
                file_path=file_path,
                file_name=file_name,
                settings=resolved_settings,
                cleanup_root=cleanup_root,
            )
            temp_dir = None
            return cloud_success({"task_id": task.task_id})
        except Exception as exc:
            cleanup_path(temp_dir)
            return exception_response(exc)

    @app.get(f"{API_PREFIX}/extract/task/{{task_id}}")
    async def get_url_task(task_id: str, request: Request):
        try:
            require_bearer_token(request, resolved_settings)
            task = await registry.get_task(task_id)
            if task is None:
                raise AdapterHTTPError(404, "Task not found")
            state, full_zip_url, err_msg = await task_status_payload(
                upstream=resolved_upstream,
                registry=registry,
                task=task,
                request=request,
            )
            data: dict[str, Any] = {"task_id": task.task_id, "state": state}
            if full_zip_url:
                data["full_zip_url"] = full_zip_url
            if err_msg:
                data["err_msg"] = err_msg
            return cloud_success(data)
        except Exception as exc:
            return exception_response(exc)

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "upstream_url": resolved_settings.upstream_url}

    return app


@click.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True)
@click.option("--port", default=DEFAULT_PORT, type=int, show_default=True)
@click.option("--upstream-url", "--mineru-api-url", default=None)
@click.option("--token", default=None)
@click.option("--lang", default=None)
@click.option("--formula-enable/--no-formula-enable", default=None)
@click.option("--table-enable/--no-table-enable", default=None)
@click.option("--allow-url-fetch/--no-allow-url-fetch", default=None)
@click.option("--task-retention-seconds", default=None, type=int)
def main(
    host: str,
    port: int,
    upstream_url: str | None,
    token: str | None,
    lang: str | None,
    formula_enable: bool | None,
    table_enable: bool | None,
    allow_url_fetch: bool | None,
    task_retention_seconds: int | None,
) -> None:
    env_settings = settings_from_env()
    settings = AdapterSettings(
        upstream_url=upstream_url or env_settings.upstream_url,
        token=env_settings.token if token is None else token,
        lang=lang or env_settings.lang,
        formula_enable=env_settings.formula_enable if formula_enable is None else formula_enable,
        table_enable=env_settings.table_enable if table_enable is None else table_enable,
        tmp_dir=env_settings.tmp_dir,
        allow_url_fetch=env_settings.allow_url_fetch if allow_url_fetch is None else allow_url_fetch,
        task_retention_seconds=(
            env_settings.task_retention_seconds
            if task_retention_seconds is None
            else max(0, task_retention_seconds)
        ),
    )
    uvicorn.run(create_app(settings=settings), host=host, port=port)


if __name__ == "__main__":
    main()
