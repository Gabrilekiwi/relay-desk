from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CodexRpcError(RuntimeError):
    pass


@dataclass
class RpcNotification:
    method: str
    params: dict[str, Any]


_SAFE_ENV_KEYS = {
    # Process/runtime discovery.
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    # User/profile locations required by Windows credential storage and temp files.
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "USERNAME",
    "TEMP",
    "TMP",
    "TMPDIR",
    # Locale.
    "LANG",
    "LC_ALL",
    # Explicit network plumbing that may be required on managed networks.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CODEX_CA_CERTIFICATE",
}



def resolve_command_path(
    command: str,
    source: dict[str, str] | None = None,
    *,
    windows: bool | None = None,
) -> str | None:
    """Resolve an executable only from an explicit path or PATH entries.

    On Windows, npm-installed commands such as Codex are commonly exposed as
    ``codex.cmd``. ``subprocess.Popen(["codex", ...])`` can fail even though
    PowerShell resolves ``codex`` successfully via PATHEXT. We resolve the full
    path first, while deliberately *not* searching the current directory unless
    it is explicitly present in PATH.
    """

    source = dict(os.environ if source is None else source)
    is_windows = (os.name == "nt") if windows is None else windows

    def candidates(base: Path) -> list[Path]:
        if not is_windows:
            return [base]
        pathext = source.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        extensions = [ext if ext.startswith(".") else f".{ext}" for ext in pathext.split(";") if ext]
        if base.suffix and any(base.name.lower().endswith(ext.lower()) for ext in extensions):
            return [base]
        return [Path(str(base) + ext) for ext in extensions] + [base]

    raw = Path(command)
    has_directory = raw.parent != Path(".") or raw.is_absolute()
    if has_directory:
        for candidate in candidates(raw.expanduser()):
            if candidate.is_file() and (is_windows or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
        return None

    path_value = source.get("PATH", "")
    separator = ";" if is_windows else os.pathsep
    for entry in path_value.split(separator):
        entry = entry.strip().strip('\"')
        if not entry:
            # Empty PATH elements mean current directory on some platforms;
            # skip them to avoid executable-search hijacking.
            continue
        directory = Path(os.path.expandvars(entry)).expanduser()
        for candidate in candidates(directory / command):
            if candidate.is_file() and (is_windows or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    return None

_WINDOWS_SECURE_KEYRING_MIN_VERSION = (0, 140, 0)


def parse_codex_version(text: str) -> tuple[int, int, int] | None:
    """Parse `codex-cli X.Y.Z` (including prerelease suffixes) into a tuple."""
    match = re.search(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)", text or "", re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def windows_keyring_version_is_safe(version: tuple[int, int, int] | None) -> bool:
    """Return whether Codex includes the Windows encrypted-secrets keyring backend.

    Codex 0.134 stores the complete OAuth payload directly in Windows Credential
    Manager, whose per-secret size limit is too small for current ChatGPT OAuth
    tokens. By 0.140, Windows defaults to the encrypted local secrets backend:
    ciphertext stays in the dedicated CODEX_HOME and only the encryption key is
    placed in the OS keyring.
    """
    return version is not None and version >= _WINDOWS_SECURE_KEYRING_MIN_VERSION


def read_codex_version(
    resolved_command: str,
    env: dict[str, str],
    cwd: Path,
    *,
    timeout: float = 8.0,
) -> tuple[tuple[int, int, int] | None, str]:
    try:
        result = subprocess.run(
            [resolved_command, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd),
            timeout=timeout,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return parse_codex_version(output), output


def friendly_login_error(message: str) -> str:
    lower = (message or "").lower()
    if "longer than platform limit of 2560 chars" in lower or (
        "failed to write oauth tokens to keyring" in lower and "utf-16" in lower
    ):
        return (
            "当前 Windows Codex CLI 的旧版 Keyring 后端无法保存较长的 ChatGPT OAuth Token。"
            "请将 Codex CLI 升级到 0.140.0 或更高版本后再登录；本程序不会为了绕过该错误而回退到明文 auth.json。"
        )
    return message


def build_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment for the Codex monitor child.

    In particular, do not forward unrelated secrets such as OPENAI_API_KEY,
    GITHUB_TOKEN, AWS_* credentials, database passwords, etc.
    """

    source = dict(os.environ if source is None else source)
    env = {key: value for key, value in source.items() if key in _SAFE_ENV_KEYS}
    env["RUST_LOG"] = "warn"
    return env


class CodexAppServerClient:
    """Small JSON-RPC client for `codex app-server` over its default stdio transport.

    Each instance receives its own CODEX_HOME. CLI authentication is forced to
    the OS keyring and plaintext auth.json storage is rejected.
    """

    def __init__(self, codex_home: Path, codex_command: str = "codex", *, proxy_env: dict[str, str] | None = None) -> None:
        self.codex_home = Path(codex_home)
        self.codex_command = codex_command
        self.proxy_env = proxy_env
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._notifications: queue.Queue[RpcNotification] = queue.Queue()
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self._closed = False

    @property
    def plaintext_auth_file(self) -> Path:
        return self.codex_home / "auth.json"

    def assert_no_plaintext_auth(self) -> None:
        if self.plaintext_auth_file.exists():
            raise CodexRpcError(
                "检测到此监控 Profile 中存在明文 auth.json。为避免账号 Token 以文件形式保存，"
                "Codex Account Hub 已拒绝继续使用该 Profile。请不要分享该文件；若它来自旧版 V0.1，"
                "请在确认不再需要该旧登录后手动删除整个监控 Profile，再用新版重新添加账号。"
            )

    def start(self) -> None:
        if self._process and self._process.poll() is None:
            return

        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.assert_no_plaintext_auth()

        source_env = dict(os.environ)
        env = build_child_env(source_env)
        if self.proxy_env is not None:
            env = {key: value for key, value in env.items()
                   if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
            env.update({key: value for key, value in self.proxy_env.items()
                        if key in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}})
        env["CODEX_HOME"] = str(self.codex_home)

        resolved_command = resolve_command_path(self.codex_command, source_env)
        if not resolved_command:
            raise CodexRpcError(
                "未找到 Codex CLI 可执行入口。PowerShell 能运行 `codex --version` 但本程序无法定位时，"
                "通常是 Windows 的 codex.cmd/PATHEXT 解析差异。"
            )

        if os.name == "nt":
            version, version_text = read_codex_version(resolved_command, env, self.codex_home)
            if not windows_keyring_version_is_safe(version):
                shown = version_text or "无法识别版本"
                raise CodexRpcError(
                    f"当前 Codex CLI（{shown}）过旧，不能安全保存此监控账号的 ChatGPT OAuth 凭据。"
                    "Windows 请升级到 Codex CLI 0.140.0 或更高版本后再试。"
                    "0.140+ 使用加密本地 secrets，并只把加密密钥放入系统 Keyring；"
                    "本程序不会回退到明文 auth.json。"
                )

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._process = subprocess.Popen(
                [resolved_command, "-c", 'cli_auth_credentials_store="keyring"', "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                cwd=str(self.codex_home),
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise CodexRpcError(
                "未找到 codex 命令。请先确认 Codex CLI 已安装并可在终端运行 `codex --version`。"
            ) from exc

        self._closed = False
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-account-hub",
                    "title": "Codex Account Hub",
                    "version": "0.1.6",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self.notify("initialized", {})
        self.assert_no_plaintext_auth()

    def close(self) -> None:
        self._closed = True
        process = self._process
        self._process = None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
        self._ensure_running()
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
            # Codex app-server intentionally omits the JSON-RPC version field on the wire.
            payload: dict[str, Any] = {"method": method, "id": request_id}
            if params is not None:
                payload["params"] = params
            self._write(payload)

        deadline = time.time() + timeout
        message: dict[str, Any] | None = None
        while time.time() < deadline:
            try:
                message = response_queue.get(timeout=min(0.2, max(0.01, deadline - time.time())))
                break
            except queue.Empty:
                process = self._process
                if process is not None and process.poll() is not None:
                    self._pending.pop(request_id, None)
                    raise CodexRpcError(self._classified_exit_error(method, process.returncode))

        if message is None:
            self._pending.pop(request_id, None)
            # Intentionally do not surface raw stderr to the UI; upstream logs may
            # contain environment-specific or authentication-adjacent metadata.
            raise CodexRpcError(f"Codex 请求超时：{method}")

        if "error" in message:
            # App-server RPC errors are structured; avoid appending raw process logs.
            error = message["error"]
            raise CodexRpcError(f"Codex RPC 错误 {method}: {error}")
        return message.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._ensure_running()
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._write(payload)

    def read_account(self, refresh_token: bool = False) -> dict[str, Any]:
        result = self.request("account/read", {"refreshToken": refresh_token})
        self.assert_no_plaintext_auth()
        return result if isinstance(result, dict) else {}

    def read_rate_limits(self) -> dict[str, Any]:
        result = self.request("account/rateLimits/read", {})
        self.assert_no_plaintext_auth()
        return result if isinstance(result, dict) else {}

    def logout_account(self) -> None:
        self.request("account/logout", {})
        self.assert_no_plaintext_auth()

    def start_device_login(self) -> dict[str, Any]:
        result = self.request("account/login/start", {"type": "chatgptDeviceCode"}, timeout=30)
        if not isinstance(result, dict):
            raise CodexRpcError("Codex 未返回设备登录信息。")
        return result

    def wait_for_login(self, login_id: str, timeout: float = 600) -> dict[str, Any]:
        deadline = time.time() + timeout
        deferred: list[RpcNotification] = []
        try:
            while time.time() < deadline:
                remaining = max(0.1, min(1.0, deadline - time.time()))
                try:
                    note = self._notifications.get(timeout=remaining)
                except queue.Empty:
                    self._ensure_running()
                    continue
                if note.method == "account/login/completed":
                    params = note.params or {}
                    if params.get("loginId") == login_id:
                        self.assert_no_plaintext_auth()
                        return params
                deferred.append(note)
            raise CodexRpcError("等待 OpenAI 登录完成超时。")
        finally:
            for note in deferred:
                self._notifications.put(note)

    def stderr_tail(self) -> str:
        """Return stderr only for local debugging; it is never shown automatically."""
        return "\n".join(self._stderr_tail[-10:]).strip()

    def _classified_exit_error(self, method: str, returncode: int | None) -> str:
        """Return a safe, actionable error without exposing raw stderr."""
        tail = "\n".join(self._stderr_tail[-20:]).lower()
        if "unexpected argument '--stdio'" in tail or 'unexpected argument "--stdio"' in tail:
            return (
                "Codex app-server 启动失败：当前 Codex CLI 版本不支持 --stdio 参数。"
                "请使用 Codex Account Hub V0.1.3 或更高版本。"
            )
        if "failed to initialize state runtime" in tail or "failed to initialize sqlite" in tail:
            return "Codex app-server 初始化本地状态失败。监控 Profile 的本地状态目录可能异常。"
        if "unknown variant" in tail and "keyring" in tail:
            return "当前 Codex CLI 版本不支持安全的 keyring 凭据存储模式；为保护账号，本程序拒绝回退到明文文件存储。"
        return f"Codex app-server 在响应 {method} 前已退出（exit={returncode}）。"

    def _ensure_running(self) -> None:
        if self._process is None:
            raise CodexRpcError("Codex app-server 尚未启动。")
        code = self._process.poll()
        if code is not None:
            # Do not surface raw stderr to user-facing errors.
            raise CodexRpcError(f"Codex app-server 已退出（exit={code}）。")

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexRpcError("无法向 Codex app-server 写入请求。") from exc

    def _reader_loop(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        for raw in process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                request_id = message.get("id")
                pending = self._pending.pop(request_id, None)
                if pending:
                    pending.put(message)
            elif isinstance(message, dict) and "method" in message:
                params = message.get("params")
                self._notifications.put(
                    RpcNotification(
                        method=str(message.get("method")),
                        params=params if isinstance(params, dict) else {},
                    )
                )

    def _stderr_loop(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        for raw in process.stderr:
            line = raw.rstrip()
            if line:
                # Keep a short in-memory tail for developer diagnostics only.
                # It is never persisted and never appended to user-facing errors.
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 20:
                    del self._stderr_tail[:-20]
