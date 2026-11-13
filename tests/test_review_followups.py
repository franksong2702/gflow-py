"""Regression tests for Carbon review findings on the portrait/idempotency PR."""

from __future__ import annotations

from gflow._proto import resolve_video_model_key
from gflow.mcp.server import _params_fingerprint


def test_omni_portrait_known_catalog_without_portrait_key_falls_back_to_base() -> None:
    # Catalog fetched but has no *_portrait variant (even if empty) -> base key, never an unlisted key.
    for catalog in (set(), {"macaron_cookie_t2v_8s"}):
        key, _ = resolve_video_model_key(
            "omni", mode="TEXT", is_portrait=True, duration_seconds=8, extra_valid_keys=catalog
        )
        assert key == "macaron_cookie_t2v_8s"


def test_omni_portrait_catalog_with_portrait_key_and_offline() -> None:
    key, _ = resolve_video_model_key(
        "omni", mode="TEXT", is_portrait=True, duration_seconds=8,
        extra_valid_keys={"macaron_cookie_t2v_8s", "macaron_cookie_t2v_8s_portrait"},
    )
    assert key == "macaron_cookie_t2v_8s_portrait"
    offline, _ = resolve_video_model_key("omni", mode="TEXT", is_portrait=True, duration_seconds=8)
    assert offline == "macaron_cookie_t2v_8s_portrait"


def test_fingerprint_normalises_enum_like_values_but_not_prompt_or_ids() -> None:
    a = {"prompt": "A cat", "aspect_ratio": "9:16", "resolution": "720p", "model": "Omni", "project_id": "P1"}
    b = {"prompt": "A cat", "aspect_ratio": " 9:16 ", "resolution": "720P", "model": "omni", "project_id": "P1"}
    assert _params_fingerprint(a) == _params_fingerprint(b)
    assert _params_fingerprint(a) != _params_fingerprint({**a, "aspect_ratio": "16:9"})
    assert _params_fingerprint(a) != _params_fingerprint({**a, "prompt": "a cat"})
    assert _params_fingerprint(a) != _params_fingerprint({**a, "project_id": "p1"})
