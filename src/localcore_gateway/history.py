"""In-memory invocation history (the ``lcgw tail`` backend).

A bounded ring buffer of per-invocation records, filled by the gateway on
every tool call and read back via ``GET /-/invocations`` (cursor-based
polling on the monotonic ``seq``). No persistence -- this is a dev tool, and
it has no AWS analog (AgentCore observability lives in CloudWatch, out of
band). Argument/payload previews are truncated so one huge payload can't
bloat gateway memory.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from typing import Any

# Per-field cap (bytes of compact JSON) for the argument/payload previews.
PREVIEW_LIMIT = 4096


def _preview(value: Any) -> tuple[str, bool]:
    """``value`` as compact JSON, capped at PREVIEW_LIMIT bytes: (text, truncated)."""
    s = json.dumps(value, ensure_ascii=False, default=str)
    b = s.encode()
    if len(b) <= PREVIEW_LIMIT:
        return s, False
    return b[:PREVIEW_LIMIT].decode(errors="ignore"), True


class InvocationLog:
    """Bounded ring of invocation records with a monotonic ``seq`` cursor."""

    def __init__(self, size: int) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = 0

    def record(
        self,
        *,
        tool: str,
        arguments: Any,
        payload: Any,
        is_error: bool,
        duration_ms: float,
        logs: list[str],
        contract_violation: str | None = None,
    ) -> dict[str, Any]:
        """Append one invocation; returns the stored record."""
        self._seq += 1
        args_text, args_trunc = _preview(arguments)
        payload_text, payload_trunc = _preview(payload)
        rec = {
            "seq": self._seq,
            "time": datetime.now(UTC).isoformat(),
            "tool": tool,
            "arguments": args_text,
            "arguments_truncated": args_trunc,
            "payload": payload_text,
            "payload_truncated": payload_trunc,
            "is_error": is_error,
            "duration_ms": round(duration_ms, 3),
            "logs": list(logs),
            # Set when server.contract_checks caught a schema violation
            # (also in `warn` mode, where the payload still passed through).
            "contract_violation": contract_violation,
        }
        self._records.append(rec)
        return rec

    def since(self, seq: int, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Records with ``seq`` greater than the cursor, oldest first.

        Returns ``(records, next)`` where ``next`` is the cursor to poll with
        next: the last returned seq, or the latest seq when nothing is
        returned (so ``since=0&limit=0`` bootstraps a tail at "now", and a
        stale cursor from before a gateway restart self-heals).
        """
        out = [r for r in self._records if r["seq"] > seq]
        if limit is not None:
            out = out[:limit]
        return out, (out[-1]["seq"] if out else self._seq)
