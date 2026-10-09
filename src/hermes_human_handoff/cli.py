from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import glob
import json
import os
import re
import secrets
import shutil
import signal
import socket
import string
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

SESSION_RE = re.compile(r"^hh-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")
CAPABILITY_ALPHABET = string.ascii_letters + string.digits
DEFAULT_TTL = 900
MIN_TTL = 60
MAX_TTL = 3600
HTTPS_PORTS = range(9440, 9500)
ORPHAN_STARTUP_GRACE = 60


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_at(timestamp: float) -> str:
    return (
        dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def state_root() -> Path:
    explicit = os.environ.get("HUMAN_HANDOFF_HOME")
    root = (
        Path(explicit).expanduser()
        if explicit
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "hermes-human-handoff"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    return root


def session_path(session_id: str) -> Path:
    if not SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid session id")
    return state_root() / "sessions" / session_id


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def mark_handed_back(session_dir: Path, capability: str) -> bool:
    """Acknowledge Done using the exact capability already used by VNC."""
    try:
        stored = read_json(session_dir / "capability.json").get("capability")
        if not isinstance(stored, str) or not secrets.compare_digest(stored, capability):
            return False
        public_path = session_dir / "public.json"
        public = read_json(public_path)
        if public.get("status") != "ready":
            return False
        public.update({"status": "handed_back", "completion": "done", "handed_back_at": iso_at(time.time())})
        atomic_json(public_path, public)
        return True
    except (FileNotFoundError, ValueError, TypeError):
        return False


class _DoneHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        # Tailscale Serve strips the configured --set-path prefix before
        # proxying to this dedicated loopback-only completion server.
        if self.path not in {"/", "/handoff-done"}:
            self.send_error(404)
            return
        capability = self.headers.get("X-Handoff-Capability", "")
        ok = mark_handed_back(self.server.session_dir, capability)  # type: ignore[attr-defined]
        self.send_response(204 if ok else 403)
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        return


def start_done_server(session_dir: Path, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _DoneHandler)
    server.session_dir = session_dir  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def executable_from(
    env_name: str, names: tuple[str, ...], extras: list[str] | None = None
) -> str | None:
    explicit = os.environ.get(env_name)
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for raw in extras or []:
        candidate = Path(raw).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_browser() -> str | None:
    extras = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
    ]
    patterns = [
        str(
            Path.home()
            / ".cache"
            / "ms-playwright"
            / "chromium-*"
            / "chrome-linux"
            / "chrome"
        ),
        str(
            Path.home()
            / ".cache"
            / "ms-playwright"
            / "chromium-*"
            / "chrome-linux64"
            / "chrome"
        ),
    ]
    for pattern in patterns:
        extras.extend(sorted(glob.glob(pattern), reverse=True))
    return executable_from(
        "HUMAN_HANDOFF_BROWSER",
        ("google-chrome", "chromium", "chromium-browser"),
        extras,
    )


def find_novnc() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("HUMAN_HANDOFF_NOVNC_DIR"):
        candidates.append(Path(os.environ["HUMAN_HANDOFF_NOVNC_DIR"]).expanduser())
    if os.environ.get("HUMAN_HANDOFF_PREFIX"):
        candidates.append(
            Path(os.environ["HUMAN_HANDOFF_PREFIX"]).expanduser() / "noVNC"
        )
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    candidates.extend(
        [data_home / "hermes-human-handoff" / "noVNC", Path("/usr/share/novnc")]
    )
    for candidate in candidates:
        if (candidate / "core" / "rfb.js").is_file():
            return candidate.resolve()
    return None


def find_dependencies(local_only: bool = False) -> dict[str, Any]:
    websockify = executable_from(
        "HUMAN_HANDOFF_WEBSOCKIFY",
        ("websockify",),
        [str(Path(sys.executable).parent / "websockify")],
    )
    values: dict[str, Any] = {
        "python": sys.executable,
        "xvfb": executable_from("HUMAN_HANDOFF_XVFB", ("Xvfb",)),
        "x11vnc": executable_from("HUMAN_HANDOFF_X11VNC", ("x11vnc",)),
        "websockify": websockify,
        "browser": find_browser(),
        "novnc": str(find_novnc()) if find_novnc() else None,
        "tailscale": None
        if local_only
        else executable_from("HUMAN_HANDOFF_TAILSCALE", ("tailscale",)),
    }
    required = ["xvfb", "x11vnc", "websockify", "browser", "novnc"]
    if not local_only:
        required.append("tailscale")
    values["ready"] = all(values.get(key) for key in required)
    values["missing"] = [key for key in required if not values.get(key)]
    return values


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_tcp(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"loopback port {port} did not become ready")


def wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("HTTP endpoint did not become ready")


def open_internal_browser_target(cdp_port: int, target: str) -> None:
    encoded = quote(target, safe="")
    request = Request(
        f"http://127.0.0.1:{cdp_port}/json/new?{encoded}",
        method="PUT",
    )
    with urlopen(request, timeout=5.0) as response:
        if response.status != 200:
            raise RuntimeError("Chromium did not open the address settings page")


def validate_target_url(value: str, purpose: str = "other") -> str:
    if value == "chrome://settings/addresses" and purpose == "address-setup":
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("target URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in the target URL")
    return value


def validate_profile_directory(path: Path, *, create: bool = False) -> Path:
    raw_profile = path.expanduser()
    if not raw_profile.is_absolute():
        raw_profile = Path.cwd() / raw_profile
    # Reject a symlink as the profile itself, but canonicalize existing parent
    # aliases (for example mounted-volume compatibility links) once before
    # validating and creating the protected path.
    if raw_profile.is_symlink():
        raise ValueError("browser profile must be a real directory")
    profile = raw_profile.resolve(strict=False)
    current = Path(profile.anchor)
    for component in profile.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("browser profile path must not contain symlinks")
        if current.exists() and not current.is_dir():
            raise ValueError("browser profile path component must be a directory")
        if create and not current.exists():
            current.mkdir(mode=0o700)
        if current.exists() and current.stat().st_mode & 0o022:
            raise ValueError("browser profile path must not be writable by group or other users")
    if not profile.is_dir() or profile.is_symlink():
        raise ValueError("browser profile must be a real directory")
    if profile.stat().st_uid != os.getuid():
        raise ValueError("browser profile must be owned by the current user")
    if profile.stat().st_mode & 0o077:
        raise ValueError(
            "browser profile must not be accessible to group or other users"
        )
    return profile


def copy_profile_template(source: Path, destination: Path) -> None:
    source = validate_profile_directory(source)
    if destination.exists():
        raise ValueError("checkout profile destination already exists")

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name.startswith("Singleton")
            or name in {"DevToolsActivePort", ".hermes-human-handoff.lock"}
        }

    for candidate in source.rglob("*"):
        if candidate.is_symlink() and not candidate.name.startswith("Singleton"):
            raise ValueError("browser profile template contains an unexpected symlink")
    shutil.copytree(source, destination, ignore=ignore)
    destination.chmod(0o700)


