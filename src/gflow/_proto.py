"""Google Flow Boq JSPB (`batchexecute`) builders and response parsers."""

from __future__ import annotations

import base64
import math
import random
import re
import uuid
from typing import Any

from .exceptions import ValidationError
from .types import (
    AccountCredits,
    MediaItem,
    ModelVariant,
    Project,
    ProjectContents,
    Scene,
    SceneClip,
    UserAccount,
    WorkflowItem,
)

# ── Boq RPC Identifiers (`AiSandboxAngularFrontend`) ─────────────────────────

RPC_WHOAMI = "o30O0e"
RPC_CREDITS = "nzlxg"
RPC_MODELS = "HTrJv"

RPC_LIST_PROJECTS = "UpteDb"
RPC_GET_PROJECT = "ngNC2"
RPC_CREATE_PROJECT = "jHPbke"
RPC_DELETE_PROJECT = "QI2zvc"
RPC_UPDATE_PROJECT = "o8DA4"
RPC_PROJECT_TILES = "Zzl0ze"

RPC_GET_MEDIA = "as29s"
RPC_UPLOAD_IMAGE = "maseQ"
RPC_SAVE_VIDEO_FRAME = "jeNy9c"

RPC_GENERATE_IMAGE = "ogiZ0b"
RPC_GENERATE_IMAGE_ASYNC = "Z9cAtf"
RPC_UPSAMPLE_IMAGE_ASYNC = "LcEY2c"

RPC_VIDEO_TEXT = "YhhmEf"
RPC_VIDEO_START_FRAME = "eb1hJf"
RPC_VIDEO_START_END_FRAMES = "nprQif"
RPC_VIDEO_REFERENCES = "MZZa6b"
RPC_VIDEO_EXTEND = "fZytfe"
RPC_VIDEO_EDIT = "jIps6"
RPC_VIDEO_UPSAMPLE = "p0UkFb"
RPC_VIDEO_EXPERIMENTAL = "IJhj9c"  # Omni 1.2 Flash / Omni 1.1

RPC_POLL_STATUS = "jwpduf"

RPC_CREATE_SCENE = "rqZuUc"
RPC_GET_SCENE = "uwAyfb"
RPC_ADD_SCENE_CLIPS = "oWTRd"
RPC_UPDATE_SCENE_CLIP = "dtQlBe"
RPC_REORDER_SCENE_CLIPS = "GoMJte"
RPC_UPDATE_SCENE_META = "BpMsoe"
RPC_EXPORT_SCENE_START = "GjQLTd"
RPC_EXPORT_SCENE_POLL = "IxdSwd"

# Tool enum for Google Flow ("PINHOLE" = 22)
FLOW_TOOL_ID = 22

# ── JSPB Sparse Array Helper ─────────────────────────────────────────────────


def jspb_msg(fields: dict[int, Any]) -> list[Any]:
    """Build a 0-offset Closure JSPB JSON array from 1-based protobuf field numbers."""
    active = {k: v for k, v in fields.items() if v is not None}
    if not active:
        return []
    max_field = max(active)
    arr: list[Any] = [None] * max_field
    for k, v in active.items():
        arr[k - 1] = v
    return arr


def _safe_get(obj: Any, idx: int, default: Any = None) -> Any:
    if isinstance(obj, list) and 0 <= idx < len(obj):
        val = obj[idx]
        return default if val is None else val
    return default


def duration_to_pb(seconds: float | int) -> list[int]:
    whole = int(math.floor(float(seconds)))
    nanos = int(round((float(seconds) - whole) * 1_000_000_000))
    return [whole, nanos] if nanos else [whole]


def pb_to_seconds(pb: Any) -> float | None:
    if not isinstance(pb, list) or not pb:
        return None
    sec = float(pb[0] or 0)
    nanos = float(pb[1] or 0) if len(pb) > 1 else 0.0
    return round(sec + nanos / 1_000_000_000.0, 3)


# ── Aspect Ratio & Resolution Encoders ───────────────────────────────────────

IMAGE_ASPECT_RATIOS: dict[str, int] = {
    "1:1": 1,
    "square": 1,
    "9:16": 2,
    "portrait": 2,
    "16:9": 3,
    "landscape": 3,
    "3:4": 4,
    "4:3": 5,
    "2:3": 6,
    "3:2": 7,
    "4:5": 8,
    "5:4": 9,
    "21:9": 10,
}

VIDEO_ASPECT_RATIOS: dict[str, int] = {
    "1:1": 0,
    "square": 0,
    "9:16": 1,
    "portrait": 1,
    "16:9": 2,
    "landscape": 2,
}

SCENE_ASPECT_RATIOS: dict[str, int] = {
    "9:16": 1,
    "portrait": 1,
    "16:9": 2,
    "landscape": 2,
    "1:1": 2,
}

VIDEO_RESOLUTIONS: dict[str, int] = {
    "360p": 4,
    "720p": 1,
    "1080p": 2,
    "4k": 3,
}


def resolve_image_aspect(aspect_ratio: str) -> int:
    key = aspect_ratio.strip().lower()
    if key not in IMAGE_ASPECT_RATIOS:
        raise ValidationError(
            f"Unsupported image aspect ratio {aspect_ratio!r}. Choose from: "
            "16:9, 9:16, 1:1, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 21:9."
        )
    return IMAGE_ASPECT_RATIOS[key]


def resolve_video_aspect(aspect_ratio: str) -> tuple[int, bool]:
    key = aspect_ratio.strip().lower()
    if key not in VIDEO_ASPECT_RATIOS:
        raise ValidationError(
            f"Unsupported video aspect ratio {aspect_ratio!r}. Choose from: 16:9, 9:16, 1:1."
        )
    code = VIDEO_ASPECT_RATIOS[key]
    return code, (code == 1)


def resolve_scene_aspect(aspect_ratio: str) -> int:
    key = aspect_ratio.strip().lower()
    if key not in SCENE_ASPECT_RATIOS:
        raise ValidationError(
            f"Unsupported scene aspect ratio {aspect_ratio!r}. Choose from: 16:9, 9:16."
        )
    return SCENE_ASPECT_RATIOS[key]


def resolve_video_resolution(resolution: str) -> int:
    key = resolution.strip().lower()
    if key not in VIDEO_RESOLUTIONS:
        raise ValidationError(
            f"Unsupported video resolution {resolution!r}. Choose from: 720p, 1080p, 4k, 360p."
        )
    return VIDEO_RESOLUTIONS[key]


# ── Model Key Resolvers ──────────────────────────────────────────────────────

