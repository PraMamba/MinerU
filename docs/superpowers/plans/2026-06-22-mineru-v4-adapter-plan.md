# MinerU v4 Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal `/api/v4` compatibility adapter so LLM Wiki can use local `mineru-api` through its current MinerU cloud API client flow.

**Architecture:** Add a focused FastAPI app in `mineru/cli/v4_adapter.py` with a `mineru-v4-adapter` console script. The adapter stores lock-protected process-local task mappings, accepts the subset of cloud API calls used by LLM Wiki, translates them into local MinerU `/tasks` calls, and streams final zip downloads from `/tasks/{task_id}/result`.

**Tech Stack:** Python 3.10+, FastAPI, httpx, click, pytest, FastAPI TestClient.

## Global Constraints

- Implement only the LLM Wiki subset: `/api/v4/extract/task`, `/api/v4/file-urls/batch`, `/api/v4/extract/task/{task_id}`, `/api/v4/extract-results/batch/{batch_id}`, `PUT /api/v4/uploads/{batch_id}/0`, and `GET /api/v4/results/{task_id}.zip`.
- Do not implement a full MinerU cloud API clone.
- Do not modify `mineru-api` or `mineru-router` behavior.
- Do not load parsing models during tests.
- Do not install or change dependencies.
- Do not revert unrelated existing worktree changes.
- Do not commit after individual sub-tasks. Make exactly one final commit for this session goal after all implementation, verification, and review are complete.
- The adapter must accept a non-empty bearer token on JSON API endpoints by default and optionally enforce `MINERU_V4_ADAPTER_TOKEN`.
- Returned upload and result URLs are capability URLs and must work without `Authorization` headers because LLM Wiki does not send headers for those requests.
- `model_version=pipeline` maps to local `backend=pipeline`; `model_version=vlm` maps to local `backend=vlm-engine`.
- Local task submission must request `return_md=true`, `return_images=true`, and `response_format_zip=true`.
- Result zip proxying must stream upstream bytes and must not buffer the complete zip in adapter memory.
- Arbitrary URL fetching is disabled by default and requires explicit opt-in.
- URL fetching must use the same vetted DNS result for the actual connection by pinning the request to a checked IP while preserving the original Host header and HTTPS SNI for DNS names.
- The LLM Wiki settings-test URL `https://cdn-mineru.openxlab.org.cn/demo/example.pdf` must be supported offline by using a packaged sample when arbitrary URL fetching is disabled.
- Upload capability URLs are single-use; repeated upload attempts must return `409` without submitting another upstream task.

---

## File Structure

- Create `mineru/cli/v4_adapter.py`
  - Owns the FastAPI app, in-memory registry, request/response models, local MinerU HTTP client wrapper, token handling, and click CLI.
- Modify `pyproject.toml`
  - Adds console script `mineru-v4-adapter = "mineru.cli.v4_adapter:main"`.
- Add `mineru/resources/v4_adapter/demo1.pdf`
  - Packaged offline sample used only for the LLM Wiki settings-test URL when arbitrary URL fetching is disabled.
- Create `tests/unittest/test_v4_adapter.py`
  - Unit-tests the adapter with stubbed upstream calls and no model loading.
- Modify `docs/zh/usage/quick_usage.md`
  - Adds a short self-hosted LLM Wiki adapter note and startup command.
- Keep existing `mineru/cli/fast_api.py`, `mineru/cli/router.py`, and model code unchanged for this goal.

---

### Task 1: Adapter Skeleton and Request Validation

**Files:**
- Create: `mineru/cli/v4_adapter.py`
- Create: `tests/unittest/test_v4_adapter.py`

**Interfaces:**
- Produces: `create_app(settings: AdapterSettings | None = None, upstream: MineruUpstreamClient | None = None, downloader: UrlDownloader | None = None) -> FastAPI`
- Produces: `AdapterSettings` dataclass with `upstream_url`, `token`, `lang`, `formula_enable`, `table_enable`, `tmp_dir`, `allow_url_fetch`, and `task_retention_seconds`.

- [ ] **Step 1: Write failing tests for batch creation and token handling**

Add tests that import `create_app`, create a TestClient, call `POST /api/v4/file-urls/batch`, and assert:

