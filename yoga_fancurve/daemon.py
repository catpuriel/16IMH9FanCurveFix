"""Background systemd daemon for Lenovo Yoga Pro 9 16IMH9 safe dynamic fan control."""

from __future__ import annotations

import argparse
import atexit
import collections
import grp
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_PATH,
    PRESETS,
    RUN_DIR,
    SOCKET_PATH,
    STATUS_PATH,
    FanCurveController,
    load_config,
    save_config,
    validate_and_sanitize_config,
)
from .hardware import YogaHardware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yoga_fancurve.daemon")


class FanCurveDaemon:
    """Controls Yoga Pro 9 16IMH9 fans via lenovo_wmi_other with ironclad failsafes."""

    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        status_path: Path = STATUS_PATH,
        socket_path: Path = SOCKET_PATH,
        socket_group: str | None = None,
    ) -> None:
        self.config_path = config_path
        self.status_path = status_path
        self.socket_path = socket_path
        self.socket_group = socket_group

        self.hw = YogaHardware()
        self.cfg = load_config(self.config_path)
        self.controller = FanCurveController(self.cfg)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._suspended = False

        self._last_written_targets: tuple[int, int] | None = None
        self._last_write_ts: float = 0.0
        self._consecutive_sensor_failures = 0
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=90)
        self._latest_status: dict[str, Any] = {}
        self._server_sock: socket.socket | None = None

    def _safe_restore_auto(self, reason: str) -> None:
        log.info("Restoring BIOS automatic fan control (%s)", reason)
        self.hw.restore_bios_auto()
        self._last_written_targets = (0, 0)
        self._last_write_ts = time.monotonic()

    def _setup_run_dir(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.status_path.parent, 0o755)

    def _start_ipc_server(self) -> None:
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o660)

        if self.socket_group:
            try:
                gid = grp.getgrnam(self.socket_group).gr_gid
                os.chown(self.socket_path, 0, gid)
            except (KeyError, PermissionError, OSError) as exc:
                log.warning("Could not chgrp socket to %s: %s", self.socket_group, exc)

        srv.listen(8)
        srv.settimeout(1.0)
        self._server_sock = srv

        t = threading.Thread(target=self._ipc_accept_loop, name="ipc-server", daemon=True)
        t.start()
        log.info("IPC socket listening on %s", self.socket_path)

    def _ipc_accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(conn,), daemon=True
            ).start()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(3.0)
            try:
                chunks: list[bytes] = []
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                    if b"\n" in data or sum(len(c) for c in chunks) > 131072:
                        break
                raw = b"".join(chunks).decode("utf-8").strip()
                if not raw:
                    return
                req = json.loads(raw)
                resp = self._process_ipc_request(req)
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except OSError:
                pass

    def _process_ipc_request(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        with self._lock:
            if cmd == "get_state":
                return {
                    "ok": True,
                    "config": self.cfg,
                    "status": self._latest_status,
                    "history": list(self._history),
                }

            if cmd == "set_config":
                new_cfg = validate_and_sanitize_config(req.get("config", {}))
                self.cfg = save_config(new_cfg, self.config_path)
                self.controller.update_config(self.cfg)
                if not self.cfg["enabled"] or self.cfg["preset"] == "BIOS Auto":
                    self.controller.reset_state()
                    self._safe_restore_auto("disabled via GUI")
                self._wake_event.set()
                return {"ok": True, "config": self.cfg}

            if cmd == "set_preset":
                preset_name = req.get("preset")
                if preset_name not in PRESETS:
                    return {"ok": False, "error": f"Unknown preset: {preset_name}"}
                updated = dict(self.cfg)
                updated["preset"] = preset_name
                if preset_name == "BIOS Auto":
                    updated["enabled"] = False
                else:
                    p = PRESETS[preset_name]
                    updated["enabled"] = True
                    updated["points"] = [list(pt) for pt in p["points"]]
                    updated["hysteresis_c"] = p["hysteresis_c"]
                    updated["auto_below_first_point"] = p["auto_below_first_point"]
                self.cfg = save_config(updated, self.config_path)
                self.controller.update_config(self.cfg)
                if not self.cfg["enabled"]:
                    self.controller.reset_state()
                    self._safe_restore_auto("BIOS Auto preset selected")
                self._wake_event.set()
                return {"ok": True, "config": self.cfg}

            if cmd == "restore_auto":
                updated = dict(self.cfg)
                updated["enabled"] = False
                self.cfg = save_config(updated, self.config_path)
                self.controller.update_config(self.cfg)
                self.controller.reset_state()
                self._safe_restore_auto("manual restore_auto command")
                self._wake_event.set()
                return {"ok": True, "config": self.cfg}

            if cmd == "suspend_prepare":
                self._suspended = True
                self.controller.reset_state()
                self._safe_restore_auto("system suspend")
                return {"ok": True}

            if cmd == "suspend_resume":
                self._suspended = False
                self.controller.reset_state()
                self._last_written_targets = None
                self._wake_event.set()
                return {"ok": True}

            return {"ok": False, "error": f"Unknown command: {cmd}"}

    def _write_status_file(self, payload: dict[str, Any]) -> None:
        try:
            tmp = self.status_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.status_path)
        except OSError as exc:
            log.warning("Failed writing status file: %s", exc)

    def _tick(self) -> None:
        with self._lock:
            cfg = dict(self.cfg)
            suspended = self._suspended

        snap = self.hw.read_snapshot(query_gpu=cfg["include_gpu_temp"])
        now_ts = time.time()
        mono = time.monotonic()

        if suspended:
            return

        if not self.hw.is_fan_interface_ready():
            log.warning("lenovo_wmi_other fan interface not available yet")
            status = {
                "timestamp": now_ts,
                "ready": False,
                "error": "lenovo_wmi_other hwmon fan attributes not found (check modprobe options)",
                "cpu_temp_c": snap.cpu_temp_c,
                "gpu_temp_c": snap.gpu_temp_c,
                "gpu_state": snap.gpu_state,
                "ac_online": snap.ac_online,
                "ac_source": snap.ac_source,
                "platform_profile": snap.platform_profile,
                "mode": "unavailable",
            }
            with self._lock:
                self._latest_status = status
            self._write_status_file(status)
            return

        if snap.cpu_temp_c is None:
            self._consecutive_sensor_failures += 1
            log.warning(
                "CPU temperature read failed (%d consecutive)",
                self._consecutive_sensor_failures,
            )
            if self._consecutive_sensor_failures >= 3:
                with self._lock:
                    self.controller.reset_state()
                    self._safe_restore_auto("sensor read failure failsafe")
            return

        self._consecutive_sensor_failures = 0

        with self._lock:
            decision = self.controller.step(
                cpu_temp_c=snap.cpu_temp_c,
                gpu_temp_c=snap.gpu_temp_c,
                ac_online=snap.ac_online,
            )
            desired = (decision.fan1_target, decision.fan2_target)

            # Write only when target changes, OR every 30s while in manual mode to ensure EC stays synced
            should_write = (
                self._last_written_targets != desired
                or (desired != (0, 0) and (mono - self._last_write_ts) >= 30.0)
            )
            if should_write:
                if self.hw.set_fan_targets(desired[0], desired[1]):
                    if self._last_written_targets != desired:
                        log.info(
                            "Mode=%s Temp=%.1f°C (CPU=%.1f GPU=%s) AC=%s -> Target=(%d, %d) RPM",
                            decision.mode,
                            decision.effective_temp_c,
                            snap.cpu_temp_c,
                            f"{snap.gpu_temp_c:.1f}" if snap.gpu_temp_c is not None else snap.gpu_state,
                            snap.ac_source,
                            desired[0],
                            desired[1],
                        )
                    self._last_written_targets = desired
                    self._last_write_ts = mono

            status = {
                "timestamp": now_ts,
                "ready": True,
                "error": None,
                "cpu_temp_c": snap.cpu_temp_c,
                "gpu_temp_c": snap.gpu_temp_c,
                "gpu_state": snap.gpu_state,
                "effective_temp_c": decision.effective_temp_c,
                "fan1_rpm": snap.fan1_rpm,
                "fan2_rpm": snap.fan2_rpm,
                "fan1_target": decision.fan1_target,
                "fan2_target": decision.fan2_target,
                "raw_curve_rpm": decision.raw_curve_rpm,
                "ac_online": snap.ac_online,
                "ac_source": snap.ac_source,
                "platform_profile": snap.platform_profile,
                "mode": decision.mode,
                "enabled": cfg["enabled"],
                "ac_only": cfg["ac_only"],
                "preset": cfg["preset"],
            }
            self._latest_status = status
            self._history.append(
                {
                    "t": now_ts,
                    "cpu": snap.cpu_temp_c,
                    "gpu": snap.gpu_temp_c,
                    "eff": decision.effective_temp_c,
                    "f1": snap.fan1_rpm or 0,
                    "f2": snap.fan2_rpm or 0,
                    "tgt": decision.fan1_target,
                }
            )

        self._write_status_file(status)

    def run(self) -> None:
        self._setup_run_dir()
        if not self.config_path.exists():
            try:
                self.cfg = save_config(self.cfg, self.config_path)
            except OSError as exc:
                log.warning("Could not create initial config at %s: %s", self.config_path, exc)

        # Register ironclad shutdown handlers
        atexit.register(self._safe_restore_auto, "atexit")

        def _handle_sig(signum: int, _frame: Any) -> None:
            log.info("Received signal %s, shutting down cleanly...", signum)
            self._stop_event.set()
            self._wake_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, _handle_sig)

        self._start_ipc_server()
        log.info(
            "Yoga Pro 9 16IMH9 fan daemon started (enabled=%s, ac_only=%s, preset=%s)",
            self.cfg["enabled"],
            self.cfg["ac_only"],
            self.cfg["preset"],
        )

        try:
            while not self._stop_event.is_set():
                try:
                    self._tick()
                except Exception:
                    log.exception("Unhandled error in control tick; restoring BIOS auto")
                    self._safe_restore_auto("exception failsafe")
                    time.sleep(3.0)
                    continue

                interval = self.cfg.get("poll_interval_s", 1.5)
                self._wake_event.wait(timeout=interval)
                self._wake_event.clear()
        finally:
            self._safe_restore_auto("daemon shutdown")
            if self._server_sock is not None:
                try:
                    self._server_sock.close()
                except OSError:
                    pass
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass


def send_ipc_command(cmd_payload: dict[str, Any], socket_path: Path = SOCKET_PATH) -> dict[str, Any]:
    """Helper used by GUI and sleep hooks to talk to the running daemon."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3.0)
        s.connect(str(socket_path))
        s.sendall((json.dumps(cmd_payload) + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
        return json.loads(b"".join(chunks).decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lenovo Yoga Pro 9 16IMH9 Fan Curve Daemon")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--status", type=Path, default=STATUS_PATH)
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--group", type=str, default=None, help="Group owning the IPC control socket")
    parser.add_argument(
        "--restore-auto-only",
        action="store_true",
        help="Immediately restore BIOS auto fan control and exit (used by ExecStopPost)",
    )
    parser.add_argument(
        "--ipc-cmd",
        type=str,
        choices=["suspend_prepare", "suspend_resume", "restore_auto"],
        help="Send a control command to the running daemon and exit",
    )
    args = parser.parse_args(argv)

    if args.restore_auto_only:
        hw = YogaHardware()
        ok = hw.restore_bios_auto()
        return 0 if ok else 1

    if args.ipc_cmd:
        try:
            resp = send_ipc_command({"cmd": args.ipc_cmd}, socket_path=args.socket)
            return 0 if resp.get("ok") else 1
        except OSError:
            if args.ipc_cmd in ("suspend_prepare", "restore_auto"):
                YogaHardware().restore_bios_auto()
            return 0

    daemon = FanCurveDaemon(
        config_path=args.config,
        status_path=args.status,
        socket_path=args.socket,
        socket_group=args.group,
    )
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