IMAGE_MODEL_ALIASES: dict[str, str] = {
    "pro": "GEM_PIX_2",
    "nano-banana-pro": "GEM_PIX_2",
    "nano_banana_pro": "GEM_PIX_2",
    "gem_pix_2": "GEM_PIX_2",
    "2": "BELUGA",
    "2.1": "BELUGA",
    "nano-banana-2.1": "BELUGA",
    "beluga": "BELUGA",
    "beluga_display": "BELUGA",
    "lite": "HARBOR_SEAL",
    "flash": "HARBOR_SEAL",
    "nano-banana-lite": "HARBOR_SEAL",
    "harbor_seal": "HARBOR_SEAL",
}


KNOWN_IMAGE_MODEL_KEYS: frozenset[str] = frozenset(
    {
        "BELUGA",
        "GEM_PIX_2",
        "GEM_PIX_2_UPSAMPLE_2K",
        "GEM_PIX_2_UPSAMPLE_4K",
        "HARBOR_SEAL",
        "NARWHAL",
        "GEM_PIX",
        "IMAGEN_3_5",
    }
)

OMNI_VIDEO_ALIASES: frozenset[str] = frozenset(
    {
        "omni",
        "omni-1.2",
        "omni-1.2-flash",
        "omni flash 1.2",
        "omni flash1.2",
        "omni_flash_1_2",
        "omni_1_2",
        "omni1.2",
        "omni-flash",
        "omni_flash",
    }
)

_RAW_MACARON_RE = re.compile(
    r"^macaron_cookie_(t2v|i2v|i2v_fl|r2v|extend)_([3-9]|1[0-5])s(_portrait|_landscape)?$"
)


def _omni_oriented_key(
    base_key: str,
    *,
    is_portrait: bool,
    extra_valid_keys: set[str] | frozenset[str] | None,
) -> str:
    """Pick the orientation-specific Omni (``macaron_cookie_*``) model key.

    Landscape keeps the bare base key (unchanged wire behaviour, no ``_landscape`` suffix).
    Portrait prefers ``<base>_portrait``:

    * live model catalog known -> use the portrait key only if the catalog lists it, otherwise fall back
      to the base key (never raise: catalog contents cannot be verified offline);
    * catalog unknown (``None`` / empty, i.e. offline) -> portrait key.
    """
    if not is_portrait:
        return base_key
    portrait_key = f"{base_key}_portrait"
    if not extra_valid_keys:
        return portrait_key
    return portrait_key if portrait_key in extra_valid_keys else base_key


def resolve_image_model_key(
    model: str,
    *,
    extra_valid_keys: set[str] | frozenset[str] | None = None,
) -> str:
    cleaned = model.strip()
    if not cleaned:
        raise ValidationError("Image model must not be empty.")
    alias = IMAGE_MODEL_ALIASES.get(cleaned.lower())
    if alias is not None:
        return alias
    upper = cleaned.upper()
    if upper in KNOWN_IMAGE_MODEL_KEYS:
        return upper
    if extra_valid_keys and (cleaned in extra_valid_keys or upper in extra_valid_keys):
        return upper if upper in extra_valid_keys else cleaned
    raise ValidationError(
        f"Unsupported image model {model!r}. Valid options: 'pro' (GEM_PIX_2), "
        "'2' / '2.1' (BELUGA), 'lite' (HARBOR_SEAL), or a valid key from flow_list_models(kind='image')."
    )


def resolve_video_model_key(
    model: str,
    *,
    mode: str,
    is_portrait: bool = False,
    duration_seconds: int = 8,
    extra_valid_keys: set[str] | frozenset[str] | None = None,
) -> tuple[str, bool]:
    """Resolve friendly model alias -> `(videoModelKey, is_experimental_omni)`.

    Modes: ``TEXT``, ``START_FRAME``, ``START_END_FRAMES``, ``REFERENCES``, ``EXTEND_VIDEO``, ``UPSAMPLE_VIDEO``.
    Raises :class:`ValidationError` (`error_code="INVALID_INPUT"`) if ``model`` is unknown.
    """
    raw = model.strip()
    low = raw.lower()

    if mode == "UPSAMPLE_VIDEO":
        if low in ("4k", "veo_3_1_upsampler_4k"):
            return "veo_3_1_upsampler_4k", False
        if low in ("1080p", "veo_3_1_upsampler_1080p", "fast", "omni", "default", ""):
            return "veo_3_1_upsampler_1080p", False
        if extra_valid_keys and raw in extra_valid_keys and "upsampler" in low:
            return raw, False
        raise ValidationError(
            f"Unsupported upsample target/model {model!r}. Choose from: '1080p', '4k'."
        )

    if low in OMNI_VIDEO_ALIASES:
        if mode in ("START_END_FRAMES", "EXTEND_VIDEO"):
            if not isinstance(duration_seconds, int) or not 3 <= duration_seconds <= 10:
                raise ValidationError(
                    f"Omni 1.2 Flash {mode} duration_seconds must be an integer between 3 and 10 "
                    f"(got {duration_seconds!r})."
                )
            if mode == "START_END_FRAMES":
                base_key = f"macaron_cookie_i2v_fl_{duration_seconds}s"
            else:
                base_key = f"macaron_cookie_extend_{duration_seconds}s"
            return (
                _omni_oriented_key(base_key, is_portrait=is_portrait, extra_valid_keys=extra_valid_keys),
                False,
            )

        if not isinstance(duration_seconds, int) or not 3 <= duration_seconds <= 15:
            raise ValidationError(
                f"Omni 1.2 Flash {mode} duration_seconds must be an integer between 3 and 15 "
                f"(got {duration_seconds!r})."
            )
        if mode == "START_FRAME":
            base_key = f"macaron_cookie_i2v_{duration_seconds}s"
        elif mode == "REFERENCES":
            base_key = f"macaron_cookie_r2v_{duration_seconds}s"
        else:
            base_key = f"macaron_cookie_t2v_{duration_seconds}s"
        return (
            _omni_oriented_key(base_key, is_portrait=is_portrait, extra_valid_keys=extra_valid_keys),
            False,
        )

    tier: str | None = None
    if low in ("quality", "pro", "veo-3.1-quality", "veo_3_1_quality"):
        tier = "quality"
    elif low in ("lite", "veo-3.1-lite", "veo_3_1_lite"):
        tier = "lite"
    elif low in ("fast", "veo-3.1-fast", "veo_3_1_fast", "default", ""):
        tier = "fast"

    if tier is None:
        # Caller passed a raw model key rather than a friendly alias: validate strictly.
        if extra_valid_keys is not None:
            if raw in extra_valid_keys:
                return raw, low.startswith("abra_")
            raise ValidationError(
                f"Unsupported video model {model!r}. Valid aliases: 'omni' (Omni 1.2 Flash), "
                "'fast', 'quality', 'lite' (Veo 3.1), or a model key from flow_list_models(kind='video')."
            )

        # Offline / unit-test fallback when live model catalog has not been fetched:
        if _RAW_MACARON_RE.match(low):
            return raw, False
        if low.startswith(("veo_3_1_", "veo_3_0_", "veo_2_", "omni_upsampler")):
            return raw, False
        if low.startswith("abra_"):
            return raw, True
        raise ValidationError(
            f"Unsupported video model {model!r}. Valid aliases: 'omni' (Omni 1.2 Flash), "
            "'fast', 'quality', 'lite' (Veo 3.1), or a model key from flow_list_models(kind='video')."
        )

    if mode == "START_END_FRAMES":
        if tier == "lite":
            return "veo_3_1_interpolation_lite", False
        if tier == "quality":
            return ("veo_3_1_i2v_s_portrait_fl" if is_portrait else "veo_3_1_i2v_s_fl"), False
        return (
            "veo_3_1_i2v_s_fast_portrait_ultra_fl"
            if is_portrait
            else "veo_3_1_i2v_s_fast_ultra_fl"
        ), False

    if mode == "EXTEND_VIDEO":
        if tier == "lite":
            return "veo_3_1_extension_lite", False
        if tier == "quality":
            return ("veo_3_1_extend_portrait" if is_portrait else "veo_3_1_extend_landscape"), False
        return (
            "veo_3_1_extend_fast_portrait_ultra"
            if is_portrait
            else "veo_3_1_extend_fast_landscape_ultra"
        ), False

    if mode == "REFERENCES":
        if tier == "lite":
            return "veo_3_1_r2v_lite", False
        return (
            "veo_3_1_r2v_fast_portrait_ultra" if is_portrait else "veo_3_1_r2v_fast_landscape_ultra"
        ), False

    if mode == "START_FRAME":
        if tier == "lite":
            return "veo_3_1_i2v_lite", False
        if tier == "quality":
            return ("veo_3_1_i2v_s_portrait" if is_portrait else "veo_3_1_i2v_s"), False
        return (
            "veo_3_1_i2v_s_fast_portrait_ultra" if is_portrait else "veo_3_1_i2v_s_fast_ultra"
        ), False

    # Default: TEXT
    if tier == "lite":
        return "veo_3_1_t2v_lite", False
    if tier == "quality":
        return ("veo_3_1_t2v_portrait" if is_portrait else "veo_3_1_t2v"), False
    return (
        "veo_3_1_t2v_fast_portrait_ultra" if is_portrait else "veo_3_1_t2v_fast_ultra"
    ), False


