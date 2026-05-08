"""Per-request wallclock watchdog for LM Studio backends.

Background: when the OpenAI HTTP client raises timeout on an LM Studio
call, LM Studio itself keeps generating. Subsequent requests queue behind
the abandoned generation. We observed this on 2026-04-20 PROD task t012:
the 600s HTTP timeout fired, LM Studio chewed on past 120k tokens, and
the slot wedged.

Remediation: when a guarded request runs longer than its HTTP timeout +
10s grace, call ``lms.Client(host).llm.unload(model)`` via lmstudio-python.
That forces LM Studio to drop the in-flight generation and free the slot.

Scope intentionally small: no registry, no background poller, no
streaming token tracking. One ``threading.Timer`` per call, cancelled on
normal exit. Single-tenant assumption — ``unload()`` is global per model
on that LM Studio instance; every fire is logged at WARNING.

See docs/superpowers/specs/2026-04-22-lmstudio-watchdog-design.md.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import lmstudio as lms


_LOG = logging.getLogger(__name__)


def _fire_unload(request_id: str, model: str, host: str) -> None:
    _LOG.warning(
        "WATCHDOG FIRED rid=%s model=%s host=%s — unloading",
        request_id, model, host,
    )
    try:
        client = lms.Client(host)
        try:
            client.llm.unload(model)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
    except Exception as exc:  # noqa: BLE001 - best-effort; HTTP timeout already surfaced
        _LOG.error("WATCHDOG unload failed rid=%s: %r", request_id, exc)


@contextmanager
def guard(
    *,
    request_id: str,
    model: str,
    host: str,
    deadline_sec: float,
) -> Iterator[None]:
    """Arm a ``threading.Timer`` that unloads ``model`` on ``host`` after
    ``deadline_sec``; cancel it on normal exit.

    Safe to use from the OpenAI HTTP call site. If the body returns (or
    raises) before the timer fires, the timer is cancelled and no unload
    is issued.
    """
    timer = threading.Timer(
        deadline_sec,
        _fire_unload,
        args=(request_id, model, host),
    )
    timer.daemon = True
    started = time.monotonic()
    _LOG.debug(
        "WATCHDOG armed rid=%s model=%s deadline=%.1fs",
        request_id, model, deadline_sec,
    )
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        _LOG.debug(
            "WATCHDOG disarmed rid=%s elapsed=%.2fs",
            request_id, time.monotonic() - started,
        )


def force_unload(host: str, model: str) -> None:
    """Unload ``model`` on LM Studio at ``host``. Used by the operator CLI."""
    client = lms.Client(host)
    try:
        client.llm.unload(model)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
