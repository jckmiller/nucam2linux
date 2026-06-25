#!/usr/bin/env python3
"""
nucam2linux – nucam.py
Stream an Android camera over USB to a Linux v4l2loopback device via scrcpy.

Usage:
    python3 nucam.py            # interactive terminal UI
    python3 nucam.py --no-ui    # headless/daemon mode (systemd)
"""
from __future__ import annotations

import argparse
import configparser
import glob
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Optional rich UI (gracefully degraded if not installed)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
CONF_PATHS = [
    Path(__file__).parent / "nucam.conf",
    Path.home() / "nucam2linux" / "nucam.conf",
    Path("/etc/nucam2linux/nucam.conf"),
]
VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    # defaults
    cfg.read_dict({
        "camera":  {"camera_facing": "back", "resolution": "1280x720", "fps": "30"},
        "device":  {"v4l2_sink": "auto", "adb_serial": "auto"},
        "scrcpy":  {"extra_flags": "", "reconnect_delay": "3"},
    })
    for path in CONF_PATHS:
        if path.exists():
            cfg.read(path)
            break
    return cfg


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config(cfg: configparser.ConfigParser) -> list[str]:
    """Validate config values and reset any invalid ones to safe defaults.

    Returns a list of human-readable warning strings for each corrected value
    so they can be logged at startup.
    """
    warnings: list[str] = []

    # fps: must be a positive integer
    fps_raw = cfg.get("camera", "fps", fallback="30")
    try:
        if int(fps_raw) <= 0:
            raise ValueError
    except ValueError:
        warnings.append(f"Invalid fps '{fps_raw}' in config — using default 30.")
        cfg.set("camera", "fps", "30")

    # reconnect_delay: must be a positive integer
    delay_raw = cfg.get("scrcpy", "reconnect_delay", fallback="3")
    try:
        if int(delay_raw) <= 0:
            raise ValueError
    except ValueError:
        warnings.append(f"Invalid reconnect_delay '{delay_raw}' in config — using default 3.")
        cfg.set("scrcpy", "reconnect_delay", "3")

    # resolution: must be exactly two positive integers separated by 'x'
    res_raw = cfg.get("camera", "resolution", fallback="1280x720")
    parts = res_raw.lower().split("x")
    if len(parts) != 2 or not all(p.strip().isdigit() and int(p.strip()) > 0 for p in parts):
        warnings.append(f"Invalid resolution '{res_raw}' in config — using default 1280x720.")
        cfg.set("camera", "resolution", "1280x720")

    # camera_facing: must be "front" or "back"
    facing = cfg.get("camera", "camera_facing", fallback="back").strip().lower()
    if facing not in ("front", "back"):
        warnings.append(f"Invalid camera_facing '{facing}' in config — using default 'back'.")
        cfg.set("camera", "camera_facing", "back")

    return warnings


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def adb_cmd(args: list[str], serial: Optional[str] = None) -> list[str]:
    base = ["adb"]
    if serial and serial != "auto":
        base += ["-s", serial]
    return base + args


def get_devices() -> list[dict]:
    """Return a list of connected ADB devices as dicts with serial/state."""
    try:
        out = subprocess.check_output(["adb", "devices"], text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def get_online_device(serial: str = "auto") -> Optional[str]:
    """Return the serial of the first online device (or the requested one)."""
    devices = get_devices()
    for d in devices:
        if d["state"] != "device":
            continue
        if serial == "auto" or d["serial"] == serial:
            return d["serial"]
    return None


def get_device_prop(serial: str, prop: str) -> str:
    try:
        return subprocess.check_output(
            adb_cmd(["shell", f"getprop {prop}"], serial),
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def get_device_info(serial: str) -> dict:
    model = get_device_prop(serial, "ro.product.model")
    brand = get_device_prop(serial, "ro.product.brand")
    android = get_device_prop(serial, "ro.build.version.release")
    return {"model": model, "brand": brand, "android": android}


# ---------------------------------------------------------------------------
# v4l2loopback helpers
# ---------------------------------------------------------------------------

def find_loopback_devices() -> list[str]:
    """Return /dev/videoX paths that belong to v4l2loopback."""
    devices = []
    for dev in sorted(glob.glob("/dev/video*")):
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "--device", dev, "--info"],
                text=True, stderr=subprocess.DEVNULL
            )
            if "v4l2 loopback" in out.lower() or "nucam2linux" in out.lower():
                devices.append(dev)
        except subprocess.CalledProcessError:
            # Non-zero exit is expected for regular (non-loopback) camera devices.
            pass
        except OSError as exc:
            # Unexpected — e.g. permission denied on the device node.
            print(f"[WARN] Could not probe {dev}: {exc}", file=sys.stderr)
    return devices


