# Google Flow Remote HTTP MCP Connector — Verified Limits & Production Semantics

This reference documents all empirically verified payload limits, concurrency bounds, link conditions/TTLs, file retention windows, timeout/idempotency guarantees, and structured error codes for **`gflow-mcp --transport http`** (e.g. `https://<host>.<tailnet>.ts.net:8443/mcp`).

All numbers below are **exact enforced or empirically verified values** (never estimates), validated against Google Flow's upstream Boq/AiSandbox service (`flow.google.com`) and `gflow-mcp` boundary tests (`tests/test_production_hardening.py`).

---

## 1. Image Upload Size, Format, and Reference Limits (`P0-1`, `P2-6`)

Remote VM callers can upload keyframes, start/end frames, and character/style reference images **directly without human browser relay** using any of three paths:

| Upload Path | Max Image Size | Accepted Formats & Validation | Filename Limit | Notes |
| --- | --- | --- | --- | --- |
| `flow_upload_image(image_base64=..., file_name=...)` | **15 MiB** (`15,728,640` decoded bytes; ~20 MiB base64 text) | `image/png` (`.png`), `image/jpeg` (`.jpg`/`.jpeg`), `image/webp` (`.webp`) — **magic-byte enforced** (`\x89PNG\r\n\x1a\n`, `\xff\xd8\xff`, `RIFF....WEBP`) | **256 characters** | Accepts raw base64 or `data:image/...;base64,...`. Exceeding 15 MiB or non-PNG/JPEG/WebP magic bytes immediately returns `INVALID_INPUT`. |
| `flow_create_upload_link` -> `PUT` / `POST /files/ul/{token}?filename=...` | **20 MiB** (`20,971,520` raw bytes — exact Google Flow upstream image cap) | `image/png`, `image/jpeg`, `image/webp` — magic-byte + `Content-Type` enforced | **120 characters** (auto-sanitized) | Single-use HMAC-SHA256 URL (**TTL = 1,800s / 30 minutes**), zero base64 token overhead. `> 20,971,520` bytes returns HTTP `413`; unsupported format returns HTTP `415`; replay after commit returns HTTP `403`. |
| `flow_upload_image(image_url=...)` / `flow_upload_image(file_path=...)` | **20 MiB** (`20,971,520` bytes) | `image/png`, `image/jpeg`, `image/webp` — magic-byte enforced | **256 characters** | `image_url` fetches over HTTP/HTTPS (`30s` timeout); `file_path` reads from server host local disk (missing file returns `NOT_FOUND`). |

### Per-Call Media & Reference Caps
- **Default Video Model (`flow_generate_video` / `flow_extend_video`)**: **`Omni 1.2 Flash`** (`model="omni"`, default; `macaron_cookie_*` family via standard production video RPCs) — supports integer durations **`3..15` seconds** (`default=4`) for Text-to-Video (`macaron_cookie_t2v_*`), Start-Frame (`macaron_cookie_i2v_*`), and Multi-Reference (`macaron_cookie_r2v_*`), and **`3..10` seconds** for Start+End-Frame Interpolation (`macaron_cookie_i2v_fl_*`) and Extend (`macaron_cookie_extend_*`). Veo 3.1 (`model="fast" | "quality" | "lite"`, `8s`) is also supported.
- **`reference_media_ids` (`flow_generate_video`)**: Maximum **`3`** reference images per video generation (`> 3` raises `INVALID_INPUT`).
- **`start_frame_media_id` + `end_frame_media_id`**: `1` start frame + optional `1` end frame (`end_frame_media_id` without `start_frame_media_id` raises `INVALID_INPUT`).
- **Batch `count` (`flow_generate_video` / `flow_generate_image`)**: **`1..4`** outputs per call (`< 1` or `> 4` raises `INVALID_INPUT`).

---

## 2. Project & Scene Builder Asset Limits (`P2-6`)

