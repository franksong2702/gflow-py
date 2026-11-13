"""High-level async `FlowClient` SDK for Google Flow (`flow.google.com`)."""

from __future__ import annotations

import asyncio
import mimetypes
import time
from pathlib import Path
from typing import Any

from ._proto import (
    RPC_ADD_SCENE_CLIPS,
    RPC_CREATE_PROJECT,
    RPC_CREATE_SCENE,
    RPC_CREDITS,
    RPC_DELETE_PROJECT,
    RPC_EXPORT_SCENE_POLL,
    RPC_EXPORT_SCENE_START,
    RPC_GET_MEDIA,
    RPC_GET_PROJECT,
    RPC_GET_SCENE,
    RPC_LIST_PROJECTS,
    RPC_MODELS,
    RPC_POLL_STATUS,
    RPC_PROJECT_TILES,
    RPC_REORDER_SCENE_CLIPS,
    RPC_SAVE_VIDEO_FRAME,
    RPC_UPDATE_PROJECT,
    RPC_UPDATE_SCENE_META,
    RPC_UPLOAD_IMAGE,
    RPC_WHOAMI,
    build_add_scene_clips_req,
    build_create_project_req,
    build_create_scene_req,
    build_export_scene_start_req,
    build_generate_image_req,
    build_generate_video_req,
    build_get_scene_req,
    build_poll_status_req,
    build_rename_project_req,
    build_rename_scene_req,
    build_save_video_frame_req,
    build_set_scene_clips_req,
    build_upload_image_req,
    jspb_msg,
    parse_create_or_get_scene,
    parse_credits,
    parse_generation_response,
    parse_get_scene_response,
    parse_media_item,
    parse_models,
    parse_project,
    parse_project_contents,
    parse_project_list,
    parse_user_account,
    resolve_image_aspect,
    resolve_image_model_key,
    resolve_video_aspect,
    resolve_video_model_key,
    resolve_video_resolution,
)
import httpx

from ._recaptcha import RecaptchaTokenProvider
from ._rpc import FlowHttpRpc
from .auth import load_cookies_from_storage, recover_google_session, rotate_google_cookies
from .exceptions import (
    AuthError,
    GenerationError,
    NotFoundError,
    QuotaExceededError,
    RateLimitedError,
    ValidationError,
)
from .paths import get_active_profile, get_default_output_dir, get_storage_state_path, load_context
from .types import (
    AccountCredits,
    MediaItem,
    ModelVariant,
    Project,
    ProjectContents,
    Scene,
    SceneClip,
    UserAccount,
)


