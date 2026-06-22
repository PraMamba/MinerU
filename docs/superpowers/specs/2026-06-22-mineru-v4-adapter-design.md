# MinerU v4 Compatibility Adapter Design

## Goal

Build a minimal self-hosted compatibility adapter that lets LLM Wiki keep its MinerU cloud API client shape while using a local `mineru-api` or `mineru-router` backend.

The adapter is intentionally narrow: it only implements the endpoint and response subset currently called by `/root/llm_wiki/src/lib/mineru.ts`. It is not a full clone of the public MinerU cloud API, and unsupported cloud parameters are ignored rather than added speculatively.

## Existing Interfaces

LLM Wiki currently hardcodes `API_BASE = "https://mineru.net/api/v4"` and calls these paths:

- `POST /extract/task`
  - JSON body: `{"url": "...", "model_version": "pipeline" | "vlm"}`
  - Expected success shape: `{"code": 0, "msg": "...", "data": {"task_id": "..."}}`
- `POST /file-urls/batch`
  - JSON body: `{"files": [{"name": "...", "data_id": "..."}], "model_version": "pipeline" | "vlm"}`
  - Expected success shape: `{"code": 0, "msg": "...", "data": {"batch_id": "...", "file_urls": ["..."]}}`
- `PUT <file_urls[0]>`
  - Raw file bytes, no JSON body.
- `GET /extract/task/{task_id}`
  - Expected state shape: `{"code": 0, "data": {"task_id": "...", "state": "pending" | "running" | "converting" | "done" | "failed", "full_zip_url": "...", "err_msg": "..."}}`
- `GET /extract-results/batch/{batch_id}`
  - Expected state shape: `{"code": 0, "data": {"batch_id": "...", "extract_result": [{"file_name": "...", "state": "...", "full_zip_url": "...", "err_msg": "..."}]}}`
- `GET <full_zip_url>`
  - Must return a zip archive containing at least one Markdown file. LLM Wiki prefers `full.md` if present but accepts any `.md` entry.

LLM Wiki also rejects empty `config.token` before any request. The adapter must either accept a placeholder bearer token or optionally enforce a configured token on JSON API endpoints.

LLM Wiki does not send `Authorization` on `PUT <file_urls[0]>` or `GET <full_zip_url>`. Those returned URLs are capability URLs and must work without request headers. The adapter keeps them unguessable by embedding adapter-generated UUIDs in the path.

Local MinerU exposes a different API:

- `POST /tasks`
  - multipart fields parsed by `mineru.cli.api_request.parse_request_form`
  - file field: `files`
- useful fields for this adapter: `backend`, `parse_method`, `return_md`, `return_images`, `response_format_zip`, `formula_enable`, `table_enable`, `lang_list`
  - returns local task status with `task_id`, `status`, `status_url`, `result_url`
- `GET /tasks/{task_id}`
  - returns local task status with `status` values such as `pending`, `processing`, `completed`, `failed`
- `GET /tasks/{task_id}/result`
  - returns zip when the original task used `response_format_zip=true`

## Architecture

Add a new CLI service, `mineru-v4-adapter`, implemented as a small FastAPI application under `mineru/cli/v4_adapter.py`.

The adapter sits in front of an already running local MinerU API:

```text
LLM Wiki
  -> http://127.0.0.1:8888/api/v4/...
      mineru-v4-adapter
        -> http://127.0.0.1:8000/tasks
        -> http://127.0.0.1:8000/tasks/{task_id}
        -> http://127.0.0.1:8000/tasks/{task_id}/result
```

This keeps MinerU's native `mineru-api` unchanged and avoids changing LLM Wiki beyond `API_BASE`.

The upstream may be either `mineru-api` or `mineru-router`, because both expose the same `/tasks`, `/tasks/{task_id}`, and `/tasks/{task_id}/result` interface.

## Endpoint Behavior

### `POST /api/v4/file-urls/batch`

The adapter validates a single-file batch request and creates an adapter batch record:

- `batch_id`: random UUID.
- `file_name`: sanitized basename from `files[0].name`.
- `model_version`: copied from request and normalized to a local backend.
- `state`: `waiting-file`.
- `upload_url`: absolute capability URL pointing back to `PUT /api/v4/uploads/{batch_id}/0`.

