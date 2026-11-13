# Google Flow (`custom.google-flow`) — Remote MCP Connector Reference

- **Endpoint**: `https://<your-host>.<tailnet>.ts.net:8443/mcp` (Streamable HTTP + OAuth 2.1 Authorization Code + PKCE)
- **Host**: Local host running `gflow-mcp --transport http` (bound to `127.0.0.1:9421` behind Tailscale Funnel `:8443` or reverse proxy)
- **Default Video Model**: **Omni 1.2 Flash** (`model="omni"`, `macaron_cookie_*` production RPCs; `3..15s` for T2V / I2V / R2V, `3..10s` for Start+End interpolation & Extend). Veo 3.1 (`"fast"`, `"quality"`, `"lite"`, `8s`) is also available.
- **Default Image Model**: **Nano Banana Pro** (`model="pro"` = `GEM_PIX_2`; `"2"` / `"2.1"` = `BELUGA`; `"lite"` = `HARBOR_SEAL`).
- **MCP Self-Serve Docs**: Also readable directly over MCP resources `gflow://docs/connector` and `gflow://docs/limits`, and programmatically via `flow_account_usage()`.

---

## 1. End-to-End Remote Workflow (Zero Human Relay Needed)

1. **Inspect Account, Credits, Renewal Telemetry, & Limits**:
   - Call `flow_account_usage()` -> returns `user`, `credits`, `file_transfer_active: true`, `session_renewal` (live `RotateCookies` counters & timestamps), and `verified_limits`.
2. **Select or Create Project**:
   - Call `flow_project_list()` / `flow_project_use(project_id=...)` or `flow_project_create(title=...)`.
3. **Upload Keyframe / Reference Images Directly from Remote Cloud VM**:
   - **Direct Base64 Upload (Recommended for `<= 15 MiB` images)**:
     ```json
     {
       "image_base64": "<raw base64 or data:image/png;base64,...>",
       "file_name": "frame01.png",
       "project_id": "<optional_project_id>"
     }
     ```
     Call `flow_upload_image(image_base64=..., file_name=...)` -> immediately returns `media_id` + 24h `download_link`. *(Note: Remote cloud VMs upload directly via `image_base64`; the legacy workaround saying VMs cannot use `flow_upload_image` is obsolete.)*
   - **Large Binary Upload (`<= 20 MiB`, Zero Base64 Token Overhead)**:
     Call `flow_create_upload_link(project_id=...)` -> `PUT <upload_url>?filename=frame01.png` with raw binary bytes -> returns JSON `{"status": "completed", "media_id": "..."}` immediately.
   - **Public URL Fetch (`<= 20 MiB`)**:
     Call `flow_upload_image(image_url="https://...", file_name="frame01.png")`.
4. **Generate Video or Image with Timeout & Idempotency Protection**:
   - Call `flow_generate_video(prompt=..., model="omni", duration_seconds=4, start_frame_media_id=..., idempotency_key="<unique-key>")`.
   - Every generation call persists `job_id` before polling and soft-waits at most `wait_timeout_seconds` (**default `25s`, max `35s`**). If still rendering, it returns `{"status": "running", "job_id": "job_...", "media_ids": [...]}` before any client HTTP timeout.
   - Poll `flow_poll_media(job_id="job_...", wait=true)` until `status == "completed"`.
   - Retrying with the same `idempotency_key` within 7 days returns `idempotent_replay: true` and **never double-submits or double-charges credits**.
5. **Download Generated Clips or Scene Builder Stitched MP4s**:
   - Fetch `items[0].download_link` (or `flow_scene_export_mp4(...).download_link`) directly over HTTPS (`GET /files/dl/{token}?download=1`, no OAuth header or Google cookie required).
   - Verify byte-exactness against HTTP response headers `X-GFlow-Bytes` and `X-GFlow-SHA256` (also returned inline by `flow_scene_export_mp4` and `flow_get_media(..., include_checksum=true)`).

---

## 2. Verified Limits, Concurrency Caps, TTLs, & Retention Windows

