---
name: gflow
description: >-
  Drive Google Flow (flow.google.com) via the gflow CLI and MCP tools to manage
  AI filmmaking projects, check credits, generate Nano Banana images, generate
  Veo 3.1 / Omni 1.2 videos (text-to-video, start-frame, start+end frame
  interpolation, multi-reference character consistency, video extension,
  1080p/4K upscaling), and assemble/export multi-shot timelines in Scene Builder.
---

# Google Flow (`gflow`) Agent Skill

Use `gflow` (or the `gflow-mcp` MCP tools when mounted) whenever the user wants to create images, cinematic video shots, multi-shot storyboards, character-consistent clips, or Scene Builder timelines using their Google Flow subscription (`flow.google.com`).

## 1. Session & Project Context

Always verify authentication and set or pass a project before generating assets:

```bash
gflow status                  # Active profile & current project context
gflow usage                   # Live Google Flow AI credits & renewal timestamp
gflow list                    # List existing Flow projects
gflow create "Story v1"       # Create a new project
gflow use <project_id_prefix> # Pin active project so subsequent commands omit -P
```

All commands support `--json` for structured machine parsing and `-P / --project <id>` to target a specific project without changing global state.

## 2. Image Generation & Reference Consistency (Nano Banana)

Generate character sheets, keyframes, or start/end frames first for multi-shot consistency:

```bash
# Text-to-Image (models: pro [Nano Banana Pro], 2.1 [Beluga], lite [Nano Banana 2 Lite])
gflow generate image " Pixar-style golden puppy wearing a tiny security cap" \
  --model pro --aspect 9:16 --download

# Upload local reference photo(s) or pass existing media IDs for character consistency
gflow upload ./puppy_ref.png
gflow generate image "Same puppy holding a megaphone on a wooden crate" \
  --ref <media_id_or_local_path> --aspect 9:16 --download
```

## 3. Video Generation & Advanced Shot Control (Veo 3.1 & Omni 1.2 Flash)

`gflow generate video` automatically selects the optimal backend RPC based on inputs:

```bash
# 1) Text-to-Video (models: fast, quality, lite, omni [Omni 1.2 Flash])
gflow generate video "Cinematic tracking shot of a puppy patrolling a sunlit hallway" \
  --model fast --aspect 16:9 --download

# 2) First-Frame Image-to-Video (accepts media_id OR local file path)
gflow generate video "Puppy takes three waddling steps forward, locked camera" \
  --start-frame <media_id_or_path> --model omni --duration 4 --aspect 9:16 --download

# 3) First + Last Frame Transition (Start + End Frame Interpolation)
gflow generate video "Smooth camera push-in as puppy falls asleep on the bed" \
  --start-frame <start_id_or_path> --end-frame <end_id_or_path> \
  --model quality --aspect 9:16 --download

# 4) Multi-Reference Ingredients-to-Video (up to 3 character/prop/scene references)
gflow generate video "The puppy plays with the yellow duck in the living room" \
  --ref <puppy_id> --ref <duck_id> --model fast --aspect 16:9 --download

# 5) Extract a Video Frame (e.g. last frame of Shot 1 to anchor Shot 2)
gflow frame-save <video_media_id> --at 3.8

# 6) Extend an Existing Video Clip (+4s..8s continuation)
gflow extend <video_media_id> "Camera pans up to reveal confetti falling" \
  --aspect 9:16 --download

# 7) Upscale Video to 1080p or 4K
gflow upsample <video_media_id> --target 1080p --download
```

## 4. Scene Builder (Multi-Shot Timeline & Stitched MP4 Export)

Assemble multiple generated clips into a storyboard timeline, trim/reorder them, and render a single stitched MP4:

```bash
gflow scene list
gflow scene create "Final Cut 18s" --aspect 9:16 --clip <media_or_workflow_id_1> --clip <id_2>
gflow scene add <scene_id> <media_or_workflow_id_3> --start 0.0 --end 3.5
gflow scene trim <scene_id> --index 0 --start 0.2 --end 3.8
gflow scene reorder <scene_id> --from-index 2 --to-index 1
gflow scene export <scene_id> -o ./final_cut.mp4
```

## 5. Remote HTTP MCP Production Semantics (`gflow-mcp --transport http`)

When driving `gflow-mcp` from a remote VM or `claude.ai` custom connector:

- **Direct Keyframe Upload (No Human Relay)**: Call `flow_upload_image(image_base64="<base64>", file_name="shot01.png")` (max `15 MiB` decoded `PNG`/`JPEG`/`WebP`) or `PUT` raw bytes to `flow_create_upload_link` (`20 MiB` max, `30m` TTL) to get `media_id` for `start_frame_media_id` / `end_frame_media_id` / `reference_media_ids` (max `3` references).
- **Direct Finished Asset Download**: Generated items and `flow_scene_export_mp4` return unauthenticated HMAC-signed `download_link` URLs (`24h` / `86,400s` TTL) plus `file_size_bytes` & `sha256` (`X-GFlow-Bytes`, `X-GFlow-SHA256` headers). Scene exports and job records are retained on server disk for `7 days`.
- **Timeout Safety & Idempotency**: Always pass a deterministic `idempotency_key` on `flow_generate_*` / `flow_scene_export_mp4`. Tools return `job_id` within `wait_timeout_seconds` (`25s` default, `35s` max) before client HTTP timeout; poll completion with `flow_poll_media(job_id=...)`. Retrying with the same `idempotency_key` never creates a duplicate task or charges duplicate Flow credits.
- **Structured Error Codes**: Inspect `error_code` on tool errors: `AUTH_EXPIRED` (stop & ask operator to login), `QUOTA_EXCEEDED` (stop & check credits), `INVALID_INPUT` (fix params/prompt), `RATE_LIMITED` (backoff & retry with same `idempotency_key`), `UPSTREAM_ERROR` (transient retry), `NOT_FOUND` (verify ID).