Only one file is supported because LLM Wiki uploads one file per ingestion. Multi-file requests return a cloud-shaped error response. Cloud request fields outside LLM Wiki's current use, such as `page_ranges`, `extra_formats`, and cache controls, are intentionally unsupported.

### `PUT /api/v4/uploads/{batch_id}/0`

The adapter stores the uploaded bytes in a temporary directory and immediately submits a local MinerU task to `/tasks`. This endpoint is an unauthenticated capability URL because LLM Wiki performs the raw `PUT` without bearer headers.

The upload capability is single-use. After the adapter accepts the first upload for a batch, repeated `PUT` requests to the same URL return `409` and do not submit another upstream MinerU task.

The local multipart request uses:

- `files`: uploaded file.
- `backend`: `pipeline` when `model_version=pipeline`; `vlm-engine` when `model_version=vlm`.
- `parse_method`: `auto`.
- `return_md`: `true`.
- `return_images`: `true`.
- `response_format_zip`: `true`.
- `formula_enable`: configurable default `true`.
- `table_enable`: configurable default `true`.
- `lang_list`: configurable default `ch`.

The batch record stores the returned local `task_id`. The `PUT` response may be a small JSON success response; LLM Wiki only checks HTTP status.

Temporary input files are deleted in `finally` blocks after local task submission succeeds or fails. The adapter must not leave uploaded/downloaded source files behind on upstream errors, downloader errors, request cancellation, or invalid local task payloads.

### `POST /api/v4/extract/task`

This endpoint supports URL tasks for LLM Wiki's existing URL path and settings-page test.

The adapter downloads the URL to a temporary file, derives a safe filename, and submits the same local `/tasks` request used by uploaded files. It returns a cloud-shaped `task_id`. The adapter task id is separate from the local MinerU task id.

URL fetching is SSRF-sensitive. The adapter defaults to `127.0.0.1` binding and disables arbitrary URL fetches unless `allow_url_fetch=true` is set by CLI or environment. The downloader only accepts `http` and `https` schemes and rejects loopback, private, multicast, unspecified, and link-local IP targets after DNS resolution. The same resolution result is used for the actual request by connecting to a checked IP address while preserving the original `Host` header and HTTPS SNI for DNS hostnames. Redirects are disabled (`follow_redirects=False`) so a public URL cannot redirect the adapter to a forbidden internal target.

LLM Wiki's settings-page test uses `https://cdn-mineru.openxlab.org.cn/demo/example.pdf`. For offline self-hosted checks, the adapter may special-case this exact URL and use the packaged local sample `mineru/resources/v4_adapter/demo1.pdf` when `allow_url_fetch=false`. This lets the settings test validate adapter/upstream plumbing without requiring internet access.

If the URL cannot be downloaded or the local task cannot be submitted, the adapter returns a non-zero cloud-shaped error.

### `GET /api/v4/extract-results/batch/{batch_id}`

The adapter maps the batch record to a cloud-shaped batch result.

State mapping:

- `waiting-file` -> `waiting-file`
- local `pending` -> `pending`
- local `processing` -> `running`
- local `completed` -> `done`
- local `failed` -> `failed`
- missing local task -> `failed`

When done, `full_zip_url` is an absolute capability URL pointing to `GET /api/v4/results/{adapter_task_id}.zip`.

### `GET /api/v4/extract/task/{task_id}`

The adapter maps a URL task to the same cloud-shaped single-task status structure.

When done, `full_zip_url` is an absolute capability URL pointing to `GET /api/v4/results/{adapter_task_id}.zip`.

### `GET /api/v4/results/{task_id}.zip`

The adapter proxies the local `/tasks/{local_task_id}/result` response. This endpoint is an unauthenticated capability URL because LLM Wiki downloads `full_zip_url` without bearer headers. It returns:

- `202` if the local task is not done.
- `409` if the local task failed.
- `200 application/zip` when ready.

The response body is streamed from the local MinerU service. The adapter must not load the complete zip into memory before responding. It preserves the upstream `content-type` and `content-disposition` headers when present.

## Configuration

CLI:

```bash
mineru-v4-adapter --host 127.0.0.1 --port 8888 --upstream-url http://127.0.0.1:8000
```

Environment fallbacks:

- `MINERU_V4_ADAPTER_UPSTREAM_URL`: default upstream local MinerU API or Router URL.
- `MINERU_V4_ADAPTER_TOKEN`: optional bearer token. Empty means accept any non-empty token.
- `MINERU_V4_ADAPTER_TMP_DIR`: optional temp root.
- `MINERU_V4_ADAPTER_FORMULA_ENABLE`: default `true`.
- `MINERU_V4_ADAPTER_TABLE_ENABLE`: default `true`.
- `MINERU_V4_ADAPTER_LANG`: default `ch`; this is a single-language simplification mapped to local `lang_list=[value]`.
- `MINERU_V4_ADAPTER_ALLOW_URL_FETCH`: default `false`.
- `MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS`: default `86400`.

The legacy option name `--mineru-api-url` may be accepted as an alias for `--upstream-url`.

The adapter itself does not control model downloads. The underlying `mineru-api` process must be started with the desired local model environment, such as `MINERU_MODEL_SOURCE=local`, `HF_HUB_OFFLINE=1`, and `TRANSFORMERS_OFFLINE=1`.

## Data Retention

The adapter stores only small task metadata plus uploaded/downloaded temporary input files during local submission. It cleans temp files on both success and failure. Result zips are not duplicated; result download is proxied from local MinerU.

The minimal adapter uses an `asyncio.Lock` protected process-local registry with task and batch records. Records include creation time, terminal completion time, state, file name, local task id, and error text. A cleanup loop removes terminal records after `MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS`.

Restarting the adapter loses task status mapping, even though the upstream local MinerU service may still retain results. This is an explicit limitation of the minimal adapter. Operators should keep the adapter process stable during LLM Wiki ingestion runs if they need in-flight task continuity.

## Error Model

The adapter returns cloud-shaped JSON errors where LLM Wiki expects JSON:

```json
{"code": -1, "msg": "specific message", "data": {}}
```

HTTP status codes still reflect transport-level errors:

- `400`: invalid request.
- `401`: missing or invalid bearer token on JSON API endpoints.
- `404`: unknown adapter task or batch.
- `502`: upstream MinerU API request failed.
- `503`: upstream MinerU API is unavailable.

Capability endpoints (`PUT /api/v4/uploads/...` and `GET /api/v4/results/...`) do not require bearer auth. Unknown or expired UUIDs still return `404`.

## Testing Strategy

Tests must not trigger model loading. Use FastAPI `TestClient` for adapter-level tests. For upstream contract tests, use a stub FastAPI app that includes MinerU's real `parse_request_form` dependency but does not parse documents.

Required tests:

- `POST /api/v4/file-urls/batch` returns a `batch_id` and absolute upload URL.
- `PUT /api/v4/uploads/{batch_id}/0` succeeds without Authorization and submits a local task with expected multipart fields.
- Batch polling maps local `completed` to `done` and exposes an absolute `full_zip_url`.
- One end-to-end in-memory test follows the actual LLM Wiki flow: bearer JSON batch request, no-auth raw upload `PUT`, bearer poll, no-auth zip `GET`.
- `GET /api/v4/results/{task_id}.zip` streams a ready zip response and returns `202`, `409`, or `404` for pending, failed, or unknown tasks.
- `POST /api/v4/extract/task` submits the hardcoded LLM Wiki settings-test URL without internet by using the bundled sample when URL fetch is disabled.
- Arbitrary URL task submission is rejected unless URL fetch is enabled; unsafe URL targets are rejected even when enabled.
- Redirect responses from allowed-looking URLs are not followed.
- Invalid token behavior covers both permissive and enforced modes.
- Unsupported multi-file batch returns a non-zero cloud-shaped error.
- Malformed client JSON payloads for both JSON entrypoints are rejected with cloud-shaped errors.
- Upstream non-2xx, malformed upstream payloads, connection errors, invalid model versions, unknown ids, and downloader failures are covered.
- A contract test proves the real upstream client sends multipart form fields accepted by `parse_request_form`.
- A CLI test proves `main --help` exits successfully.

## Acceptance Criteria

- LLM Wiki can set `API_BASE` to `http://<host>:8888/api/v4`.
- LLM Wiki can keep its current upload, poll, download, unzip flow.
- Returned upload and result URLs are absolute and require no headers.
- No changes are required to local `mineru-api` or `mineru-router`.
- Tests validate the adapter without loading MinerU models.
- New code is isolated to the adapter module, tests, docs, and script entry point.
- Existing unrelated local changes are not reverted and are not included in the final commit for this goal.