- non-empty bearer token succeeds when no configured token is set
- returned `batch_id` is non-empty
- returned upload URL contains `/api/v4/uploads/{batch_id}/0`
- missing bearer token returns HTTP 401
- configured token rejects the wrong bearer token
- upload `PUT` succeeds without `Authorization`
- a request with two files returns HTTP 400 and JSON `code != 0`
- `POST /api/v4/file-urls/batch` rejects missing `files`, empty `files`, malformed `files[0]`, and missing `model_version` with HTTP 400 and JSON `code != 0`
- unknown upload capability URL returns HTTP 404

- [ ] **Step 2: Run targeted test and verify RED**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: import failure because `mineru.cli.v4_adapter` does not exist.

- [ ] **Step 3: Implement minimal adapter skeleton**

Implement:

- `AdapterSettings`
- `AdapterRegistry`
- `create_app`
- `POST /api/v4/file-urls/batch`
- `PUT /api/v4/uploads/{batch_id}/0` temporary implementation that accepts bytes without Authorization and records an uploaded state without upstream submission for this task
- token helper that accepts any non-empty bearer token when settings token is empty
- cloud success/error helpers

- [ ] **Step 4: Run targeted test and verify GREEN**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: Task 1 tests pass.

### Task 2: Upstream MinerU Task Submission

**Files:**
- Modify: `mineru/cli/v4_adapter.py`
- Modify: `tests/unittest/test_v4_adapter.py`

**Interfaces:**
- Produces: `MineruUpstreamClient.submit_task(file_path: Path, file_name: str, model_version: str, settings: AdapterSettings) -> str`
- Produces: `model_version_to_backend(model_version: str) -> str`

- [ ] **Step 1: Write failing tests for upload submission**

Add a fake upstream client recording submissions. Test that `PUT /api/v4/uploads/{batch_id}/0`:

- writes the uploaded bytes to a temp file
- calls upstream submit exactly once
- maps `pipeline` to `backend=pipeline`
- maps `vlm` to `backend=vlm-engine`
- includes `parse_method=auto`, `return_md=true`, `return_images=true`, `response_format_zip=true`, `formula_enable`, `table_enable`, and `lang_list`
- stores the returned local task id on the adapter batch
- deletes the staged temp file when upstream submission succeeds
- deletes the staged temp file when upstream submission raises
- invalid `model_version` returns HTTP 400 and JSON `code != 0`
- upstream submit returning non-2xx maps to HTTP 502 and JSON `code != 0`
- upstream submit returning success JSON without `task_id` maps to HTTP 502 and JSON `code != 0`
- upstream submit transport error maps to HTTP 503 and JSON `code != 0`

- [ ] **Step 2: Run targeted test and verify RED**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: fails because upload submission is still a placeholder.

- [ ] **Step 3: Implement upstream submission**

Implement `MineruUpstreamClient` by reusing `mineru.cli.api_client.build_parse_request_form_data` and `mineru.cli.api_client.submit_parse_task` against `{upstream_url}/tasks`. The test fake should satisfy the same `submit_task` interface.

Local multipart form values:

```python
{
    "backend": backend,
    "effort": "medium",
    "parse_method": "auto",
    "return_md": "true",
    "return_middle_json": "false",
    "return_model_output": "false",
    "return_content_list": "false",
    "return_images": "true",
    "response_format_zip": "true",
    "return_original_file": "false",
    "client_side_output_generation": "false",
    "formula_enable": str(settings.formula_enable).lower(),
    "table_enable": str(settings.table_enable).lower(),
    "image_analysis": "true",
    "lang_list": settings.lang,
    "start_page_id": "0",
    "end_page_id": "99999",
}
```

- [ ] **Step 4: Run targeted test and verify GREEN**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: Task 1 and Task 2 tests pass.

### Task 3: Polling and Zip Result Proxy

**Files:**
- Modify: `mineru/cli/v4_adapter.py`
- Modify: `tests/unittest/test_v4_adapter.py`

**Interfaces:**
- Produces: `MineruUpstreamClient.get_status(local_task_id: str) -> dict`
- Produces: `MineruUpstreamClient.stream_result_zip(local_task_id: str) -> Response`

- [ ] **Step 1: Write failing tests for state mapping and zip proxy**

Add tests that:

- create a batch, upload bytes, configure fake upstream status `completed`, poll `GET /api/v4/extract-results/batch/{batch_id}`, and assert state `done` plus `full_zip_url`
- configure fake upstream status `processing`, poll batch status, and assert state `running` without `full_zip_url`
- configure fake upstream status `failed` with an error and assert state `failed` plus `err_msg`
- call `GET /api/v4/results/{adapter_task_id}.zip` without Authorization and assert `application/zip` body is a valid zip containing one `.md` entry
- call result URL while pending and assert HTTP 202
- call result URL after failure and assert HTTP 409
- call unknown result URL and assert HTTP 404
- assert `full_zip_url` is absolute
- unknown batch poll `GET /api/v4/extract-results/batch/{unknown}` returns HTTP 404 and JSON `code != 0`
- upstream poll returning non-2xx maps to HTTP 502 and JSON `code != 0`
- upstream poll returning malformed JSON maps to HTTP 502 and JSON `code != 0`
- upstream poll transport error maps to HTTP 503 and JSON `code != 0`
- upstream result transport error maps to HTTP 503
- upstream result non-2xx response that is not a normal pending/failed result maps to HTTP 502

- [ ] **Step 2: Run targeted test and verify RED**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: fails because polling and result proxy endpoints are absent or incomplete.

- [ ] **Step 3: Implement polling and result proxy**

Implement:

- local-to-cloud state mapper
- `GET /api/v4/extract-results/batch/{batch_id}`
- `GET /api/v4/results/{task_id}.zip`
- upstream status/result methods
- streaming result proxy using `httpx.AsyncClient.stream`

- [ ] **Step 4: Run targeted test and verify GREEN**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: Task 1 through Task 3 tests pass.

### Task 4: URL Task Endpoint

**Files:**
- Modify: `mineru/cli/v4_adapter.py`
- Modify: `tests/unittest/test_v4_adapter.py`

**Interfaces:**
- Produces: `UrlDownloader.download(url: str, target_dir: Path) -> tuple[Path, str]`

- [ ] **Step 1: Write failing tests for URL task flow**

Add tests that:

- inject a fake downloader returning a temp PDF path and filename
- call `POST /api/v4/extract/task` with URL and `model_version=pipeline`
- assert response has `data.task_id`
- assert fake upstream submit was called
- configure fake upstream status `completed`
- poll `GET /api/v4/extract/task/{task_id}` and assert `state=done` and `full_zip_url`
- assert the settings-page test URL path uses a local bundled sample and does not require internet when `allow_url_fetch=false`
- assert arbitrary URL submission returns HTTP 400 when `allow_url_fetch=false`
- assert unsafe URL targets are rejected when URL fetching is enabled
- assert redirects are not followed by simulating a public URL returning `302 Location: http://127.0.0.1/secret`
- force downloader failure and assert a non-zero cloud-shaped error
- assert staged downloaded files are cleaned up when upstream submission raises
- assert DNS-name URL fetching connects to the vetted IP URL with the original Host header and HTTPS SNI, and does not follow redirects
- assert IP-literal HTTPS URL fetching does not set SNI
- `POST /api/v4/extract/task` rejects missing `url`, non-string `url`, missing `model_version`, and invalid `model_version` with HTTP 400 and JSON `code != 0`
- unknown URL task poll `GET /api/v4/extract/task/{unknown}` returns HTTP 404 and JSON `code != 0`

- [ ] **Step 2: Run targeted test and verify RED**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: fails because URL task flow is absent.

- [ ] **Step 3: Implement URL task flow**

Implement:

- `UrlDownloader` using `httpx.AsyncClient`
- URL filename inference from `Content-Disposition` or URL path, falling back to `document.pdf`
- safe URL validation for `http` and `https`, DNS resolution, private/loopback/link-local rejection, IP-pinned connection with original Host/SNI, and `follow_redirects=False`
- local sample substitution for the exact LLM Wiki settings-test URL when URL fetching is disabled
- `POST /api/v4/extract/task`
- `GET /api/v4/extract/task/{task_id}`

- [ ] **Step 4: Run targeted test and verify GREEN**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: Task 1 through Task 4 tests pass.

### Task 5: CLI Entry Point and Usage Docs

**Files:**
- Modify: `mineru/cli/v4_adapter.py`
- Modify: `pyproject.toml`
- Modify: `docs/zh/usage/quick_usage.md`
- Modify: `tests/unittest/test_v4_adapter.py`