def configure_browser_profile(profile: Path) -> None:
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True, mode=0o700)
    preferences_path = default / "Preferences"
    preferences: dict[str, Any] = {}
    if preferences_path.exists():
        preferences = read_json(preferences_path)
    autofill = preferences.setdefault("autofill", {})
    if not isinstance(autofill, dict):
        autofill = preferences["autofill"] = {}
    autofill["profile_enabled"] = True
    autofill["credit_card_enabled"] = False
    preferences["credentials_enable_service"] = False
    profile_preferences = preferences.setdefault("profile", {})
    if not isinstance(profile_preferences, dict):
        profile_preferences = preferences["profile"] = {}
    profile_preferences["password_manager_enabled"] = False
    payments = preferences.setdefault("payments", {})
    if not isinstance(payments, dict):
        payments = preferences["payments"] = {}
    payments["can_make_payment_enabled"] = False
    atomic_json(preferences_path, preferences)


def process_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def tailscale_base_command(binary: str) -> tuple[list[str], dict[str, Any]]:
    explicit = os.environ.get("HUMAN_HANDOFF_TAILSCALE_SOCKET")
    sockets: list[str | None] = [explicit] if explicit else [None]
    uid = os.getuid()
    sockets.extend(
        [
            f"/run/user/{uid}/tailscale/tailscaled.sock",
            f"/run/user/{uid}/tailscaled.sock",
        ]
    )
    seen: set[str | None] = set()
    for candidate in sockets:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate and not Path(candidate).exists():
            continue
        base = [binary] + ([f"--socket={candidate}"] if candidate else [])
        result = subprocess.run(
            base + ["status", "--json"], capture_output=True, text=True
        )
        if result.returncode != 0:
            continue
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if status.get("BackendState") == "Running" and status.get("Self", {}).get(
            "DNSName"
        ):
            return base, status
    raise RuntimeError("no running Tailscale daemon with MagicDNS was found")


