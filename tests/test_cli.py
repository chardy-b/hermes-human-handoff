from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_human_handoff import cli  # noqa: E402


class CliUnitTests(unittest.TestCase):
    def test_doctor_verifies_serve_without_printing_tailnet_identity(self):
        args = type("Args", (), {"local_only": False})()
        report = {"ready": True, "missing": [], "tailscale": "/bin/tailscale"}
        private_dns = "private-node.private-tailnet.invalid"
        output = io.StringIO()
        with (
            mock.patch.object(cli, "find_dependencies", return_value=report),
            mock.patch.object(
                cli,
                "tailscale_base_command",
                return_value=(
                    ["/bin/tailscale", "--socket=/private/socket"],
                    {"Self": {"DNSName": private_dns}},
                ),
            ),
            mock.patch.object(cli, "serve_status", return_value={}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_doctor(args), 0)
        rendered = output.getvalue()
        parsed = json.loads(rendered)
        self.assertTrue(parsed["tailnet_connected"])
        self.assertTrue(parsed["serve_access"])
        self.assertNotIn(private_dns, rendered)
        self.assertNotIn("/private/socket", rendered)

    def test_doctor_rejects_missing_magicdns(self):
        args = type("Args", (), {"local_only": False})()
        report = {"ready": True, "missing": [], "tailscale": "/bin/tailscale"}
        output = io.StringIO()
        with (
            mock.patch.object(cli, "find_dependencies", return_value=report),
            mock.patch.object(
                cli,
                "tailscale_base_command",
                return_value=(["/bin/tailscale"], {"Self": {"DNSName": ""}}),
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_doctor(args), 2)
        parsed = json.loads(output.getvalue())
        self.assertFalse(parsed["ready"])
        self.assertIn("MagicDNS", parsed["tailscale_error"])

    def test_validate_target_url(self):
        self.assertEqual(
            cli.validate_target_url("https://example.com/signup"),
            "https://example.com/signup",
        )
        self.assertEqual(
            cli.validate_target_url("chrome://settings/addresses", "address-setup"),
            "chrome://settings/addresses",
        )
        with self.assertRaises(ValueError):
            cli.validate_target_url("chrome://settings/addresses", "payment")
        for value in (
            "javascript:alert(1)",
            "file:///tmp/x",
            "https://user:pass@example.com/",
        ):
            with self.assertRaises(ValueError):
                cli.validate_target_url(value)

    def test_choose_https_port_skips_existing_ports(self):
        status = {"Web": {"node.invalid:9440": {}, "node.invalid:9441": {}}}
        self.assertEqual(cli.choose_https_port(status), 9442)

    def test_choose_https_port_skips_funnel_enabled_port(self):
        status = {"AllowFunnel": {"node.invalid:9440": True}}
        self.assertEqual(cli.choose_https_port(status), 9441)

    def test_route_proxy_reads_only_root_handler(self):
        status = {
            "Web": {
                "node.invalid:9440": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:6100"},
                        "/other": {"Proxy": "http://127.0.0.1:6200"},
                    }
                }
            }
        }
        self.assertEqual(
            cli.route_proxy(status, "node.invalid", 9440), "http://127.0.0.1:6100"
        )
        self.assertIsNone(cli.route_proxy(status, "node.invalid", 9441))

    def test_route_funnel_detection_handles_status_shapes(self):
        key = "node.invalid:9440"
        self.assertTrue(
            cli.route_allows_funnel({"AllowFunnel": {key: True}}, "node.invalid", 9440)
        )
        self.assertTrue(
            cli.route_allows_funnel({"AllowFunnel": [key]}, "node.invalid", 9440)
        )
        self.assertFalse(
            cli.route_allows_funnel({"AllowFunnel": {}}, "node.invalid", 9440)
        )

    def test_activate_route_refuses_funnel_before_side_effect(self):
        route = {
            "base_command": ["tailscale"],
            "dns_name": "node.invalid",
            "https_port": 9440,
            "target": "http://127.0.0.1:6100",
        }
        status = {"AllowFunnel": {"node.invalid:9440": True}}
        with (
            mock.patch.object(cli, "serve_status", return_value=status),
            mock.patch.object(cli.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "already enabled"):
                cli.activate_tailnet_route(route)
        run.assert_not_called()

    def test_session_path_rejects_traversal(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"HUMAN_HANDOFF_HOME": temp}),
        ):
            with self.assertRaises(ValueError):
                cli.session_path("../../escape")

    def test_atomic_json_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state" / "value.json"
            cli.atomic_json(target, {"ok": True})
            self.assertEqual(cli.read_json(target), {"ok": True})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_handoff_page_uses_fragment_and_clears_location(self):
        page = (ROOT / "src/hermes_human_handoff/assets/handoff.html").read_text()
        self.assertIn("location.hash.slice(1)", page)
        self.assertIn("history.replaceState", page)
        self.assertIn("credentials: { password: capability }", page)
        self.assertNotIn("?password=", page)
        self.assertNotIn("localStorage", page)

    def test_remove_route_refuses_foreign_mapping(self):
        route = {
            "base_command": ["tailscale"],
            "dns_name": "node.invalid",
            "https_port": 9440,
            "target": "http://127.0.0.1:6100",
        }
        foreign = {
            "Web": {
                "node.invalid:9440": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
                }
            }
        }
        with mock.patch.object(cli, "serve_status", return_value=foreign):
            with self.assertRaisesRegex(RuntimeError, "no longer owned"):
                cli.remove_tailnet_route(route)

    def test_route_is_journaled_before_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "hh-20260101-010101-abcdef"
            directory.mkdir(mode=0o700)
            cli.atomic_json(
                directory / "config.json",
                {
                    "session_id": directory.name,
                    "url": "https://service.invalid/checkout",
                    "ttl": 120,
                    "purpose": "payment",
                    "local_only": False,
                },
            )
            worker = cli.HandoffWorker(directory / "config.json")
            route = {
                "base_command": ["tailscale"],
                "dns_name": "node.invalid",
                "https_port": 9440,
                "target": "http://127.0.0.1:6100",
            }

            def assert_journaled(value):
                self.assertEqual(value, route)
                runtime = cli.read_json(directory / "runtime.json")
                self.assertEqual(runtime["route"], route)
                self.assertEqual(runtime["worker_pid"], os.getpid())

            with (
                mock.patch.object(cli, "plan_tailnet_route", return_value=route),
                mock.patch.object(
                    cli, "activate_tailnet_route", side_effect=assert_journaled
                ) as activate,
            ):
                worker.publish_route(6100, "tailscale")
            activate.assert_called_once_with(route)

    def test_runtime_only_orphan_route_is_recovered(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"HUMAN_HANDOFF_HOME": temp}),
        ):
            session_id = "hh-20260101-010101-abcdef"
            directory = cli.session_path(session_id)
            directory.mkdir(parents=True)
            route = {
                "base_command": ["tailscale"],
                "dns_name": "node.invalid",
                "https_port": 9440,
                "target": "http://127.0.0.1:6100",
            }
            cli.atomic_json(
                directory / "runtime.json",
                {"children": [], "worker_pid": 99999999, "route": route},
            )
            cli.atomic_json(directory / "launcher.json", {"worker_pid": 99999999})
            cli.atomic_json(
                directory / "config.json",
                {
                    "url": "https://service.invalid/signed?token=private",
                    "ttl": 120,
                    "purpose": "payment",
                },
            )
            cli.atomic_json(directory / "capability.json", {"handoff_url": "secret"})
            (directory / "browser-profile").mkdir()
            with mock.patch.object(cli, "remove_tailnet_route") as remove:
                result = cli.recover_stale_sessions()
            self.assertEqual(result["recovered"], [session_id])
            remove.assert_called_once_with(route)
            public = cli.read_json(directory / "public.json")
            self.assertEqual(public["status"], "stopped")
            self.assertTrue(public["recovered_after_worker_exit"])
            self.assertFalse((directory / "config.json").exists())
            self.assertFalse((directory / "capability.json").exists())
            self.assertFalse((directory / "browser-profile").exists())

    def test_terminal_cleanup_error_is_retried(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"HUMAN_HANDOFF_HOME": temp}),
        ):
            session_id = "hh-20260101-010101-abcdef"
            directory = cli.session_path(session_id)
            directory.mkdir(parents=True)
            route = {
                "base_command": ["tailscale"],
                "dns_name": "node.invalid",
                "https_port": 9440,
                "target": "http://127.0.0.1:6100",
            }
            cli.atomic_json(
                directory / "public.json",
                {
                    "session_id": session_id,
                    "status": "expired",
                    "worker_pid": 99999999,
                    "expires_at": "2026-01-01T01:02:01Z",
                    "cleanup_error": "temporary route remained",
                },
            )
            cli.atomic_json(
                directory / "runtime.json",
                {"children": [], "worker_pid": 99999999, "route": route},
            )
            with mock.patch.object(cli, "remove_tailnet_route") as remove:
                result = cli.recover_stale_sessions()
            self.assertEqual(result["errors"], {})
            remove.assert_called_once_with(route)
            public = cli.read_json(directory / "public.json")
            self.assertEqual(public["status"], "stopped")
            self.assertIsNone(public["cleanup_error"])

    def test_stale_recovery_scrubs_artifacts_and_route(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.dict(os.environ, {"HUMAN_HANDOFF_HOME": temp}),
        ):
            session_id = "hh-20260101-010101-abcdef"
            directory = cli.session_path(session_id)
            directory.mkdir(parents=True)
            cli.atomic_json(
                directory / "public.json",
                {
                    "session_id": session_id,
                    "status": "ready",
                    "worker_pid": 99999999,
                    "expires_at": "2026-01-01T01:02:01Z",
                },
            )
            cli.atomic_json(
                directory / "runtime.json",
                {
                    "children": [],
                    "route": {
                        "base_command": ["tailscale"],
                        "dns_name": "node.invalid",
                        "https_port": 9440,
                        "target": "http://127.0.0.1:6100",
                    },
                },
            )
            cli.atomic_json(directory / "capability.json", {"handoff_url": "secret"})
            cli.atomic_json(
                directory / "config.json", {"url": "https://service.example/signed"}
            )
            (directory / "browser-profile").mkdir()
            with mock.patch.object(cli, "remove_tailnet_route") as remove:
                recovered, errors = cli.recover_stale_session(directory)
            self.assertTrue(recovered)
            self.assertEqual(errors, [])
            remove.assert_called_once()
            self.assertFalse((directory / "capability.json").exists())
            self.assertFalse((directory / "config.json").exists())
            self.assertFalse((directory / "browser-profile").exists())
            public = cli.read_json(directory / "public.json")
            self.assertEqual(public["status"], "stopped")
            self.assertTrue(public["recovered_after_worker_exit"])

    def test_pre_service_setup_failure_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "hh-20260101-010101-abcdef"
            directory.mkdir(mode=0o700)
            cli.atomic_json(
                directory / "config.json",
                {
                    "session_id": directory.name,
                    "url": "https://service.invalid/signed?token=private",
                    "ttl": 120,
                    "purpose": "payment",
                    "local_only": True,
                },
            )
            worker = cli.HandoffWorker(directory / "config.json")
            with (
                mock.patch.object(
                    cli,
                    "find_dependencies",
                    return_value={"ready": True, "missing": [], "novnc": "/missing"},
                ),
                mock.patch.object(
                    worker,
                    "prepare_webroot",
                    side_effect=RuntimeError("synthetic webroot failure"),
                ),
            ):
                self.assertEqual(worker.run(), 1)
            public = cli.read_json(directory / "public.json")
            self.assertEqual(public["status"], "failed")
            self.assertFalse((directory / "capability.json").exists())
            self.assertFalse((directory / "config.json").exists())
            self.assertFalse((directory / "browser-profile").exists())

    def test_profile_template_is_cloned_without_chromium_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "Default").mkdir()
            (source / "Default" / "Web Data").write_bytes(b"native-address-data")
            (source / "SingletonLock").symlink_to("host-123")
            destination = root / "clone"
            cli.copy_profile_template(source, destination)
            self.assertEqual(
                (destination / "Default" / "Web Data").read_bytes(),
                b"native-address-data",
            )
            self.assertFalse((destination / "SingletonLock").exists())

    def test_profile_preferences_enable_addresses_and_disable_payment_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            profile.mkdir(mode=0o700)
            cli.configure_browser_profile(profile)
            preferences = cli.read_json(profile / "Default" / "Preferences")
            self.assertTrue(preferences["autofill"]["profile_enabled"])
            self.assertFalse(preferences["autofill"]["credit_card_enabled"])
            self.assertFalse(preferences["credentials_enable_service"])
            self.assertFalse(preferences["profile"]["password_manager_enabled"])
            self.assertFalse(preferences["payments"]["can_make_payment_enabled"])

    def test_persistent_profile_survives_worker_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "hh-20260101-010101-abcdef"
            directory.mkdir(mode=0o700)
            profile = root / "persistent"
            cli.atomic_json(
                directory / "config.json",
                {
                    "session_id": directory.name,
                    "url": "chrome://settings/addresses",
                    "ttl": 120,
                    "purpose": "address-setup",
                    "local_only": True,
                    "profile_mode": "persistent",
                    "profile_path": str(profile),
                },
            )
            worker = cli.HandoffWorker(directory / "config.json")
            self.assertEqual(worker.prepare_profile(), profile)
            (profile / "Default" / "marker").write_text("kept")
            worker.cleanup()
            self.assertEqual((profile / "Default" / "marker").read_text(), "kept")

    def test_worker_initialization_failure_scrubs_config(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "hh-20260101-010101-abcdef"
            directory.mkdir(mode=0o700)
            (directory / "config.json").write_text("{invalid", encoding="utf-8")
            self.assertEqual(cli.worker_entry(str(directory / "config.json")), 1)
            self.assertFalse((directory / "config.json").exists())
            self.assertEqual(
                cli.read_json(directory / "public.json")["status"], "failed"
            )


if __name__ == "__main__":
    unittest.main()