All values below are exact enforced server constants or empirically verified upstream Google Flow caps:

| Category | Item | Verified Value | Behavior When Exceeded |
| --- | --- | --- | --- |
| **Upload Limits** | `flow_upload_image(image_base64=...)` | **`<= 15 MiB`** (`15,728,640` decoded bytes; `/mcp` HTTP body cap = `24 MiB`) | Returns `error_code: "INVALID_INPUT"` (`retriable: false`) |
| **Upload Limits** | `flow_create_upload_link` (`PUT /files/ul/{token}`) / `image_url` / `file_path` | **`<= 20 MiB`** (`20,971,520` bytes — Google Flow upstream image cap) | `PUT /files/ul` returns HTTP `413`; tool calls return `INVALID_INPUT` |
| **Upload Limits** | Supported image formats | `image/png`, `image/jpeg`, `image/webp` (magic-byte validated) | Unsupported format returns `INVALID_INPUT` (or HTTP `415` on `/files/ul`) |
| **Generation Caps** | `reference_media_ids` per video (`flow_generate_video` / `flow_extend_video`) | **Max `3` reference images** | `> 3` returns `INVALID_INPUT` |
| **Generation Caps** | Video `aspect_ratio` (`flow_generate_video` / `flow_extend_video` / `flow_upsample_video`) | **`"16:9"`** (`1280×720` / `1920×1080` / `3840×2160`), **`"9:16"`** (`720×1280` / `1080×1920` / `2160×3840`), **`"1:1"`** (`720×720`) | Unsupported ratio returns `INVALID_INPUT`; `flow_upsample_video` auto-preserves source `9:16` / `16:9` when `aspect_ratio` is omitted |
| **Generation Caps** | Batch `count` per call (`flow_generate_video` / `flow_generate_image`) | **`1..4` outputs per call** | `< 1` or `> 4` returns `INVALID_INPUT` |
| **Generation Caps** | Omni 1.2 Flash `duration_seconds` | **`3..15`s** (T2V / Start-Frame / Refs), **`3..10`s** (Start+End / Extend) | Out-of-range integer returns `INVALID_INPUT` |
| **Concurrency Caps** | **Google Flow parallel active video renders (per Google account)** | **`4` concurrent active video generations** | 5th simultaneous active render returns upstream `PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED` (`51`) -> `error_code: "RATE_LIMITED"` (`retriable: true`) |
| **Concurrency Caps** | **Signed HTTP upload streams (`/files/ul/{token}`)** | **`4` concurrent streams** (`_MAX_CONCURRENT_UPLOADS = 4`) | 5th simultaneous upload receives HTTP `429 Too Many Requests` (single-use JTI rolled back for retry) |
| **Concurrency Caps** | **Signed HTTP download streams (`/files/dl/{token}`)** | **`4` concurrent streams** (`_MAX_CONCURRENT_DOWNLOADS = 4`) | 5th simultaneous download receives HTTP `429 Too Many Requests` (link stays valid for full 24h TTL) |
| **Concurrency Caps** | Enterprise reCAPTCHA minting (`RecaptchaTokenProvider`) | **Serialized (`1` in-process mutex, ~`0.25–0.45s`/token)** | Concurrent tool calls queue cleanly without browser race conditions |
| **Link TTLs** | `download_link` (`/files/dl/{token}?download=1`) | **`24 hours` (`86,400` seconds)** | Expired/tampered token returns HTTP `403`; call `flow_get_media(media_id)` to mint a fresh 24h link |
| **Link TTLs** | `upload_url` (`/files/ul/{token}`) | **`30 minutes` (`1,800` seconds)**, single-use | Expired or already-committed token returns HTTP `403` |
| **Retention** | Scene Builder exported MP4s (`~/.gflow/exports/`) | **`7 days` (`604,800` seconds)** on the server host disk | Auto-pruned after 7 days; re-run `flow_scene_export_mp4(scene_id)` to regenerate |
| **Retention** | `job_id` & `idempotency_key` store (`~/.gflow/jobs.json`) | **`7 days` (`604,800` seconds)**, up to `2,000` jobs | Deduplicates retries & preserves `flow_poll_media(job_id=...)` across server restarts |
| **Retention** | Project media (`flow.google.com` cloud) | **Persistent until project or asset is deleted** | Always retrievable via `flow_get_media(media_id)` / `flow_project_contents` |

