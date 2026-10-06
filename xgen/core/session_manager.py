"""
Appium Session Manager (Windows Driver / WinAppDriver and macOS Mac2 Driver).
Maintains session lifecycle, window handles, heartbeats, and REST communication.

Which driver is on the other end is decided per session, not per machine —
see xgen/core/driver_dialect.py — because the Appium server URL is
user-configurable and may well be a Mac reached from a Windows box.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot, Qt

from xgen.config import XGenConfig
from xgen.core.driver_dialect import (
    DriverDialect,
    DriverDialectStore,
    dialect_for_platform,
    dialect_from_capabilities,
)

logger = logging.getLogger("xgen.session")


class SessionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"


@dataclass
class WindowInfo:
    handle: str          # e.g. "0x001A0B2C" or decimal handle
    title: str = ""
    is_active: bool = False


@dataclass
class SessionInfo:
    session_id: str
    appium_url: str
    app_name: str
    windows: List[WindowInfo] = field(default_factory=list)
    active_handle: str = ""


class SessionWorker(QObject):
    """Worker object to run HTTP REST calls off the UI thread."""
    connect_success = pyqtSignal(object)   # SessionInfo
    connect_failed = pyqtSignal(str)       # Error message
    source_ready = pyqtSignal(str)         # XML
    source_failed = pyqtSignal(str)        # Error message
    window_switched = pyqtSignal(str)      # handle
    window_switch_failed = pyqtSignal(str) # Error message
    handles_ready = pyqtSignal(list)       # list of WindowInfo

    def __init__(self, session_manager: SessionManager):
        super().__init__()
        self.mgr = session_manager

    @pyqtSlot(object)
    def do_connect(self, config: XGenConfig) -> None:
        try:
            info = self.mgr._create_session(config)
            self.connect_success.emit(info)
        except Exception as e:
            logger.error("Session creation failed: %s", e)
            self.connect_failed.emit(str(e))

    @pyqtSlot()
    def do_fetch_source(self) -> None:
        try:
            xml = self.mgr._fetch_source_internal()
            self.source_ready.emit(xml)
        except Exception as e:
            if not self.mgr.session_id or "terminated" in str(e).lower():
                logger.info("Source fetch aborted: session disconnected.")
            else:
                logger.error("Failed to fetch source: %s", e)
                self.source_failed.emit(str(e))

    @pyqtSlot(str)
    def do_switch_window(self, handle: str) -> None:
        try:
            self.mgr._switch_window_internal(handle)
            self.window_switched.emit(handle)
        except Exception as e:
            logger.error("Failed to switch window: %s", e)
            self.window_switch_failed.emit(str(e))

    @pyqtSlot()
    def do_refresh_handles(self) -> None:
        try:
            windows = self.mgr._refresh_handles_internal()
            self.handles_ready.emit(windows)
        except Exception as e:
            logger.warning("Failed to refresh window handles: %s", e)


class SessionManager(QObject):
    """
    Manages active Appium Windows Driver session state and communication.
    """
    state_changed = pyqtSignal(str)        # "disconnected", "connecting", "connected", "lost"
    session_started = pyqtSignal(object)   # SessionInfo
    windows_updated = pyqtSignal(list)     # List[WindowInfo]
    error_occurred = pyqtSignal(str)

    # Internal signals to dispatch tasks to worker thread
    _req_connect = pyqtSignal(object)
    _req_switch_window = pyqtSignal(str)
    _req_refresh_handles = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.state: SessionState = SessionState.DISCONNECTED
        self.session_info: Optional[SessionInfo] = None
        self.current_config: Optional[XGenConfig] = None
        self._session_id: Optional[str] = None
        self._base_url: str = "http://127.0.0.1:4723"
        self._http_session = requests.Session()
        self._heartbeat_failures: int = 0
        self._disconnect_requested: bool = False
        self._delete_thread: Optional[threading.Thread] = None
        # Heartbeat probes, tried in order. A driver that answers "unknown
        # command" for one is moved to the next rather than being treated as
        # a dead session — Mac2Driver has no window handles, and reporting a
        # healthy session as LOST every 90s would be worse than not probing.
        self._heartbeat_probes: List[str] = ["window/handles", "timeouts"]
        self._heartbeat_probe_index: int = 0

        # Threading for background execution
        self._worker_thread = QThread()
        self._worker = SessionWorker(self)
        self._worker.moveToThread(self._worker_thread)

        # Wire internal requests to worker slots
        self._req_connect.connect(self._worker.do_connect)
        self._req_switch_window.connect(self._worker.do_switch_window)
        self._req_refresh_handles.connect(self._worker.do_refresh_handles)

        # Worker signals
        self._worker.connect_success.connect(self._on_connect_success)
        self._worker.connect_failed.connect(self._on_connect_failed)
        self._worker.window_switched.connect(self._on_window_switched)
        self._worker.window_switch_failed.connect(self._on_window_switch_failed)
        self._worker.handles_ready.connect(self._on_handles_ready)

        self._worker_thread.start()

        # Heartbeat timer (30s interval)
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(30000)
        self._heartbeat_timer.timeout.connect(self._check_heartbeat)

    def close(self) -> None:
        """Cleanup threads and close session without emitting signals to dead Qt widgets."""
        self._heartbeat_timer.stop()
        self.blockSignals(True)
        if self._session_id:
            try:
                self.disconnect()
            except Exception:
                pass
        if self._delete_thread and self._delete_thread.is_alive():
            self._delete_thread.join(timeout=3.0)
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        self.blockSignals(False)

    # --- Public API ---

    @staticmethod
    def check_server_status(url: str, timeout_seconds: float = 3.0) -> tuple[bool, str]:
        """
        Check if Appium / WinAppDriver server is reachable at the given URL.
        Returns (is_running, message).
        """
        base = url.rstrip("/")
        try:
            r = requests.get(f"{base}/status", timeout=timeout_seconds)
            if r.status_code == 200:
                data = r.json()
                value = data.get("value", {})
                ready = value.get("ready", True) if isinstance(value, dict) else True
                msg = value.get("message", "Server is ready") if isinstance(value, dict) else "Server is ready"
                return (True, f"Server running: {msg}")
            return (False, f"Server returned HTTP {r.status_code}")
        except requests.ConnectionError:
            return (False, f"Connection refused at {url}. Is Appium server started?")
        except Exception as e:
            return (False, f"Status check failed: {e}")

    @staticmethod
    def resolve_dialect(config: XGenConfig) -> DriverDialect:
        """
        Pick the driver dialect for a session that hasn't started yet: the
        user's explicit choice if they made one, otherwise the OS xGen is
        running on (asked of the platform backend so this module stays free
        of platform branching). Once the server answers, its own reported
        capabilities take over — see _create_session.
        """
        if config.target_platform:
            return dialect_for_platform(config.target_platform)
        from xgen.platform.factory import get_platform_backend
        return dialect_for_platform(get_platform_backend().default_driver_platform())

    def connect(self, config: XGenConfig) -> None:
        """Asynchronously connect to or start an Appium session."""
        if self.state == SessionState.CONNECTING:
            logger.warning("Already connecting to session, ignoring duplicate request.")
            return
        if self.state == SessionState.CONNECTED:
            logger.warning("Already connected to session, ignoring connect request.")
            return
        self._disconnect_requested = False
        self.current_config = config
        self._base_url = config.appium_url.rstrip("/")
        self._heartbeat_probe_index = 0

        # Activate the dialect before anything parses a tree or builds a
        # selector for this session.
        dialect = self.resolve_dialect(config)
        DriverDialectStore.instance().set_active(dialect)
        logger.info("Session target: %s", dialect.display_name)

        self._set_state(SessionState.CONNECTING)
        # Dispatch to worker thread via queued signal
        self._req_connect.emit(config)

    def disconnect(self) -> None:
        """Terminate active session."""
        self._heartbeat_timer.stop()
        self._disconnect_requested = True
        sid = self._session_id
        self._session_id = None
        self.session_info = None

        if sid:
            def _delete():
                try:
                    self._http_session.delete(f"{self._base_url}/session/{sid}", timeout=5.0)
                    logger.info("Session %s deleted.", sid)
                except Exception as e:
                    logger.warning("Error deleting session %s: %s", sid, e)

            self._delete_thread = threading.Thread(target=_delete, daemon=True)
            self._delete_thread.start()

        self._set_state(SessionState.DISCONNECTED)

    def reconnect(self, config: Optional[XGenConfig] = None) -> None:
        """Disconnect active session (if any) and connect with specified or current config."""
        cfg = config or self.current_config
        if not cfg:
            logger.warning("No configuration available to reconnect.")
            return
        self.disconnect()
        self.connect(cfg)

    def switch_window(self, handle: str) -> None:
        """Asynchronously switch active window context in session."""
        if not self._session_id:
            return
        self._req_switch_window.emit(handle)

    def refresh_window_handles(self) -> None:
        """Query driver for open window handles."""
        if not self._session_id:
            return
        self._req_refresh_handles.emit()

    def get_source(self, timeout_seconds: int = 60) -> str:
        """
        Synchronous fetch of XML source (call from worker threads).
        """
        return self._fetch_source_internal(timeout_seconds)

    @property
    def http_session(self) -> requests.Session:
        """Shared keep-alive HTTP session for all Appium REST calls."""
        return self._http_session

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_connected(self) -> bool:
        return self.state == SessionState.CONNECTED and bool(self._session_id)

    @property
    def base_url(self) -> str:
        return self._base_url

    def find_element_by_xpath(self, xpath: str) -> Optional[str]:
        """
        Execute POST /session/{id}/element with xpath locator.
        Returns element ID string or None.
        """
        if not self._session_id:
            return None
        try:
            url = f"{self._base_url}/session/{self._session_id}/element"
            body = {"using": "xpath", "value": xpath}
            r = self._http_session.post(url, json=body, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                val = data.get("value", {})
                if isinstance(val, dict):
                    # W3C uses 'element-6066-11e4-a52e-4f735466cecf', JSONWP uses 'ELEMENT'
                    return val.get("element-6066-11e4-a52e-4f735466cecf") or val.get("ELEMENT")
                elif isinstance(val, str):
                    return val
            return None
        except Exception as e:
            logger.debug("find_element_by_xpath failed for %s: %s", xpath, e)
            return None

    def get_element_attribute(self, element_id: str, attr_name: str) -> Optional[str]:
        """Query element attribute value via REST."""
        if not self._session_id or not element_id:
            return None
        try:
            url = f"{self._base_url}/session/{self._session_id}/element/{element_id}/attribute/{attr_name}"
            r = self._http_session.get(url, timeout=5.0)
            if r.status_code == 200:
                val = r.json().get("value")
                return str(val) if val is not None else None
            return None
        except Exception as e:
            logger.debug("get_element_attribute %s failed: %s", attr_name, e)
            return None

    # --- Internal HTTP Operations (run in worker thread) ---

    def _create_session(self, config: XGenConfig) -> SessionInfo:
        base = config.appium_url.rstrip("/")
        dialect = self.resolve_dialect(config)
        is_mac = dialect.key == "mac2"

        # Construct Appium 2 / W3C capabilities with JSONWP backwards compatibility
        app_target = config.app_path.strip()
        app_window = config.app_top_level_window.strip()
        bundle_id = (config.app_bundle_id or "").strip()

        # 1. First wait for any in-flight session deletion thread to finish before touching server
        if self._delete_thread and self._delete_thread.is_alive():
            logger.debug("Waiting for previous session deletion to complete...")
            self._delete_thread.join(timeout=4.0)
            time.sleep(0.2)

        # 2. Check if an active session already exists on the server
        from xgen.utils.window_finder import get_open_windows, normalize_handle

        try:
            r_sessions = self._http_session.get(f"{base}/sessions", timeout=2.0)
            if r_sessions.status_code == 200:
                data = r_sessions.json()
                active_list = data.get("value", [])
                if isinstance(active_list, list) and len(active_list) > 0:
                    reusable = self._find_reusable_session(
                        active_list,
                        dialect,
                        app_target=app_target,
                        app_window=app_window,
                        bundle_id=bundle_id,
                    )

                    if reusable is not None:
                        sid, existing_app = reusable
                        logger.info("Reusing compatible Appium session: %s (target: %s)", sid, existing_app or "Root")
                        self._session_id = str(sid)
                        windows = self._refresh_handles_internal()
                        active_handle = app_window or (windows[0].handle if windows else "")

                        app_name = existing_app or "Desktop Root"
                        if app_window:
                            try:
                                norm_target = normalize_handle(app_window)
                                for w in get_open_windows():
                                    if w.hwnd and normalize_handle(w.hwnd) == norm_target:
                                        app_name = w.title or w.exe_name or f"Window {app_window}"
                                        break
                                else:
                                    app_name = f"Window {app_window}"
                            except Exception:
                                app_name = f"Window {app_window}"
                        elif app_name.lower() == "root":
                            app_name = "Desktop Root"
                        elif "\\" in app_name or "/" in app_name:
                            app_name = Path(app_name).stem

                        return SessionInfo(
                            session_id=str(sid),
                            appium_url=base,
                            app_name=str(app_name),
                            windows=windows,
                            active_handle=active_handle
                        )

                    # Nothing on the server matches what was asked for. Standalone
                    # WinAppDriver only hosts one session at a time, so the existing
                    # one is cleared to make room — same behavior as before.
                    first_sess = active_list[0]
                    sid = first_sess.get("id") or first_sess.get("sessionId")
                    if sid:
                        logger.info(
                            "No existing session matches the requested target (%s). "
                            "Deleting session %s and creating a new one.",
                            app_target or app_window or bundle_id or "Desktop Root", sid,
                        )
                        try:
                            self._http_session.delete(f"{base}/session/{sid}", timeout=5.0)
                            time.sleep(0.3)
                        except Exception as del_err:
                            logger.warning("Could not delete old incompatible session %s: %s", sid, del_err)
        except Exception as e:
            logger.debug("Active sessions query note: %s", e)

        w3c_caps: Dict[str, Any] = {
            "platformName": dialect.platform_name,
            "appium:automationName": dialect.automation_name,
            "appium:newCommandTimeout": 3600,
        }

        if is_mac:
            # XCUITest sessions are always scoped to one application — macOS has
            # no equivalent of WinAppDriver's desktop-wide "Root" target, so a
            # Mac session always names an app. Mac2Driver would silently default
            # to Finder; name it explicitly instead so the UI tells the truth
            # about what the tree belongs to.
            if app_target and not bundle_id and app_target != "Root":
                w3c_caps["appium:appPath"] = app_target
                app_name = Path(app_target).stem
            else:
                effective_bundle = bundle_id
                app_name = ""
                if not effective_bundle:
                    # Mac2Driver's own default is Finder, which is almost never
                    # what someone wants to inspect. Target the frontmost app
                    # instead — the first entry the platform backend reports.
                    try:
                        running = get_open_windows()
                        frontmost = next((w for w in running if w.bundle_id), None)
                        if frontmost is not None:
                            effective_bundle = frontmost.bundle_id
                            app_name = frontmost.title
                            logger.info(
                                "No macOS app specified; attaching to the frontmost app (%s).",
                                effective_bundle,
                            )
                    except Exception as e:
                        logger.debug("Could not determine the frontmost app: %s", e)
                if not effective_bundle:
                    effective_bundle = "com.apple.finder"
                    logger.info("No running app could be identified; falling back to Finder.")
                w3c_caps["appium:bundleId"] = effective_bundle
                if not app_name:
                    app_name = effective_bundle.split(".")[-1].title()
            # Mac2Driver terminates the application it was attached to when the
            # session is deleted, so without this, disconnecting xGen closes the
            # user's app — it quit TextEdit out from under them. xGen is an
            # inspector: it must never destroy the thing it is inspecting, so
            # skipAppKill is on for every Mac session, not just explicit attach.
            # (The Windows driver has no equivalent — WinAppDriver leaves the
            # target process alone on session delete.)
            w3c_caps["appium:skipAppKill"] = True
            if config.attach_to_running:
                # Attach to the app as it stands instead of restarting it.
                w3c_caps["appium:noReset"] = True

            self._apply_extra_capabilities(w3c_caps, config)

            # Appium 2+ only; no legacy JSONWP peer exists for this driver, so
            # the deprecated desiredCapabilities block is deliberately omitted.
            payload = {
                "capabilities": {
                    "alwaysMatch": w3c_caps,
                    "firstMatch": [{}]
                }
            }
        else:
            if not app_target and not app_window:
                # Default to Desktop Root if nothing specified
                app_target = "Root"

            app_name = "Desktop Root"
            if app_window:
                w3c_caps["appium:appTopLevelWindow"] = app_window
                try:
                    norm_target = normalize_handle(app_window)
                    for w in get_open_windows():
                        if w.hwnd and normalize_handle(w.hwnd) == norm_target:
                            app_name = w.title or w.exe_name or f"Window {app_window}"
                            break
                    else:
                        app_name = f"Window {app_window}"
                except Exception:
                    app_name = f"Window {app_window}"
            else:
                w3c_caps["appium:app"] = app_target
                if app_target and app_target != "Root":
                    app_name = Path(app_target).stem

            self._apply_extra_capabilities(w3c_caps, config)

            # Add non-prefixed desiredCapabilities for JSONWP / standalone WinAppDriver
            jsonwp_caps = dict(w3c_caps)
            for k, v in list(w3c_caps.items()):
                if k.startswith("appium:"):
                    jsonwp_caps[k[7:]] = v

            payload = {
                "capabilities": {
                    "alwaysMatch": w3c_caps,
                    "firstMatch": [{}]
                },
                "desiredCapabilities": jsonwp_caps
            }

        logger.info("Connecting to Appium at %s with payload: %s", base, payload)
        r = self._http_session.post(
            f"{base}/session",
            json=payload,
            timeout=float(config.session_connect_timeout_seconds)
        )

        if r.status_code not in (200, 201):
            err_msg = f"HTTP {r.status_code}: {r.text}"
            try:
                err_json = r.json()
                err_msg = err_json.get("value", {}).get("message", err_msg)
            except Exception:
                pass
            raise RuntimeError(f"Appium session creation failed: {err_msg}")

        resp_data = r.json()
        sid = resp_data.get("sessionId") or resp_data.get("value", {}).get("sessionId")
        if not sid:
            raise RuntimeError(f"Invalid session response from Appium: {resp_data}")

        self._session_id = sid
        logger.info("Appium session established: %s", sid)

        # The server's own reported capabilities are authoritative — if it
        # negotiated a different driver than we assumed, follow it rather than
        # parsing its XML with the wrong vocabulary.
        value = resp_data.get("value")
        server_caps = value.get("capabilities") if isinstance(value, dict) else None
        if not isinstance(server_caps, dict) and isinstance(value, dict):
            server_caps = value
        confirmed = dialect_from_capabilities(server_caps if isinstance(server_caps, dict) else None)
        if confirmed is not None and confirmed.key != dialect.key:
            logger.info(
                "Server negotiated %s (expected %s); switching dialect to match.",
                confirmed.display_name, dialect.display_name,
            )
            dialect = confirmed
        DriverDialectStore.instance().set_active(dialect)

        # Retrieve initial window handles
        windows = self._refresh_handles_internal()
        if not app_window and (not app_target or app_target == "Root"):
            active_handle = ""
        else:
            active_handle = app_window or (windows[0].handle if windows else "")

        info = SessionInfo(
            session_id=sid,
            appium_url=base,
            app_name=app_name,
            windows=windows,
            active_handle=active_handle
        )
        return info

    @staticmethod
    def _find_reusable_session(
        sessions: List[Dict[str, Any]],
        dialect: DriverDialect,
        *,
        app_target: str = "",
        app_window: str = "",
        bundle_id: str = "",
    ) -> Optional[tuple]:
        """
        Find a session already on the server that we can *positively identify*
        as targeting what the user asked for. Returns (session_id, target) or None.

        The bar is deliberately "positively identify", not "can't prove it's
        different". A session whose capabilities don't say what it targets —
        which is what a stale session left by another automation tool usually
        looks like — is unidentifiable, so xGen starts a fresh one rather than
        silently adopting someone else's app session and labelling it Desktop
        Root. The cost of being wrong here is high (you inspect a tree that
        isn't the app you chose); the cost of being strict is one extra session
        creation.

        Every session is considered, not just the first: with several on the
        server, the matching one may be any of them.
        """
        from xgen.utils.window_finder import normalize_handle

        for sess in sessions or []:
            if not isinstance(sess, dict):
                continue
            sid = sess.get("id") or sess.get("sessionId")
            if not sid:
                continue
            caps = sess.get("capabilities") or {}
            if not isinstance(caps, dict):
                continue

            # Never reuse a session belonging to a different driver (a leftover
            # Windows session when the user now wants a Mac one).
            existing_dialect = dialect_from_capabilities(caps)
            if existing_dialect is not None and existing_dialect.key != dialect.key:
                continue

            if dialect.key == "mac2":
                existing_bundle = str(caps.get("appium:bundleId") or caps.get("bundleId") or "").strip()
                existing_app_path = str(caps.get("appium:appPath") or caps.get("appPath") or "").strip()
                wanted = (bundle_id or app_target).strip()
                if wanted and wanted in (existing_bundle, existing_app_path):
                    return str(sid), existing_bundle or existing_app_path
                continue

            existing_app = str(caps.get("appium:app") or caps.get("app") or "").strip()
            existing_window_cap = str(
                caps.get("appium:appTopLevelWindow") or caps.get("appTopLevelWindow") or ""
            ).strip()

            user_wants_root = not app_target and not app_window
            # An explicit "Root" and nothing else. An absent app capability is
            # unknown, NOT root, and a session scoped to a top-level window is
            # definitively not the desktop — either one previously satisfied
            # this check and let a foreign app session masquerade as Desktop Root.
            existing_is_root = existing_app.lower() == "root" and not existing_window_cap

            same_app = False
            if existing_app and app_target and existing_app.lower() != "root":
                try:
                    same_app = Path(existing_app).resolve() == Path(app_target).resolve()
                except (OSError, ValueError):
                    same_app = existing_app == app_target

            same_window = bool(
                app_window
                and existing_window_cap
                and normalize_handle(existing_window_cap) == normalize_handle(app_window)
            )

            if (user_wants_root and existing_is_root) or same_app or same_window:
                return str(sid), existing_app

        return None

    @staticmethod
    def parse_extra_capabilities(raw: str) -> Dict[str, Any]:
        """
        Parse the user's custom-capability JSON from the Session dialog.

        Raises ValueError with a readable message so the dialog can refuse to
        connect rather than letting the driver reject a malformed session.
        """
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from e
        if not isinstance(parsed, dict):
            raise ValueError("Capabilities must be a JSON object, e.g. {\"appium:showServerLogs\": true}")
        return parsed

    def _apply_extra_capabilities(self, caps: Dict[str, Any], config: XGenConfig) -> None:
        """Merge the user's own capabilities in last, so they can override anything xGen set."""
        raw = getattr(config, "extra_capabilities", "") or ""
        try:
            extra = self.parse_extra_capabilities(raw)
        except ValueError as e:
            # The dialog validates before connecting; reaching here means the
            # value came from a stored config, so warn and carry on rather than
            # failing the whole session over it.
            logger.warning("Ignoring invalid custom capabilities: %s", e)
            return
        for key, value in extra.items():
            if key in caps and caps[key] != value:
                logger.info("Custom capability overrides %s: %r -> %r", key, caps[key], value)
            caps[key] = value

    def _fetch_source_internal(self, timeout_seconds: int = 60) -> str:
        if not self._session_id:
            raise RuntimeError("No active session.")

        url = f"{self._base_url}/session/{self._session_id}/source"
        logger.debug("Fetching source from %s", url)

        # Retry with backoff on transient WinAppDriver Desktop Root COM timeouts (HTTP 500)
        max_attempts = 3
        for attempt in range(max_attempts):
            if not self._session_id:
                raise RuntimeError("Session disconnected.")
            try:
                r = self._http_session.get(url, timeout=float(timeout_seconds))
                if r.status_code == 200:
                    data = r.json()
                    val = data.get("value", "")
                    if isinstance(val, str):
                        return val
                    elif isinstance(val, dict) and "source" in val:
                        return str(val["source"])
                    return str(val)
                elif r.status_code == 500 and attempt < max_attempts - 1:
                    delay = 0.8 * (attempt + 1)
                    logger.warning("WinAppDriver GET /source returned 500 (attempt %d/%d). Retrying in %.1fs...", attempt + 1, max_attempts, delay)
                    time.sleep(delay)
                    continue
                elif r.status_code == 404 and ("invalid session" in r.text.lower() or "terminated" in r.text.lower()):
                    raise RuntimeError("Session terminated or closed.")
                else:
                    raise RuntimeError(f"Failed to fetch source: HTTP {r.status_code} ({r.text[:200]})")
            except requests.exceptions.RequestException as e:
                if not self._session_id:
                    raise RuntimeError("Session disconnected.")
                if attempt < max_attempts - 1:
                    delay = 0.8 * (attempt + 1)
                    logger.warning("GET /source network exception: %s. Retrying in %.1fs...", e, delay)
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Failed to fetch source: {e}") from e

        raise RuntimeError("Failed to fetch source after retries.")

    def _switch_window_internal(self, handle: str) -> None:
        if not self._session_id:
            raise RuntimeError("No active session.")

        url = f"{self._base_url}/session/{self._session_id}/window"
        # W3C uses {"handle": handle}, JSONWP uses {"name": handle}
        payload = {"handle": handle, "name": handle}
        r = self._http_session.post(url, json=payload, timeout=10.0)

        if r.status_code != 200:
            raise RuntimeError(f"Window switch failed: HTTP {r.status_code} ({r.text[:200]})")

        if self.session_info:
            self.session_info.active_handle = handle
            for w in self.session_info.windows:
                w.is_active = (w.handle == handle)

    def _refresh_handles_internal(self) -> List[WindowInfo]:
        if not self._session_id:
            return []

        url = f"{self._base_url}/session/{self._session_id}/window/handles"
        try:
            r = self._http_session.get(url, timeout=5.0)
            if r.status_code == 200:
                raw_handles = r.json().get("value", [])
                if isinstance(raw_handles, list):
                    current_active = self.session_info.active_handle if self.session_info else ""
                    return [
                        WindowInfo(
                            handle=str(h),
                            title=f"Window {h}",
                            is_active=(str(h) == current_active)
                        )
                        for h in raw_handles
                    ]
        except Exception as e:
            logger.debug("Could not get window handles: %s", e)
        return []

    # Error text that means "this driver doesn't implement that endpoint",
    # as opposed to "your session is gone" — both arrive as HTTP 404 under
    # the W3C spec, and telling them apart is what keeps a perfectly healthy
    # Mac2 session (which has no window handles) from being declared LOST.
    _UNSUPPORTED_ENDPOINT_MARKERS = (
        "unknown command",
        "not implemented",
        "notimplemented",
        "unsupported",
        "did not match a known command",
    )

    def _check_heartbeat(self) -> None:
        if not self._session_id or self.state != SessionState.CONNECTED:
            return
        if self._disconnect_requested:
            return

        if self._heartbeat_probe_index >= len(self._heartbeat_probes):
            return  # no probe this driver understands; heartbeat disabled

        probe = self._heartbeat_probes[self._heartbeat_probe_index]

        def _ping():
            try:
                r = self._http_session.get(f"{self._base_url}/session/{self._session_id}/{probe}", timeout=10.0)
                if r.status_code == 200:
                    self._heartbeat_failures = 0
                elif r.status_code in (404, 405, 501) and any(
                    m in r.text.lower() for m in self._UNSUPPORTED_ENDPOINT_MARKERS
                ):
                    self._heartbeat_probe_index += 1
                    self._heartbeat_failures = 0
                    if self._heartbeat_probe_index < len(self._heartbeat_probes):
                        logger.info(
                            "Heartbeat probe '%s' isn't supported by this driver; falling back to '%s'.",
                            probe, self._heartbeat_probes[self._heartbeat_probe_index],
                        )
                    else:
                        logger.info(
                            "No supported heartbeat endpoint on this driver; disabling heartbeat "
                            "(session loss will surface on the next real request instead)."
                        )
                elif r.status_code == 404:
                    logger.warning("Heartbeat: session expired or closed on server (HTTP 404). Session lost.")
                    self._report_session_lost()
                else:
                    self._heartbeat_failures += 1
                    logger.warning("Heartbeat returned HTTP %d (fail %d/3).", r.status_code, self._heartbeat_failures)
                    if self._heartbeat_failures >= 3:
                        logger.warning("Heartbeat failed 3 consecutive times. Session lost.")
                        self._report_session_lost()
            except Exception as e:
                self._heartbeat_failures += 1
                logger.debug("Heartbeat ping timeout/exception (driver busy, fail %d/3): %s", self._heartbeat_failures, e)
                if self._heartbeat_failures >= 3:
                    logger.warning("Heartbeat failed 3 consecutive times. Session lost.")
                    self._report_session_lost()

        threading.Thread(target=_ping, daemon=True).start()

    # --- Callbacks ---

    def _on_connect_success(self, info: SessionInfo) -> None:
        if self._disconnect_requested:
            # User requested disconnect while connecting — delete in-flight session
            sid = info.session_id
            def _abort_delete():
                try:
                    self._http_session.delete(f"{self._base_url}/session/{sid}", timeout=5.0)
                    logger.info("Aborted in-flight session %s (disconnect requested).", sid)
                except Exception as e:
                    logger.warning("Error aborting in-flight session %s: %s", sid, e)
            self._delete_thread = threading.Thread(target=_abort_delete, daemon=True)
            self._delete_thread.start()
            # Clear the id too: _create_session set it on the worker thread, and
            # everything that gates on "is there a session" (switch_window,
            # refresh_window_handles, find_element_by_xpath, DriverRunner) checks
            # _session_id rather than state. Leaving it set after aborting means
            # those fire at a session we just deleted and the user gets an HTTP
            # 404 instead of a clean "no active session".
            self._session_id = None
            self._set_state(SessionState.DISCONNECTED)
            return

        self.session_info = info
        self._heartbeat_failures = 0
        self._set_state(SessionState.CONNECTED)
        self.session_started.emit(info)
        self.windows_updated.emit(info.windows)
        self._heartbeat_timer.start()

    def _on_connect_failed(self, err: str) -> None:
        self._session_id = None
        self.session_info = None
        self._set_state(SessionState.DISCONNECTED)
        self.error_occurred.emit(err)

    def _on_window_switched(self, handle: str) -> None:
        if self.session_info:
            self.session_info.active_handle = handle
            for w in self.session_info.windows:
                w.is_active = (w.handle == handle)
            self.windows_updated.emit(self.session_info.windows)

    def _on_window_switch_failed(self, err: str) -> None:
        self.error_occurred.emit(f"Failed to switch window: {err}")

    def _on_handles_ready(self, windows: List[WindowInfo]) -> None:
        if self.session_info:
            self.session_info.windows = windows
            self.windows_updated.emit(windows)

    def _report_session_lost(self) -> None:
        """Mark the session lost, unless it was closed deliberately meanwhile.

        Heartbeat pings run on short-lived daemon threads with a 10s timeout,
        so one can still be in flight when the user disconnects. Without this
        guard the late reply flips a cleanly-closed session to "Session lost"
        and the UI sticks there.
        """
        if self._disconnect_requested or not self._session_id:
            logger.debug("Ignoring heartbeat failure: session was closed deliberately.")
            return
        self._set_state(SessionState.LOST)

    def _set_state(self, state: SessionState) -> None:
        if self.state != state:
            self.state = state
            self.state_changed.emit(state.value)
