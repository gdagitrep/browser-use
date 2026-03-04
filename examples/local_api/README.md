# Local Browser-Use API (FastAPI)

Run Browser-Use tasks locally over HTTP with a cloud-like task API.

## 1. Start server

```bash
uv run browser-use-api
```

Alternative:

```bash
uv run uvicorn browser_use.local_api.app:app --host 0.0.0.0 --port 8000
```

## 2. Endpoints

Primary endpoints:

- `GET /health`
- `POST /api/v1/run-task`
- `GET /api/v1/task/{task_id}`
- `GET /api/v1/task/{task_id}/status`
- `GET /api/v1/tasks`
- `POST /api/v1/task/{task_id}/stop`
- `GET /api/v1/task/{task_id}/wait`

Optional specialized endpoint:

- `POST /api/v1/run-kumo-task`

Backward-compatible legacy endpoints:

- `/v1/jobs/*`

## 3. Generic task flow

Create task:

```bash
curl -X POST http://localhost:8000/api/v1/run-task \
  -H 'content-type: application/json' \
  -d '{
    "task": "Go to example.com and summarize the page",
    "llm_provider": "browser_use",
    "llm_model": "bu-latest",
    "max_agent_steps": 40,
    "use_cloud": false,
    "downloads_path": "/Users/zephyr/wealth/browser-use/downloads"
  }'
```

Sample response:

```json
{
  "id": "f8e3b2d8-5d4d-4ed6-b0c2-7c3d9a3f7f63",
  "status": "running"
}
```

Check status:

```bash
curl http://localhost:8000/api/v1/task/<task_id>/status
```

Get details/output:

```bash
curl http://localhost:8000/api/v1/task/<task_id>
```

`/api/v1/task/{task_id}` includes `downloaded_files` so you can verify what was actually saved.

Block until complete:

```bash
curl "http://localhost:8000/api/v1/task/<task_id>/wait?timeout_seconds=300"
```

Stop task:

```bash
curl -X POST http://localhost:8000/api/v1/task/<task_id>/stop
```

## 4. Running Kumo with the generic endpoint

You can run Kumo via `run-task` by putting the whole workflow into the `task` string:

```bash
curl -X POST http://localhost:8000/api/v1/run-task \
  -H 'content-type: application/json' \
  -d '{
    "task": "Navigate to https://app.withkumo.com/search/saved/your-id. If login appears, sign in with email YOUR_EMAIL and password YOUR_PASSWORD. Return to that saved-search URL. Wait for the Kumo Scraper bar. Click Clear, then Start. Wait until extraction is done and AI processing is ready to download. Click Upload to Google Drive and finish only when upload success is visible.",
    "llm_provider": "browser_use",
    "llm_model": "bu-latest",
    "max_agent_steps": 120
  }'
```

This uses general agent behavior (LLM-driven UI interaction).

## 5. Running Kumo with deterministic helper tools

If you want the custom Kumo helper tools (`click_plugin_clear`, `click_plugin_start`, wait-for-processing, upload-to-drive checks), use:

```bash
curl -X POST http://localhost:8000/api/v1/run-kumo-task \
  -H 'content-type: application/json' \
  -d '{
    "saved_search_url": "https://app.withkumo.com/search/saved/your-id",
    "email": "you@example.com",
    "password": "your-password"
  }'
```

## 6. BrowserSession and BrowserProfile behavior

`BrowserSession`:

- Not passed in API requests.
- Created and managed internally by `Agent`.

`BrowserProfile`:

- Created internally from request fields.
- Supported profile-related fields on `run-task`:
  - `use_cloud`
  - `headless`
  - `keep_alive`
  - `user_data_dir`
  - `executable_path`
  - `allowed_domains`
  - `prohibited_domains`

Model/provider fields:

- `llm_provider`: `browser_use` (recommended), `openai`, `google`, `anthropic`
- `llm_model`: model name for that provider

## 7. Recommended default

For most browser automation tasks:

- `llm_provider="browser_use"`
- `llm_model="bu-latest"`

## 8. Security note

Do not commit credentials in curl payloads. Prefer environment-based templating or secret injection in your own calling layer.