| Scope | Verified Limit / Behavior | Details |
| --- | --- | --- |
| **Assets per Project** (`flow_project_contents` / RPC `Zzl0ze`) | **No hard cap observed up to 500+ tiles** (`page_size=500` per RPC fetch; Google Flow frontend `MAX_GLOBAL_TOTAL_BYTES` in-memory upload batch cap = `52,428,800` bytes per single UI drag-drop) | Recommended operational practice for storyboarded short-film production: **1 project per episode/short (typically 20–150 keyframes + clips)** so `flow_project_contents` returns in a single sub-second RPC. |
| **Clips per Scene Builder Timeline** (`flow_scene_create` / `flow_scene_add_clips` / RPC `GoMJte`) | **No fixed protocol array cap**; Scene export (`GjQLTd` + `IxdSwd`) stitches ordered clips server-side | Empirically verified multi-clip timeline creation, trimming, reordering, and server-side MP4 stitching. Keep single exported timelines **<= 60 clips** so upstream `GjQLTd` stitching completes within the `300s` export poll window. |

---

## 3. Parallel Generation & HTTP File-Transfer Concurrency (`P2-6`)

| Layer | Concurrency Limit | Behavior When Exceeded |
| --- | --- | --- |
| **Signed HTTP Uploads (`/files/ul/{token}`)** | **`4` concurrent streams** (`_MAX_CONCURRENT_UPLOADS = 4`) | 5th simultaneous upload immediately receives **HTTP `429 Too Many Requests`** (JTI is rolled back so retrying the same upload link succeeds). |
| **Signed HTTP Downloads (`/files/dl/{token}`)** | **`4` concurrent streams** (`_MAX_CONCURRENT_DOWNLOADS = 4`) | 5th simultaneous download immediately receives **HTTP `429 Too Many Requests`** (`download_link` remains valid for its full 24h TTL). |
| **reCAPTCHA Enterprise Token Minting (`RecaptchaTokenProvider`)** | **Serialized (`1` mutex lock, ~0.25–0.45s per token)** | Concurrent `flow_upload_image` / `flow_generate_*` calls queue cleanly in-process without browser race conditions, then execute their Google Flow RPCs concurrently. |
| **Upstream Google Flow Parallel Video Generations** | **`4` concurrent active video renders per Google account** (`wait=False` or soft-wait; single-call batch `count` = `1..4`) | Submitting a 5th simultaneous active video render returns upstream `PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED` (`51`), which `gflow-mcp` decodes from `PublicAitkError` and maps deterministically to `error_code = "RATE_LIMITED"` (`retriable = true`). |

---

## 4. Download Link Activation Condition, Auth Method, & TTL (`P0-2`, `P2-6`)

### Exact Condition When `download_link` Is Returned
- `download_link` (and `browser_url`) is **always returned** on `flow_generate_video`, `flow_generate_image`, `flow_extend_video`, `flow_upsample_video`, `flow_upload_image`, `flow_save_video_frame`, `flow_get_media`, `flow_poll_media`, `flow_project_contents`, and **`flow_scene_export_mp4`** whenever **`gflow-mcp` is started with `GFLOW_MCP_OAUTH_BASE_URL` or `GFLOW_MCP_PUBLIC_URL` set**.
- Because remote OAuth HTTP deployments (`https://<host>.<tailnet>.ts.net:8443/mcp`) always set `GFLOW_MCP_OAUTH_BASE_URL`, **HTTP file transfer is 100% active on all remote calls**. Callers can programmatically confirm this via `flow_account_usage()`, which returns:
  - `"file_transfer_active": true`
  - `"download_link_ttl_seconds": 86400`

### Authentication & Byte-Exact Integrity Verification
- **Authentication**: Capability-URL authentication via **HMAC-SHA256 signature** embedded in `/files/dl/{token}?download=1`. **No `Authorization` header, cookie, or Google login is needed** — any remote VM can fetch directly with `curl -fsSL "<download_link>" -o out.mp4` or `httpx.get(download_link)`.
- **Link Validity (TTL)**: **`86,400` seconds (24 hours)** by default (configurable via `GFLOW_MCP_DOWNLOAD_TTL_SECONDS`, minimum `60s`). Every response includes exact ISO-8601 expiration in `download_expires_at`.
- **Restart Survival**: The 32-byte HMAC signing key is persisted in `~/.gflow/filelink_secret.key` (`0600`), so issued 24-hour download links remain valid even if `gflow-mcp` restarts.
- **Expired / Tampered Link**: Returns **HTTP `403 Forbidden`**. For any project media item, calling `flow_get_media(media_id=...)` mints a brand-new 24-hour `download_link` on demand.
- **Byte-Exactness Verification**:
  - Every `/files/dl/{token}` HTTP `200` response includes `Content-Length`, `X-GFlow-Bytes` (exact byte count), and `X-GFlow-SHA256` (lowercase hex SHA-256).
  - `flow_scene_export_mp4` directly returns `file_size_bytes` and `sha256` in its tool JSON response.
  - `flow_get_media(media_id=..., include_checksum=True)` returns `file_size_bytes` and `sha256` in its tool JSON response.

