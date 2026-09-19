"""
Live Appium Driver Action Runner.
Executes real-time selector queries, clicks, input typing, and bounding box queries against Appium sessions.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional
import requests
from PyQt6.QtCore import QObject, pyqtSignal

from xgen.core.session_manager import SessionManager
from xgen.platform.factory import get_platform_backend
from xgen.utils.rect import Rect

logger = logging.getLogger("xgen.runner")


@dataclass
class TestElementResult:
    success: bool
    element_id: str = ""
    duration_ms: float = 0.0
    bounding_rect: Optional[Rect] = None
    error_message: str = ""


class DriverRunner(QObject):
    """
    Executes live Appium REST calls on background thread for rapid element testing.
    Serializes requests via thread locking to prevent socket timeouts on single-threaded WinAppDriver.
    """
    test_completed = pyqtSignal(object)  # TestElementResult
    test_finished = pyqtSignal(object, object)  # TestElementResult, caller_token/card
    action_completed = pyqtSignal(str, bool, str)  # action_name, success, message

    def __init__(self, session_manager: SessionManager, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.session_manager = session_manager
        import threading
        self._driver_lock = threading.RLock()

    def async_test_xpath(self, xpath: str, card: object = None) -> None:
        """Asynchronously test selector on worker thread and emit test_finished on main thread."""
        import threading
        def _run():
            res = self.test_xpath(xpath)
            self.test_finished.emit(res, card)
        threading.Thread(target=_run, daemon=True).start()

    def async_click_xpath(self, xpath: str) -> None:
        """Asynchronously click element on worker thread."""
        import threading
        threading.Thread(target=lambda: self.click_xpath(xpath), daemon=True).start()

    def async_hover_xpath(self, xpath: str) -> None:
        """Asynchronously hover mouse over element on worker thread."""
        import threading
        threading.Thread(target=lambda: self.hover_xpath(xpath), daemon=True).start()

    def async_send_keys_xpath(self, xpath: str, text: str) -> None:
        """Asynchronously type text into element on worker thread."""
        import threading
        threading.Thread(target=lambda: self.send_keys_xpath(xpath, text), daemon=True).start()

    def test_xpath(self, xpath: str, timeout: float = 12.0) -> TestElementResult:
        """
        Queries Appium POST /session/{id}/element with the given XPath.
        Returns TestElementResult with elapsed time and rect if found.
        """
        session_id = self.session_manager.session_id
        base_url = self.session_manager.base_url

        if not session_id:
            res = TestElementResult(success=False, error_message="No active Appium session.")
            self.test_completed.emit(res)
            return res

        url = f"{base_url}/session/{session_id}/element"
        payload = {"using": "xpath", "value": xpath}

        with self._driver_lock:
            t0 = time.perf_counter()
            try:
                r = self.session_manager.http_session.post(url, json=payload, timeout=timeout)
                elapsed = (time.perf_counter() - t0) * 1000.0

                if r.status_code == 200:
                    val = r.json().get("value", {})
                    element_id = ""
                    if isinstance(val, dict):
                        element_id = val.get("ELEMENT") or val.get("element-6066-11e4-a52e-4f735466cecf") or ""
                    elif isinstance(val, str):
                        element_id = val

                    # Query element bounding rect if found
                    rect = self._get_rect_internal(session_id, base_url, element_id) if element_id else None

                    res = TestElementResult(
                        success=True,
                        element_id=element_id,
                        duration_ms=elapsed,
                        bounding_rect=rect
                    )
                    self.test_completed.emit(res)
                    return res
                else:
                    err_msg = f"HTTP {r.status_code}: {r.text[:120]}"
                    res = TestElementResult(success=False, duration_ms=elapsed, error_message=err_msg)
                    self.test_completed.emit(res)
                    return res
            except requests.exceptions.Timeout:
                elapsed = (time.perf_counter() - t0) * 1000.0
                res = TestElementResult(
                    success=False,
                    duration_ms=elapsed,
                    error_message="Query timed out (Driver was busy searching deep UI tree)"
                )
                self.test_completed.emit(res)
                return res
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000.0
                err_str = str(e)
                if "Read timed out" in err_str or "timeout" in err_str.lower():
                    err_str = "Driver query timed out (Tree search took >20s)"
                elif "Connection refused" in err_str or "Max retries exceeded" in err_str:
                    err_str = "Connection to Appium server lost"
                res = TestElementResult(success=False, duration_ms=elapsed, error_message=err_str)
                self.test_completed.emit(res)
                return res

    def click_xpath(self, xpath: str) -> bool:
        """Finds element and executes click action."""
        test_res = self.test_xpath(xpath)
        if not test_res.success or not test_res.element_id:
            self.action_completed.emit("Click", False, test_res.error_message or "Element not found")
            return False

        session_id = self.session_manager.session_id
        base_url = self.session_manager.base_url
        url = f"{base_url}/session/{session_id}/element/{test_res.element_id}/click"

        with self._driver_lock:
            try:
                r = self.session_manager.http_session.post(url, json={}, timeout=10.0)
                if r.status_code == 200:
                    self.action_completed.emit("Click", True, "Clicked successfully")
                    return True
                else:
                    self.action_completed.emit("Click", False, f"Click failed: HTTP {r.status_code}")
                    return False
            except Exception as e:
                self.action_completed.emit("Click", False, str(e))
                return False

    def send_keys_xpath(self, xpath: str, text: str) -> bool:
        """Finds element and types text."""
        test_res = self.test_xpath(xpath)
        if not test_res.success or not test_res.element_id:
            self.action_completed.emit("Type", False, test_res.error_message or "Element not found")
            return False

        session_id = self.session_manager.session_id
        base_url = self.session_manager.base_url
        url = f"{base_url}/session/{session_id}/element/{test_res.element_id}/value"
        payload = {"text": text, "value": list(text)}

        with self._driver_lock:
            try:
                r = self.session_manager.http_session.post(url, json=payload, timeout=10.0)
                if r.status_code == 200:
                    self.action_completed.emit("Type", True, f"Typed '{text}' successfully")
                    return True
                else:
                    self.action_completed.emit("Type", False, f"Type failed: HTTP {r.status_code}")
                    return False
            except Exception as e:
                self.action_completed.emit("Type", False, str(e))
                return False

    def hover_xpath(self, xpath: str) -> bool:
        """Finds element and moves mouse cursor over it via W3C Actions, /moveto, or Win32 cursor fallback."""
        test_res = self.test_xpath(xpath)
        if not test_res.success or not test_res.element_id:
            self.action_completed.emit("Hover", False, test_res.error_message or "Element not found")
            return False

        session_id = self.session_manager.session_id
        base_url = self.session_manager.base_url

        # 1. Try standard W3C Actions pointerMove
        w3c_payload = {
            "actions": [{
                "type": "pointer",
                "id": "mouse",
                "parameters": {"pointerType": "mouse"},
                "actions": [{
                    "type": "pointerMove",
                    "duration": 100,
                    "origin": {"ELEMENT": test_res.element_id}
                }]
            }]
        }
        with self._driver_lock:
            try:
                r = self.session_manager.http_session.post(f"{base_url}/session/{session_id}/actions", json=w3c_payload, timeout=5.0)
                if r.status_code == 200:
                    self.action_completed.emit("Hover", True, "Hovered element successfully")
                    return True
            except Exception:
                pass

            # 2. Try JSONWP /moveto endpoint
            try:
                r_moveto = self.session_manager.http_session.post(
                    f"{base_url}/session/{session_id}/moveto",
                    json={"element": test_res.element_id},
                    timeout=5.0
                )
                if r_moveto.status_code == 200:
                    self.action_completed.emit("Hover", True, "Hovered element successfully")
                    return True
            except Exception:
                pass

            # 3. Fallback to physical cursor move via test_res.bounding_rect
            if test_res.bounding_rect:
                try:
                    cx = test_res.bounding_rect.center_x
                    cy = test_res.bounding_rect.center_y
                    if get_platform_backend().move_cursor_to(cx, cy):
                        self.action_completed.emit("Hover", True, f"Moved mouse to ({cx}, {cy})")
                        return True
                except Exception as e:
                    self.action_completed.emit("Hover", False, str(e))
                    return False

            self.action_completed.emit("Hover", False, "Could not hover element")
            return False

    def _get_rect_internal(self, session_id: str, base_url: str, element_id: str) -> Optional[Rect]:
        """Query element bounding rectangle using W3C /rect or JSONWP /location + /size fallback."""
        # 1. Try standard W3C /rect
        url_rect = f"{base_url}/session/{session_id}/element/{element_id}/rect"
        try:
            r = self.session_manager.http_session.get(url_rect, timeout=2.0)
            if r.status_code == 200:
                v = r.json().get("value", {})
                if isinstance(v, dict) and "x" in v and "width" in v:
                    x = int(v.get("x", 0))
                    y = int(v.get("y", 0))
                    w = int(v.get("width", 0))
                    h = int(v.get("height", 0))
                    if w > 0 and h > 0:
                        return Rect(left=x, top=y, right=x + w, bottom=y + h)
        except Exception:
            pass

        # 2. Fallback to WinAppDriver JSONWP /location and /size endpoints
        try:
            url_loc = f"{base_url}/session/{session_id}/element/{element_id}/location"
            url_size = f"{base_url}/session/{session_id}/element/{element_id}/size"
            r_loc = self.session_manager.http_session.get(url_loc, timeout=2.0)
            r_size = self.session_manager.http_session.get(url_size, timeout=2.0)
            if r_loc.status_code == 200 and r_size.status_code == 200:
                v_loc = r_loc.json().get("value", {})
                v_size = r_size.json().get("value", {})
                if isinstance(v_loc, dict) and isinstance(v_size, dict):
                    x = int(v_loc.get("x", 0))
                    y = int(v_loc.get("y", 0))
                    w = int(v_size.get("width", 0))
                    h = int(v_size.get("height", 0))
                    if w > 0 and h > 0:
                        return Rect(left=x, top=y, right=x + w, bottom=y + h)
        except Exception:
            pass

        return None