---

## 3. Session Auto-Renewal Mechanism & Empirical `AUTH_EXPIRED` Verification

### How Automatic Session Renewal Works
1. **Proactive Background Cookie Rotation (Every `9 minutes` / `540 seconds`)**:
   - `gflow-mcp` runs a background keepalive loop (`DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 540`) that sends `POST https://accounts.google.com/RotateCookies` using `SID` / `__Secure-1PSID` / `__Secure-1PSIDTS`, hot-swaps fresh `__Secure-1PSIDTS` and `SIDCC` cookies in memory, and atomically persists `storage_state.json` (`0600`).
   - Live telemetry (`last_rotation_succeeded_at`, `next_scheduled_rotation_at`, `rotation_attempt_count`, `rotation_success_count`, `last_rotation_status`) is returned on every `flow_account_usage()` call under `session_renewal`.
2. **Reactive 3-Rung Self-Healing (On Any `ServiceLogin` Redirect / HTTP `401` / gRPC `16`)**:
   - **Rung 1**: Reload `storage_state.json` from disk + execute immediate `RotateCookies` POST and verify `WIZ_global_data.SNlM0e` against `https://flow.google.com/?pli=1`.
   - **Rung 2**: For `default`, sync fresher co-located `~/.notebooklm/profiles/default/storage_state.json` if valid + `RotateCookies`.
   - **Rung 3**: Launch headless Chromium against persistent `browser_profile/` to silently re-mint Google SSO cookies + `RotateCookies`.
3. **Deterministic Fail-Fast When Renewal Fails (`AUTH_EXPIRED`)**:
   - If all renewal rungs fail (e.g., Google session revoked or password changed), `gflow-mcp` **never hangs and never fails silently** — it immediately returns structured error `error_code: "AUTH_EXPIRED"` (`retriable: false`) with `error.details.relogin_command = "gflow login"` instructing the operator to run `gflow login` on the server host.

### How Callers Can Empirically Trigger Real `AUTH_EXPIRED` Non-Destructively
- Operators can create a dedicated isolated test profile `expired-test` (`~/.gflow/profiles/expired-test/storage_state.json`) populated with revoked/expired Google cookies.
- Any remote caller can invoke:
  ```json
  // Tool: flow_account_usage (or flow_project_list)
  { "profile": "expired-test" }
  ```
