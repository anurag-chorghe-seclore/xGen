"""
xGen Configuration Manager
Loads, validates, and atomically persists user settings to %APPDATA%/xGen/config.json.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger("xgen.config")


@dataclass
class RecentSession:
    name: str
    app_path: str = ""
    app_top_level_window: str = ""
    appium_url: str = "http://127.0.0.1:4723"


@dataclass
class XGenConfig:
    appium_url: str = "http://127.0.0.1:4723"
    app_path: str = ""
    app_top_level_window: str = ""
    implicit_wait_ms: int = 0
    auto_detect_new_windows: bool = True
    auto_connect_on_startup: bool = True
    confirm_disconnect: bool = True
    confirm_reconnect_switch: bool = True

    tree_stale_warning_seconds: int = 60
    source_fetch_timeout_seconds: int = 60
    session_connect_timeout_seconds: int = 90
    hover_poll_interval_ms: int = 100
    transient_node_ttl_minutes: int = 10
    large_tree_warning_threshold: int = 3000

    # Layout & Window Persistence
    window_width: int = 1200
    window_height: int = 750
    window_x: int = -1
    window_y: int = -1
    window_maximized: bool = False
    splitter_sizes: List[int] = field(default_factory=lambda: [250, 300, 450])
    pin_on_top: bool = False

    recent_sessions: List[RecentSession] = field(default_factory=list)


class ConfigManager:
    """Manages disk persistence and atomic updates of XGenConfig."""

    _appdata = os.getenv("APPDATA")
    if _appdata:
        CONFIG_DIR = Path(_appdata) / "xGen"
    else:
        CONFIG_DIR = Path.home() / ".xgen"

    CONFIG_PATH: Path = CONFIG_DIR / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> XGenConfig:
        """
        Load configuration from disk.
        If the file is missing or invalid JSON, returns a default XGenConfig instance.
        """
        target_path = path or cls.CONFIG_PATH
        if not target_path.exists():
            logger.info("Config file not found at %s. Using default configuration.", target_path)
            return XGenConfig()

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            recent_sessions_raw = data.get("recent_sessions", [])
            recent_sessions = [
                RecentSession(
                    name=s.get("name", "Untitled"),
                    app_path=s.get("app_path", ""),
                    app_top_level_window=s.get("app_top_level_window", ""),
                    appium_url=s.get("appium_url", "http://127.0.0.1:4723"),
                )
                for s in recent_sessions_raw
                if isinstance(s, dict)
            ]

            return XGenConfig(
                appium_url=data.get("appium_url", "http://127.0.0.1:4723"),
                app_path=data.get("app_path", ""),
                app_top_level_window=data.get("app_top_level_window", ""),
                implicit_wait_ms=int(data.get("implicit_wait_ms", 0)),
                auto_detect_new_windows=bool(data.get("auto_detect_new_windows", True)),
                auto_connect_on_startup=bool(data.get("auto_connect_on_startup", True)),
                confirm_disconnect=bool(data.get("confirm_disconnect", True)),
                confirm_reconnect_switch=bool(data.get("confirm_reconnect_switch", True)),
                tree_stale_warning_seconds=int(data.get("tree_stale_warning_seconds", 60)),
                source_fetch_timeout_seconds=int(data.get("source_fetch_timeout_seconds", 60)),
                session_connect_timeout_seconds=int(data.get("session_connect_timeout_seconds", 90)),
                hover_poll_interval_ms=int(data.get("hover_poll_interval_ms", 100)),
                transient_node_ttl_minutes=int(data.get("transient_node_ttl_minutes", 10)),
                large_tree_warning_threshold=int(data.get("large_tree_warning_threshold", 3000)),
                window_width=int(data.get("window_width", 1200)),
                window_height=int(data.get("window_height", 750)),
                window_x=int(data.get("window_x", -1)),
                window_y=int(data.get("window_y", -1)),
                window_maximized=bool(data.get("window_maximized", False)),
                splitter_sizes=data.get("splitter_sizes", [250, 300, 450]),
                pin_on_top=bool(data.get("pin_on_top", False)),
                recent_sessions=recent_sessions,
            )
        except Exception as e:
            logger.warning("Error reading config file %s (%s). Falling back to defaults.", target_path, e)
            return XGenConfig()

    @classmethod
    def save(cls, config: XGenConfig, path: Path | None = None) -> None:
        """
        Persist configuration safely and atomically.
        """
        target_path = path or cls.CONFIG_PATH
        target_dir = target_path.parent
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        data = asdict(config)
        tmp_file = target_dir / f"{target_path.name}.tmp_{os.getpid()}"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(target_path)
            logger.debug("Successfully saved configuration to %s", target_path)
        except Exception:
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                logger.error("Failed to save config to %s: %s", target_path, e)
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass

    @classmethod
    def add_recent_session(cls, config: XGenConfig, session: RecentSession) -> XGenConfig:
        """
        Prepend session to recent_sessions, deduping by name/app_path, and capping at 10 items.
        """
        # Filter out duplicates
        filtered = [
            s for s in config.recent_sessions
            if not (s.name == session.name and s.app_path == session.app_path and s.app_top_level_window == session.app_top_level_window)
        ]
        config.recent_sessions = [session] + filtered[:9]
        return config
