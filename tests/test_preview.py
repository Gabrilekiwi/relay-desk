from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hub_control as preview


class NetworkTests(unittest.TestCase):
    def test_system_uses_current_network_without_a_fixed_proxy(self):
        settings = preview.validated_settings({"mode": "system", "proxy": "ignored"})
        self.assertEqual(preview.proxy_environment(settings, {}), {})
        self.assertEqual(preview.proxy_environment(settings, {"https": "http://proxy.test:4321"}),
                         {"HTTPS_PROXY": "http://proxy.test:4321"})

    def test_explicit_proxy_keeps_a_user_supplied_port(self):
        settings = preview.validated_settings({"mode": "proxy", "proxy": "http://127.0.0.1:12345/"})
        self.assertEqual(preview.proxy_environment(settings, {"https": "ignored"})["HTTPS_PROXY"],
                         "http://127.0.0.1:12345")
        settings = preview.validated_settings({"mode": "proxy", "proxy": "https://[::1]:12345"})
        self.assertEqual(settings["proxy"], "https://[::1]:12345")

    def test_proxy_rejects_credentials_unsupported_scheme_and_invalid_port(self):
        for address in ("http://user:password@proxy.test:12345", "http://proxy.test",
                        "https://proxy.test:65536", "socks5://proxy.test:12345",
                        "http://proxy.test:12345?query", "http://proxy.test:12345/path"):
            with self.subTest(address=address), self.assertRaises(preview.UserError):
                preview.validated_settings({"mode": "proxy", "proxy": address})

    def test_device_login_requires_openai_https(self):
        self.assertTrue(preview.trusted_device_url("https://auth.openai.com/codex/device"))
        for address in ("http://openai.com", "https://openai.com.example.test",
                        "https://user@openai.com", "https://openai.com:444"):
            self.assertFalse(preview.trusted_device_url(address))

    def test_report_detects_configured_proxy_port_without_returning_credentials(self):
        proxies = {"HTTPS_PROXY": "http://fixture:secret@127.0.0.1:4321"}
        with patch.object(preview, "probe_target", return_value={"status": "reachable", "http": 200}) as check:
            report = preview.network_report(preview.DEFAULT_SETTINGS, proxies)
        self.assertEqual(report["route"], "system_proxy")
        self.assertEqual(report["proxyPort"], 4321)
        self.assertEqual(set(report["checks"]), {"codex", "login"})
        self.assertNotIn("secret", str(report))
        for call in check.call_args_list:
            self.assertEqual(call.args[2]["HTTPS_PROXY"], proxies["HTTPS_PROXY"])

    def test_connection_errors_distinguish_proxy_auth_tls_and_upstream_responses(self):
        cases = [
            (56, "000 407", "proxy_auth"),
            (7, "000 000", "connection"),
            (28, "000 200", "timeout"),
            (60, "000 200", "tls"),
            (0, "403 200", "http_error"),
            (0, "302 000", "reachable"),
        ]
        for result, output, status in cases:
            with self.subTest(status=status):
                self.assertEqual(preview.probe_result(result, output)["status"], status)

    def test_probe_uses_only_https_target_and_passes_proxy_in_child_environment(self):
        environment = {"HTTPS_PROXY": "http://127.0.0.1:4321"}
        with patch.object(preview.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="200 200")) as run:
            result = preview.probe_target("fixture-curl", preview.NETWORK_TARGETS["codex"], environment)
        self.assertEqual(result, {"status": "reachable", "http": 200})
        args = run.call_args.args[0]
        self.assertEqual(args[1], "--disable")
        self.assertEqual(args[-1], "https://chatgpt.com/")
        self.assertNotIn(environment["HTTPS_PROXY"], args)
        self.assertIs(run.call_args.kwargs["env"], environment)


class FakeClient:
    def __init__(self, home, command, *, proxy_env):
        self.home = home
        self.proxy_env = proxy_env

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def start_device_login(self):
        return dict(loginId="login-1", verificationUrl="https://auth.openai.com/codex/device", userCode="TEST-CODE")

    def wait_for_login(self, login_id, timeout):
        return dict(success=login_id == "login-1")

    def read_account(self):
        return dict(account=dict(type="chatgpt", planType="business"))

    def read_rate_limits(self):
        return dict(rateLimits=dict(
            primary=dict(usedPercent=25, windowDurationMins=300, resetsAt=1800000000),
            secondary=dict(usedPercent=50, windowDurationMins=10080, resetsAt=1800500000)))

    def logout_account(self):
        pass


class AccountTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve() / "relay-preview"
        self.root.mkdir()
        self.workbench = preview.AccountWorkbench(self.root)

    def test_first_start_and_persisted_proxy_selection(self):
        self.assertEqual(self.workbench.state()["settings"]["mode"], "system")
        self.assertFalse(self.workbench.settings_path.exists())
        self.workbench.handle({"action": "settings", "mode": "proxy", "proxy": "https://proxy.test:3128"})
        reopened = preview.AccountWorkbench(self.root)
        self.assertEqual(reopened.settings["proxy"], "https://proxy.test:3128")

    @patch.object(preview, "resolve_command_path", return_value="fixture")
    @patch.object(preview, "CodexAppServerClient", FakeClient)
    @patch.object(preview, "emit")
    def test_device_login_quota_refresh_and_confirmed_removal(self, event, command):
        self.workbench.handle({"action": "add", "name": "测试账号"})
        account, = self.workbench.state()["accounts"]
        self.assertEqual(account["five"], 75)
        self.assertEqual(account["week"], 50)
        self.assertIsNotNone(account["weekReset"])
        self.assertEqual(event.call_args.kwargs["code"], "TEST-CODE")
        self.assertNotIn("TEST-CODE", self.workbench.quota_path.read_text(encoding="utf-8"))
        self.assertEqual(self.workbench.handle({"action": "refresh", "id": account["id"]}), "已查询 1/1 个账号。")
        with self.assertRaises(preview.UserError):
            self.workbench.handle({"action": "remove", "id": account["id"]})
        self.workbench.handle({"action": "remove", "id": account["id"], "confirmed": True})
        self.assertEqual(self.workbench.state()["accounts"], [])


if __name__ == "__main__":
    unittest.main()
