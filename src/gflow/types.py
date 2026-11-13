"""Typed domain models for Google Flow entities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _ts_to_iso(sec: int | float | None) -> str | None:
    if not sec:
        return None
    try:
        return datetime.fromtimestamp(float(sec), tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


@dataclass(frozen=True)
class UserAccount:
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    photo_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountCredits:
    total_credits: int
    subscription_credits: int
    topup_credits: int = 0
    service_tier: int | None = None
    paygate_tier: int | None = None
    renews_at_epoch: int | None = None

    @property
    def renews_at(self) -> str | None:
        return _ts_to_iso(self.renews_at_epoch)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["renews_at"] = self.renews_at
        return d


@dataclass(frozen=True)
class ModelVariant:
    key: str
    family_id: str
    family_title: str
    kind: str  # "video" | "image" | "audio"
    duration_seconds: int | None = None
    capabilities: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    created_at_epoch: int | None = None
    thumbnail_url: str | None = None
    cover_media_id: str | None = None

    @property
    def created_at(self) -> str | None:
        return _ts_to_iso(self.created_at_epoch)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d


@dataclass(frozen=True)
class MediaItem:
    id: str
    project_id: str | None
    workflow_id: str | None = None
    media_type: str = "unknown"  # "image" | "video" | "pending" | "failed"
    status: str = "completed"  # "pending" | "running" | "completed" | "failed"
    prompt: str | None = None
    model_key: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    image_url: str | None = None
    video_url: str | None = None
    thumbnail_url: str | None = None
    created_at_epoch: int | None = None
    error_message: str | None = None
    # Provenance of width/height: "reported" (present in Flow response), "inferred_from_aspect"
    # (derived from aspect-ratio/resolution enum codes, NOT measured), "measured_mp4" (probed from bytes).
    dimensions_source: str | None = None

    @property
    def download_url(self) -> str | None:
        return self.video_url or self.image_url or self.thumbnail_url

    @property
    def created_at(self) -> str | None:
        return _ts_to_iso(self.created_at_epoch)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["download_url"] = self.download_url
        d["created_at"] = self.created_at
        return d


@dataclass(frozen=True)
class WorkflowItem:
    id: str
    project_id: str
    title: str
    primary_media_id: str | None = None
    created_at_epoch: int | None = None
    updated_at_epoch: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SceneClip:
    clip_id: str | None
    scene_id: str
    workflow_id: str
    primary_media_id: str | None
    position: int
    duration_seconds: float | None = None
    start_time_seconds: float | None = None
    end_time_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Scene:
    id: str
    project_id: str
    title: str
    aspect_ratio: str = "16:9"
    clips: list[SceneClip] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "aspect_ratio": self.aspect_ratio,
            "clip_count": len(self.clips),
            "clips": [c.to_dict() for c in self.clips],
        }


@dataclass(frozen=True)
class ProjectContents:
    project_id: str
    workflows: list[WorkflowItem] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "workflows": [w.to_dict() for w in self.workflows],
            "media": [m.to_dict() for m in self.media],
            "scenes": [s.to_dict() for s in self.scenes],
        }