def resolve_v4l2_sink(cfg_value: str) -> Optional[str]:
    if cfg_value != "auto":
        if os.path.exists(cfg_value):
            return cfg_value
        else:
            return None
    devs = find_loopback_devices()
    return devs[0] if devs else None


# ---------------------------------------------------------------------------
# State shared between the worker thread and the UI
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    status: str = "Initializing…"
    device_serial: Optional[str] = None
    device_model: str = "—"
    device_brand: str = "—"
    device_android: str = "—"
    v4l2_sink: str = "—"
    resolution: str = "—"
    fps: str = "—"
    stream_start: Optional[float] = None
    scrcpy_pid: Optional[int] = None
    error_msg: str = ""
    running: bool = True
    restart_requested: bool = False
    log_lines: list = field(default_factory=list)

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{ts}] {msg}")
        if len(self.log_lines) > 200:
            self.log_lines.pop(0)
        # Always echo to stderr so journalctl/--no-ui works
        print(f"[{ts}] {msg}", file=sys.stderr, flush=True)

    def uptime_str(self) -> str:
        if self.stream_start is None:
            return "—"
        secs = int(time.time() - self.stream_start)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Worker thread: device watching + scrcpy management
# ---------------------------------------------------------------------------

class StreamWorker(threading.Thread):
    def __init__(self, state: AppState, cfg: configparser.ConfigParser):
        super().__init__(daemon=True)
        self.state = state
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None

    # ---- public API -------------------------------------------------------

    def stop(self):
        self.state.running = False
        self._kill_scrcpy()

    def restart(self):
        self.state.restart_requested = True
        self._kill_scrcpy()

    # ---- internal ---------------------------------------------------------

    def _kill_scrcpy(self):
        if self._proc and self._proc.poll() is None:
            self.state.log("Stopping scrcpy…")
            try:
                self._proc.terminate()
                self._proc.wait(timeout=4)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self.state.scrcpy_pid = None
        self.state.stream_start = None

    def _build_scrcpy_cmd(self, serial: str, sink: str) -> list[str]:
        cfg = self.cfg
        facing = cfg.get("camera", "camera_facing", fallback="back")
        res = cfg.get("camera", "resolution", fallback="1280x720")
        fps = cfg.get("camera", "fps", fallback="30")
        extra = cfg.get("scrcpy", "extra_flags", fallback="").strip()

        # Parse WxH — guaranteed to be valid at this point by validate_config()
        w, h = res.lower().split("x")
        self.state.resolution = f"{w}x{h}"
        self.state.fps = fps

        cmd = [
            "scrcpy",
            "-s", serial,
            "--video-source=camera",
            f"--camera-facing={facing}",
            f"--camera-size={w}x{h}",
            f"--max-fps={fps}",
            f"--v4l2-sink={sink}",
            "--no-audio",
            "--no-window",
        ]
        if extra:
            cmd += shlex.split(extra)
        return cmd

    def run(self):
        cfg = self.cfg
        adb_serial_cfg = cfg.get("device", "adb_serial", fallback="auto")
        v4l2_cfg = cfg.get("device", "v4l2_sink", fallback="auto")
        reconnect_delay = int(cfg.get("scrcpy", "reconnect_delay", fallback="3"))

        while self.state.running:
            self.state.restart_requested = False

            # ── Step 1: find ADB device ──────────────────────────────────
            self.state.status = "🔍 Waiting for ADB device…"
            self.state.log("Scanning for ADB device…")
            serial = None
            while self.state.running and not self.state.restart_requested:
                serial = get_online_device(adb_serial_cfg)
                if serial:
                    break
                time.sleep(2)
            if not self.state.running:
                break
            if self.state.restart_requested:
                continue

            self.state.device_serial = serial
            info = get_device_info(serial)
            self.state.device_model = info["model"]
            self.state.device_brand = info["brand"]
            self.state.device_android = info["android"]
            self.state.log(f"Device found: {info['brand']} {info['model']} (Android {info['android']}) [{serial}]")

            # ── Step 2: find v4l2loopback sink ───────────────────────────
            self.state.status = "🎥 Locating v4l2loopback device…"
            sink = resolve_v4l2_sink(v4l2_cfg)
            if not sink:
                self.state.status = "❌ No v4l2loopback device found"
                self.state.error_msg = (
                    "No v4l2loopback device found. Run:\n"
                    "  sudo modprobe v4l2loopback devices=1 video_nr=10 "
                    'card_label="nucam2linux" exclusive_caps=1'
                )
                self.state.log("ERROR: " + self.state.error_msg.replace("\n", " "))
                time.sleep(reconnect_delay)
                continue
            self.state.v4l2_sink = sink
            self.state.error_msg = ""
            self.state.log(f"Using v4l2 sink: {sink}")

            # ── Step 3: launch scrcpy ────────────────────────────────────
            cmd = self._build_scrcpy_cmd(serial, sink)
            self.state.log(f"Launching: {' '.join(cmd)}")
            self.state.status = "📡 Streaming"
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                self.state.scrcpy_pid = self._proc.pid
                self.state.stream_start = time.time()
                self.state.log(f"scrcpy started (PID {self._proc.pid})")

                # Drain stdout/stderr from scrcpy
                for line in self._proc.stdout:
                    line = line.rstrip()
                    if line:
                        self.state.log(f"scrcpy: {line}")
                        # Detect disconnection messages
                        if any(kw in line.lower() for kw in
                               ["disconnected", "error", "failed", "no device"]):
                            self.state.status = "⚠️  Stream interrupted"
                    if not self.state.running or self.state.restart_requested:
                        break

                self._proc.wait()
                rc = self._proc.returncode
                self.state.log(f"scrcpy exited with code {rc}")
                self._proc = None
                self.state.scrcpy_pid = None
                self.state.stream_start = None

            except FileNotFoundError:
                self.state.status = "❌ scrcpy not found"
                self.state.error_msg = "scrcpy is not installed or not in PATH. Run setup.sh."
                self.state.log("ERROR: scrcpy not found. Run setup.sh.")
                self.state.running = False
                break
            except Exception as exc:
                self.state.log(f"ERROR: {exc}")

            if not self.state.running:
                break

            if not self.state.restart_requested:
                self.state.status = f"🔄 Reconnecting in {reconnect_delay}s…"
                self.state.device_serial = None
                time.sleep(reconnect_delay)


