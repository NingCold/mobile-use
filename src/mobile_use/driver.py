"""Android adapter. No shell tool and no automatic replay of input on transport errors."""
from __future__ import annotations

import contextlib
import io
import sys

import adbutils
import uiautomator2 as u2
from uiautomator2.core import _jsonrpc_call
from defusedxml.ElementTree import fromstring

from .gestures import Gesture


class NoReplayDevice(u2.Device):
    def jsonrpc_call(self, method, params=None, timeout=10):
        # u2 3.7.0 normally restarts its service and replays a failed RPC. A tap may
        # already have happened. Pin the version and deliberately send exactly once.
        return _jsonrpc_call(self._dev, self._device_server_port, method, params, timeout, self._debug)


class AndroidDriver:
    def __init__(self, serial: str):
        self.serial = serial
        with contextlib.redirect_stdout(sys.stderr):
            self.device = NoReplayDevice(serial)
        self.device.settings["wait_timeout"] = 2.0

    @staticmethod
    def list_devices() -> list[dict]:
        return [{"serial": d.serial, "state": "device"} for d in adbutils.adb.device_list()]

    def header(self) -> dict:
        info = self.device.info
        # Vendor ROMs may give stale app_current()/dumpsys activity results.
        # UiAutomation's current package reflects the accessibility observation.
        return {"package": info.get("currentPackageName", ""), "width": info["displayWidth"],
                "height": info["displayHeight"], "rotation": info["displayRotation"],
                "screen_on": info.get("screenOn", True)}

    def hierarchy(self) -> str:
        xml = self.device.dump_hierarchy()
        self.hierarchy_diagnostics = {"source": "default_windows", "active_window_fallback": False}
        def useful_app_nodes(value):
            return sum(1 for node in fromstring(value).iter("node")
                       if node.get("visible-to-user") != "false" and
                       node.get("package") not in ("com.android.systemui", "miui.systemui.plugin") and
                       (node.get("text") or node.get("content-desc") or
                        any(node.get(flag) == "true" for flag in ("clickable", "scrollable", "focused"))))
        if useful_app_nodes(xml) == 0:
            # Read-only alternative for ROMs/apps whose window enumeration is empty.
            # Keep hidden-node filtering; never invent elements from screenshots.
            self.hierarchy_diagnostics["active_window_fallback"] = True
            try:
                active = self.device.dump_hierarchy(root_in_active=True)
                if useful_app_nodes(active):
                    xml = active
                    self.hierarchy_diagnostics["source"] = "active_window"
            except Exception as exc:
                self.hierarchy_diagnostics["active_window_error"] = type(exc).__name__
        return xml

    def screenshot(self) -> tuple[bytes, tuple[int, int]]:
        image = self.device.screenshot()
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue(), image.size

    def execute(self, gesture: Gesture):
        if gesture.kind == "tap":
            x, y = gesture.points[0]
            result = self.device.long_click(x, y, gesture.hold_ms / 1000)
        else:
            # One device-side RPC. duration is PER SEGMENT in this u2 version.
            result = self.device.swipe_points(gesture.points, gesture.segment_ms / 1000)
        if result is False:
            raise RuntimeError("Android did not acknowledge the gesture")

    def set_text(self, text: str):
        result = self.device(focused=True).set_text(text)
        if result is False:
            raise RuntimeError("Android did not acknowledge text replacement")

    def key(self, key: str):
        result = self.device.press(key)
        if result is False:
            raise RuntimeError("Android did not acknowledge the key")

    def launch(self, package: str):
        self.device.app_start(package, wait=False, stop=False)