# ── Request Payload Builders ─────────────────────────────────────────────────


def build_client_context(
    project_id: str | None = None,
    *,
    recaptcha_token: str | None = None,
    collection_id: str | None = None,
    workflow_id: str | None = None,
) -> list[Any]:
    token_msg = jspb_msg({1: recaptcha_token, 2: 1}) if recaptcha_token else None
    return jspb_msg(
        {
            2: FLOW_TOOL_ID,
            5: collection_id,
            6: project_id,
            8: workflow_id,
            11: token_msg,
        }
    )


def build_structured_prompt(prompt: str) -> list[Any]:
    """Build Closure JSPB structured prompt (`[[[prompt]]]`)."""
    return [[[prompt]]]


def build_create_project_req(title: str) -> list[Any]:
    return jspb_msg(
        {
            1: "projects/*",
            2: jspb_msg({2: jspb_msg({1: title})}),
            3: build_client_context(),
        }
    )


def build_rename_project_req(project_id: str, new_title: str) -> list[Any]:
    return jspb_msg(
        {
            1: f"projects/{project_id}",
            2: jspb_msg({1: new_title}),
            3: [["project_title"]],
            4: build_client_context(),
        }
    )


def build_upload_image_req(
    project_id: str,
    image_bytes: bytes,
    *,
    mime_type: str = "image/png",
    file_name: str = "upload.png",
    recaptcha_token: str | None = None,
    is_hidden: bool = False,
) -> list[Any]:
    ctx = build_client_context(project_id, recaptcha_token=recaptcha_token)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    wf_seed = str(uuid.uuid4())
    media_seed = str(uuid.uuid4())
    return jspb_msg(
        {
            1: ctx,
            2: b64,
            3: mime_type,
            4: True,
            8: is_hidden,
            9: file_name,
            11: wf_seed,
            12: media_seed,
            14: [1],
        }
    )


def build_generate_image_req(
    project_id: str,
    prompt: str,
    *,
    model_key: str = "GEM_PIX_2",
    aspect_ratio: str = "16:9",
    count: int = 1,
    reference_media_ids: list[str] | None = None,
    base_image_media_id: str | None = None,
    recaptcha_token: str | None = None,
    seed: int | None = None,
    async_mode: bool = False,
) -> tuple[str, list[Any]]:
    if not 1 <= count <= 4:
        raise ValidationError("Image generation count must be between 1 and 4.")
    aspect_code = resolve_image_aspect(aspect_ratio)
    ctx = build_client_context(project_id, recaptcha_token=recaptcha_token)
    ql = build_structured_prompt(prompt)

    refs: list[Any] = []
    if base_image_media_id:
        refs.append(jspb_msg({1: base_image_media_id, 5: 2}))
    for ref_id in reference_media_ids or []:
        refs.append(jspb_msg({1: ref_id, 5: 1}))

    batch_info = jspb_msg({1: str(uuid.uuid4())})
    items: list[Any] = []
    for i in range(count):
        item_seed = (seed + i) if seed is not None else random.randint(1, 2_147_483_646)
        media_uuid = str(uuid.uuid4())
        items.append(
            jspb_msg(
                {
                    3: refs if refs else [],
                    4: item_seed,
                    5: aspect_code,
                    6: model_key,
                    8: ctx,
                    9: ql,
                    14: media_uuid,
                }
            )
        )

    if async_mode:
        return RPC_GENERATE_IMAGE_ASYNC, jspb_msg({1: items, 3: ctx, 4: batch_info})
    return RPC_GENERATE_IMAGE, jspb_msg({2: items, 3: True, 4: ctx, 5: batch_info})