- This executes a **100% live upstream request** to `https://flow.google.com/?pli=1` (hitting Google's real `accounts.google.com/ServiceLogin` wall + attempting real `RotateCookies` renewal which fails), returning genuine `error_code: "AUTH_EXPIRED"` in ~2–3 seconds **without disrupting the live `default` production session**.

---

## 4. Complete Structured Error Code Mapping Table (`6` Canonical Codes)

Every failed tool call returns `isError=True` with deterministic JSON:

```json
{
  "ok": false,
  "error_code": "INVALID_INPUT",
  "error": {
    "code": "INVALID_INPUT",
    "message": "Unsupported video model 'nonexistent-model'...",
    "retriable": false
  }
}
```

| `error_code` | `retriable` | Client / HTTP / gRPC Triggers | Upstream `PublicAitkError` (`_.pta`) Enum Codes | Caller Action |
| --- | --- | --- | --- | --- |
| **`AUTH_EXPIRED`** | `false` | Google `ServiceLogin` redirect, HTTP `401`/`403`, or Boq gRPC `16` (`UNAUTHENTICATED`) after all 3 auto-renewal rungs fail | — | **Stop immediately**; alert operator to run `gflow login` on the server host |
| **`QUOTA_EXCEEDED`** | `false` | Boq gRPC `8` (`RESOURCE_EXHAUSTED` credit/quota), `google.rpc.ErrorInfo` credit/paygate exhaustion, or poll failure due to insufficient AI credits | `2` (`USER_QUOTA_REACHED`), `32` (`PER_MODEL_DAILY_QUOTA_REACHED`), `34` (`PER_MODEL_DAILY_QUOTA_REACHED_UPGRADEABLE`), `48` (`WORKSPACE_ACCOUNT_QUOTA_REACHED`), `57` (`APPLET_STORAGE_QUOTA_EXCEEDED`), `70` (`VERTEX_QUOTA_EXHAUSTED`), `71` (`VERTEX_QUOTA_EXHAUSTED_ADMIN`) | **Stop immediately**; alert operator that AI credits (`nzlxg`) or daily model quota are exhausted |
| **`INVALID_INPUT`** | `false` | Unknown/unsupported `model` (`flow_generate_video`, `flow_extend_video`, `flow_generate_image`, `flow_upsample_video`), invalid `aspect_ratio` / `resolution` / `duration_seconds` / `count`, missing active `project_id`, bad base64, unsupported image magic bytes or size (`> 15 MiB` inline / `> 20 MiB` URL), HTTP `400`, Boq gRPC `3` (`INVALID_ARGUMENT`), or safety/policy rejection | `1` (`UNSAFE_GENERATION`), `7` (`NON_ENGLISH_PROMPT`), `8` (`UNSAFE_IMAGE_UPLOAD`), `11` (`MODEL_ACCESS_DENIED`), `12` (`PROMINENT_PEOPLE_FILTER_FAILED`), `14` (`VIOLENCE_FILTER`), `15` (`DANGER_FILTER`), `16` (`SEXUAL`), `17`–`25` (minor/sexual/prominent-people/photoreal upload & input filters), `26` (`UNKNOWN_IMAGE_FILE_FORMAT`), `31` (`IMAGE_TOO_LARGE`), `33` (`AUDIO_FILTERED`), `41` (`IP_INPUT_IMAGE`), `44` (`IMAGE_TOO_SMALL`), `45`–`47`, `53`, `55`, `56`, `59`–`76` (excl. quota/rate/5xx) | **Do not blind-retry**; fix model name, parameter bounds, image format/size, or prompt wording |
| **`RATE_LIMITED`** | `true` | HTTP `429 Too Many Requests`, Boq gRPC `8` with rate/concurrency reason, or `> 4` parallel active video renders | `3` (`USER_REQUESTS_THROTTLED`), `4` (`HIGH_TRAFFIC`), `5` (`MODEL_DISABLED_DUE_TO_TRAFFIC`), `9` (`GENERATION_ALREADY_IN_PROGRESS`), `28` (`MODEL_OVERLOADED`), `49` (`UNUSUAL_ACTIVITY`), `50` (`UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC`), `51` (`CONCURRENT_LIMIT_REACHED`) | **Back off `10–30s` and retry** (pass the same `idempotency_key` so credits are never double-charged) |
| **`NOT_FOUND`** | `false` | Nonexistent `project_id`, `scene_id`, `media_id`, `job_id`, `upload_id`, missing server-local `file_path`, HTTP `404`, or Boq gRPC `5` (`NOT_FOUND`) on entity lookup | — | **Stop**; verify `project_id` / `media_id` / `job_id` / `scene_id` |
| **`UPSTREAM_ERROR`** | `true` | Transient network error, HTTP `5xx`, Boq gRPC `4` (`DEADLINE_EXCEEDED`) / `13` (`INTERNAL`) / `14` (`UNAVAILABLE`), or upstream render timeout | `0` (`UNSPECIFIED`), `27` (`VIDEO_GENERATION_TIMED_OUT`), `35` (`SOMETHING_WENT_WRONG`), `42` (`APP_UNAVAILABLE`), `54` (`VIDEO_UPLOAD_TIMEOUT`), `58` (`VIDEO_UPLOAD_FAILED`), `72` (`VERTEX_SERVICE_UNAVAILABLE`) | **Retry after short backoff** (reuse `idempotency_key` for generation/export calls) |
