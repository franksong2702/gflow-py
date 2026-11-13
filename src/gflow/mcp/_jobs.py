"""Persistent job registry & idempotency coordinator (`P1-4`).

Guarantees:
1. Every generation/export submission receives a stable ``job_id`` persisted to
   ``~/.gflow/jobs.json`` (`0600`) inside ``asyncio.shield`` — even if the remote
   MCP client disconnects mid-request, the ``job_id`` and ``idempotency_key`` are saved.
2. Duplicate submissions carrying the same ``(op, idempotency_key)`` coalesce onto any
   in-flight submission task OR return the already-committed ``job_id`` without firing
   a second Google Flow RPC or charging duplicate credits.
3. Reusing an ``idempotency_key`` with *different* output-affecting parameters (tracked via a
   ``params_fingerprint``) is rejected with ``ValidationError`` (``INVALID_INPUT``) instead of silently
   replaying a job generated from other parameters (e.g. 16:9 vs 9:16).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..exceptions import NotFoundError, ValidationError
from ..paths import _atomic_write_json, get_home_dir

JOB_RETENTION_SECONDS = 7 * 86400  # 7 days
MAX_STORED_JOBS = 2000


class JobStore:
    """File-backed (`0600`) job & idempotency registry with single-flight coalescing."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (get_home_dir() / "jobs.json")
        self._lock = asyncio.Lock()
        self._in_flight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._in_flight_fp: dict[str, str | None] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idem_index: dict[str, str] = {}
        self._load()

    def _idem_composite(self, op: str, key: str) -> str:
        return f"{op}:{key.strip()}"

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            jobs = raw.get("jobs", {})
            if isinstance(jobs, dict):
                now = time.time()
                for jid, rec in jobs.items():
                    if isinstance(rec, dict) and now - float(rec.get("created_at_epoch", now)) <= JOB_RETENTION_SECONDS:
                        self._jobs[str(jid)] = rec
                        ik = rec.get("idempotency_key")
                        op = rec.get("op")
                        if isinstance(ik, str) and ik and isinstance(op, str) and op:
                            self._idem_index[self._idem_composite(op, ik)] = str(jid)
        except (OSError, ValueError):
            pass

    def _prune_and_persist_unlocked(self) -> None:
        now = time.time()
        expired = [
            jid
            for jid, rec in self._jobs.items()
            if now - float(rec.get("created_at_epoch", now)) > JOB_RETENTION_SECONDS
        ]
        for jid in expired:
            rec = self._jobs.pop(jid, None)
            if rec and rec.get("idempotency_key") and rec.get("op"):
                self._idem_index.pop(self._idem_composite(rec["op"], rec["idempotency_key"]), None)

        while len(self._jobs) > MAX_STORED_JOBS:
            oldest = min(self._jobs, key=lambda k: float(self._jobs[k].get("created_at_epoch", 0)))
            rec = self._jobs.pop(oldest, None)
            if rec and rec.get("idempotency_key") and rec.get("op"):
                self._idem_index.pop(self._idem_composite(rec["op"], rec["idempotency_key"]), None)

        _atomic_write_json(self._path, {"jobs": self._jobs}, mode=0o600)

    @staticmethod
    def _check_fingerprint(
        *,
        op: str,
        key: str | None,
        stored: Any,
        incoming: str | None,
        job_id: str | None,
    ) -> None:
        """Reject reuse of ``key`` when both fingerprints are known and differ.

        Legacy records without a stored fingerprint, or callers that pass none, keep plain reuse semantics.
        """
        if isinstance(stored, str) and stored and incoming and stored != incoming:
            where = f"job {job_id!r}" if job_id else "an in-flight submission"
            raise ValidationError(
                f"idempotency_key {key!r} was already used for {op!r} with different parameters ({where}); "
                "use a new idempotency_key when changing prompt/model/aspect_ratio/etc. "
                "(idempotency_key 已用于不同参数，请换新 key)"
            )

    async def submit_or_reuse(
        self,
        *,
        op: str,
        idempotency_key: str | None,
        submit_fn: Callable[[str], Awaitable[dict[str, Any]]],
        params_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Execute ``submit_fn(job_id)`` once, or return existing record if ``idempotency_key`` matches.

        When ``params_fingerprint`` is given and the same ``(op, idempotency_key)`` was previously used
        (committed or in-flight) with a different fingerprint, raises :class:`ValidationError`
        (``INVALID_INPUT``) instead of replaying the old job.

        Returns ``(job_record, is_idempotent_replay)``.
        """
        cleaned_key = (idempotency_key or "").strip() or None
        comp_key = self._idem_composite(op, cleaned_key) if cleaned_key else None
        fingerprint = (params_fingerprint or "").strip() or None

        existing_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._lock:
            if comp_key is not None:
                if comp_key in self._idem_index:
                    jid = self._idem_index[comp_key]
                    if jid in self._jobs:
                        self._check_fingerprint(
                            op=op,
                            key=cleaned_key,
                            stored=self._jobs[jid].get("params_fingerprint"),
                            incoming=fingerprint,
                            job_id=jid,
                        )
                        return dict(self._jobs[jid]), True
                existing_task = self._in_flight.get(comp_key)
                if existing_task is not None:
                    self._check_fingerprint(
                        op=op,
                        key=cleaned_key,
                        stored=self._in_flight_fp.get(comp_key),
                        incoming=fingerprint,
                        job_id=None,
                    )

            if existing_task is None:
                job_id = f"job_{uuid.uuid4().hex[:16]}"

                async def _runner() -> dict[str, Any]:
                    try:
                        created_meta = await submit_fn(job_id)
                        record: dict[str, Any] = {
                            "job_id": job_id,
                            "op": op,
                            "idempotency_key": cleaned_key,
                            "params_fingerprint": fingerprint,
                            "created_at_epoch": int(time.time()),
                            **created_meta,
                        }
                        async with self._lock:
                            self._jobs[job_id] = record
                            if comp_key is not None:
                                self._idem_index[comp_key] = job_id
                            self._prune_and_persist_unlocked()
                        return record
                    finally:
                        if comp_key is not None:
                            async with self._lock:
                                self._in_flight.pop(comp_key, None)
                                self._in_flight_fp.pop(comp_key, None)

                task = asyncio.create_task(_runner())
                if comp_key is not None:
                    self._in_flight[comp_key] = task
                    self._in_flight_fp[comp_key] = fingerprint
            else:
                task = None

        if existing_task is not None:
            rec = await asyncio.shield(existing_task)
            return dict(rec), True

        assert task is not None
        rec = await asyncio.shield(task)
        return dict(rec), False

    async def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        async with self._lock:
            if job_id not in self._jobs:
                return None
            self._jobs[job_id] = {**self._jobs[job_id], **updates}
            self._prune_and_persist_unlocked()
            return dict(self._jobs[job_id])

    def lookup(
        self,
        *,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if job_id:
            jid = job_id.strip()
            if jid in self._jobs:
                return dict(self._jobs[jid])
            raise NotFoundError(f"Job {jid!r} not found.")

        if idempotency_key:
            ik = idempotency_key.strip()
            for comp, jid in self._idem_index.items():
                if comp.endswith(f":{ik}") and jid in self._jobs:
                    return dict(self._jobs[jid])
            raise NotFoundError(f"No job found for idempotency_key {ik!r}.")

        raise NotFoundError("Neither job_id nor idempotency_key matched any stored job.")
