from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_rpc import CodexAppServerClient
from profiles import AccountProfile
from quota import QuotaSnapshot, parse_rate_limits


@dataclass
class AccountState:
    profile: AccountProfile
    logged_in: bool
    email: str | None
    plan_type: str | None
    quota: QuotaSnapshot
    updated_at: datetime
    error: str | None = None


class AccountService:
    def __init__(self, codex_command: str = "codex") -> None:
        self.codex_command = codex_command

    def refresh(self, profile: AccountProfile) -> AccountState:
        try:
            with CodexAppServerClient(Path(profile.codex_home), self.codex_command) as client:
                account = client.read_account(refresh_token=False)
                email, plan, logged_in = _extract_account(account)
                # Reading rate limits requires an authenticated Codex account.
                # Treat an unauthenticated monitor profile as a normal state, not
                # as an operational error. This also avoids noisy -32600 RPC
                # errors during first-run and after logout.
                if not logged_in:
                    return AccountState(
                        profile=profile,
                        logged_in=False,
                        email=email,
                        plan_type=plan,
                        quota=QuotaSnapshot(plan_type=None, windows=()),
                        updated_at=datetime.now().astimezone(),
                    )
                rate_limits = client.read_rate_limits()
            quota = parse_rate_limits(rate_limits)
            return AccountState(
                profile=profile,
                logged_in=True,
                email=email,
                plan_type=plan or quota.plan_type,
                quota=quota,
                updated_at=datetime.now().astimezone(),
            )
        except Exception as exc:  # surfaced as an account-card error, not a process crash
            return AccountState(
                profile=profile,
                logged_in=False,
                email=None,
                plan_type=None,
                quota=QuotaSnapshot(plan_type=None, windows=()),
                updated_at=datetime.now().astimezone(),
                error=str(exc),
            )


def _extract_account(payload: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    raw = payload.get("account")
    requires_auth = bool(payload.get("requiresOpenaiAuth", False))
    if not isinstance(raw, dict):
        return None, None, False if requires_auth else False

    account_type = str(raw.get("type") or "")
    email = _pick_string(raw, "email", "userEmail")
    plan = _pick_string(raw, "planType", "plan_type")
    logged_in = account_type.lower() in {"chatgpt", "chatgptauthtokens"} or bool(email)
    return email, plan, logged_in


def _pick_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None
