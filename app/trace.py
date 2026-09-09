"""Agent run trace — records the pipeline as ordered, inspectable steps.

Every AD-console agent pipeline (intake → replan/options → narrate → human
gate → publish) opens one ``agent_runs`` row and appends ``agent_steps`` as
it executes, so ``GET /agents`` can show the multi-step agent thinking.

Contract:
  * Recorder failures are ALWAYS swallowed — tracing must never change
    pipeline behavior (the run is simply incomplete).
  * Pipeline failures INSIDE a step are recorded as a failed step and then
    re-raised, so the existing fallback handling still runs unchanged.
  * Step ``seq`` is monotonic within a run; terminal run statuses are
    ``ok | fallback | failed | awaiting_human | published``.
  * A crashed request leaves at most one 'running' row: the next
    ``start_run`` marks stale running rows failed (see Store.start_run).
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

KINDS = ("llm", "deterministic", "human", "io")
RUN_TERMINAL_STATUSES = ("ok", "fallback", "failed", "awaiting_human", "published")


class _StepHandle:
    """Mutable per-step fields the wrapped code may set before exiting."""

    def __init__(self) -> None:
        self.verdict = ""
        self.summary = ""
        self.status = "ok"


class RunRecorder:
    """One pipeline run: N ordered steps recorded to the store."""

    def __init__(self, store: Any, trigger: str, run_id: str | None = None):
        self._store = store
        self.trigger = trigger
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._t0 = time.perf_counter()
        self._seq = 0

    # ---------------------------------------------------------------- api
    def start(self) -> "RunRecorder":
        try:
            self._store.start_run(self.run_id, self.trigger)
        except Exception:  # noqa: BLE001 - tracing must never break the pipeline
            pass
        return self

    @contextmanager
    def step(
        self,
        name: str,
        agent: str = "",
        kind: str = "deterministic",
        model: str = "",
    ) -> Iterator[_StepHandle]:
        """Record one step around a ``with`` block.

        Body exceptions are recorded (status=failed) and re-raised so the
        route's existing fallback/error handling is untouched.
        """
        handle = _StepHandle()
        started_ms = self.elapsed_ms
        self._seq += 1
        seq = self._seq
        try:
            yield handle
        except Exception as exc:  # record, then propagate: pipeline owns errors
            handle.status = "failed"
            detail = f"{type(exc).__name__}: {exc}"
            handle.summary = (handle.summary + " | " + detail)[:500]
            self._write(seq, name, agent, kind, model, started_ms, handle)
            raise
        else:
            self._write(seq, name, agent, kind, model, started_ms, handle)

    def finish(self, status: str = "ok", summary: dict | None = None) -> None:
        try:
            self._store.finish_run(
                self.run_id,
                status if status in RUN_TERMINAL_STATUSES else "failed",
                self.elapsed_ms,
                summary or {},
            )
        except Exception:  # noqa: BLE001
            pass

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    # ------------------------------------------------------------ internals
    def _write(
        self,
        seq: int,
        name: str,
        agent: str,
        kind: str,
        model: str,
        started_ms: int,
        handle: _StepHandle,
    ) -> None:
        try:
            self._store.add_step(
                run_id=self.run_id,
                seq=seq,
                name=name,
                agent=agent,
                kind=kind if kind in KINDS else "deterministic",
                model=model,
                started_ms=started_ms,
                duration_ms=self.elapsed_ms - started_ms,
                status=handle.status,
                verdict=handle.verdict,
                summary=handle.summary,
            )
        except Exception:  # noqa: BLE001 - tracing must never break the pipeline
            pass