def _build_frame_input(media_id: str, crop: dict[str, float] | None = None) -> list[Any]:
    crop_pb = None
    if crop:
        crop_pb = jspb_msg(
            {
                1: crop.get("top", 0.0),
                2: crop.get("left", 0.0),
                3: crop.get("bottom", 1.0),
                4: crop.get("right", 1.0),
            }
        )
    return jspb_msg({2: media_id, 6: crop_pb})


def _build_dest_metadata(
    media_uuid: str,
    *,
    scene_id: str | None = None,
    workflow_id: str | None = None,
    collection_id: str | None = None,
    workflow_seed_uuid: str | None = None,
) -> list[Any]:
    return jspb_msg(
        {
            1: scene_id,
            2: None if workflow_seed_uuid else workflow_id,
            3: collection_id,
            5: media_uuid,
            6: workflow_seed_uuid,
        }
    )


def build_generate_video_req(
    project_id: str,
    prompt: str,
    *,
    model: str = "fast",
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    duration_seconds: int = 8,
    count: int = 1,
    start_frame_media_id: str | None = None,
    end_frame_media_id: str | None = None,
    reference_media_ids: list[str] | None = None,
    extend_video_media_id: str | None = None,
    extend_start_frame: int | None = None,
    extend_end_frame: int | None = None,
    upsample_video_media_id: str | None = None,
    scene_id: str | None = None,
    scene_position: int | None = None,
    workflow_id: str | None = None,
    recaptcha_token: str | None = None,
    seed: int | None = None,
    valid_video_model_keys: set[str] | frozenset[str] | None = None,
) -> tuple[str, list[Any], list[str]]:
    """Build the appropriate Boq video generation RPC & payload.

    Returns ``(rpc_id, payload, expected_media_uuids)``.
    """
    if not 1 <= count <= 4:
        raise ValidationError("Video generation count must be between 1 and 4.")
    if reference_media_ids and len(reference_media_ids) > 3:
        raise ValidationError("reference_media_ids supports at most 3 reference images.")

    aspect_code, is_portrait = resolve_video_aspect(aspect_ratio)
    res_code = resolve_video_resolution(resolution)
    res_msg = jspb_msg({1: res_code}) if res_code != 1 else None

    if upsample_video_media_id:
        mode = "UPSAMPLE_VIDEO"
    elif extend_video_media_id:
        mode = "EXTEND_VIDEO"
    elif start_frame_media_id and end_frame_media_id:
        mode = "START_END_FRAMES"
    elif start_frame_media_id:
        mode = "START_FRAME"
    elif reference_media_ids:
        mode = "REFERENCES"
    else:
        mode = "TEXT"

    model_key, is_omni = resolve_video_model_key(
        model,
        mode=mode,
        is_portrait=is_portrait,
        duration_seconds=duration_seconds,
        extra_valid_keys=valid_video_model_keys,
    )

    ql = build_structured_prompt(prompt) if prompt else None
    text_input = jspb_msg({3: ql}) if ql else None
    ctx = build_client_context(project_id, recaptcha_token=recaptcha_token)

    scene_dest = jspb_msg({1: scene_id, 2: scene_position or 0}) if scene_id is not None else None
    batch_meta = jspb_msg({1: str(uuid.uuid4()), 3: scene_dest})

    media_uuids = [str(uuid.uuid4()) for _ in range(count)]
    items: list[Any] = []

    for i, m_uuid in enumerate(media_uuids):
        wf_seed = str(uuid.uuid4()) if (workflow_id or scene_id) else None
        dest = _build_dest_metadata(
            m_uuid,
            scene_id=scene_id,
            workflow_id=workflow_id,
            workflow_seed_uuid=wf_seed,
        )
        item_seed = (seed + i) if seed is not None else random.randint(1, 2_147_483_646)

        if is_omni:
            omni_prompt = jspb_msg({1: prompt.strip(), 3: ql}) if prompt.strip() else None
            first_f = _build_frame_input(start_frame_media_id) if start_frame_media_id else None
            last_f = _build_frame_input(end_frame_media_id) if end_frame_media_id else None
            refs = [_build_frame_input(rid) for rid in (reference_media_ids or [])]
            src_vid = (
                jspb_msg({2: extend_video_media_id, 3: extend_start_frame, 4: extend_end_frame})
                if extend_video_media_id
                else None
            )
            items.append(
                jspb_msg(
                    {
                        2: model_key,
                        3: omni_prompt,
                        4: first_f,
                        5: last_f,
                        6: src_vid,
                        7: refs,
                        11: dest,
                        12: aspect_code,
                    }
                )
            )
            rpc_id = RPC_VIDEO_EXPERIMENTAL
        elif mode == "UPSAMPLE_VIDEO":
            items.append(
                jspb_msg(
                    {
                        1: jspb_msg({2: upsample_video_media_id}),
                        3: aspect_code,
                        4: item_seed,
                        5: dest,
                        7: res_code,
                        32: model_key,
                    }
                )
            )
            rpc_id = RPC_VIDEO_UPSAMPLE
        elif mode == "EXTEND_VIDEO":
            refs = [_build_frame_input(rid) for rid in (reference_media_ids or [])]
            src_vid = jspb_msg(
                {
                    2: extend_video_media_id,
                    3: extend_start_frame,
                    4: extend_end_frame,
                }
            )
            items.append(
                jspb_msg(
                    {
                        1: src_vid,
                        2: text_input,
                        3: model_key,
                        4: aspect_code,
                        6: dest,
                        9: refs,
                        13: res_msg,
                    }
                )
            )
            rpc_id = RPC_VIDEO_EXTEND
        elif mode == "START_END_FRAMES":
            items.append(
                jspb_msg(
                    {
                        1: text_input,
                        2: model_key,
                        3: aspect_code,
                        5: _build_frame_input(start_frame_media_id or ""),
                        6: _build_frame_input(end_frame_media_id or ""),
                        7: dest,
                    }
                )
            )
            rpc_id = RPC_VIDEO_START_END_FRAMES
        elif mode == "START_FRAME":
            items.append(
                jspb_msg(
                    {
                        1: text_input,
                        2: model_key,
                        3: aspect_code,
                        5: _build_frame_input(start_frame_media_id or ""),
                        6: dest,
                        10: res_msg,
                    }
                )
            )
            rpc_id = RPC_VIDEO_START_FRAME
        elif mode == "REFERENCES":
            refs = [_build_frame_input(rid) for rid in (reference_media_ids or [])]
            items.append(
                jspb_msg(
                    {
                        1: text_input,
                        2: refs,
                        3: model_key,
                        4: aspect_code,
                        6: dest,
                        12: res_msg,
                    }
                )
            )
            rpc_id = RPC_VIDEO_REFERENCES
        else:
            items.append(
                jspb_msg(
                    {
                        1: text_input,
                        2: model_key,
                        3: aspect_code,
                        5: dest,
                        8: res_msg,
                    }
                )
            )
            rpc_id = RPC_VIDEO_TEXT

    return rpc_id, jspb_msg({1: items, 2: ctx, 3: batch_meta}), media_uuids