# ---------------------------------------------------------------------------
# Terminal UI (rich)
# ---------------------------------------------------------------------------

def make_ui(state: AppState) -> Layout:
    """Build a rich Layout from the current AppState."""
    # ── Header ──────────────────────────────────────────────────────────────
    header_text = Text()
    header_text.append("  nucam2linux ", style="bold white")
    header_text.append(f"v{VERSION}", style="dim")
    header_text.append("  │  ", style="dim")
    header_text.append(state.status, style="bold green" if "Streaming" in state.status else "yellow")
    header = Panel(header_text, style="blue", box=box.ROUNDED, height=3)

    # ── Device info table ────────────────────────────────────────────────
    info_table = Table(box=None, show_header=False, padding=(0, 2))
    info_table.add_column("Key",   style="dim", width=20)
    info_table.add_column("Value", style="bold white")

    info_table.add_row("📱 Device",
        f"{state.device_brand} {state.device_model}" if state.device_serial else "[dim]—[/dim]")
    info_table.add_row("🤖 Android", state.device_android if state.device_serial else "[dim]—[/dim]")
    info_table.add_row("🔌 ADB serial",
        state.device_serial if state.device_serial else "[dim]waiting…[/dim]")
    info_table.add_row("🎥 v4l2 sink", state.v4l2_sink)
    info_table.add_row("📐 Resolution", state.resolution)
    info_table.add_row("🎞  FPS target", state.fps)
    info_table.add_row("⏱  Uptime", state.uptime_str())

    if state.error_msg:
        info_table.add_row("❌ Error", f"[red]{state.error_msg}[/red]")

    device_panel = Panel(info_table, title="[bold]Stream Info[/bold]",
                         box=box.ROUNDED, style="white")

    # ── Log panel ───────────────────────────────────────────────────────
    log_lines = state.log_lines[-18:]
    log_text = Text()
    for line in log_lines:
        if "ERROR" in line:
            log_text.append(line + "\n", style="red")
        elif "scrcpy:" in line:
            log_text.append(line + "\n", style="dim")
        else:
            log_text.append(line + "\n", style="white")
    log_panel = Panel(log_text, title="[bold]Log[/bold]", box=box.ROUNDED,
                      style="white")

    # ── Key hints ───────────────────────────────────────────────────────
    keys = Text("  [r] Restart stream    [q] Quit", style="dim")
    footer = Panel(keys, box=box.ROUNDED, height=3, style="blue")

    # ── Compose ─────────────────────────────────────────────────────────
    layout = Layout()
    layout.split_column(
        Layout(header,      name="header",  size=3),
        Layout(name="body", ratio=1),
        Layout(footer,      name="footer",  size=3),
    )
    layout["body"].split_row(
        Layout(device_panel, name="info",  ratio=2),
        Layout(log_panel,    name="log",   ratio=3),
    )
    return layout


