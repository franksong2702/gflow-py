# gflow-py

Unofficial Agent-native **Python SDK**, **CLI (`gflow`)**, **Dual-Mode MCP Server (`gflow-mcp`)**, and **Agent Skill** for **[Google Flow](https://flow.google.com/)** — modeled directly after [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py).

Turn Google Flow's web creative studio (**Veo 3.1**, **Omni 1.2 Flash**, **Nano Banana Pro / 2**, and **Scene Builder**) into programmable Python APIs, shell commands, local IDE tools, and remote `claude.ai` connectors.

---

## Features

- **Hybrid Zero-API-Key Architecture**:
  - **Pure `httpx` (`batchexecute` JSPB RPCs)** for instant (~50ms) project management, live credit checks, model discovery, status polling, media downloads, frame capture, and Scene Builder timeline editing/stitching.
  - **Lazy Headless Playwright Enterprise reCAPTCHA** invoked *only* on mutations that require `grecaptcha.enterprise.execute` (`UPLOAD_IMAGE`, `IMAGE_GENERATION`, `VIDEO_GENERATION`).
- **Full Google Flow Studio Coverage**:
  - **Account & Models**: Live AI credits & renewal timestamp (`gflow usage`), full video/image model catalog (`gflow models`).
  - **Projects**: `list`, `create`, `rename`, `delete`, `use` (active project context), and `media` inspection.
  - **Nano Banana Image Generation**: Nano Banana Pro (`GEM_PIX_2`), Nano Banana 2 (`NARWHAL`), and Imagen (`IMAGEN_3_5`) with multi-reference character/style consistency and base-image editing.
  - **Veo 3.1 & Omni 1.2 Video Generation**:
    - Text-to-Video (`YhhmEf` / `IJhj9c`)
    - Start-Frame Image-to-Video (`eb1hJf`)
    - **Start + End Frame Interpolation** (`nprQif`)
    - **Multi-Reference Ingredients-to-Video (R2V)** (`MZZa6b`)
    - **Video Extend** (`fZytfe`) & **1080p / 4K Upsample** (`p0UkFb`)
    - **Video Frame Extraction** (`fdJi4e` — capture any timestamp as a reusable image `media_id`)
  - **Scene Builder**: Create multi-clip timelines, append/insert clips, reorder, trim in/out timestamps, remove clips, and render/stitch continuous **MP4 exports** (`GjQLTd` + `IxdSwd`).
- **Dual-Mode MCP Server (`gflow-mcp`)**:
  - **`stdio`** (default) for Antigravity, Claude Code, Claude Desktop, Cursor, and Codex.
  - **`http`** with built-in **Self-Hosted Single-Password OAuth 2.1**, `MultiAuth` static bearer support, DNS-rebinding `Host` guard, and **HMAC-signed `/files/dl/{token}` & `/files/ul/{token}`** out-of-band media streaming & browser image upload links for **`claude.ai` Custom Connectors** over Tailscale Funnel or Cloudflare Tunnel.
- **One-Command Agent Skill & MCP Installer**:
  - `gflow skill install` installs `SKILL.md` for Antigravity, Claude Code, Codex, and Cursor.
  - `gflow mcp install` registers `gflow-mcp` in your local IDE MCP config.

---

## Installation

```bash
# Install CLI + Browser login/reCAPTCHA + MCP server via uv:
uv tool install --python 3.12 --editable ".[browser,mcp]"

# Ensure Playwright Chromium is installed once:
playwright install chromium
```

---

## Quickstart

### 1. Authenticate with Google Flow

```bash
# Launch interactive browser login:
gflow login

# OR reuse an existing notebooklm-py Google login session instantly:
gflow login --from-notebooklm

# Verify authentication, live credits, and XSRF/reCAPTCHA bootstrap:
gflow doctor
gflow usage
```

### 2. Manage Projects & Generate Assets via CLI

```bash
# List projects or create & activate a new one
gflow list
gflow create "Cyberpunk Short Film" --use

# Generate a Nano Banana Pro keyframe image
gflow generate image "Neon-lit rainy alley in Neo-Tokyo, anamorphic 35mm" --aspect 16:9 -o start.jpg

# Generate an 8s Veo 3.1 video starting from an image or media_id
gflow generate video "Camera dollies forward through steam as neon signs flicker" \
  --model fast --start-frame start.jpg --wait -o shot1.mp4

# Extract the final frame at 7.8s to chain Shot 2 seamlessly
gflow frame-save <shot1_media_id> --at 7.8

# Start + End frame interpolation
gflow generate video "Smooth whip-pan transition from alley to rooftop skyline" \
  --start-frame <frame_a_id> --end-frame <frame_b_id> --wait

# Upsample a clip to 1080p or 4K
gflow upsample <shot1_media_id> --target 1080p --wait -o shot1_1080p.mp4

# Assemble & export a multi-clip stitched MP4 in Scene Builder
gflow scene create "Main Cut" --aspect 16:9 --clip <shot1_media_id> --clip <shot2_media_id>
gflow scene trim <scene_id> 0 --start 0.5 --end 7.5
gflow scene export <scene_id> -o final_cut.mp4
```

Every command supports `--json` for deterministic agent parsing.

---

## Python SDK Usage

```python
import asyncio
from gflow import FlowClient

async def main():
    async with FlowClient.from_storage() as client:
        credits = await client.get_credits()
        print(f"Remaining Flow credits: {credits.total_credits}")

        project = await client.create_project("SDK Demo")
        images = await client.generate_image(
            project.id,
            "Minimalist sunlit ceramic studio, architectural photography",
            model="pro",
            aspect_ratio="16:9",
        )
        videos = await client.generate_video(
            project.id,
            "Slow orbital camera movement around the potter's wheel, natural morning light",
            model="fast",
            start_frame=images[0].id,
            wait=True,
        )
        saved = await client.download_media(videos[0], "studio_shot.mp4")
        print(f"Saved MP4 to {saved}")

asyncio.run(main())
```

---

## Agent Integration (Antigravity / Claude Code / `claude.ai`)

### Install Agent Skill & Local `stdio` MCP

```bash
gflow skill install --target all
gflow mcp install --target antigravity
```

### Expose Remote HTTP MCP for `claude.ai` via Tailscale Funnel

`gflow-mcp` defaults to port **`9421`** so it can run side-by-side with `notebooklm-mcp` (`9420`). When running both on the same machine with Tailscale Funnel, expose `gflow-mcp` on Tailscale Funnel's secondary HTTPS port **`8443`**:

```bash
# 1. Expose localhost:9421 on Tailscale Funnel HTTPS port 8443
/Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --bg --https=8443 9421

# 2. Launch gflow-mcp with self-hosted single-password OAuth 2.1
export GFLOW_MCP_OAUTH_PASSWORD="$(openssl rand -base64 18)"
export GFLOW_MCP_OAUTH_BASE_URL="https://<your-device>.<tailnet>.ts.net:8443"
export GFLOW_MCP_ALLOW_EXTERNAL_BIND=1

gflow-mcp --transport http --host 127.0.0.1 --port 9421
```

Then in **`claude.ai` (or any remote MCP client) -> Settings -> Connectors -> Add custom connector**:
- **Remote MCP Server URL**: `https://<your-device>.<tailnet>.ts.net:8443/mcp`
- Authorize in the browser popup using your `GFLOW_MCP_OAUTH_PASSWORD`.
- **Direct Remote Image Upload (`P0-1`)**: Remote VM agents can pass `image_base64` directly to `flow_upload_image` (up to **15 MiB** decoded `PNG`/`JPEG`/`WebP`), or `PUT` raw binary bytes to `flow_create_upload_link` (up to **20 MiB** / `20,971,520` bytes, 30m TTL) with zero human relay, receiving a `media_id` for `start_frame_media_id` / `end_frame_media_id` / `reference_media_ids`.
- **Direct Remote Asset Retrieval (`P0-2`)**: Generated images/videos and Scene Builder stitched MP4 exports (`flow_scene_export_mp4`) return **24-hour (`86,400s`)** HMAC-signed HTTPS `download_link` URLs with `file_size_bytes` / `sha256` (`X-GFlow-Bytes` & `X-GFlow-SHA256` headers) for unauthenticated byte-exact VM downloads.
- **Session Auto-Renewal (`P0-3`), Timeout/Idempotency (`P1-4`), Structured Errors (`P1-5`), & Verified Limits (`P2-6`)**:
  - 9-minute `RotateCookies` keepalive + 3-rung session recovery; unrecoverable revocation fails fast with `AUTH_EXPIRED`.
  - Soft-wait ceiling (`25s` default, `35s` max) + shielded `job_id` persistence + 7-day `idempotency_key` deduplication (never double-submits or double-charges Flow credits).
  - Canonical JSON `error_code` values: `AUTH_EXPIRED`, `QUOTA_EXCEEDED`, `INVALID_INPUT`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `NOT_FOUND`.
  - Full empirical limits & retention reference: [`docs/MCP_CONNECTOR_LIMITS.md`](docs/MCP_CONNECTOR_LIMITS.md).

---

## Development & Tests

```bash
pytest -q
```
