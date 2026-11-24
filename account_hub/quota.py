from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class QuotaWindow:
    label: str
    duration_minutes: int | None
    used_percent: float | None
    remaining_percent: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class QuotaSnapshot:
    plan_type: str | None
    windows: tuple[QuotaWindow, ...]

    @property
    def five_hour(self) -> QuotaWindow | None:
        return next((w for w in self.windows if w.duration_minutes == 300), None)

    @property
    def weekly(self) -> QuotaWindow | None:
        return next((w for w in self.windows if w.duration_minutes == 10080), None)


def parse_rate_limits(payload: dict[str, Any]) -> QuotaSnapshot:
    rate_limit = _select_codex_rate_limit(payload)
    if not rate_limit:
        return QuotaSnapshot(plan_type=None, windows=())

    plan_type = _string_or_none(rate_limit.get("planType"))
    windows: list[QuotaWindow] = []
    for key in ("primary", "secondary"):
        raw = rate_limit.get(key)
        window = _parse_window(raw)
        if window:
            windows.append(window)

    # Future-proof: de-duplicate by duration if primary/secondary happen to repeat.
    deduped: dict[int | None, QuotaWindow] = {}
    for window in windows:
        deduped.setdefault(window.duration_minutes, window)

    ordered = sorted(
        deduped.values(),
        key=lambda item: item.duration_minutes if item.duration_minutes is not None else 10**9,
    )
    return QuotaSnapshot(plan_type=plan_type, windows=tuple(ordered))


def _select_codex_rate_limit(payload: dict[str, Any]) -> dict[str, Any] | None:
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex = by_id.get("codex")
        if isinstance(codex, dict):
            return codex
        for value in by_id.values():
            if isinstance(value, dict) and value.get("limitId") == "codex":
                return value

    fallback = payload.get("rateLimits")
    if isinstance(fallback, dict):
        if fallback.get("limitId") in (None, "codex"):
            return fallback
    return None


def _parse_window(raw: Any) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None

    duration = _int_or_none(raw.get("windowDurationMins"))
    used = _float_or_none(raw.get("usedPercent"))
    remaining = None if used is None else max(0.0, min(100.0, 100.0 - used))
    resets_at = _parse_timestamp(raw.get("resetsAt"))
    return QuotaWindow(
        label=_label_for_duration(duration),
        duration_minutes=duration,
        used_percent=used,
        remaining_percent=remaining,
        resets_at=resets_at,
    )


def _label_for_duration(duration: int | None) -> str:
    if duration == 300:
        return "5 小时"
    if duration == 10080:
        return "周额度"
    if duration is None:
        return "未知窗口"
    if duration % 1440 == 0:
        days = duration // 1440
        return f"{days} 天"
    if duration % 60 == 0:
        return f"{duration // 60} 小时"
    return f"{duration} 分钟"


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc).astimezone()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None
