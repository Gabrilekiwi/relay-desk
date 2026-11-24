"""Local account controls and read-only diagnostics for Relay Desk."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
import msvcrt
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies

sys.path.insert(0, str(Path(__file__).resolve().parent / "account_hub"))
from account_service import _extract_account
from codex_rpc import CodexAppServerClient, build_child_env, read_codex_version, resolve_command_path, windows_keyring_version_is_safe
from profiles import ProfileStore
from quota import parse_rate_limits


PROFILE_ID = re.compile(r"[0-9a-f]{10}")
PROFILE_ACTIONS = {"add", "login", "refresh", "remove", "folder"}
DEFAULT_SETTINGS = {"mode": "system", "proxy": ""}
NETWORK_TARGETS = {
    "codex": "https://chatgpt.com/",
    "login": "https://auth.openai.com/",
}
PROBE_ERRORS = {5: "proxy_dns", 6: "dns", 7: "connection", 28: "timeout", 35: "tls", 51: "tls", 60: "tls", 77: "tls"}


class UserError(Exception):
    pass


def emit(kind, **payload):
    print(json.dumps(dict(kind=kind, **payload), ensure_ascii=True, allow_nan=False), flush=True)


def data_root():
    return Path(os.environ["LOCALAPPDATA"]) / "RelayDeskPreview"


def read_json(path, default):
    try:
        with path.open(encoding="utf-8") as source:
            text = source.read(1_048_577)
        if len(text) > 1_048_576:
            raise UserError("本地文件超出大小限制。")
        return json.loads(text)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as failure:
        raise UserError("无法读取本地数据。请检查文件权限和格式。") from failure


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=True, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validated_settings(value):
    if not isinstance(value, dict) or value.get("mode") not in {"system", "proxy"}:
        raise UserError("请选择本机网络或手动代理。")
    if value["mode"] == "system":
        return dict(DEFAULT_SETTINGS)
    raw = value.get("proxy")
    if not isinstance(raw, str) or not raw.isascii() or len(raw) > 256:
        raise UserError("请输入 HTTP 或 HTTPS 代理地址和端口。")
    try:
        if any(c.isspace() or not c.isprintable() or c == "\\" for c in raw.strip()):
            raise ValueError()
        parsed = urlsplit(raw.strip())
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or not port or parsed.username is not None
                or parsed.path not in ("", "/") or "?" in raw or "#" in raw):
            raise ValueError()
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host)}]"
        elif not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            raise ValueError()
        return {"mode": "proxy", "proxy": urlunsplit((parsed.scheme, f"{host}:{port}", "", "", ""))}
    except ValueError as failure:
        raise UserError("代理格式应为 http://主机:端口 或 https://主机:端口，地址中请勿加入凭据。") from failure


def proxy_environment(settings, system=None):
    if settings["mode"] == "proxy":
        return {"HTTP_PROXY": settings["proxy"], "HTTPS_PROXY": settings["proxy"],
                "NO_PROXY": "localhost,127.0.0.1,::1"}
    available = getproxies() if system is None else system
    return {key.upper() + "_PROXY": value for key, value in available.items()
            if key in {"http", "https", "all", "no"} and value}


def probe_result(exit_code, output):
    fields = output.strip().split()
    if len(fields) == 2 and all(re.fullmatch(r"[0-9]{3}", value) for value in fields):
        http, connect = map(int, fields)
        if connect == 407:
            return dict(status="proxy_auth", http=407)
        if connect >= 400:
            return dict(status="proxy_rejected", http=connect)
        if exit_code == 0 and 100 <= http <= 599:
            status = "reachable" if 200 <= http < 400 else "http_error"
            return dict(status=status, http=http)
    return dict(status=PROBE_ERRORS.get(exit_code, "failed"))


def probe_target(curl, address, environment):
    try:
        result = subprocess.run(
            [str(curl), "--disable", "--head", "--silent", "--output", os.devnull,
             "--write-out", "%{http_code} %{http_connect}", "--connect-timeout", "3",
             "--max-time", "5", address],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, encoding="ascii", errors="replace", timeout=7,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired:
        return dict(status="timeout")
    except OSError:
        return dict(status="unavailable")
    return probe_result(result.returncode, result.stdout)


def network_report(settings, proxies):
    default = proxies.get("HTTPS_PROXY") or proxies.get("ALL_PROXY")
    port = None
    if default:
        try:
            parsed = urlsplit(default if "://" in default else "http://" + default)
            port = parsed.port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None)
        except ValueError:
            pass
    curl = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "curl.exe"
    environment = {name: value for name, value in build_child_env().items() if not name.upper().endswith("_PROXY")}
    environment.update(proxies)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = dict(zip(NETWORK_TARGETS, workers.map(
            lambda address: probe_target(curl, address, environment), NETWORK_TARGETS.values())))
    route = "manual" if settings["mode"] == "proxy" else "system_proxy" if default else "system_network"
    return dict(route=route, proxyPort=port, checks=results)


def trusted_device_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and parsed.port in (None, 443) and parsed.username is None
                and (host == "openai.com" or host.endswith(".openai.com")))
    except ValueError:
        return False


def sample_account(profile, command, env):
    observed = datetime.now(timezone.utc).isoformat()
    summary = {"status": "error", "at": observed}
    try:
        with CodexAppServerClient(Path(profile.codex_home), command, proxy_env=env) as client:
            email, plan, logged_in = _extract_account(client.read_account())
            quota = parse_rate_limits(client.read_rate_limits()) if logged_in else None
        summary.update(status="signed_in" if logged_in else "signed_out", email=email,
                       plan=plan or (quota.plan_type if quota else None))
        for key, window in (("five", quota.five_hour if quota else None), ("week", quota.weekly if quota else None)):
            if window is not None:
                amount = window.remaining_percent
                summary[key] = round(amount, 1) if amount is not None and 0 <= amount <= 100 else None
                summary[key + "Reset"] = window.resets_at.isoformat() if window.resets_at else None
        summary["at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        # Keep upstream exception details out of diagnostics and account status.
        pass
    return profile.id, summary


class AccountWorkbench:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink() or self.root.is_junction():
            raise UserError("本地数据目录是重定向目录。")
        self.store = ProfileStore(self.root)
        if self.store.profiles_dir.is_symlink() or self.store.profiles_dir.is_junction():
            raise UserError("账号存储目录是重定向目录。")
        raw = read_json(self.store.accounts_file, [])
        if not isinstance(raw, list):
            raise UserError("账号列表格式无效。")
        self.settings_path = self.root / "settings.json"
        self.settings_warning = ""
        try:
            stored = read_json(self.settings_path, None)
            self.settings = validated_settings(DEFAULT_SETTINGS if stored is None else stored)
        except UserError:
            self.settings = dict(DEFAULT_SETTINGS)
            self.settings_warning = "网络设置文件无效，当前沿用本机网络。可以重新保存设置。"
        self.quota_path = self.root / "quota.json"
        self.samples = read_json(self.quota_path, {})
        if not isinstance(self.samples, dict):
            self.samples = {}

    def state(self):
        accounts = []
        for profile in self.store.list():
            saved = self.samples.get(profile.id)
            account = saved.copy() if isinstance(saved, dict) else {}
            account.update(id=profile.id, name=profile.name, enabled=profile.enabled)
            accounts.append(account)
        return dict(settings=self.settings, settingsWarning=self.settings_warning, accounts=accounts)

    def refresh(self, profiles, command, env):
        if not profiles:
            return "暂无可刷新的账号。"
        with ThreadPoolExecutor(max_workers=min(len(profiles), 4)) as workers:
            summaries = dict(workers.map(lambda p: sample_account(p, command, env), profiles))
        self.samples.update(summaries)
        write_json(self.quota_path, self.samples)
        success = sum(item["status"] != "error" for item in summaries.values())
        return f"已查询 {success}/{len(summaries)} 个账号。"

    def login(self, profile, command, env):
        with CodexAppServerClient(Path(profile.codex_home), command, proxy_env=env) as client:
            info = client.start_device_login()
            url, code, login_id = (info.get(key) for key in ("verificationUrl", "userCode", "loginId"))
            if (not trusted_device_url(url) or not isinstance(code, str)
                    or not re.fullmatch(r"[0-9A-Za-z-]{3,32}", code) or not login_id):
                raise UserError("未收到有效的 OpenAI 设备登录信息。")
            emit("device", name=profile.name, url=url, code=code)
            if client.wait_for_login(login_id, timeout=600).get("success") is not True:
                raise UserError("设备登录未完成。请检查浏览器提示后重试。")
        return "登录成功。" + self.refresh([profile], command, env)

    def remove(self, profile, command, env):
        path = Path(profile.codex_home)
        base = self.store.profiles_dir
        expected = base.resolve()
        if (not path.is_absolute() or path.parent.resolve() != expected or path.resolve().parent != expected
                or base.is_symlink() or base.is_junction() or self.root.is_symlink() or self.root.is_junction()
                or path.is_symlink() or path.is_junction() or not path.name.endswith("-" + profile.id)):
            raise UserError("账号目录无法安全核验。")
        if path.exists() and not (path / "auth.json").exists():
            if command is None:
                raise UserError("请先安装 Codex CLI，以便注销本地会话。")
            with CodexAppServerClient(path, command, proxy_env=env) as client:
                client.logout_account()
        self.store.purge_home(profile)
        self.store.remove(profile.id)
        self.samples.pop(profile.id, None)
        write_json(self.quota_path, self.samples)
        return "已注销并删除此本地账号。"

    def handle(self, request):
        action = request.get("action")
        if action == "state":
            return ""
        if action == "settings":
            settings = validated_settings(request)
            write_json(self.settings_path, settings)
            self.settings = settings
            self.settings_warning = ""
            return "已保存网络设置。"
        if action not in PROFILE_ACTIONS | {"diagnose", "network"}:
            raise UserError("无法识别该操作。")
        if action == "folder":
            os.startfile(self.root)
            return "已打开本地数据目录。"
        env = proxy_environment(self.settings)
        if action == "network":
            emit("network", **network_report(self.settings, env))
            return ""
        command = resolve_command_path("codex")
        if action == "diagnose":
            version, _ = read_codex_version(command, build_child_env(), self.root) if command else (None, "")
            emit("diagnostic", cli=".".join(map(str, version)) if version else None,
                 cliSupported=windows_keyring_version_is_safe(version), python=sys.version.split()[0],
                 profileCount=len(self.store.list()), route=self.settings["mode"],
                 proxyConfigured=bool(env.keys() & {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}))
            return "本机检查完成。"
        target = request.get("id")
        profile = None
        if target is not None:
            if not isinstance(target, str) or not PROFILE_ID.fullmatch(target):
                raise UserError("账号编号无效。")
            profile = next((entry for entry in self.store.list() if entry.id == target), None)
            if profile is None:
                raise UserError("找不到所选账号。")
        if action == "remove" and profile is not None:
            if request.get("confirmed") is not True:
                raise UserError("请确认注销并删除。")
            return self.remove(profile, command, env)
        if not command:
            raise UserError("未找到 Codex CLI。请安装官方 CLI，并在终端确认 codex --version 可用。")
        if action == "add":
            name = request.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or not name.isprintable():
                raise UserError("请输入 1 至 64 个可显示字符作为账号名称。")
            profile = self.store.add(name.strip())
            return self.login(profile, command, env)
        if action == "refresh":
            return self.refresh([profile] if profile else [item for item in self.store.list() if item.enabled], command, env)
        if action == "login" and profile is not None:
            return self.login(profile, command, env)
        raise UserError("请先选择一个账号。")


def main():
    smoke = sys.argv[1:] == ["--smoke"]
    root = Path(__file__).resolve().parent / ".smoke-state" if smoke else data_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "workbench.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            emit("hub-result", ok=False, message="已有一项账号操作在进行。")
            return
        model = None
        try:
            request = json.loads(sys.stdin.readline(4097))
            if not isinstance(request, dict):
                raise UserError("账号操作参数无效。")
            if smoke and request.get("action") != "state":
                raise UserError("窗口启动测试只读取初始状态。")
            model = AccountWorkbench(root)
            message = model.handle(request)
            emit("hub-result", ok=True, message=message, state=model.state())
        except UserError as failure:
            emit("hub-result", ok=False, message=str(failure), state=model.state() if model else None)
        except Exception:
            emit("hub-result", ok=False, message="操作失败。请检查网络、Codex CLI 和本地账号目录。",
                 state=model.state() if model else None)


if __name__ == "__main__":
    main()
