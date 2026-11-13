"""Idempotency fingerprint tests for ``JobStore.submit_or_reuse`` (key reuse with a changed aspect ratio)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from gflow.exceptions import ERR_INVALID_INPUT, ValidationError
from gflow.mcp._jobs import JobStore
from gflow.mcp.server import _params_fingerprint

OP = "generate_video"

PARAMS_LANDSCAPE = {
    "prompt": "Marley walking on the beach",
    "project_id": None,
    "model": "omni",
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "duration_seconds": 4,
    "count": 1,
}
PARAMS_PORTRAIT = {**PARAMS_LANDSCAPE, "aspect_ratio": "9:16"}
FP_LANDSCAPE = _params_fingerprint(PARAMS_LANDSCAPE)
FP_PORTRAIT = _params_fingerprint(PARAMS_PORTRAIT)


class _Submitter:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.gate = gate

    async def __call__(self, job_id: str) -> dict[str, Any]:
        self.calls.append(job_id)
        if self.gate is not None:
            await self.gate.wait()
        return {"kind": "media", "project_id": "proj-1", "media_ids": [f"vid-{len(self.calls):03d}"]}


def _assert_invalid_input(exc: BaseException) -> None:
    assert isinstance(exc, ValidationError)
    assert exc.error_code == ERR_INVALID_INPUT
    assert "idempotency_key" in str(exc)


def test_params_fingerprint_is_stable_and_order_independent() -> None:
    reordered = dict(reversed(list(PARAMS_LANDSCAPE.items())))
    assert _params_fingerprint(reordered) == FP_LANDSCAPE
    assert FP_LANDSCAPE != FP_PORTRAIT
    assert _params_fingerprint(None) is None
    assert isinstance(FP_LANDSCAPE, str) and len(FP_LANDSCAPE) == 64


def test_same_key_same_params_reuses_and_replays(tmp_path: Path) -> None:
    async def main() -> None:
        store = JobStore(path=tmp_path / "jobs.json")
        submit = _Submitter()
        rec1, replay1 = await store.submit_or_reuse(
            op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE
        )
        rec2, replay2 = await store.submit_or_reuse(
            op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE
        )
        assert replay1 is False
        assert replay2 is True
        assert rec1["job_id"] == rec2["job_id"]
        assert rec1["params_fingerprint"] == FP_LANDSCAPE
        assert len(submit.calls) == 1

    asyncio.run(main())


def test_same_key_different_aspect_ratio_raises_invalid_input(tmp_path: Path) -> None:
    async def main() -> None:
        store = JobStore(path=tmp_path / "jobs.json")
        submit = _Submitter()
        await store.submit_or_reuse(op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE)
        with pytest.raises(ValidationError) as ei:
            await store.submit_or_reuse(
                op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_PORTRAIT
            )
        _assert_invalid_input(ei.value)
        assert len(submit.calls) == 1

        # A fresh key with the new params submits a new job.
        rec3, replay3 = await store.submit_or_reuse(
            op=OP, idempotency_key="k2", submit_fn=submit, params_fingerprint=FP_PORTRAIT
        )
        assert replay3 is False
        assert len(submit.calls) == 2
        assert rec3["params_fingerprint"] == FP_PORTRAIT

    asyncio.run(main())


def test_fingerprint_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"

    async def first() -> str:
        store = JobStore(path=path)
        rec, _ = await store.submit_or_reuse(
            op=OP, idempotency_key="k1", submit_fn=_Submitter(), params_fingerprint=FP_LANDSCAPE
        )
        return str(rec["job_id"])

    async def second(job_id: str) -> None:
        store = JobStore(path=path)
        submit = _Submitter()
        with pytest.raises(ValidationError) as ei:
            await store.submit_or_reuse(
                op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_PORTRAIT
            )
        _assert_invalid_input(ei.value)
        rec, replay = await store.submit_or_reuse(
            op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE
        )
        assert replay is True
        assert rec["job_id"] == job_id
        assert submit.calls == []

    jid = asyncio.run(first())
    asyncio.run(second(jid))


def test_legacy_record_without_fingerprint_is_reused(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    legacy_id = "job_legacy00000001"
    path.write_text(
        json.dumps(
            {
                "jobs": {
                    legacy_id: {
                        "job_id": legacy_id,
                        "op": OP,
                        "idempotency_key": "old-key",
                        "created_at_epoch": int(time.time()),
                        "kind": "media",
                        "media_ids": ["vid-legacy"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def main() -> None:
        store = JobStore(path=path)
        submit = _Submitter()
        rec, replay = await store.submit_or_reuse(
            op=OP, idempotency_key="old-key", submit_fn=submit, params_fingerprint=FP_PORTRAIT
        )
        assert replay is True
        assert rec["job_id"] == legacy_id
        assert submit.calls == []

    asyncio.run(main())


def test_caller_without_fingerprint_keeps_plain_reuse(tmp_path: Path) -> None:
    async def main() -> None:
        store = JobStore(path=tmp_path / "jobs.json")
        submit = _Submitter()
        rec1, _ = await store.submit_or_reuse(
            op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE
        )
        rec2, replay2 = await store.submit_or_reuse(op=OP, idempotency_key="k1", submit_fn=submit)
        assert replay2 is True
        assert rec2["job_id"] == rec1["job_id"]
        assert len(submit.calls) == 1

    asyncio.run(main())


def test_in_flight_coalescing_checks_fingerprint(tmp_path: Path) -> None:
    async def main() -> None:
        store = JobStore(path=tmp_path / "jobs.json")
        gate = asyncio.Event()
        submit = _Submitter(gate)
        first = asyncio.create_task(
            store.submit_or_reuse(op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE)
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if submit.calls:
                break
        assert submit.calls, "first submission should be in flight"

        with pytest.raises(ValidationError) as ei:
            await store.submit_or_reuse(
                op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_PORTRAIT
            )
        _assert_invalid_input(ei.value)

        second = asyncio.create_task(
            store.submit_or_reuse(op=OP, idempotency_key="k1", submit_fn=submit, params_fingerprint=FP_LANDSCAPE)
        )
        await asyncio.sleep(0)
        gate.set()
        rec1, replay1 = await first
        rec2, replay2 = await second
        assert replay1 is False
        assert replay2 is True
        assert rec1["job_id"] == rec2["job_id"]
        assert len(submit.calls) == 1
        assert not store._in_flight
        assert not store._in_flight_fp

    asyncio.run(main())