def serve_status(base: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        base + ["serve", "status", "--json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError("could not read Tailscale Serve status")
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tailscale Serve returned invalid JSON") from exc
    return value if isinstance(value, dict) else {}


def choose_https_port(status: dict[str, Any]) -> int:
    used: set[int] = set()
    for key in status.get("Web", {}):
        with contextlib.suppress(ValueError, IndexError):
            used.add(int(key.rsplit(":", 1)[1]))
    allowed = status.get("AllowFunnel")
    funnel_keys = (
        allowed.keys()
        if isinstance(allowed, dict)
        else allowed
        if isinstance(allowed, list)
        else []
    )
    for key in funnel_keys:
        with contextlib.suppress(ValueError, IndexError):
            used.add(int(str(key).rsplit(":", 1)[1]))
    for port in HTTPS_PORTS:
        if port not in used:
            return port
    raise RuntimeError("no free Human Handoff HTTPS port in 9440-9499")


def route_path_proxy(
    status: dict[str, Any], dns_name: str, https_port: int, path: str
) -> str | None:
    web = status.get("Web", {}).get(f"{dns_name}:{https_port}", {})
    handlers = web.get("Handlers", {}) if isinstance(web, dict) else {}
    handler = handlers.get(path, {}) if isinstance(handlers, dict) else {}
    proxy = handler.get("Proxy") if isinstance(handler, dict) else None
    return str(proxy) if proxy else None


def route_proxy(status: dict[str, Any], dns_name: str, https_port: int) -> str | None:
    return route_path_proxy(status, dns_name, https_port, "/")


def route_allows_funnel(status: dict[str, Any], dns_name: str, https_port: int) -> bool:
    key = f"{dns_name}:{https_port}"
    allowed = status.get("AllowFunnel")
    if isinstance(allowed, dict):
        return bool(allowed.get(key))
    if isinstance(allowed, list):
        return key in allowed
    return bool(allowed)


def plan_tailnet_route(
    web_port: int, done_port: int, tailscale_binary: str
) -> dict[str, Any]:
    base, status = tailscale_base_command(tailscale_binary)
    dns_name = status["Self"]["DNSName"].rstrip(".")
    if not dns_name:
        raise RuntimeError("Tailscale MagicDNS name is unavailable")
    before = serve_status(base)
    https_port = choose_https_port(before)
    target = f"http://127.0.0.1:{web_port}"
    done_target = f"http://127.0.0.1:{done_port}"
    return {
        "base_command": base,
        "dns_name": dns_name,
        "https_port": https_port,
        "target": target,
        "done_target": done_target,
        "neighbor_keys_before": sorted(before.get("Web", {}).keys()),
    }


def activate_tailnet_route(route: dict[str, Any]) -> None:
    base = [str(x) for x in route["base_command"]]
    dns_name = str(route["dns_name"])
    https_port = int(route["https_port"])
    target = str(route["target"])
    done_target = str(route["done_target"])
    current = serve_status(base)
    if route_proxy(current, dns_name, https_port) is not None:
        raise RuntimeError("temporary Tailscale Serve port is no longer free")
    if route_allows_funnel(current, dns_name, https_port):
        raise RuntimeError(
            "refusing a handoff port already enabled for Tailscale Funnel"
        )
    result = subprocess.run(
        base + ["serve", "--bg", "--yes", f"--https={https_port}", target],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Tailscale Serve rejected the temporary route")
    result = subprocess.run(
        base
        + [
            "serve",
            "--bg",
            "--yes",
            f"--https={https_port}",
            "--set-path=/handoff-done",
            done_target,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        subprocess.run(
            base + ["serve", f"--https={https_port}", "off"],
            capture_output=True,
            text=True,
        )
        raise RuntimeError("Tailscale Serve rejected the completion route")
    try:
        after = serve_status(base)
        if route_proxy(after, dns_name, https_port) != target:
            raise RuntimeError("temporary Tailscale Serve route failed verification")
        if (
            route_path_proxy(after, dns_name, https_port, "/handoff-done")
            != done_target
        ):
            raise RuntimeError("temporary completion route failed verification")
        if route_allows_funnel(after, dns_name, https_port):
            raise RuntimeError("refusing a handoff route with Tailscale Funnel enabled")
    except Exception:
        # This route was journaled before activation. Roll it back immediately,
        # while crash recovery retains enough ownership data to retry later.
        with contextlib.suppress(Exception):
            current = serve_status(base)
            if route_proxy(current, dns_name, https_port) == target:
                subprocess.run(
                    base + ["serve", f"--https={https_port}", "off"],
                    capture_output=True,
                    text=True,
                )
        raise


def remove_tailnet_route(route: dict[str, Any]) -> None:
    base = [str(x) for x in route.get("base_command", [])]
    if not base:
        return
    dns_name = str(route["dns_name"])
    https_port = int(route["https_port"])
    target = str(route["target"])
    done_target = str(route.get("done_target") or "")
    current = serve_status(base)
    current_proxy = route_proxy(current, dns_name, https_port)
    if current_proxy is None:
        return
    if current_proxy != target:
        raise RuntimeError(
            "refusing to remove a Tailscale route no longer owned by this session"
        )
    if done_target and (
        route_path_proxy(current, dns_name, https_port, "/handoff-done")
        != done_target
    ):
        raise RuntimeError(
            "refusing to remove a completion route no longer owned by this session"
        )
    result = subprocess.run(
        base + ["serve", f"--https={https_port}", "off"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError("failed to remove temporary Tailscale Serve route")
    after = serve_status(base)
    if route_proxy(after, dns_name, https_port) is not None:
        raise RuntimeError("temporary Tailscale Serve route remained after cleanup")


def process_cmdline(pid: int) -> str:
    if not process_alive(pid):
        return ""
    try:
        return (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", "replace")
        )
    except OSError:
        return ""


def process_start_time(pid: int) -> int | None:
    try:
        # Field 2 (comm) is parenthesized and can contain spaces. Split after
        # its final ')' so field 22 (starttime) remains at a stable offset.
        fields_after_comm = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields_after_comm[19])
    except (OSError, IndexError, ValueError):
        return None


def process_matches(pid: int, expected: Any) -> bool:
    try:
        return process_start_time(pid) == int(expected)
    except (TypeError, ValueError):
        return False


def worker_owned_by_session(
    pid: int, directory: Path, expected_start_time: Any = None
) -> bool:
    if expected_start_time is not None and not process_matches(pid, expected_start_time):
        return False
    command = process_cmdline(pid)
    return bool(
        command
        and "hermes_human_handoff.cli" in command
        and "_worker" in command
        and str(directory / "config.json") in command
    )


def child_owned_by_session(
    name: str, pid: int, directory: Path, runtime: dict[str, Any]
) -> bool:
    command = process_cmdline(pid)
    expected = runtime.get("child_start_times", {}).get(str(pid))
    if expected is not None and not process_matches(pid, expected):
        return False
    if not command:
        return False
    if name == "browser":
        profile = str(runtime.get("profile") or directory / "browser-profile")
        return f"--user-data-dir={profile}" in command
    if name == "websockify":
        return "websockify" in command and str(directory / "webroot") in command
    if name == "x11vnc":
        return "x11vnc" in command and str(directory / "vnc-passwd") in command
    if name == "xvfb":
        return "Xvfb" in command and f":{runtime.get('display')}" in command
    return False


def terminate_owned_children(directory: Path, runtime: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    owned: list[tuple[str, int]] = []
    for child in reversed(runtime.get("children", [])):
        name = str(child.get("name", ""))
        pid = int(child.get("pid", 0))
        if not process_alive(pid):
            continue
        if not child_owned_by_session(name, pid, directory, runtime):
            errors.append(f"refused to stop unverified {name} pid {pid}")
            continue
        owned.append((name, pid))
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    for name, pid in owned:
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if process_alive(pid):
            if child_owned_by_session(name, pid, directory, runtime):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            else:
                errors.append(f"refused to kill reused {name} pid {pid}")
    return errors


def scrub_session_files(directory: Path) -> None:
    for name in ("capability.json", "config.json", "vnc-passwd"):
        with contextlib.suppress(FileNotFoundError):
            (directory / name).unlink()
    for name in ("browser-profile", "webroot"):
        path = directory / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def session_worker_pid(
    directory: Path,
    public: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> int:
    candidates: list[Any] = [(public or {}).get("worker_pid")]
    with contextlib.suppress(Exception):
        candidates.append(read_json(directory / "launcher.json").get("worker_pid"))
    candidates.append((runtime or {}).get("worker_pid"))
    for candidate in candidates:
        with contextlib.suppress(TypeError, ValueError):
            pid = int(candidate)
            if pid > 0:
                return pid
    return 0


def session_worker_start_time(
    directory: Path,
    public: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> Any:
    candidates: list[Any] = [
        (public or {}).get("worker_start_time"),
        (runtime or {}).get("worker_start_time"),
    ]
    with contextlib.suppress(Exception):
        candidates.append(read_json(directory / "launcher.json").get("worker_start_time"))
    return next((candidate for candidate in candidates if candidate is not None), None)


def recovery_public(directory: Path, pid: int) -> dict[str, Any]:
    started_at = directory.stat().st_mtime
    purpose = "other"
    ttl = 0
    with contextlib.suppress(Exception):
        config = read_json(directory / "config.json")
        purpose = str(config.get("purpose", purpose))
        ttl = int(config.get("ttl", 0))
    return {
        "session_id": directory.name,
        "status": "failed",
        "purpose": purpose,
        "started_at": iso_at(started_at),
        "expires_at": iso_at(started_at + ttl),
        "worker_pid": pid,
        "error": "worker exited before publishing session state",
    }


def recover_stale_session(directory: Path) -> tuple[bool, list[str]]:
    public_path = directory / "public.json"
    public: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        public = read_json(public_path)
    runtime: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        runtime = read_json(directory / "runtime.json")
    pid = session_worker_pid(directory, public, runtime)
    if worker_owned_by_session(
        pid, directory, session_worker_start_time(directory, public, runtime)
    ):
        return False, []
    if not public and not runtime and not (directory / "launcher.json").exists():
        if time.time() - directory.stat().st_mtime < ORPHAN_STARTUP_GRACE:
            return False, []
    if not public:
        public = recovery_public(directory, pid)
    errors: list[str] = []
    route = runtime.get("route")
    if isinstance(route, dict):
        try:
            remove_tailnet_route(route)
        except Exception as exc:
            errors.append(str(exc))
    errors.extend(terminate_owned_children(directory, runtime))
    scrub_session_files(directory)
    cleanup_target = public.get("cleanup_target_status")
    recovered_status = (
        str(cleanup_target) if cleanup_target in {"stopped", "expired"} else "stopped"
    )
    public.update(
        {
            "status": "failed" if errors else recovered_status,
            "cleanup_error": "; ".join(errors) if errors else None,
            "cleanup_target_status": None,
            "recovered_after_worker_exit": True,
        }
    )
    atomic_json(public_path, public)
    return True, errors


def recover_stale_sessions(request_expired_stop: bool = True) -> dict[str, Any]:
    sessions_root = state_root() / "sessions"
    result: dict[str, Any] = {"stop_requested": [], "recovered": [], "errors": {}}
    if not sessions_root.exists():
        return result
    for directory in sorted(sessions_root.iterdir()):
        public_path = directory / "public.json"
        if not SESSION_RE.fullmatch(directory.name):
            continue
        with contextlib.suppress(Exception):
            public: dict[str, Any] = {}
            if public_path.exists():
                public = read_json(public_path)
            if public.get("status") in {"stopped", "expired"} and not public.get(
                "cleanup_error"
            ):
                continue
            runtime: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                runtime = read_json(directory / "runtime.json")
            pid = session_worker_pid(directory, public, runtime)
            if worker_owned_by_session(
                pid, directory, session_worker_start_time(directory, public, runtime)
            ):
                if public.get("expires_at"):
                    expires = dt.datetime.fromisoformat(
                        str(public["expires_at"]).replace("Z", "+00:00")
                    ).timestamp()
                    if request_expired_stop and expires <= time.time():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGTERM)
                        result["stop_requested"].append(directory.name)
                continue
            recovered, errors = recover_stale_session(directory)
            if recovered:
                result["recovered"].append(directory.name)
            if errors:
                result["errors"][directory.name] = errors
    return result


class HandoffWorker:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.session_dir = config_path.parent
        self.config = read_json(config_path)
        self.children: list[tuple[str, subprocess.Popen[Any]]] = []
        self.route: dict[str, Any] | None = None
        self.profile_lock: Any | None = None
        self.done_server: ThreadingHTTPServer | None = None
        self.stop_requested = False
        self.runtime: dict[str, Any] = {
            "children": [],
            "worker_pid": os.getpid(),
            "worker_start_time": process_start_time(os.getpid()),
        }
        self.started_at = time.time()
        self.expires_at = self.started_at + int(self.config["ttl"])

    def write_public(self, status: str, **extra: Any) -> None:
        existing: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            existing = read_json(self.session_dir / "public.json")
        existing.update(
            {
                "session_id": self.config["session_id"],
                "status": status,
                "purpose": self.config["purpose"],
                "started_at": iso_at(self.started_at),
                "expires_at": iso_at(self.expires_at),
                "worker_pid": os.getpid(),
                "worker_start_time": self.runtime["worker_start_time"],
            }
        )
        existing.update(extra)
        atomic_json(self.session_dir / "public.json", existing)

    def write_runtime(self) -> None:
        value = dict(self.runtime)
        value["route"] = self.route
        atomic_json(self.session_dir / "runtime.json", value)

    def publish_route(
        self, web_port: int, done_port: int, tailscale_binary: str
    ) -> None:
        self.route = plan_tailnet_route(web_port, done_port, tailscale_binary)
        # Journal exact ownership before the external Serve side effect. A
        # killed worker can then remove only this route during stale recovery.
        self.write_runtime()
        try:
            activate_tailnet_route(self.route)
        except Exception:
            # Clear the journal only when the route is verifiably absent or
            # belongs to someone else. Retain it when status is unavailable or
            # our mapping remains, so stale recovery can retry exact cleanup.
            try:
                base = [str(x) for x in self.route["base_command"]]
                current = serve_status(base)
                owned = route_proxy(
                    current,
                    str(self.route["dns_name"]),
                    int(self.route["https_port"]),
                ) == str(self.route["target"])
                if not owned:
                    self.route = None
                    self.write_runtime()
            except Exception:
                pass
            raise

    def spawn(
        self, name: str, command: list[str], env: dict[str, str] | None = None
    ) -> subprocess.Popen[Any]:
        log_path = self.session_dir / f"{name}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log = os.fdopen(fd, "ab", buffering=0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        self.children.append((name, process))
        self.runtime["children"] = [{"name": n, "pid": p.pid} for n, p in self.children]
        self.runtime.setdefault("child_start_times", {})[str(process.pid)] = process_start_time(process.pid)
        self.write_runtime()
        return process

    def prepare_profile(self) -> Path:
        mode = str(self.config.get("profile_mode", "disposable"))
        configured = str(self.config.get("profile_path") or "")
        if mode == "persistent":
            profile = validate_profile_directory(Path(configured), create=True)
            lock_fd = os.open(
                profile / ".hermes-human-handoff.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            lock = os.fdopen(lock_fd, "r+")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock.close()
                raise RuntimeError(
                    "persistent browser profile is already in use"
                ) from exc
            self.profile_lock = lock
        else:
            profile = self.session_dir / "browser-profile"
            if mode == "clone":
                copy_profile_template(Path(configured), profile)
            else:
                profile.mkdir(mode=0o700)
        configure_browser_profile(profile)
        return profile

    def prepare_webroot(self, novnc: Path) -> Path:
        webroot = self.session_dir / "webroot"
        webroot.mkdir(mode=0o700)
        assets = Path(__file__).parent / "assets"
        for name in ("handoff.html", "mobile-keyboard.js"):
            shutil.copy2(assets / name, webroot / name)
        for name in ("core", "vendor"):
            source_dir = novnc / name
            if not source_dir.is_dir() or source_dir.is_symlink():
                raise RuntimeError(f"noVNC {name} must be a real directory")
            if (
                source_dir.stat().st_uid != os.getuid()
                or source_dir.stat().st_mode & 0o022
            ):
                raise RuntimeError(f"noVNC {name} has unsafe ownership or permissions")
            for item in source_dir.rglob("*"):
                if (
                    item.is_symlink()
                    or not (item.is_file() or item.is_dir())
                    or item.stat().st_uid != os.getuid()
                    or item.stat().st_mode & 0o022
                ):
                    raise RuntimeError(f"noVNC {name} contains unsafe files")
            (webroot / name).symlink_to(source_dir, target_is_directory=True)
        return webroot

    def store_vnc_password(
        self, x11vnc: str, capability: str, password_file: Path, env: dict[str, str]
    ) -> None:
        result = subprocess.run(
            [x11vnc, "-storepasswd", str(password_file)],
            input=f"{capability}\n{capability}\ny\n",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        if result.returncode != 0 or not password_file.is_file():
            raise RuntimeError("x11vnc could not create its private password file")
        password_file.chmod(0o600)

    def _run_inner(self) -> int:
        deps = find_dependencies(local_only=bool(self.config["local_only"]))
        if not deps["ready"]:
            self.write_public(
                "failed", error="missing dependencies: " + ", ".join(deps["missing"])
            )
            return 2

        display = next(
            (
                number
                for number in range(90, 200)
                if not Path(f"/tmp/.X11-unix/X{number}").exists()
            ),
            None,
        )
        if display is None:
            self.write_public("failed", error="no free X display")
            return 2

        vnc_port, web_port, cdp_port, done_port = (
            free_loopback_port(),
            free_loopback_port(),
            free_loopback_port(),
            free_loopback_port(),
        )
        while len({vnc_port, web_port, cdp_port, done_port}) != 4:
            vnc_port, web_port, cdp_port, done_port = (
                free_loopback_port(),
                free_loopback_port(),
                free_loopback_port(),
                free_loopback_port(),
            )
        capability = "".join(secrets.choice(CAPABILITY_ALPHABET) for _ in range(8))
        atomic_json(self.session_dir / "capability.json", {"capability": capability})
        profile = self.prepare_profile()
        webroot = self.prepare_webroot(Path(deps["novnc"]))
        env = os.environ.copy()
        env["DISPLAY"] = f":{display}"

        self.runtime.update(
            {
                "display": display,
                "vnc_port": vnc_port,
                "web_port": web_port,
                "cdp_port": cdp_port,
                "done_port": done_port,
                "profile": str(profile),
            }
        )
        self.write_runtime()

        try:
            self.spawn(
                "xvfb",
                [
                    deps["xvfb"],
                    f":{display}",
                    "-screen",
                    "0",
                    "1280x900x24",
                    "-nolisten",
                    "tcp",
                ],
                env,
            )
            deadline = time.monotonic() + 10
            while (
                time.monotonic() < deadline
                and not Path(f"/tmp/.X11-unix/X{display}").exists()
            ):
                time.sleep(0.1)
            if not Path(f"/tmp/.X11-unix/X{display}").exists():
                raise RuntimeError("Xvfb did not create its display socket")

            password_file = self.session_dir / "vnc-passwd"
            self.store_vnc_password(deps["x11vnc"], capability, password_file, env)
            self.spawn(
                "x11vnc",
                [
                    deps["x11vnc"],
                    "-display",
                    f":{display}",
                    "-localhost",
                    "-rfbport",
                    str(vnc_port),
                    "-forever",
                    "-shared",
                    "-rfbauth",
                    str(password_file),
                    "-noxdamage",
                    "-repeat",
                    "-quiet",
                    "-noclipboard",
                    "-nosetclipboard",
                ],
                env,
            )
            wait_tcp(vnc_port)

            self.spawn(
                "websockify",
                [
                    deps["websockify"],
                    "--web",
                    str(webroot),
                    f"127.0.0.1:{web_port}",
                    f"127.0.0.1:{vnc_port}",
                ],
                env,
            )
            wait_http(f"http://127.0.0.1:{web_port}/handoff.html")
            self.done_server = start_done_server(self.session_dir, done_port)

            browser_args = [
                deps["browser"],
                f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={cdp_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-session-crashed-bubble",
                "--disable-features=PasswordManagerOnboarding,AutofillServerCommunication,AutofillEnablePayments",
                "--password-store=basic",
                "--window-position=0,0",
                "--window-size=1280,900",
                self.config["url"],
            ]
            browser_requires_no_sandbox = (
                os.geteuid() == 0
                or "ms-playwright" in str(deps["browser"])
                or os.environ.get("HUMAN_HANDOFF_NO_SANDBOX") == "1"
            )
            if browser_requires_no_sandbox:
                browser_args.insert(-1, "--no-sandbox")
            self.spawn("browser", browser_args, env)
            wait_http(f"http://127.0.0.1:{cdp_port}/json/version")
            if self.config["url"].startswith("chrome://"):
                open_internal_browser_target(cdp_port, self.config["url"])

            if self.config["local_only"]:
                handoff_base = f"http://127.0.0.1:{web_port}"
            else:
                self.publish_route(web_port, done_port, deps["tailscale"])
                handoff_base = (
                    f"https://{self.route['dns_name']}:{self.route['https_port']}"
                )

            handoff_url = f"{handoff_base}/handoff.html#{capability}"
            atomic_json(
                self.session_dir / "capability.json",
                {
                    "capability": capability,
                    "handoff_url": handoff_url,
                },
            )
            self.write_public(
                "ready",
                display=f":{display}",
                cdp_url=f"http://127.0.0.1:{cdp_port}",
                local_web_url=f"http://127.0.0.1:{web_port}/handoff.html",
                tailnet_https_port=self.route["https_port"] if self.route else None,
            )

            while not self.stop_requested and time.time() < self.expires_at:
                current = read_json(self.session_dir / "public.json")
                if current.get("status") == "handed_back":
                    self.stop_requested = True
                    break
                for name, process in self.children:
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited unexpectedly")
                time.sleep(0.25)
            return 0
        except Exception as exc:
            self.write_public("failed", error=str(exc))
            return 1
        finally:
            cleanup_error = self.cleanup()
            current = (
                read_json(self.session_dir / "public.json")
                if (self.session_dir / "public.json").exists()
                else {}
            )
            if current.get("status") == "failed":
                if cleanup_error:
                    self.write_public("failed", cleanup_error=cleanup_error)
            else:
                final = "expired" if time.time() >= self.expires_at else "stopped"
                if cleanup_error:
                    self.write_public(
                        "failed",
                        cleanup_error=cleanup_error,
                        cleanup_target_status=final,
                    )
                else:
                    self.write_public(final, cleanup_error=None)

    def run(self) -> int:
        """Run the worker with cleanup covering every setup phase."""
        try:
            return self._run_inner()
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.write_public("failed", error=str(exc))
            return 1
        finally:
            cleanup_error = self.cleanup()
            if (self.session_dir / "public.json").exists():
                current = read_json(self.session_dir / "public.json")
                if cleanup_error and current.get("status") == "failed":
                    self.write_public("failed", cleanup_error=cleanup_error)
                elif not cleanup_error and current.get("cleanup_target_status") in {
                    "stopped",
                    "expired",
                }:
                    self.write_public(
                        str(current["cleanup_target_status"]),
                        cleanup_error=None,
                        cleanup_target_status=None,
                    )

    def cleanup(self) -> str | None:
        error: str | None = None
        if self.done_server is not None:
            self.done_server.shutdown()
            self.done_server.server_close()
            self.done_server = None
        if self.route:
            try:
                remove_tailnet_route(self.route)
            except Exception as exc:
                error = str(exc)
        for _name, process in reversed(self.children):
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        for _name, process in reversed(self.children):
            remaining = max(0.0, deadline - time.monotonic())
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=remaining)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        for name in ("capability.json", "config.json", "vnc-passwd"):
            with contextlib.suppress(FileNotFoundError):
                (self.session_dir / name).unlink()
        profile = self.session_dir / "browser-profile"
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        webroot = self.session_dir / "webroot"
        if webroot.exists():
            shutil.rmtree(webroot, ignore_errors=True)
        if self.profile_lock is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.profile_lock.fileno(), fcntl.LOCK_UN)
            self.profile_lock.close()
            self.profile_lock = None
        return error


def cmd_doctor(args: argparse.Namespace) -> int:
    deps = find_dependencies(local_only=args.local_only)
    if deps.get("tailscale"):
        try:
            base, status = tailscale_base_command(deps["tailscale"])
            if not status.get("Self", {}).get("DNSName", "").rstrip("."):
                raise RuntimeError("Tailscale MagicDNS name is unavailable")
            serve = serve_status(base)
            dns_name = status["Self"]["DNSName"].rstrip(".")
            funnel_ports = [
                port
                for port in HTTPS_PORTS
                if route_allows_funnel(serve, dns_name, port)
            ]
            if funnel_ports:
                raise RuntimeError("Tailscale Funnel is enabled on a handoff port")
            deps["tailnet_connected"] = True
            deps["serve_access"] = True
            deps["funnel_disabled_on_handoff_ports"] = True
        except Exception as exc:
            deps["ready"] = False
            deps["tailnet_connected"] = False
            deps["serve_access"] = False
            deps.setdefault("missing", []).append("running-tailscale-with-serve-access")
            deps["tailscale_error"] = str(exc)
    print(json.dumps(deps, indent=2, sort_keys=True))
    return 0 if deps["ready"] else 2


def cmd_start(args: argparse.Namespace) -> int:
    try:
        target_url = validate_target_url(args.url, args.purpose)
        profile_template = getattr(args, "profile_template", None)
        persistent_profile = getattr(args, "persistent_profile", None)
        if profile_template:
            profile_mode = "clone"
            profile_path = str(validate_profile_directory(Path(profile_template)))
        elif persistent_profile:
            profile_mode = "persistent"
            profile_path = str(
                validate_profile_directory(Path(persistent_profile), create=True)
            )
        else:
            profile_mode = "disposable"
            profile_path = None
        if args.purpose == "address-setup" and profile_mode != "persistent":
            raise ValueError("address setup requires --persistent-profile")
    except ValueError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 2
    if not MIN_TTL <= args.ttl <= MAX_TTL:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"ttl must be {MIN_TTL}-{MAX_TTL} seconds",
                }
            )
        )
        return 2
    recovery = recover_stale_sessions()
    if recovery["errors"]:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "stale handoff cleanup failed",
                    "recovery": recovery,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    deps = find_dependencies(local_only=args.local_only)
    if not deps["ready"]:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "missing dependencies",
                    "missing": deps["missing"],
                }
            )
        )
        return 2

    session_id = utc_now().strftime("hh-%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    directory = state_root() / "sessions" / session_id
    directory.mkdir(parents=True, mode=0o700)
    config = {
        "session_id": session_id,
        "url": target_url,
        "ttl": args.ttl,
        "purpose": args.purpose,
        "local_only": bool(args.local_only),
        "profile_mode": profile_mode,
        "profile_path": profile_path,
    }
    config_path = directory / "config.json"
    atomic_json(config_path, config)
    log_path = directory / "worker.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log = os.fdopen(fd, "ab", buffering=0)
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hermes_human_handoff.cli",
                "_worker",
                str(config_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    atomic_json(
        directory / "launcher.json",
        {"worker_pid": worker.pid, "worker_start_time": process_start_time(worker.pid)},
    )

    deadline = time.monotonic() + 35
    public_path = directory / "public.json"
    while time.monotonic() < deadline:
        if public_path.exists():
            public = read_json(public_path)
            if public.get("status") == "ready":
                capability = read_json(directory / "capability.json")
                result = dict(public)
                result["handoff_url"] = capability["handoff_url"]
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            if public.get("status") == "failed":
                print(json.dumps(public, indent=2, sort_keys=True))
                return 1
        if worker.poll() is not None:
            print(
                json.dumps(
                    {
                        "session_id": session_id,
                        "status": "failed",
                        "error": "worker exited before readiness",
                    }
                )
            )
            return 1
        time.sleep(0.15)
    with contextlib.suppress(ProcessLookupError):
        os.kill(worker.pid, signal.SIGTERM)
    print(
        json.dumps(
            {"session_id": session_id, "status": "failed", "error": "startup timeout"}
        )
    )
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    if args.session_id:
        try:
            public = read_json(session_path(args.session_id) / "public.json")
        except (FileNotFoundError, ValueError) as exc:
            print(json.dumps({"status": "missing", "error": str(exc)}))
            return 2
        print(json.dumps(public, indent=2, sort_keys=True))
        return 0
    sessions_root = state_root() / "sessions"
    values: list[dict[str, Any]] = []
    if sessions_root.exists():
        for path in sorted(sessions_root.iterdir(), reverse=True):
            if SESSION_RE.fullmatch(path.name) and (path / "public.json").exists():
                with contextlib.suppress(Exception):
                    values.append(read_json(path / "public.json"))
    print(json.dumps(values, indent=2, sort_keys=True))
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    directory = session_path(args.session_id)
    public = read_json(directory / "public.json")
    if public.get("status") != "ready":
        print(
            json.dumps(
                {"status": public.get("status"), "error": "handoff is not active"}
            )
        )
        return 2
    capability = read_json(directory / "capability.json")
    print(capability["handoff_url"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    directory = session_path(args.session_id)
    public = read_json(directory / "public.json")
    if public.get("status") != "ready":
        print(
            json.dumps(
                {"status": public.get("status"), "error": "handoff is not active"}
            )
        )
        return 2
    script = Path(args.script).expanduser().resolve()
    if not script.is_file():
        print(json.dumps({"status": "failed", "error": f"script not found: {script}"}))
        return 2
    env = os.environ.copy()
    env["HANDOFF_SESSION_ID"] = args.session_id
    env["HANDOFF_CDP_URL"] = str(public["cdp_url"])
    result = subprocess.run([sys.executable, str(script), *args.script_args], env=env)
    return int(result.returncode)


def cmd_wait(args: argparse.Namespace) -> int:
    public_path = session_path(args.session_id) / "public.json"
    deadline = time.monotonic() + max(0.0, float(args.timeout))
    while True:
        current = read_json(public_path)
        if current.get("completion") == "done":
            print(json.dumps(current, indent=2, sort_keys=True))
            return 0
        if current.get("status") in {"failed", "expired", "stopped"}:
            print(json.dumps(current, indent=2, sort_keys=True))
            return 2
        if time.monotonic() >= deadline:
            value = dict(current)
            value["error"] = "wait timed out"
            print(json.dumps(value, indent=2, sort_keys=True))
            return 1
        time.sleep(0.2)


def cmd_stop(args: argparse.Namespace) -> int:
    directory = session_path(args.session_id)
    public_path = directory / "public.json"
    if not directory.exists():
        print(json.dumps({"status": "missing"}))
        return 2
    if not public_path.exists():
        recovered, errors = recover_stale_session(directory)
        if recovered and public_path.exists():
            current = read_json(public_path)
            print(json.dumps(current, indent=2, sort_keys=True))
            return 1 if errors else 0
        print(json.dumps({"status": "missing"}))
        return 2
    public = read_json(public_path)
    if public.get("status") in {"stopped", "expired"} and not public.get(
        "cleanup_error"
    ):
        print(json.dumps(public, indent=2, sort_keys=True))
        return 0
    runtime: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        runtime = read_json(directory / "runtime.json")
    pid = session_worker_pid(directory, public, runtime)
    expected_start_time = session_worker_start_time(directory, public, runtime)
    if worker_owned_by_session(pid, directory, expected_start_time):
        os.kill(pid, signal.SIGTERM)
    else:
        _recovered, errors = recover_stale_session(directory)
        current = read_json(public_path)
        print(json.dumps(current, indent=2, sort_keys=True))
        return 1 if errors else 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(0.2)
        current = read_json(public_path)
        if current.get("status") in {"stopped", "expired"} and not current.get(
            "cleanup_error"
        ):
            print(json.dumps(current, indent=2, sort_keys=True))
            return 0
        if not worker_owned_by_session(pid, directory, expected_start_time):
            _recovered, errors = recover_stale_session(directory)
            current = read_json(public_path)
            print(json.dumps(current, indent=2, sort_keys=True))
            return 1 if errors else 0
    print(
        json.dumps(
            {
                "session_id": args.session_id,
                "status": "failed",
                "error": "worker did not stop cleanly",
            }
        )
    )
    return 1


def cmd_cleanup(_args: argparse.Namespace) -> int:
    result = recover_stale_sessions()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


def worker_entry(config_path: str) -> int:
    path = Path(config_path)
    try:
        worker = HandoffWorker(path)
    except Exception:
        directory = path.parent
        scrub_session_files(directory)
        now = time.time()
        with contextlib.suppress(Exception):
            atomic_json(
                directory / "public.json",
                {
                    "session_id": directory.name,
                    "status": "failed",
                    "purpose": "other",
                    "started_at": iso_at(now),
                    "expires_at": iso_at(now),
                    "worker_pid": os.getpid(),
                    "error": "worker initialization failed",
                },
            )
        return 1

    def request_stop(_signum: int, _frame: Any) -> None:
        worker.stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return worker.run()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="hermes-human-handoff",
        description="Create an expiring Tailnet browser takeover session.",
    )
    root.add_argument("--version", action="version", version="0.1.0")
    sub = root.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check runtime dependencies")
    doctor.add_argument(
        "--local-only", action="store_true", help="skip Tailscale checks"
    )
    doctor.set_defaults(func=cmd_doctor)

    start = sub.add_parser("start", help="launch an isolated handoff browser")
    start.add_argument("--url", required=True, help="initial absolute http(s) URL")
    start.add_argument(
        "--ttl", type=int, default=DEFAULT_TTL, help="lifetime in seconds (60-3600)"
    )
    start.add_argument(
        "--purpose",
        choices=(
            "captcha",
            "payment",
            "oauth",
            "otp",
            "identity",
            "consent",
            "address-setup",
            "other",
        ),
        default="other",
    )
    start.add_argument(
        "--local-only", action="store_true", help="serve only on loopback for QA"
    )
    profiles = start.add_mutually_exclusive_group()
    profiles.add_argument(
        "--profile-template",
        help="clone a private Chromium profile into this disposable session",
    )
    profiles.add_argument(
        "--persistent-profile",
        help="use a private Chromium profile in place for address setup",
    )
    start.set_defaults(func=cmd_start)

    status = sub.add_parser("status", help="show sanitized session state")
    status.add_argument("session_id", nargs="?")
    status.set_defaults(func=cmd_status)

    url = sub.add_parser("url", help="reprint an active capability URL")
    url.add_argument("session_id")
    url.set_defaults(func=cmd_url)

    run = sub.add_parser(
        "run", help="run a local Playwright script against the managed browser"
    )
    run.add_argument("session_id")
    run.add_argument("script")
    run.add_argument("script_args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    wait = sub.add_parser(
        "wait", help="wait for the human to press Done or for the handoff to end"
    )
    wait.add_argument("session_id")
    wait.add_argument(
        "--timeout", type=float, default=DEFAULT_TTL, help="maximum seconds to wait"
    )
    wait.set_defaults(func=cmd_wait)

    stop = sub.add_parser(
        "stop", help="retire a handoff and destroy any disposable browser profile"
    )
    stop.add_argument("session_id")
    stop.set_defaults(func=cmd_stop)

    cleanup = sub.add_parser("cleanup", help="retire active sessions whose TTL elapsed")
    cleanup.set_defaults(func=cmd_cleanup)

    hidden = sub.add_parser("_worker", help=argparse.SUPPRESS)
    hidden.add_argument("config_path")
    hidden.set_defaults(func=lambda args: worker_entry(args.config_path))
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