def build_poll_status_req(media_ids: list[str]) -> list[Any]:
    return jspb_msg({3: [jspb_msg({1: mid}) for mid in media_ids]})


def build_save_video_frame_req(
    project_id: str,
    video_media_id: str,
    timestamp_seconds: float,
    *,
    file_name: str = "captured_frame.png",
) -> list[Any]:
    return jspb_msg(
        {
            1: video_media_id,
            2: duration_to_pb(max(0.0, timestamp_seconds)),
            4: str(uuid.uuid4()),
            5: str(uuid.uuid4()),
            7: [1],
            8: file_name,
        }
    )


# ── Scene Builder Payload Builders ───────────────────────────────────────────


def build_create_scene_req(
    project_id: str,
    title: str | None = None,
    *,
    aspect_ratio: str = "16:9",
    workflow_ids: list[str] | None = None,
    collection_id: str | None = None,
) -> list[Any]:
    return jspb_msg(
        {
            1: f"projects/{project_id}",
            2: workflow_ids or [],
            3: collection_id,
            4: title,
            5: resolve_scene_aspect(aspect_ratio),
        }
    )


def build_get_scene_req(project_id: str, scene_id: str) -> list[Any]:
    return jspb_msg({1: scene_id, 2: project_id})


def build_add_scene_clips_req(
    project_id: str,
    scene_id: str,
    workflow_ids: list[str],
    *,
    position: int | None = None,
    start_time_seconds: float | None = None,
    end_time_seconds: float | None = None,
) -> list[Any]:
    return jspb_msg(
        {
            1: project_id,
            2: scene_id,
            3: workflow_ids,
            4: position,
            5: duration_to_pb(start_time_seconds) if start_time_seconds is not None else None,
            6: duration_to_pb(end_time_seconds) if end_time_seconds is not None else None,
        }
    )


def build_scene_clip_pb(clip: SceneClip) -> list[Any]:
    workflow_ref = jspb_msg(
        {
            1: clip.workflow_id,
            4: jspb_msg({5: clip.primary_media_id}) if clip.primary_media_id else None,
        }
    )
    timing = jspb_msg(
        {
            1: clip.position,
            2: duration_to_pb(clip.duration_seconds) if clip.duration_seconds is not None else None,
            3: duration_to_pb(clip.start_time_seconds) if clip.start_time_seconds is not None else None,
            4: duration_to_pb(clip.end_time_seconds) if clip.end_time_seconds is not None else None,
        }
    )
    return jspb_msg(
        {
            1: workflow_ref,
            2: clip.scene_id,
            3: timing,
        }
    )


def build_set_scene_clips_req(
    project_id: str,
    scene_id: str,
    clips: list[SceneClip],
) -> list[Any]:
    reindexed = [
        SceneClip(
            clip_id=c.clip_id,
            scene_id=scene_id,
            workflow_id=c.workflow_id,
            primary_media_id=c.primary_media_id,
            position=idx,
            duration_seconds=c.duration_seconds,
            start_time_seconds=c.start_time_seconds,
            end_time_seconds=c.end_time_seconds,
        )
        for idx, c in enumerate(clips)
    ]
    return jspb_msg(
        {
            1: project_id,
            2: scene_id,
            3: [build_scene_clip_pb(c) for c in reindexed],
        }
    )


def build_rename_scene_req(project_id: str, scene_id: str, new_title: str) -> list[Any]:
    return jspb_msg(
        {
            1: project_id,
            2: scene_id,
            3: jspb_msg({1: scene_id, 2: new_title}),
            4: [["display_name"]],
        }
    )


def build_export_scene_start_req(clips: list[SceneClip]) -> list[Any]:
    """Build `GjQLTd` request to stitch & render Scene Builder clips into a single MP4.

    Per `erb`/`frb`/`grb` (`VOa` / `hrb` in `js_5.js`), each clip item sets:
    - field 1: `primary_media_id`
    - field 3: `start_time` (`google.protobuf.Duration`)
    - field 4: `end_time` (`google.protobuf.Duration`)
    """
    items: list[Any] = []
    for c in clips:
        if not c.primary_media_id:
            continue
        dur_default = (
            c.duration_seconds
            if (c.duration_seconds is not None and c.duration_seconds > 0)
            else 8.0
        )
        end_sec = (
            c.end_time_seconds
            if (c.end_time_seconds is not None and c.end_time_seconds > 0)
            else dur_default
        )
        start_sec = max(
            0.0,
            min(
                c.start_time_seconds if c.start_time_seconds is not None else 0.0,
                end_sec,
            ),
        )
        items.append(
            jspb_msg(
                {
                    1: c.primary_media_id,
                    3: duration_to_pb(start_sec),
                    4: duration_to_pb(end_sec),
                }
            )
        )
    if not items:
        raise ValidationError("Scene has no clips with primary_media_id to export.")
    return jspb_msg({1: items})


# ── Response Parsers ─────────────────────────────────────────────────────────


def parse_user_account(raw: Any) -> UserAccount:
    text = str(raw)
    import re

    emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    uid = None
    try:
        uid = raw[0][0][2][0]
    except (IndexError, KeyError, TypeError):
        pass
    name = None
    photo = None
    urls = re.findall(r"https://lh3\.googleusercontent\.com/[^\s\"'\\]+", text)
    if urls:
        photo = urls[0]
    return UserAccount(
        user_id=str(uid) if uid else None,
        email=emails[0] if emails else None,
        display_name=name,
        photo_url=photo,
    )


def _as_int(val: Any, default: int = 0) -> int:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val)
    if isinstance(val, str) and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return default


