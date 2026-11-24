from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AccountProfile:
    id: str
    name: str
    codex_home: str
    enabled: bool = True


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            local = os.environ.get("LOCALAPPDATA")
            root = Path(local) / "CodexAccountHub" if local else Path.home() / ".codex-account-hub"
        self.root = Path(root)
        self.profiles_dir = self.root / "profiles"
        self.accounts_file = self.root / "accounts.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[AccountProfile]:
        if not self.accounts_file.exists():
            return []
        try:
            data = json.loads(self.accounts_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        result: list[AccountProfile] = []
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                profile = AccountProfile(**row)
            except TypeError:
                continue
            # Never trust a persisted codex_home path blindly. A modified
            # accounts.json must not make the monitor operate on ~/.codex or
            # any directory outside its dedicated profile root. resolve() also
            # follows existing symlinks/junction-like paths where supported.
            if not self._is_safe_profile_home(Path(profile.codex_home)):
                continue
            result.append(profile)
        return result


    def _is_safe_profile_home(self, path: Path) -> bool:
        try:
            root = self.profiles_dir.resolve()
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return False
        return resolved != root

    def add(self, name: str) -> AccountProfile:
        name = name.strip() or "Business Account"
        profiles = self.list()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower() or "account"
        profile_id = uuid.uuid4().hex[:10]
        codex_home = self.profiles_dir / f"{slug}-{profile_id}"
        codex_home.mkdir(parents=True, exist_ok=True)
        profile = AccountProfile(id=profile_id, name=name, codex_home=str(codex_home))
        profiles.append(profile)
        self._save(profiles)
        return profile

    def remove(self, profile_id: str) -> None:
        profiles = [p for p in self.list() if p.id != profile_id]
        self._save(profiles)

    def purge_home(self, profile: AccountProfile) -> None:
        path = Path(profile.codex_home)
        if not self._is_safe_profile_home(path):
            raise ValueError("拒绝删除监控 Profile 根目录之外的路径。")
        if path.exists():
            shutil.rmtree(path)

    def rename(self, profile_id: str, new_name: str) -> None:
        profiles = self.list()
        for profile in profiles:
            if profile.id == profile_id:
                profile.name = new_name.strip() or profile.name
        self._save(profiles)

    def _save(self, profiles: list[AccountProfile]) -> None:
        temp = self.accounts_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps([asdict(profile) for profile in profiles], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.accounts_file)