def run_ui(state: AppState, worker: StreamWorker):
    """Run the interactive rich live UI, handling keypresses."""
    import select
    import tty
    import termios

    console = Console()
    old_settings = None
    try:
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    except Exception:
        pass

    try:
        with Live(make_ui(state), console=console, refresh_per_second=4,
                  screen=True, transient=False) as live:
            while state.running:
                live.update(make_ui(state))
                # Non-blocking key check
                if old_settings:
                    r, _, _ = select.select([sys.stdin], [], [], 0.25)
                    if r:
                        ch = sys.stdin.read(1).lower()
                        if ch == "q":
                            state.log("Quit requested by user.")
                            state.running = False
                        elif ch == "r":
                            state.log("Restart requested by user.")
                            worker.restart()
                else:
                    time.sleep(0.25)
    finally:
        if old_settings:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            except Exception:
                pass


def run_headless(state: AppState):
    """Run without UI — just print logs to stderr (for systemd)."""
    try:
        while state.running:
            time.sleep(1)
    except KeyboardInterrupt:
        state.running = False


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def setup_signals(state: AppState, worker: StreamWorker):
    def _handler(sig, frame):
        state.log(f"Signal {sig} received. Shutting down…")
        state.running = False
        worker.stop()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT,  _handler)


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def preflight(no_ui: bool) -> bool:
    ok = True
    issues = []

    if not shutil.which("adb"):
        issues.append("adb not found in PATH. Run setup.sh.")
        ok = False
    if not shutil.which("scrcpy"):
        issues.append("scrcpy not found in PATH. Run setup.sh.")
        ok = False
    if not shutil.which("v4l2-ctl"):
        issues.append("v4l2-ctl not found in PATH. Install v4l-utils: sudo apt-get install v4l-utils")
        ok = False
    if not RICH_AVAILABLE and not no_ui:
        issues.append("Python 'rich' library not installed. Run: pip3 install rich")
        # non-fatal — will fall back to headless

    for issue in issues:
        print(f"[ERROR] {issue}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="nucam2linux – Android USB webcam bridge for Linux"
    )
    parser.add_argument("--no-ui", action="store_true",
                        help="Run headless (daemon/systemd mode, no terminal UI)")
    parser.add_argument("--version", action="version", version=f"nucam2linux {VERSION}")
    args = parser.parse_args()

    use_ui = not args.no_ui and RICH_AVAILABLE and sys.stdout.isatty()

    if not preflight(args.no_ui):
        sys.exit(1)

    cfg   = load_config()
    state = AppState()
    state.log(f"nucam2linux {VERSION} starting…")

    # Validate config values; log any fields that were corrected
    for warning in validate_config(cfg):
        state.log(f"WARN: {warning}")

    # Pre-populate config values in state for display before first connection
    state.resolution = cfg.get("camera", "resolution", fallback="1280x720")
    state.fps        = cfg.get("camera", "fps",        fallback="30")
    v4l2_cfg         = cfg.get("device",  "v4l2_sink",  fallback="auto")
    if v4l2_cfg != "auto":
        state.v4l2_sink = v4l2_cfg
    else:
        sink = resolve_v4l2_sink("auto")
        state.v4l2_sink = sink if sink else "auto (not yet found)"

    worker = StreamWorker(state, cfg)
    setup_signals(state, worker)

    worker.start()

    try:
        if use_ui:
            run_ui(state, worker)
        else:
            if not RICH_AVAILABLE and not args.no_ui:
                print("[WARN] rich not installed — running in headless mode. "
                      "pip3 install rich to enable the TUI.", file=sys.stderr)
            run_headless(state)
    finally:
        state.log("Shutting down…")
        state.running = False
        worker.stop()
        worker.join(timeout=6)
        state.log("Goodbye.")


if __name__ == "__main__":
    main()