def parse_credits(raw: Any) -> AccountCredits:
    if not isinstance(raw, list):
        return AccountCredits(total_credits=0, subscription_credits=0)
    total = _as_int(_safe_get(raw, 0, 0), 0)
    service_tier = _safe_get(raw, 1)
    paygate_tier = _safe_get(raw, 2)
    topup = _as_int(_safe_get(raw, 4, 0), 0)
    sub_credits = _as_int(_safe_get(raw, 5, total), total)
    renew_pb = _safe_get(raw, 6)
    renew_epoch = _as_int(renew_pb[0], 0) if isinstance(renew_pb, list) and renew_pb else None
    return AccountCredits(
        total_credits=total,
        subscription_credits=sub_credits,
        topup_credits=topup,
        service_tier=int(service_tier) if isinstance(service_tier, int) else None,
        paygate_tier=int(paygate_tier) if isinstance(paygate_tier, int) else None,
        renews_at_epoch=renew_epoch,
    )


def parse_models(raw: Any) -> list[ModelVariant]:
    root = _safe_get(raw, 0)
    if not isinstance(root, list):
        return []
    out: list[ModelVariant] = []
    for idx, kind in ((4, "video"), (5, "image"), (7, "audio")):
        families = _safe_get(root, idx, [])
        if not isinstance(families, list):
            continue
        for fam in families:
            if not isinstance(fam, list) or len(fam) < 2:
                continue
            fam_title = str(_safe_get(fam, 0, kind))
            fam_id = str(_safe_get(fam, 3, fam_title.lower().replace(" ", "_")))
            usages = _safe_get(fam, 1, [])
            if not isinstance(usages, list):
                continue
            for u in usages:
                if not isinstance(u, list) or not u:
                    continue
                key = str(u[0])
                dur = _safe_get(u, 16)
                caps: list[int] = []
                cap_tree = _safe_get(u, 7)
                if isinstance(cap_tree, list):
                    _collect_ints(cap_tree, caps)
                out.append(
                    ModelVariant(
                        key=key,
                        family_id=fam_id,
                        family_title=fam_title,
                        kind=kind,
                        duration_seconds=int(dur) if isinstance(dur, int) else None,
                        capabilities=sorted(set(caps)),
                    )
                )
    return out


def _collect_ints(node: Any, acc: list[int]) -> None:
    if isinstance(node, int):
        acc.append(node)
    elif isinstance(node, list):
        for child in node:
            _collect_ints(child, acc)


def parse_project(entry: Any) -> Project | None:
    if not isinstance(entry, list) or len(entry) < 2:
        return None
    pid = _safe_get(entry, 0)
    if not isinstance(pid, str) or not pid:
        return None
    pid = pid.removeprefix("projects/").removeprefix("tools/PINHOLE/projects/")
    meta = _safe_get(entry, 1, [])
    title = "Untitled"
    created_epoch = None
    thumb = None
    cover_id = None
    if isinstance(meta, list):
        if isinstance(_safe_get(meta, 0), str):
            title = meta[0]
        ts = _safe_get(meta, 2)
        if isinstance(ts, list) and ts and isinstance(ts[0], int):
            created_epoch = ts[0]
        if isinstance(_safe_get(meta, 3), str):
            thumb = meta[3]
        if isinstance(_safe_get(meta, 4), str):
            cover_id = meta[4]
        elif isinstance(_safe_get(meta, 1), str) and len(meta) == 2:
            cover_id = meta[1]
    return Project(
        id=pid,
        title=title,
        created_at_epoch=created_epoch,
        thumbnail_url=thumb,
        cover_media_id=cover_id,
    )


def parse_project_list(raw: Any) -> list[Project]:
    items = _safe_get(raw, 0, [])
    if not isinstance(items, list):
        return []
    out: list[Project] = []
    for item in items:
        proj = parse_project(item)
        if proj:
            out.append(proj)
    return out