**Interfaces:**
- Produces: click command `main()`
- Produces: console script `mineru-v4-adapter`

- [ ] **Step 1: Write failing tests for settings and CLI construction**

Add tests for:

- `settings_from_env()` reads `MINERU_V4_ADAPTER_UPSTREAM_URL`, `MINERU_V4_ADAPTER_TOKEN`, `MINERU_V4_ADAPTER_LANG`, `MINERU_V4_ADAPTER_FORMULA_ENABLE`, `MINERU_V4_ADAPTER_TABLE_ENABLE`, `MINERU_V4_ADAPTER_ALLOW_URL_FETCH`, and `MINERU_V4_ADAPTER_TASK_RETENTION_SECONDS`
- `create_app` exposes `/docs` and route paths include all six LLM Wiki adapter endpoints
- `CliRunner().invoke(main, ["--help"])` exits with code 0
- a real upstream contract test uses a stub FastAPI `/tasks` endpoint with MinerU's `parse_request_form` dependency and proves the submitted multipart fields are accepted without model loading
- an end-to-end in-memory LLM Wiki flow succeeds: bearer batch POST, no-auth upload PUT, bearer poll, no-auth zip GET

- [ ] **Step 2: Run targeted test and verify RED**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: fails because env/CLI helpers or entry point are incomplete.

- [ ] **Step 3: Implement CLI and docs**

Implement click options:

```bash
mineru-v4-adapter \
  --host 127.0.0.1 \
  --port 8888 \
  --upstream-url http://127.0.0.1:8000
```

Add pyproject script:

```toml
mineru-v4-adapter = "mineru.cli.v4_adapter:main"
```

Add a short docs section explaining:

```bash
MINERU_MODEL_SOURCE=local HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
mineru-api --host 0.0.0.0 --port 8000

mineru-v4-adapter --host 127.0.0.1 --port 8888 --upstream-url http://127.0.0.1:8000
```

LLM Wiki should set:

```ts
const API_BASE = "http://<host>:8888/api/v4"
```

and configure a non-empty placeholder token if adapter token enforcement is disabled.

- [ ] **Step 4: Run targeted test and verify GREEN**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: all adapter tests pass.

### Task 6: Final Verification and Single Commit

**Files:**
- Verify all files touched by this goal.

- [ ] **Step 1: Run targeted adapter tests**

Run:

```bash
pytest tests/unittest/test_v4_adapter.py -q
```

Expected: all adapter tests pass.

- [ ] **Step 2: Run import/CLI smoke checks without starting long-lived servers**

Run:

```bash
python - <<'PY'
from mineru.cli.v4_adapter import create_app, settings_from_env
app = create_app()
paths = sorted(route.path for route in app.routes)
required = {
    "/api/v4/extract/task",
    "/api/v4/file-urls/batch",
    "/api/v4/extract/task/{task_id}",
    "/api/v4/extract-results/batch/{batch_id}",
    "/api/v4/uploads/{batch_id}/{file_index}",
    "/api/v4/results/{task_id}.zip",
}
missing = required.difference(paths)
if missing:
    raise SystemExit(f"missing routes: {sorted(missing)}")
print("routes ok")
print(settings_from_env().upstream_url)
PY
```

Expected: prints `routes ok` and the default upstream URL.

- [ ] **Step 3: Inspect diff boundary**

Run:

```bash
git status --short
git diff --stat
```

Expected: adapter-related files are present; unrelated pre-existing files remain uncommitted and must not be staged for this goal commit.

- [ ] **Step 4: Request code review and fix blocking findings**

Use a code-review subagent over only this goal's diff. Fix Critical and Important findings, rerun targeted tests, and re-review if needed.

- [ ] **Step 5: Make one final commit for this session goal**

Stage only:

```bash
git add \
  mineru/cli/v4_adapter.py \
  tests/unittest/test_v4_adapter.py \
  docs/superpowers/specs/2026-06-22-mineru-v4-adapter-design.md \
  docs/superpowers/plans/2026-06-22-mineru-v4-adapter-plan.md \
  docs/zh/usage/quick_usage.md \
  mineru/resources/v4_adapter/demo1.pdf \
  pyproject.toml
```

Commit:

```bash
git commit -m "feat: add MinerU v4 compatibility adapter"
```

Expected: one commit containing only this goal's changes.