---

## 5. Server-Side File & Metadata Retention Windows (`P2-6`)

| Artifact Type | Storage Location | Retention Period | Expiration Behavior |
| --- | --- | --- | --- |
| **Generated Images, Videos, Uploaded Keyframes, & Saved Frames** | Google Flow Cloud Project (`flow.google.com`) | **Persistent until project or asset is deleted** | Stored in Google Flow's cloud backend; `flow_get_media(media_id)` dynamically fetches a fresh upstream signed GCS URL and mints a new 24h `download_link` at any time. |
| **Scene Builder Exported MP4s (`flow_scene_export_mp4`)** | server host local disk `~/.gflow/exports/gflow_scene_*.mp4` | **7 days (`604,800` seconds)** | Automatically pruned by `prune_expired_exports()` on server startup and before each Scene export (`EXPORT_RETENTION_SECONDS = 604800`). Re-running `flow_scene_export_mp4(scene_id=...)` regenerates the MP4 if needed. |
| **Job & Idempotency Records (`job_id` / `idempotency_key`)** | server host local disk `~/.gflow/jobs.json` (`0600`) | **7 days (`604,800` seconds)**, up to `2,000` recent jobs | Deduplicates retries carrying the same `(op, idempotency_key)` and allows `flow_poll_media(job_id=...)` / `flow_poll_media(idempotency_key=...)` lookups across disconnects and server restarts. |
| **Single-Use Upload Link Metadata (`upload_id`)** | In-memory `JtiStore` | **30 minutes (`1,800` seconds)** | Pruned automatically after `exp` (`DEFAULT_UPLOAD_TTL_SECONDS = 1800`). |

---

## 6. Timeout Protection, `job_id`, & Idempotency (`P1-4`)

- **Shielded Submission**: Every `flow_generate_video`, `flow_extend_video`, `flow_upsample_video`, `flow_generate_image`, and `flow_scene_export_mp4` call executes upstream submission inside `asyncio.shield` and atomically persists `job_id` + `media_ids` + `idempotency_key` to `~/.gflow/jobs.json` (`0600`). Even if the remote VM HTTP client disconnects mid-request, the job record is preserved.
- **Soft-Wait Ceiling (< 45s Client Timeout)**: Even when `wait=True` (default), generation tools poll for at most `wait_timeout_seconds` (**default `25.0s`, hard cap `35.0s`**) and return `{"job_id": "job_...", "status": "running", "media_ids": [...]}` cleanly *before* a 45-second remote MCP HTTP timeout can sever the connection.
- **Polling**: Call `flow_poll_media(job_id=...)` (or `flow_poll_media(idempotency_key=...)`) until `"status": "completed"`, at which point `items[i]["download_link"]` is ready to download.
- **Idempotency**: Passing `idempotency_key="<caller-unique-id>"` guarantees **at-most-once upstream execution** (both for concurrent in-flight retries and sequential retries over 7 days), returning `"idempotent_replay": true` with the original `job_id` and **never charging duplicate Flow credits**.

---

## 7. Google Session Auto-Renewal & Structured Error Codes (`P0-3`, `P1-5`)

### Automatic Session Renewal (`P0-3`)
1. **Proactive Background Keepalive**: `gflow-mcp` runs a background task every **9 minutes (`540s`)** calling `POST https://accounts.google.com/RotateCookies` to refresh short-lived `__Secure-1PSIDTS` / `SIDCC` cookies atomically (`0600`).
2. **Reactive 3-Rung Self-Healing**: If a Google session expires while idle, `FlowClient` transparently attempts:
   - **Rung 1**: Reload `storage_state.json` + `RotateCookies`
   - **Rung 2**: Sync fresher sibling `~/.notebooklm/profiles/<profile>/storage_state.json` + `RotateCookies`
   - **Rung 3**: Silent headless Chromium cookie re-mint from persistent `browser_profile/` + `RotateCookies`