def parse_media_item(entry: Any) -> MediaItem | None:
    if not isinstance(entry, list) or not entry:
        return None
    mid = _safe_get(entry, 0)
    if not isinstance(mid, str) or not mid:
        return None
    project_id = _safe_get(entry, 1)
    workflow_id = _safe_get(entry, 2)

    meta = _safe_get(entry, 5, [])
    created_epoch = None
    prompt = None
    thumb_url = None
    model_key = None
    status = "completed"
    err_msg = None

    spec_aspect_code: int | None = None
    spec_res_code: int | None = None

    if isinstance(meta, list):
        ts = _safe_get(meta, 0)
        if isinstance(ts, list) and ts and isinstance(ts[0], int):
            created_epoch = ts[0]
        if isinstance(_safe_get(meta, 1), str):
            prompt = meta[1]
        if isinstance(_safe_get(meta, 5), str) and meta[5].startswith("http"):
            thumb_url = meta[5]
        if isinstance(_safe_get(meta, 10), str) and meta[10].startswith("http"):
            thumb_url = meta[10]
        gen_info = _safe_get(meta, 6)
        if isinstance(gen_info, list):
            spec = _safe_get(gen_info, 1)
            if isinstance(spec, list) and spec and isinstance(spec[0], list) and spec[0]:
                if isinstance(spec[0][0], str):
                    model_key = spec[0][0]
                raw_asp = _safe_get(spec[0], 4)
                if isinstance(raw_asp, int) and not isinstance(raw_asp, bool):
                    spec_aspect_code = raw_asp
                raw_res = _safe_get(spec[0], 5)
                if isinstance(raw_res, int) and not isinstance(raw_res, bool):
                    spec_res_code = raw_res
                elif isinstance(raw_res, list) and raw_res and isinstance(raw_res[0], int):
                    spec_res_code = raw_res[0]
            if not prompt:
                p_list = _safe_get(gen_info, 2)
                if isinstance(p_list, list) and p_list and isinstance(p_list[0], list) and p_list[0]:
                    if isinstance(p_list[0][0], str):
                        prompt = p_list[0][0]
        # Status info at meta[8] (`field 9`) when pending / running / failed
        state_box = _safe_get(meta, 8)
        if isinstance(state_box, list) and state_box:
            code = _safe_get(state_box, 0)
            if code in (1, 2, 6):
                status = "running"
            elif code in (4, 5, 7):
                status = "failed"
                if isinstance(_safe_get(state_box, 1), str):
                    err_msg = state_box[1]

    img_box = _safe_get(entry, 6)
    vid_box = _safe_get(entry, 7)

    image_url = None
    video_url = None
    mime_type = None
    width = None
    height = None
    duration_sec = None
    media_type = "pending" if status == "running" else ("failed" if status == "failed" else "unknown")

    if isinstance(img_box, list):
        media_type = "image"
        sub = _safe_get(img_box, 1)
        if isinstance(sub, list):
            if isinstance(_safe_get(sub, 3), str):
                image_url = sub[3]
            if isinstance(_safe_get(sub, 6), str):
                mime_type = sub[6]
        dims = _safe_get(img_box, 2)
        if isinstance(dims, list) and len(dims) >= 2:
            if isinstance(dims[0], int):
                width = dims[0]
            if isinstance(dims[1], int):
                height = dims[1]
        alt = _safe_get(img_box, 5)
        if not image_url and isinstance(alt, list) and isinstance(_safe_get(alt, 1), str):
            image_url = alt[1]
        if not image_url and thumb_url and "flow-content.google/image/" in thumb_url:
            image_url = thumb_url

    # Provenance of width/height: real values present in the response are "reported"; values derived
    # below from the aspect-ratio / resolution enum codes are flagged "inferred_from_aspect".
    dims_source: str | None = "reported" if (width is not None and height is not None) else None
    if isinstance(vid_box, list) and vid_box:
        media_type = "video"
        vid_aspect_code = spec_aspect_code
        v0 = _safe_get(vid_box, 0)
        if isinstance(v0, list):
            if not prompt and isinstance(_safe_get(v0, 7), str):
                prompt = v0[7]
            if isinstance(_safe_get(v0, 8), str) and v0[8].startswith("http"):
                video_url = v0[8]
                mime_type = "video/mp4"
            if not model_key and isinstance(_safe_get(v0, 12), str):
                model_key = v0[12]
            raw_v_asp = _safe_get(v0, 16)
            if isinstance(raw_v_asp, int) and not isinstance(raw_v_asp, bool):
                vid_aspect_code = raw_v_asp
        v_meta = _safe_get(vid_box, 1)
        if isinstance(v_meta, list):
            dur_pb = _safe_get(v_meta, 2)
            if isinstance(dur_pb, list) and dur_pb and isinstance(dur_pb[0], (int, float)):
                nanos = float(dur_pb[1]) / 1e9 if len(dur_pb) > 1 and isinstance(dur_pb[1], (int, float)) else 0.0
                duration_sec = round(float(dur_pb[0]) + nanos, 2)
        if width is None and height is None and vid_aspect_code in (0, 1, 2):
            dims_source = "inferred_from_aspect"
            eff_res = spec_res_code
            if eff_res not in (1, 2, 3, 4):
                low_mk = (model_key or "").lower()
                if "4k" in low_mk:
                    eff_res = 3
                elif "1080p" in low_mk:
                    eff_res = 2
                else:
                    eff_res = 1
            long_short_map = {1: (1280, 720), 2: (1920, 1080), 3: (3840, 2160), 4: (640, 360)}
            long_side, short_side = long_short_map[eff_res]
            if vid_aspect_code == 1:  # 9:16 portrait
                width, height = short_side, long_side
            elif vid_aspect_code == 2:  # 16:9 landscape
                width, height = long_side, short_side
            else:  # 1:1 square
                width, height = short_side, short_side

    if status == "completed" and not (image_url or video_url):
        # Still rendering if neither image_url nor video_url has materialized yet
        if status != "failed":
            status = "running"
            if media_type == "unknown":
                media_type = "pending"

    return MediaItem(
        id=mid,
        project_id=project_id if isinstance(project_id, str) else None,
        workflow_id=workflow_id if isinstance(workflow_id, str) else None,
        media_type=media_type,
        status=status,
        prompt=prompt,
        model_key=model_key,
        mime_type=mime_type,
        width=width,
        height=height,
        dimensions_source=dims_source,
        duration_seconds=duration_sec,
        image_url=image_url,
        video_url=video_url,
        thumbnail_url=thumb_url,
        created_at_epoch=created_epoch,
        error_message=err_msg,
    )


def parse_generation_response(raw: Any) -> list[MediaItem]:
    """Parse media items from image (`ogiZ0b`/`Z9cAtf`), video (`*`), or poll (`jwpduf`) RPCs."""
    if not isinstance(raw, list):
        return []
    # Check candidate repeated MediaItem slots:
    # - Poll (`jwpduf`) / Image (`ogiZ0b`): field 1 -> raw[0]
    # - Video (`YhhmEf` etc.): field 4 -> raw[3] (or raw[0])
    # - Upload / Save frame (`maseQ`/`jeNy9c`): single MediaItem at raw[0]
    for candidate_idx in (3, 0, 1):
        slot = _safe_get(raw, candidate_idx)
        if isinstance(slot, list) and slot:
            # Is `slot` itself a single MediaItem `[uuid_str, project_id, ...]`?
            if isinstance(slot[0], str) and len(slot) >= 6:
                parsed = parse_media_item(slot)
                if parsed:
                    return [parsed]
            # Or a repeated list of MediaItems `[[uuid_str, ...], ...]`?
            if isinstance(slot[0], list):
                items = [parse_media_item(entry) for entry in slot]
                valid = [it for it in items if it is not None]
                if valid:
                    return valid
    return []


def parse_workflow(entry: Any) -> WorkflowItem | None:
    if not isinstance(entry, list) or len(entry) < 4:
        return None
    wid = _safe_get(entry, 0)
    if not isinstance(wid, str) or not wid:
        return None
    meta = _safe_get(entry, 3, [])
    pid = str(_safe_get(entry, 4, ""))
    title = "Untitled clip"
    primary_mid = None
    c_epoch = None
    u_epoch = None
    if isinstance(meta, list):
        if isinstance(_safe_get(meta, 0), str):
            title = meta[0]
        ts1 = _safe_get(meta, 1)
        if isinstance(ts1, list) and ts1 and isinstance(ts1[0], int):
            c_epoch = ts1[0]
        if isinstance(_safe_get(meta, 4), str):
            primary_mid = meta[4]
        ts2 = _safe_get(meta, 6)
        if isinstance(ts2, list) and ts2 and isinstance(ts2[0], int):
            u_epoch = ts2[0]
    return WorkflowItem(
        id=wid,
        project_id=pid,
        title=title,
        primary_media_id=primary_mid,
        created_at_epoch=c_epoch,
        updated_at_epoch=u_epoch,
    )