class FlowClient:
    """Async client for Google Flow projects, Veo/Omni video, Nano Banana images, and Scene Builder."""

    def __init__(
        self,
        rpc: FlowHttpRpc,
        *,
        storage_state_path: Path,
        profile: str = "default",
        headless_recaptcha: bool = True,
    ) -> None:
        self._rpc = rpc
        self._storage_state_path = storage_state_path
        self._profile = profile
        self._recaptcha = RecaptchaTokenProvider(
            storage_state_path,
            headless=headless_recaptcha,
        )
        self._recovery_lock = asyncio.Lock()
        self._rpc._on_auth_expired = self._recover_session
        self._scene_meta_cache: dict[str, tuple[str, str]] = {}
        self._cached_model_keys: dict[str, set[str]] | None = None

    async def _recover_session(self) -> httpx.Cookies:
        async with self._recovery_lock:
            jar = await recover_google_session(
                profile=self._profile,
                storage_path=self._storage_state_path,
            )
            await self._recaptcha.reset()
            return jar

    async def keepalive_rotate(self) -> bool:
        """Proactively rotate Google `__Secure-1PSIDTS` / `SIDCC` cookies and persist to disk."""
        async with self._recovery_lock:
            rotated = await rotate_google_cookies(self._storage_state_path)
            if rotated is not None:
                await self._rpc.replace_cookies(rotated)
                return True
            return False

    @classmethod
    def from_storage(
        cls,
        *,
        profile: str | None = None,
        storage_path: str | Path | None = None,
        timeout: float = 45.0,
    ) -> FlowClient:
        resolved_profile = get_active_profile(profile)
        path = get_storage_state_path(resolved_profile, explicit_storage=storage_path)
        try:
            cookies = load_cookies_from_storage(path)
        except AuthError:
            cookies = httpx.Cookies()
        rpc = FlowHttpRpc(cookies, timeout=timeout)
        return cls(rpc, storage_state_path=path, profile=resolved_profile)

    async def __aenter__(self) -> FlowClient:
        await self._rpc.bootstrap()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._recaptcha.close()
        await self._rpc.close()

    # ── Account & Model Catalog ──────────────────────────────────────────────

    async def whoami(self) -> UserAccount:
        payload = [["me"], [[["person.photo", "person.name", "person.email", "person.metadata"]], None, [1, 7, 10]]]
        raw = await self._rpc.call(RPC_WHOAMI, payload)
        return parse_user_account(raw)

    async def get_credits(self) -> AccountCredits:
        raw = await self._rpc.call(RPC_CREDITS, [])
        return parse_credits(raw)

    async def list_models(self, kind: str | None = None) -> list[ModelVariant]:
        raw = await self._rpc.call(RPC_MODELS, [])
        models = parse_models(raw)
        by_kind: dict[str, set[str]] = {}
        for m in models:
            by_kind.setdefault(m.kind, set()).add(m.key)
        self._cached_model_keys = by_kind
        if kind:
            low = kind.strip().lower()
            models = [m for m in models if m.kind == low]
        return models

    async def _get_valid_model_keys(self, kind: str) -> set[str] | None:
        if self._cached_model_keys is None:
            try:
                await self.list_models()
            except AuthError:
                raise
            except Exception:
                return None
        if self._cached_model_keys is None:
            return None
        return self._cached_model_keys.get(kind)

    # ── Projects ─────────────────────────────────────────────────────────────

    async def list_projects(self, limit: int = 30) -> list[Project]:
        payload = ["projects/*", max(1, min(limit, 100)), None, None, None, None, [1]]
        raw = await self._rpc.call(RPC_LIST_PROJECTS, payload)
        return parse_project_list(raw)

    async def resolve_project_id(self, project_id_or_prefix: str | None = None) -> str:
        """Resolve an explicit ID, partial prefix, or the profile's active project context."""
        target = (project_id_or_prefix or "").strip()
        if not target:
            ctx = load_context(self._profile)
            target = str(ctx.get("project_id") or "").strip()
            if not target:
                raise ValidationError(
                    "No project specified and no active project set. "
                    "Pass --project <id> or run 'gflow use <project_id>' first."
                )

        # Full UUID fast-path
        if len(target) == 36 and target.count("-") == 4:
            return target

        projects = await self.list_projects(limit=50)
        matches = [
            p for p in projects if p.id.startswith(target) or p.title.lower() == target.lower()
        ]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            summary = ", ".join(f"{p.id[:8]} ({p.title})" for p in matches[:5])
            raise ValidationError(
                f"Ambiguous project prefix {target!r} matched {len(matches)} projects: {summary}"
            )
        return target

    async def get_project(self, project_id: str) -> Project:
        pid = await self.resolve_project_id(project_id)
        raw = await self._rpc.call(RPC_GET_PROJECT, [f"tools/PINHOLE/projects/{pid}"])
        proj = parse_project(raw)
        if not proj:
            raise NotFoundError(f"Project {pid!r} not found.")
        return proj

    async def create_project(self, title: str) -> Project:
        req = build_create_project_req(title.strip() or "Untitled project")
        raw = await self._rpc.call(RPC_CREATE_PROJECT, req)
        proj = parse_project(raw)
        if not proj:
            raise GenerationError(f"Unexpected create_project response: {raw}")
        return proj

    async def rename_project(self, project_id: str, new_title: str) -> Project:
        pid = await self.resolve_project_id(project_id)
        req = build_rename_project_req(pid, new_title.strip())
        await self._rpc.call(RPC_UPDATE_PROJECT, req)
        return await self.get_project(pid)

    async def delete_project(self, project_id: str) -> bool:
        pid = await self.resolve_project_id(project_id)
        await self._rpc.call(RPC_DELETE_PROJECT, [f"projects/{pid}"])
        return True

    async def get_project_contents(self, project_id: str | None = None) -> ProjectContents:
        pid = await self.resolve_project_id(project_id)
        raw = await self._rpc.call(
            RPC_PROJECT_TILES,
            [f"projects/{pid}", None, None, None, [1]],
            source_path=f"/project/{pid}",
        )
        return parse_project_contents(pid, raw)

    # ── Media Inspection, Upload, Frame Capture & Download ───────────────────

    async def get_media(self, media_id: str) -> MediaItem:
        mid = media_id.strip()
        if not mid:
            raise ValidationError("media_id must not be empty.")
        raw = await self._rpc.call(RPC_GET_MEDIA, [mid])
        item = parse_media_item(raw)
        if not item:
            raise NotFoundError(f"Media item {mid!r} not found.")
        return item

    async def _call_guarded(
        self,
        action: str,
        project_id: str,
        build_fn: Any,
    ) -> Any:
        """Execute a reCAPTCHA-protected mutation via same-page Chrome fetch, with 1-shot auth recovery."""
        if hasattr(self._recaptcha, "call_guarded_rpc"):
            try:
                return await self._recaptcha.call_guarded_rpc(action, project_id, build_fn)
            except AuthError:
                recovered_jar = await self._recover_session()
                await self._rpc.replace_cookies(recovered_jar)
                return await self._recaptcha.call_guarded_rpc(action, project_id, build_fn)

        token = await self._recaptcha.get_token(action)
        rpc_id, req = build_fn(token)
        return await self._rpc.call(rpc_id, req, source_path=f"/project/{project_id}")

    async def upload_image(
        self,
        project_id: str | None,
        source: str | Path | bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        is_hidden: bool = False,
    ) -> MediaItem:
        pid = await self.resolve_project_id(project_id)
        if isinstance(source, (str, Path)):
            path = Path(source).expanduser()
            if not path.is_file():
                raise ValidationError(f"Image file does not exist: {path}")
            image_bytes = path.read_bytes()
            fname = file_name or path.name
            guessed, _ = mimetypes.guess_type(fname)
            mtype = mime_type or guessed or "image/png"
        else:
            image_bytes = source
            fname = file_name or "upload.png"
            mtype = mime_type or "image/png"

        raw = await self._call_guarded(
            "UPLOAD_IMAGE",
            pid,
            lambda tok: (
                RPC_UPLOAD_IMAGE,
                build_upload_image_req(
                    pid,
                    image_bytes,
                    mime_type=mtype,
                    file_name=fname,
                    recaptcha_token=tok,
                    is_hidden=is_hidden,
                ),
            ),
        )
        items = parse_generation_response(raw)
        if not items:
            raise GenerationError(f"Image upload succeeded but returned no MediaItem: {raw}")
        return await self.get_media(items[0].id)

    async def _ensure_media_id(self, project_id: str, ref_or_path: str | Path) -> str:
        """Accept either an existing Flow `media_id` UUID or a local image file path."""
        s = str(ref_or_path).strip()
        candidate_path = Path(s).expanduser()
        if candidate_path.is_file():
            uploaded = await self.upload_image(project_id, candidate_path)
            return uploaded.id
        return s

    async def save_video_frame(
        self,
        project_id: str | None,
        video_media_id: str,
        timestamp_seconds: float = 0.0,
        *,
        file_name: str = "frame.png",
    ) -> MediaItem:
        """Extract a still frame from an existing Flow video clip as a reusable image MediaItem."""
        pid = await self.resolve_project_id(project_id)
        req = build_save_video_frame_req(
            pid,
            video_media_id.strip(),
            timestamp_seconds,
            file_name=file_name,
        )
        raw = await self._rpc.call(RPC_SAVE_VIDEO_FRAME, req, source_path=f"/project/{pid}")
        items = parse_generation_response(raw)
        if not items:
            raise GenerationError(f"Frame capture returned no MediaItem: {raw}")
        return await self.get_media(items[0].id)

    async def download_media(
        self,
        media: str | MediaItem,
        output_path: str | Path | None = None,
    ) -> Path:
        item = await self.get_media(media) if isinstance(media, str) else media
        if not item.download_url:
            item = await self.get_media(item.id)
        url = item.download_url
        if not url:
            raise GenerationError(
                f"Media {item.id} has no downloadable URL yet (status={item.status})."
            )

        data, ctype = await self._rpc.download_bytes(url)
        ext = ".mp4" if (item.media_type == "video" or (ctype and "video" in ctype)) else ".jpg"
        if ctype and "png" in ctype:
            ext = ".png"

        if output_path is not None:
            out = Path(output_path).expanduser()
            if out.is_dir():
                out = out / f"gflow_{item.media_type}_{item.id[:8]}{ext}"
        else:
            out = get_default_output_dir() / f"gflow_{item.media_type}_{item.id[:8]}{ext}"

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return out

    # ── Image Generation (Nano Banana Pro / 2.1 / Lite) ──────────────────────

    async def generate_image(
        self,
        project_id: str | None,
        prompt: str,
        *,
        model: str = "pro",
        aspect_ratio: str = "16:9",
        count: int = 1,
        references: list[str | Path] | None = None,
        base_image: str | Path | None = None,
        seed: int | None = None,
        wait: bool = True,
        poll_interval: float = 3.0,
        timeout: float = 180.0,
    ) -> list[MediaItem]:
        if not 1 <= count <= 4:
            raise ValidationError("Image generation count must be between 1 and 4.")
        resolve_image_aspect(aspect_ratio)
        valid_img_keys = await self._get_valid_model_keys("image")
        model_key = resolve_image_model_key(model, extra_valid_keys=valid_img_keys)

        pid = await self.resolve_project_id(project_id)
        ref_ids = [await self._ensure_media_id(pid, r) for r in (references or [])]
        base_id = await self._ensure_media_id(pid, base_image) if base_image else None

        raw = await self._call_guarded(
            "IMAGE_GENERATION",
            pid,
            lambda tok: build_generate_image_req(
                pid,
                prompt,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                count=count,
                reference_media_ids=ref_ids,
                base_image_media_id=base_id,
                recaptcha_token=tok,
                seed=seed,
            ),
        )
        items = parse_generation_response(raw)
        if not items:
            raise GenerationError(f"Image generation returned no items: {raw}")

        if not wait:
            return items

        return await self.poll_media(
            [it.id for it in items],
            wait=True,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    # ── Video Generation (Text, Start Frame, Start+End, Refs, Extend, Upsample, Omni) ──

    async def generate_video(
        self,
        project_id: str | None,
        prompt: str,
        *,
        model: str = "fast",
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        duration_seconds: int = 8,
        count: int = 1,
        start_frame: str | Path | None = None,
        end_frame: str | Path | None = None,
        references: list[str | Path] | None = None,
        scene_id: str | None = None,
        scene_position: int | None = None,
        seed: int | None = None,
        wait: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> list[MediaItem]:
        if not 1 <= count <= 4:
            raise ValidationError("Video generation count must be between 1 and 4.")
        if references and len(references) > 3:
            raise ValidationError("reference_media_ids supports at most 3 reference images.")
        _, is_portrait = resolve_video_aspect(aspect_ratio)
        resolve_video_resolution(resolution)
        if start_frame and end_frame:
            pre_mode = "START_END_FRAMES"
        elif start_frame:
            pre_mode = "START_FRAME"
        elif references:
            pre_mode = "REFERENCES"
        else:
            pre_mode = "TEXT"
        valid_vid_keys = await self._get_valid_model_keys("video")
        resolve_video_model_key(
            model,
            mode=pre_mode,
            is_portrait=is_portrait,
            duration_seconds=duration_seconds,
            extra_valid_keys=valid_vid_keys,
        )

        pid = await self.resolve_project_id(project_id)
        start_id = await self._ensure_media_id(pid, start_frame) if start_frame else None
        end_id = await self._ensure_media_id(pid, end_frame) if end_frame else None
        ref_ids = [await self._ensure_media_id(pid, r) for r in (references or [])]

        expected_uuids: list[str] = []

        def _build_vid(tok: str | None) -> tuple[str, Any]:
            nonlocal expected_uuids
            r_id, r_payload, uuids = build_generate_video_req(
                pid,
                prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                duration_seconds=duration_seconds,
                count=count,
                start_frame_media_id=start_id,
                end_frame_media_id=end_id,
                reference_media_ids=ref_ids if ref_ids else None,
                scene_id=scene_id,
                scene_position=scene_position,
                recaptcha_token=tok,
                seed=seed,
                valid_video_model_keys=valid_vid_keys,
            )
            expected_uuids = uuids
            return r_id, r_payload

        raw = await self._call_guarded("VIDEO_GENERATION", pid, _build_vid)
        items = parse_generation_response(raw)
        media_ids = [it.id for it in items] if items else expected_uuids

        if not wait:
            return items if items else [MediaItem(id=mid, project_id=pid, media_type="pending", status="running") for mid in media_ids]

        return await self.poll_media(
            media_ids,
            wait=True,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    async def extend_video(
        self,
        project_id: str | None,
        video_media_id: str,
        prompt: str,
        *,
        model: str = "fast",
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        start_frame_index: int | None = None,
        end_frame_index: int | None = None,
        references: list[str | Path] | None = None,
        wait: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> list[MediaItem]:
        if references and len(references) > 3:
            raise ValidationError("reference_media_ids supports at most 3 reference images.")
        _, is_portrait = resolve_video_aspect(aspect_ratio)
        resolve_video_resolution(resolution)
        valid_vid_keys = await self._get_valid_model_keys("video")
        resolve_video_model_key(
            model,
            mode="EXTEND_VIDEO",
            is_portrait=is_portrait,
            extra_valid_keys=valid_vid_keys,
        )

        pid = await self.resolve_project_id(project_id)
        ref_ids = [await self._ensure_media_id(pid, r) for r in (references or [])]
        expected_uuids: list[str] = []

        def _build_ext(tok: str | None) -> tuple[str, Any]:
            nonlocal expected_uuids
            r_id, r_payload, uuids = build_generate_video_req(
                pid,
                prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                extend_video_media_id=video_media_id.strip(),
                extend_start_frame=start_frame_index,
                extend_end_frame=end_frame_index,
                reference_media_ids=ref_ids if ref_ids else None,
                recaptcha_token=tok,
                valid_video_model_keys=valid_vid_keys,
            )
            expected_uuids = uuids
            return r_id, r_payload

        raw = await self._call_guarded("VIDEO_GENERATION", pid, _build_ext)
        items = parse_generation_response(raw)
        media_ids = [it.id for it in items] if items else expected_uuids
        if not wait:
            return items if items else [MediaItem(id=mid, project_id=pid, media_type="pending", status="running") for mid in media_ids]
        return await self.poll_media(media_ids, wait=True, poll_interval=poll_interval, timeout=timeout)

    async def upsample_video(
        self,
        project_id: str | None,
        video_media_id: str,
        *,
        target: str = "1080p",
        aspect_ratio: str | None = None,
        wait: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> list[MediaItem]:
        eff_aspect = aspect_ratio
        if eff_aspect is None or not eff_aspect.strip():
            try:
                src_media = await self.get_media(video_media_id.strip())
                if src_media.width and src_media.height:
                    if src_media.height > src_media.width:
                        eff_aspect = "9:16"
                    elif src_media.width == src_media.height:
                        eff_aspect = "1:1"
                    else:
                        eff_aspect = "16:9"
                else:
                    eff_aspect = "16:9"
            except Exception:
                eff_aspect = "16:9"

        _, is_portrait = resolve_video_aspect(eff_aspect)
        valid_vid_keys = await self._get_valid_model_keys("video")
        resolve_video_model_key(
            target,
            mode="UPSAMPLE_VIDEO",
            is_portrait=is_portrait,
            extra_valid_keys=valid_vid_keys,
        )
        resolve_video_resolution(target)

        pid = await self.resolve_project_id(project_id)
        expected_uuids: list[str] = []

        def _build_up(tok: str | None) -> tuple[str, Any]:
            nonlocal expected_uuids
            r_id, r_payload, uuids = build_generate_video_req(
                pid,
                "",
                model=target,
                aspect_ratio=eff_aspect,
                resolution=target,
                upsample_video_media_id=video_media_id.strip(),
                recaptcha_token=tok,
                valid_video_model_keys=valid_vid_keys,
            )
            expected_uuids = uuids
            return r_id, r_payload

        raw = await self._call_guarded("VIDEO_GENERATION", pid, _build_up)
        items = parse_generation_response(raw)
        media_ids = [it.id for it in items] if items else expected_uuids
        if not wait:
            return items if items else [MediaItem(id=mid, project_id=pid, media_type="pending", status="running") for mid in media_ids]
        return await self.poll_media(media_ids, wait=True, poll_interval=poll_interval, timeout=timeout)

    async def poll_media(
        self,
        media_ids: list[str],
        *,
        wait: bool = False,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> list[MediaItem]:
        ids = [m.strip() for m in media_ids if m.strip()]
        if not ids:
            return []

        deadline = time.monotonic() + timeout
        while True:
            raw = await self._rpc.call(RPC_POLL_STATUS, build_poll_status_req(ids))
            polled = parse_generation_response(raw)
            by_id = {it.id: it for it in polled}

            # Hydrate completed items via `as29s` if signed URL isn't populated yet
            results: list[MediaItem] = []
            all_settled = True
            for mid in ids:
                it = by_id.get(mid)
                if it is None:
                    try:
                        it = await self.get_media(mid)
                    except Exception:
                        it = MediaItem(id=mid, project_id=None, media_type="pending", status="running")

                if it.status == "failed":
                    err_txt = it.error_message or "upstream failure"
                    low_err = err_txt.lower()
                    if any(
                        k in low_err
                        for k in (
                            "concurrent_limit",
                            "requests_throttled",
                            "rate_limit",
                            "too_much_traffic",
                        )
                    ):
                        raise RateLimitedError(
                            f"Rate or concurrency limit reached while generating {mid}: {err_txt}"
                        )
                    if any(k in low_err for k in ("credit", "quota", "insufficient", "balance")):
                        raise QuotaExceededError(
                            f"Quota or credits exhausted while generating {mid}: {err_txt}"
                        )
                    if any(
                        k in low_err
                        for k in (
                            "policy",
                            "safety",
                            "violat",
                            "prohibited",
                            "blocked",
                            "unsafe",
                            "danger_filter",
                            "sexual",
                            "minor",
                            "prominent_people",
                            "non_english_prompt",
                        )
                    ):
                        raise ValidationError(
                            f"Prompt or media blocked by Google Flow policy for {mid}: {err_txt}"
                        )
                    raise GenerationError(f"Generation failed for media {mid}: {err_txt}")
                if not it.download_url:
                    # Double-check via `as29s` in case poll summary omitted signed URL
                    try:
                        full = await self.get_media(mid)
                        if full.download_url:
                            it = full
                    except Exception:
                        pass

                if not it.download_url:
                    all_settled = False
                results.append(it)

            if not wait or all_settled:
                return results

            if time.monotonic() >= deadline:
                raise GenerationError(
                    f"Timed out after {timeout:.0f}s waiting for media {', '.join(ids)} to complete."
                )
            await asyncio.sleep(poll_interval)

    # ── Scene Builder (Create / List / Add Clips / Reorder / Trim / Remove / Export MP4) ──

    async def list_scenes(self, project_id: str | None = None) -> list[Scene]:
        contents = await self.get_project_contents(project_id)
        for s in contents.scenes:
            self._scene_meta_cache[s.id] = (s.title, s.aspect_ratio)
        return contents.scenes

    async def _enrich_scene_clips(self, project_id: str, clips: list[SceneClip]) -> list[SceneClip]:
        if not clips or all(c.primary_media_id and c.duration_seconds for c in clips):
            return clips
        contents = await self.get_project_contents(project_id)
        wf_primary = {w.id: w.primary_media_id for w in contents.workflows if w.primary_media_id}
        media_by_id = {m.id: m for m in contents.media}
        wf_media = {m.workflow_id: m for m in contents.media if m.workflow_id}

        enriched: list[SceneClip] = []
        for c in clips:
            pmid = (
                c.primary_media_id
                or wf_primary.get(c.workflow_id)
                or (wf_media[c.workflow_id].id if c.workflow_id in wf_media else None)
            )
            dur = c.duration_seconds
            if pmid and dur is None:
                m_obj = media_by_id.get(pmid) or wf_media.get(c.workflow_id)
                if m_obj is None:
                    try:
                        m_obj = await self.get_media(pmid)
                    except Exception:
                        m_obj = None
                if m_obj is not None:
                    dur = m_obj.duration_seconds
            enriched.append(
                SceneClip(
                    clip_id=c.clip_id,
                    scene_id=c.scene_id,
                    workflow_id=c.workflow_id,
                    primary_media_id=pmid,
                    position=c.position,
                    duration_seconds=dur,
                    start_time_seconds=c.start_time_seconds,
                    end_time_seconds=c.end_time_seconds,
                )
            )
        return enriched

    async def get_scene(self, project_id: str | None, scene_id: str) -> Scene:
        pid = await self.resolve_project_id(project_id)
        sid = scene_id.strip()
        if not sid:
            raise ValidationError("scene_id must not be empty.")
        raw = await self._rpc.call(
            RPC_GET_SCENE,
            build_get_scene_req(pid, sid),
            source_path=f"/project/{pid}",
        )
        cached_title, cached_aspect = self._scene_meta_cache.get(
            sid, ("Untitled scene", "16:9")
        )
        parsed = parse_get_scene_response(
            pid,
            sid,
            raw,
            title=cached_title,
            aspect_ratio=cached_aspect,
        )
        if parsed is not None:
            if sid not in self._scene_meta_cache:
                for s in await self.list_scenes(pid):
                    if s.id == sid:
                        parsed = Scene(
                            id=parsed.id,
                            project_id=pid,
                            title=s.title,
                            aspect_ratio=s.aspect_ratio,
                            clips=parsed.clips,
                        )
                        break
            enriched_clips = await self._enrich_scene_clips(pid, parsed.clips)
            return Scene(
                id=parsed.id,
                project_id=pid,
                title=parsed.title,
                aspect_ratio=parsed.aspect_ratio,
                clips=enriched_clips,
            )

        # Fallback for newly created empty scenes (0 clips)
        for s in await self.list_scenes(pid):
            if s.id == sid or s.id.startswith(sid):
                return s
        if sid in self._scene_meta_cache:
            return Scene(
                id=sid,
                project_id=pid,
                title=cached_title,
                aspect_ratio=cached_aspect,
                clips=[],
            )
        raise NotFoundError(f"Scene {scene_id!r} not found in project {pid}.")

    async def _resolve_workflow_ids(
        self,
        project_id: str,
        workflow_or_media_ids: list[str],
    ) -> list[str]:
        """Allow callers to pass either `workflow_id` OR `media_id` when adding clips to a Scene."""
        contents = await self.get_project_contents(project_id)
        wf_set = {w.id for w in contents.workflows}
        media_to_wf = {m.id: m.workflow_id for m in contents.media if m.workflow_id}

        resolved: list[str] = []
        for raw_id in workflow_or_media_ids:
            cid = raw_id.strip()
            if cid in wf_set:
                resolved.append(cid)
            elif cid in media_to_wf and media_to_wf[cid]:
                resolved.append(media_to_wf[cid] or cid)
            else:
                # Try live `as29s` media lookup in case it was just generated
                try:
                    m = await self.get_media(cid)
                    if m.workflow_id:
                        resolved.append(m.workflow_id)
                        continue
                except Exception:
                    pass
                resolved.append(cid)
        return resolved

    async def create_scene(
        self,
        project_id: str | None,
        title: str = "Untitled scene",
        *,
        aspect_ratio: str = "16:9",
        clip_ids: list[str] | None = None,
    ) -> Scene:
        pid = await self.resolve_project_id(project_id)
        wf_ids = await self._resolve_workflow_ids(pid, clip_ids) if clip_ids else []
        req = build_create_scene_req(
            pid,
            title=title.strip() or "Untitled scene",
            aspect_ratio=aspect_ratio,
            workflow_ids=wf_ids,
        )
        raw = await self._rpc.call(RPC_CREATE_SCENE, req, source_path=f"/project/{pid}")
        scene = parse_create_or_get_scene(pid, raw)
        if not scene:
            raise GenerationError(f"Unexpected create_scene response: {raw}")
        self._scene_meta_cache[scene.id] = (scene.title, scene.aspect_ratio)
        if wf_ids:
            return await self.get_scene(pid, scene.id)
        return scene

    async def rename_scene(self, project_id: str | None, scene_id: str, new_title: str) -> Scene:
        pid = await self.resolve_project_id(project_id)
        sid = scene_id.strip()
        clean_title = new_title.strip()
        await self._rpc.call(
            RPC_UPDATE_SCENE_META,
            build_rename_scene_req(pid, sid, clean_title),
            source_path=f"/project/{pid}",
        )
        prev_aspect = self._scene_meta_cache.get(sid, ("Untitled scene", "16:9"))[1]
        self._scene_meta_cache[sid] = (clean_title, prev_aspect)
        return await self.get_scene(pid, sid)

    async def add_scene_clips(
        self,
        project_id: str | None,
        scene_id: str,
        clip_ids: list[str],
        *,
        position: int | None = None,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
    ) -> Scene:
        pid = await self.resolve_project_id(project_id)
        wf_ids = await self._resolve_workflow_ids(pid, clip_ids)
        req = build_add_scene_clips_req(
            pid,
            scene_id.strip(),
            wf_ids,
            position=position,
            start_time_seconds=start_time_seconds,
            end_time_seconds=end_time_seconds,
        )
        await self._rpc.call(RPC_ADD_SCENE_CLIPS, req, source_path=f"/project/{pid}")
        return await self.get_scene(pid, scene_id)

    async def reorder_scene_clip(
        self,
        project_id: str | None,
        scene_id: str,
        from_index: int,
        to_index: int,
    ) -> Scene:
        pid = await self.resolve_project_id(project_id)
        scene = await self.get_scene(pid, scene_id)
        clips = list(scene.clips)
        if not (0 <= from_index < len(clips)) or not (0 <= to_index < len(clips)):
            raise ValidationError(
                f"Clip indices out of range (0..{max(0, len(clips) - 1)}): from={from_index}, to={to_index}"
            )
        moved = clips.pop(from_index)
        clips.insert(to_index, moved)
        await self._rpc.call(
            RPC_REORDER_SCENE_CLIPS,
            build_set_scene_clips_req(pid, scene.id, clips),
            source_path=f"/project/{pid}",
        )
        return await self.get_scene(pid, scene.id)

    async def trim_scene_clip(
        self,
        project_id: str | None,
        scene_id: str,
        clip_index: int,
        *,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
    ) -> Scene:
        pid = await self.resolve_project_id(project_id)
        scene = await self.get_scene(pid, scene_id)
        clips = list(scene.clips)
        if not (0 <= clip_index < len(clips)):
            raise ValidationError(f"Clip index {clip_index} out of range (0..{max(0, len(clips) - 1)}).")
        old = clips[clip_index]
        clips[clip_index] = SceneClip(
            clip_id=old.clip_id,
            scene_id=scene.id,
            workflow_id=old.workflow_id,
            primary_media_id=old.primary_media_id,
            position=old.position,
            duration_seconds=old.duration_seconds,
            start_time_seconds=start_time_seconds if start_time_seconds is not None else old.start_time_seconds,
            end_time_seconds=end_time_seconds if end_time_seconds is not None else old.end_time_seconds,
        )
        await self._rpc.call(
            RPC_REORDER_SCENE_CLIPS,
            build_set_scene_clips_req(pid, scene.id, clips),
            source_path=f"/project/{pid}",
        )
        return await self.get_scene(pid, scene.id)

    async def remove_scene_clip(
        self,
        project_id: str | None,
        scene_id: str,
        clip_index: int,
    ) -> Scene:
        pid = await self.resolve_project_id(project_id)
        scene = await self.get_scene(pid, scene_id)
        clips = list(scene.clips)
        if not (0 <= clip_index < len(clips)):
            raise ValidationError(f"Clip index {clip_index} out of range (0..{max(0, len(clips) - 1)}).")
        clips.pop(clip_index)
        await self._rpc.call(
            RPC_REORDER_SCENE_CLIPS,
            build_set_scene_clips_req(pid, scene.id, clips),
            source_path=f"/project/{pid}",
        )
        return await self.get_scene(pid, scene.id)

    async def export_scene_mp4(
        self,
        project_id: str | None,
        scene_id: str,
        *,
        output_path: str | Path | None = None,
        timeout: float = 180.0,
    ) -> tuple[str, Path | None]:
        """Render & stitch a Scene Builder timeline into a single MP4 via `GjQLTd` + `IxdSwd`.

        Returns ``(signed_mp4_url, saved_local_path_or_none)``.
        """
        pid = await self.resolve_project_id(project_id)
        scene = await self.get_scene(pid, scene_id)
        if not scene.clips:
            raise ValidationError(f"Scene {scene.id} ({scene.title!r}) has no clips to export.")

        start_raw = await self._rpc.call(
            RPC_EXPORT_SCENE_START,
            build_export_scene_start_req(scene.clips),
            source_path=f"/project/{pid}",
        )
        job_handle = start_raw[1] if isinstance(start_raw, list) and len(start_raw) > 1 else None
        if not job_handle:
            raise GenerationError(f"Scene export did not return a job handle: {start_raw}")

        deadline = time.monotonic() + timeout
        mp4_url: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(3.0)
            poll_raw = await self._rpc.call(
                RPC_EXPORT_SCENE_POLL,
                jspb_msg({1: job_handle}),
                source_path=f"/project/{pid}",
            )
            if not isinstance(poll_raw, list):
                continue
            state_code = poll_raw[0] if len(poll_raw) > 0 else None
            # Per `TOa` / `Mwb` (`js_5.js`), `f.wf()` (`_.Mu(this, 1)` -> `poll_raw[0]`) == 3 indicates done,
            # and `_.$y(f, 5)` -> `poll_raw[4]` carries the base64 MP4 bytes (or signed URL).
            candidate = poll_raw[4] if len(poll_raw) > 4 else None
            if (state_code == 3 or state_code is None) and isinstance(candidate, str) and candidate:
                if candidate.startswith("http"):
                    mp4_url = candidate
                    break
                import base64

                mp4_bytes = base64.b64decode(candidate)
                dest = (
                    Path(output_path).expanduser()
                    if output_path
                    else (get_default_output_dir() / f"gflow_scene_{scene.id[:8]}.mp4")
                )
                if dest.is_dir():
                    dest = dest / f"gflow_scene_{scene.id[:8]}.mp4"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(mp4_bytes)
                return f"file://{dest}", dest
            if state_code in (4, 5, 7):
                raise GenerationError(f"Scene MP4 export failed (state={state_code}): {poll_raw}")

        if not mp4_url:
            raise GenerationError(f"Timed out after {timeout:.0f}s exporting scene {scene.id}.")

        saved_path: Path | None = None
        if output_path is not None:
            data, _ = await self._rpc.download_bytes(mp4_url)
            dest = Path(output_path).expanduser()
            if dest.is_dir():
                dest = dest / f"gflow_scene_{scene.id[:8]}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            saved_path = dest

        return mp4_url, saved_path