3. **Fail-Fast Signal & Non-Destructive Empirical Verification**:
   - If all renewal rungs fail (e.g. Google account credentials revoked), tools fail immediately (typically `< 4s`) with `error_code = "AUTH_EXPIRED"` (`retriable = false`) and `error.details.relogin_command = "gflow login"`, instructing the operator to run `gflow login` on server host.
   - Live keepalive counters & timestamps (`last_rotation_succeeded_at`, `next_scheduled_rotation_at`, `rotation_attempt_count`, `rotation_success_count`) are exposed in `flow_account_usage().session_renewal`.
   - Remote callers can empirically trigger a live upstream `AUTH_EXPIRED` error at any time without disrupting the `default` session by calling `flow_account_usage(profile="expired-test")` (or `flow_project_list(profile="expired-test")`).

### Canonical Structured Error Envelope (`P1-5`)
Every tool failure returns an MCP error (`isError=True`) whose body is deterministic JSON:

```json
{
  "ok": false,
  "error_code": "QUOTA_EXCEEDED",
  "error": {
    "code": "QUOTA_EXCEEDED",
    "message": "Google Flow quota or AI credits exhausted (YhhmEf, PUBLIC_ERROR_USER_QUOTA_REACHED): ...",
    "retriable": false,
    "details": {
      "rpc_id": "YhhmEf",
      "upstream_code": 8,
      "public_error": "PUBLIC_ERROR_USER_QUOTA_REACHED",
      "public_error_code": 2
    }
  }
}
```

| `error_code` | `retriable` | Exact Client / HTTP / gRPC / `PublicAitkError` (`_.pta`) Triggers | Recommended Caller Action |
| --- | --- | --- | --- |
| `AUTH_EXPIRED` | `false` | Google `ServiceLogin` redirect, HTTP `401`/`403`, or Boq gRPC `16` (`UNAUTHENTICATED`) after all 3 auto-renewal rungs fail | **Stop** pipeline immediately and notify operator to run `gflow login` on server host |
| `QUOTA_EXCEEDED` | `false` | Boq gRPC `8` (`RESOURCE_EXHAUSTED` non-rate), `PublicAitkError` codes `{2, 32, 34, 48, 57, 70, 71}` (`USER_QUOTA_REACHED`, `PER_MODEL_DAILY_QUOTA_REACHED*`, `WORKSPACE_ACCOUNT_QUOTA_REACHED`, `VERTEX_QUOTA_EXHAUSTED*`), or poll failure due to insufficient credits | **Stop** pipeline and notify operator to top up / check quota |
| `INVALID_INPUT` | `false` | Unknown/unsupported `model` (`flow_generate_video`, `flow_extend_video`, `flow_generate_image`, `flow_upsample_video`), invalid `aspect_ratio`/`resolution`/`duration_seconds`/`count`, missing `project_id`, bad base64, unsupported image magic bytes or size (`> 15 MiB` inline / `> 20 MiB` URL), HTTP `400`, Boq gRPC `3` (`INVALID_ARGUMENT`), or `PublicAitkError` safety/policy/input codes (`{1, 7, 8, 11, 12, 14..26, 29..31, 33, 36, 39..41, 43..47, 52, 53, 55, 56, 59..69, 73..76}`) | Fix model/parameters/prompt before retrying |
| `RATE_LIMITED` | `true` | HTTP `429`, Boq gRPC `8` (rate/concurrency), or `PublicAitkError` codes `{3, 4, 5, 9, 28, 49, 50, 51}` (`USER_REQUESTS_THROTTLED`, `HIGH_TRAFFIC`, `MODEL_OVERLOADED`, `CONCURRENT_LIMIT_REACHED` when `> 4` parallel video renders run) | Back off (`10–30s`) and retry (reuse the same `idempotency_key`) |
| `UPSTREAM_ERROR` | `true` | Network error, HTTP `5xx`, Boq gRPC `4`/`13`/`14`, or `PublicAitkError` codes `{0, 27, 35, 42, 54, 58, 72}` (`VIDEO_GENERATION_TIMED_OUT`, `SOMETHING_WENT_WRONG`, `APP_UNAVAILABLE`, `VERTEX_SERVICE_UNAVAILABLE`) | Retry after a short delay (reuse `idempotency_key` if generating) |
| `NOT_FOUND` | `false` | Nonexistent `project_id`, `scene_id`, `media_id`, `job_id`, `upload_id`, missing local `file_path`, HTTP `404`, or Boq gRPC `5` (`NOT_FOUND`) on entity lookup | **Stop** and verify identifier |