def parse_scene_clip(entry: Any) -> SceneClip | None:
    if not isinstance(entry, list) or len(entry) < 2:
        return None
    wf_box = _safe_get(entry, 0)
    scene_id = _safe_get(entry, 1)
    timing_box = _safe_get(entry, 2, [])
    if not isinstance(wf_box, list) or not isinstance(scene_id, str):
        return None
    workflow_id = _safe_get(wf_box, 0)
    if not isinstance(workflow_id, str):
        return None
    primary_media_id = None
    wf_meta = _safe_get(wf_box, 3)
    if isinstance(wf_meta, list) and isinstance(_safe_get(wf_meta, 4), str) and wf_meta[4]:
        primary_media_id = wf_meta[4]

    pos = 0
    dur = None
    start_t = None
    end_t = None
    if isinstance(timing_box, list):
        if isinstance(_safe_get(timing_box, 0), int):
            pos = timing_box[0]
        dur = pb_to_seconds(_safe_get(timing_box, 1))
        start_t = pb_to_seconds(_safe_get(timing_box, 2))
        end_t = pb_to_seconds(_safe_get(timing_box, 3))

    return SceneClip(
        clip_id=f"{scene_id}:{workflow_id}:{pos}",
        scene_id=scene_id,
        workflow_id=workflow_id,
        primary_media_id=primary_media_id,
        position=pos,
        duration_seconds=dur,
        start_time_seconds=start_t,
        end_time_seconds=end_t,
    )


def parse_scene(entry: Any, project_id: str, all_clips: list[SceneClip]) -> Scene | None:
    if not isinstance(entry, list) or not entry:
        return None
    sid = _safe_get(entry, 0)
    if not isinstance(sid, str) or not sid:
        return None
    title = str(_safe_get(entry, 1, "Untitled scene") or "Untitled scene")
    aspect_code = _safe_get(entry, 5)
    aspect = "9:16" if aspect_code == 1 else "16:9"
    scene_clips = sorted(
        [c for c in all_clips if c.scene_id == sid],
        key=lambda c: c.position,
    )
    return Scene(
        id=sid,
        project_id=project_id,
        title=title,
        aspect_ratio=aspect,
        clips=scene_clips,
    )


def parse_project_contents(project_id: str, raw: Any) -> ProjectContents:
    if not isinstance(raw, list):
        return ProjectContents(project_id=project_id)
    workflows = [
        w for w in (parse_workflow(e) for e in (_safe_get(raw, 1, []) or [])) if w is not None
    ]
    wf_primary = {w.id: w.primary_media_id for w in workflows if w.primary_media_id}
    media = [
        m for m in (parse_media_item(e) for e in (_safe_get(raw, 2, []) or [])) if m is not None
    ]
    media_by_id = {m.id: m for m in media}
    wf_latest_media: dict[str, MediaItem] = {}
    for m in media:
        if m.workflow_id:
            wf_latest_media[m.workflow_id] = m
    raw_clips = [
        c for c in (parse_scene_clip(e) for e in (_safe_get(raw, 6, []) or [])) if c is not None
    ]
    # Enrich clips with workflow's primary_media_id & duration when clip only carries workflow_id
    enriched_clips: list[SceneClip] = []
    for c in raw_clips:
        pmid = (
            c.primary_media_id
            or wf_primary.get(c.workflow_id)
            or (wf_latest_media[c.workflow_id].id if c.workflow_id in wf_latest_media else None)
        )
        m_obj = media_by_id.get(pmid) if pmid else wf_latest_media.get(c.workflow_id)
        enriched_clips.append(
            SceneClip(
                clip_id=c.clip_id,
                scene_id=c.scene_id,
                workflow_id=c.workflow_id,
                primary_media_id=pmid,
                position=c.position,
                duration_seconds=c.duration_seconds or (m_obj.duration_seconds if m_obj else None),
                start_time_seconds=c.start_time_seconds,
                end_time_seconds=c.end_time_seconds,
            )
        )
    scenes = [
        s
        for s in (
            parse_scene(e, project_id, enriched_clips) for e in (_safe_get(raw, 4, []) or [])
        )
        if s is not None
    ]
    return ProjectContents(
        project_id=project_id,
        workflows=workflows,
        media=media,
        scenes=scenes,
    )


def parse_create_or_get_scene(project_id: str, raw: Any) -> Scene | None:
    """Parse `rqZuUc` (`RPC_CREATE_SCENE`) / `O2COMe` (`[scene_entry, clip_entries]`) response."""
    if not isinstance(raw, list) or not raw:
        return None
    scene_entry = _safe_get(raw, 0)
    clip_entries = _safe_get(raw, 1, []) or []
    clips = [c for c in (parse_scene_clip(e) for e in clip_entries) if c is not None]
    if isinstance(scene_entry, list) and scene_entry and isinstance(scene_entry[0], str):
        return parse_scene(scene_entry, project_id, clips)
    return None


def parse_get_scene_response(
    project_id: str,
    scene_id: str,
    raw: Any,
    *,
    title: str = "Untitled scene",
    aspect_ratio: str = "16:9",
) -> Scene | None:
    """Parse `uwAyfb` (`RPC_GET_SCENE`) response (`[clip_entries, media_entries]`, `_.zB` in `js_5.js`)."""
    if not isinstance(raw, list):
        return None
    # If caller happened to receive `[scene_entry, clip_entries]` format, handle it too
    direct = parse_create_or_get_scene(project_id, raw)
    if direct is not None:
        return direct

    clip_entries = _safe_get(raw, 0, []) or []
    media_entries = _safe_get(raw, 1, []) or []
    if not isinstance(clip_entries, list):
        return None

    raw_clips = [c for c in (parse_scene_clip(e) for e in clip_entries) if c is not None]
    media_items = [
        m for m in (parse_media_item(e) for e in (media_entries if isinstance(media_entries, list) else []))
        if m is not None
    ]
    if not raw_clips and not media_items:
        return None

    media_by_id = {m.id: m for m in media_items}
    wf_latest_media: dict[str, MediaItem] = {}
    for m in media_items:
        if m.workflow_id:
            wf_latest_media[m.workflow_id] = m

    enriched_clips: list[SceneClip] = []
    for c in sorted(raw_clips, key=lambda item: item.position):
        pmid = c.primary_media_id or (
            wf_latest_media[c.workflow_id].id if c.workflow_id in wf_latest_media else None
        )
        m_obj = media_by_id.get(pmid) if pmid else wf_latest_media.get(c.workflow_id)
        enriched_clips.append(
            SceneClip(
                clip_id=c.clip_id,
                scene_id=c.scene_id or scene_id,
                workflow_id=c.workflow_id,
                primary_media_id=pmid,
                position=c.position,
                duration_seconds=c.duration_seconds or (m_obj.duration_seconds if m_obj else None),
                start_time_seconds=c.start_time_seconds,
                end_time_seconds=c.end_time_seconds,
            )
        )

    return Scene(
        id=scene_id,
        project_id=project_id,
        title=title,
        aspect_ratio=aspect_ratio,
        clips=enriched_clips,
    )
