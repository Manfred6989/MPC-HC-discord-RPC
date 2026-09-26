"""Discord Rich Presence for MPC-HC.

Polls MPC-HC's built-in web interface (variables.html) and mirrors the
current file / playback state to Discord via the local Discord IPC pipe.

Requirements in MPC-HC: Options -> Player -> Web Interface ->
"Listen on port" enabled (default 13579). "Allow access from localhost only"
can (and should) stay on.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mpc-discord-rpc")

# DirectShow OAFilterState values, as reported by MPC-HC. -1 = nothing loaded.
STATE_NONE = -1
STATE_STOPPED = 0
STATE_PAUSED = 1
STATE_PLAYING = 2

DEFAULT_CONFIG = {
    "discord_client_id": "",
    "mpc_host": "127.0.0.1",
    "mpc_port": 13579,
    "poll_interval": 2.0,
    "large_image": "mpc-hc",
    "large_text": "MPC-HC",
    "show_filename": True,
    "strip_extension": True,
    "hide_when_paused": False,
    "hide_when_stopped": True,
}

# Discord rate-limits activity updates (roughly 5 per 20 s). We only push
# when something meaningful changes, and never faster than this.
MIN_UPDATE_INTERVAL = 5.0
# A position drift beyond this (seconds) is treated as a seek.
SEEK_TOLERANCE = 3.0
# Re-send the current activity this often so it reappears after Discord restarts.
REFRESH_INTERVAL = 60.0

# Talk to MPC-HC directly, never through a system/env HTTP proxy.
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_VAR_RE = re.compile(r'<p id="([a-z]+)">(.*?)</p>', re.S)


@dataclass(frozen=True)
class PlayerStatus:
    file: str
    state: int
    position_ms: int
    duration_ms: int
    rate: float


def parse_variables(html: str) -> PlayerStatus:
    """Parse MPC-HC's /variables.html page.

    Values are inserted raw (not HTML-escaped) by MPC-HC, so no unescaping.
    """
    v = dict(_VAR_RE.findall(html))

    def num(key: str, default: float = 0) -> float:
        try:
            return float(v.get(key, "").strip().replace(",", "."))
        except ValueError:
            return default

    rate = num("playbackrate", 1.0) or 1.0
    return PlayerStatus(
        file=v.get("file", "").strip(),
        state=int(num("state", STATE_NONE)),
        position_ms=int(num("position")),
        duration_ms=int(num("duration")),
        rate=rate,
    )


def fetch_status(host: str, port: int, timeout: float = 2.0) -> PlayerStatus | None:
    """Return the player status, or None if MPC-HC is not reachable."""
    url = f"http://{host}:{port}/variables.html"
    try:
        with _HTTP.open(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    return parse_variables(raw.decode("utf-8", errors="replace"))


def build_activity(status: PlayerStatus, cfg: dict, now: float | None = None) -> dict | None:
    """Translate a PlayerStatus into kwargs for pypresence's Presence.update.

    Returns None when the presence should be cleared.
    """
    if now is None:
        now = time.time()

    if status.state == STATE_NONE or not status.file:
        return None
    if status.state == STATE_STOPPED and cfg["hide_when_stopped"]:
        return None
    if status.state == STATE_PAUSED and cfg["hide_when_paused"]:
        return None

    if cfg["show_filename"]:
        title = status.file
        if cfg["strip_extension"]:
            stem, ext = os.path.splitext(title)
            # Only strip things that look like real extensions, keep URLs etc. intact.
            if stem and 1 < len(ext) <= 6:
                title = stem
    else:
        title = "Watching a video"

    # Discord requires 2..128 characters for these fields.
    title = title[:128].ljust(2)

    activity: dict = {
        "details": title,
        "large_image": cfg["large_image"] or None,
        "large_text": cfg["large_text"] or None,
    }

    if status.state == STATE_PLAYING:
        activity["state"] = "Playing" if status.rate == 1.0 else f"Playing ({status.rate:g}x)"
        if status.duration_ms > 0:
            # Discord shows a progress bar for WATCHING when start+end are set.
            # Timestamps are wall-clock, so scale by playback rate.
            pos = status.position_ms / 1000 / status.rate
            dur = status.duration_ms / 1000 / status.rate
            start = now - pos
            activity["start"] = int(start)
            activity["end"] = int(start + dur)
        else:
            # Live stream / unknown length: show elapsed time.
            activity["start"] = int(now - status.position_ms / 1000)
    elif status.state == STATE_PAUSED:
        activity["state"] = f"Paused at {_fmt(status.position_ms)}" + (
            f" / {_fmt(status.duration_ms)}" if status.duration_ms > 0 else ""
        )
    else:
        activity["state"] = "Stopped"

    return activity


def _fmt(ms: int) -> str:
    s = max(ms, 0) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def needs_update(prev: dict | None, new: dict | None) -> bool:
    """True if the change between two activities is worth sending to Discord."""
    if prev is None or new is None:
        return prev is not new
    static_keys = ("details", "state", "large_image", "large_text")
    if any(prev.get(k) != new.get(k) for k in static_keys):
        return True
    if ("start" in prev) != ("start" in new):
        return True
    if "start" in new and abs(prev["start"] - new["start"]) > SEEK_TOLERANCE:
        return True  # seek
    return False


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict:
    """Load config.json; create it from config.example.json if missing."""
    if not path.exists():
        example = path.with_name("config.example.json")
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            log.info("Created %s from config.example.json", path)
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with path.open(encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:
            raise ConfigError(f"Can't read {path.name}: {e}") from e
    env_id = os.environ.get("MPC_DISCORD_CLIENT_ID")
    if env_id:
        cfg["discord_client_id"] = env_id
    cfg["discord_client_id"] = str(cfg["discord_client_id"]).strip()
    if not cfg["discord_client_id"].isdigit():
        raise ConfigError(f"Set discord_client_id in {path.name} (see README)")
    return cfg


def _describe(err: Exception) -> str:
    """Short human-readable reason for a pypresence / IPC error."""
    name = type(err).__name__
    hints = {
        "DiscordNotFound": "Discord desktop app not running",
        "InvalidPipe": "Discord IPC pipe not usable",
        "InvalidID": "Discord rejected the Application ID",
        "ConnectionTimeout": "timed out connecting to Discord",
        "ResponseTimeout": "Discord did not answer",
        "PipeClosed": "Discord closed the connection",
    }
    msg = hints.get(name)
    if msg:
        return msg
    text = str(err).strip()
    return f"{name}: {text}" if text else name


class DiscordPresence:
    """Thin wrapper around pypresence that reconnects on failure."""

    RETRY_INTERVAL = 10.0

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.rpc = None
        self.error: str | None = None
        self.fatal = False  # Discord actively refused (bad ID / bad activity), not just absent
        self._next_try = 0.0

    @property
    def connected(self) -> bool:
        return self.rpc is not None

    def ensure_connected(self) -> bool:
        if self.rpc is not None:
            return True
        if time.monotonic() < self._next_try:
            return False
        self._next_try = time.monotonic() + self.RETRY_INTERVAL
        from pypresence import Presence

        try:
            rpc = Presence(self.client_id)
            rpc.connect()
        except Exception as e:
            self._set_error(e)
            log.debug("Discord connect failed: %r", e)
            return False
        self.rpc = rpc
        self.error, self.fatal = None, False
        log.info("Connected to Discord.")
        return True

    def _set_error(self, err: Exception) -> None:
        self.error = _describe(err)
        self.fatal = type(err).__name__ in ("InvalidID", "ServerError", "DiscordError")

    def _drop(self, err: Exception) -> None:
        self._set_error(err)
        log.warning("Discord error (%r); reconnecting.", err)
        try:
            self.rpc.close()
        except Exception:
            pass
        self.rpc = None
        self._next_try = 0.0

    def update(self, activity: dict) -> bool:
        from pypresence import ActivityType, StatusDisplayType

        if not self.ensure_connected():
            return False
        try:
            self.rpc.update(
                activity_type=ActivityType.WATCHING,
                status_display_type=StatusDisplayType.DETAILS,
                **{k: v for k, v in activity.items() if v is not None},
            )
            self.error, self.fatal = None, False
            return True
        except Exception as e:
            self._drop(e)
            return False

    def clear(self) -> bool:
        if self.rpc is None:
            return True
        try:
            self.rpc.clear()
            return True
        except Exception as e:
            self._drop(e)
            return False

    def close(self) -> None:
        if self.rpc is not None:
            try:
                self.rpc.clear()
                self.rpc.close()
            except Exception:
                pass
            self.rpc = None


@dataclass
class AppStatus:
    """What the tray (or console) shows. level: ok | idle | error."""

    level: str = "idle"
    mpc: str = "MPC-HC: checking..."
    discord: str = "Discord: checking..."
    presence: str = "Presence: none"
    problem: str | None = None


class Worker:
    """Polling loop. Runs in the main thread (console) or a background thread (tray)."""

    def __init__(self, config_path: Path, on_status=None):
        self.config_path = config_path
        self.on_status = on_status or (lambda st: None)
        self.stop_event = threading.Event()
        self.paused = False  # user toggle from the tray menu
        self.status = AppStatus()
        self._cfg: dict | None = None
        self._cfg_mtime: float | None = None
        self._discord: DiscordPresence | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def _publish(self, st: AppStatus) -> None:
        if st != self.status:
            self.status = st
            if st.problem:
                log.info("Status: %s", st.problem)
            self.on_status(st)

    def _reload_config(self) -> str | None:
        """(Re)load config when the file changes. Returns an error message or None."""
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cfg is not None and mtime == self._cfg_mtime:
            return None
        self._cfg_mtime = mtime
        try:
            cfg = load_config(self.config_path)
        except ConfigError as e:
            self._cfg = None
            if self._discord:
                self._discord.close()
                self._discord = None
            return str(e)
        if self._discord is None or self._discord.client_id != cfg["discord_client_id"]:
            if self._discord:
                self._discord.close()
            self._discord = DiscordPresence(cfg["discord_client_id"])
        self._cfg = cfg
        log.info("Config loaded; watching MPC-HC at http://%s:%s/", cfg["mpc_host"], cfg["mpc_port"])
        return None

    def run(self) -> None:
        sent: dict | None = None  # what Discord currently shows
        last_push = float("-inf")
        try:
            while not self.stop_event.is_set():
                err = self._reload_config()
                if err:
                    self._publish(AppStatus("error", "MPC-HC: -", "Discord: -", "Presence: none", err))
                    sent = None
                    self.stop_event.wait(2.0)
                    continue
                cfg, discord = self._cfg, self._discord

                status = fetch_status(cfg["mpc_host"], int(cfg["mpc_port"]))
                activity = build_activity(status, cfg) if status and not self.paused else None
                discord.ensure_connected()

                now = time.monotonic()
                changed = needs_update(sent, activity)
                stale = activity is not None and now - last_push >= REFRESH_INTERVAL
                if discord.connected and (changed or stale) and now - last_push >= MIN_UPDATE_INTERVAL:
                    ok = discord.update(activity) if activity else discord.clear()
                    if ok:
                        sent = activity
                        last_push = now
                        if changed:
                            log.info("Presence: %s", f"{activity['details']} - {activity['state']}" if activity else "cleared")
                if not discord.connected:
                    sent = None  # Discord shows nothing; resend once reconnected

                self._publish(self._make_status(status, activity, sent, discord))
                self.stop_event.wait(max(float(cfg["poll_interval"]), 0.5))
        except Exception as e:
            log.exception("Worker crashed")
            self._publish(AppStatus("error", "MPC-HC: -", "Discord: -", "Presence: none", f"Crashed: {e!r}"))
            raise
        finally:
            if self._discord:
                self._discord.close()

    def _make_status(self, status, activity, sent, discord) -> AppStatus:
        if status is None:
            mpc = "MPC-HC: not reachable"
            problem = (f"MPC-HC not reachable on port {self._cfg['mpc_port']} "
                       "(not running, or web interface off)")
        else:
            mpc = "MPC-HC: " + {STATE_PLAYING: "playing", STATE_PAUSED: "paused",
                                STATE_STOPPED: "stopped"}.get(status.state, "no file")
            problem = None
        if discord.connected:
            dc = "Discord: connected"
        else:
            dc = f"Discord: {discord.error or 'not connected'}"
            problem = problem or dc
        if self.paused:
            pres = "Presence: paused by you"
        elif sent:
            pres = f"Presence: {sent['details']} - {sent['state']}"
        elif activity:
            pres = "Presence: waiting to send"
        else:
            pres = "Presence: none"
        if discord.error and discord.fatal:
            dc = f"Discord: {discord.error}"
            problem = dc
            level = "error"
        else:
            level = "ok" if sent else "idle"
        return AppStatus(level, mpc, dc, pres, problem)


# ---------------------------------------------------------------------------
# Tray UI (Windows system tray via pystray; also works on Linux/macOS)
# ---------------------------------------------------------------------------

LEVEL_COLORS = {"ok": (67, 181, 129), "idle": (250, 166, 26), "error": (240, 71, 71)}


def make_icon_image(level: str):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(40, 43, 48))
    d.polygon([(14, 10), (14, 42), (40, 26)], fill=(255, 255, 255))  # play symbol
    c = LEVEL_COLORS.get(level, LEVEL_COLORS["idle"])
    d.ellipse((28, 28, 63, 63), fill=c, outline=(40, 43, 48), width=4)  # status dot
    return img


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opens with the user's default app
    else:
        import subprocess

        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def tray_title(st: AppStatus) -> str:
    lines = ["MPC-HC Discord RPC", st.presence if st.level == "ok" else (st.problem or st.presence)]
    return "\n".join(lines)[:127]  # Windows tooltip limit


def run_tray(worker: Worker, log_path: Path | None) -> None:
    import pystray
    from pystray import Menu, MenuItem as Item

    def refresh(st: AppStatus) -> None:
        icon.icon = make_icon_image(st.level)
        icon.title = tray_title(st)
        icon.update_menu()

    def toggle_pause(icon_, item) -> None:
        worker.paused = not worker.paused

    def quit_(icon_, item) -> None:
        worker.stop()
        icon.stop()

    menu = Menu(
        Item(lambda i: worker.status.mpc, None, enabled=False),
        Item(lambda i: worker.status.discord, None, enabled=False),
        Item(lambda i: worker.status.presence[:100], None, enabled=False),
        Item(lambda i: f"Problem: {worker.status.problem}"[:100], None, enabled=False,
             visible=lambda i: bool(worker.status.problem)),
        Menu.SEPARATOR,
        Item("Pause presence", toggle_pause, checked=lambda i: worker.paused),
        Item("Open config", lambda *_: open_path(worker.config_path)),
        Item("Open log", lambda *_: open_path(log_path), visible=log_path is not None),
        Menu.SEPARATOR,
        Item("Quit", quit_),
    )
    icon = pystray.Icon("mpc-discord-rpc", make_icon_image("idle"), tray_title(worker.status), menu)
    worker.on_status = refresh

    thread = threading.Thread(target=worker.run, name="worker", daemon=True)

    def setup(icon_) -> None:
        icon_.visible = True
        thread.start()

    icon.run(setup=setup)
    worker.stop()
    thread.join(timeout=5)  # let it clear the presence


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def run_check(config_path: Path) -> int:
    """Step-by-step self test, printed to the console. Returns an exit code."""
    def say(ok, msg):
        print(f"[{'OK' if ok else 'FAIL'}] {msg}")
        return ok

    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    try:
        import pypresence
        say(True, f"pypresence {pypresence.__version__} installed")
    except ImportError:
        say(False, "pypresence not installed: run  pip install -r requirements.txt")
        return 1
    try:
        cfg = load_config(config_path)
        say(True, f"config {config_path} (client id {cfg['discord_client_id']})")
    except ConfigError as e:
        say(False, str(e))
        return 1

    url = f"http://{cfg['mpc_host']}:{cfg['mpc_port']}/variables.html"
    st = fetch_status(cfg["mpc_host"], int(cfg["mpc_port"]))
    if not say(st is not None, f"MPC-HC web interface at {url}"):
        print("       Enable it: MPC-HC > View > Options > Player > Web Interface > 'Listen on port'.")
    elif st.state == STATE_NONE:
        print("       MPC-HC is running but no file is open.")
    else:
        print(f"       file={st.file!r} state={st.state} pos={st.position_ms}ms dur={st.duration_ms}ms")

    from pypresence.utils import get_ipc_path
    pipe = get_ipc_path()
    if not say(pipe is not None, f"Discord IPC pipe {pipe or '(none found)'}"):
        print("       Start the Discord desktop app (the browser version has no IPC).")
        return 1

    d = DiscordPresence(cfg["discord_client_id"])
    if not say(d.ensure_connected(), f"Discord handshake{'' if d.connected else ': ' + str(d.error)}"):
        return 1
    test = (build_activity(st, cfg) if st else None) or {
        "details": "Test from mpc_discord_rpc --check", "state": "Testing",
        "large_image": cfg["large_image"] or None, "large_text": cfg["large_text"] or None}
    ok = say(d.update(test), f"Set activity{'' if d.connected else ': ' + str(d.error)}")
    if ok:
        print("       Check your Discord profile now; clearing in 10 s...")
        time.sleep(10)
        print("       Not visible? Discord > User Settings > Activity Privacy >")
        print("       'Share your detected activities with others' must be on.")
    d.close()
    return 0 if ok else 1


def main() -> None:
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    p = argparse.ArgumentParser(description="Discord Rich Presence for MPC-HC")
    p.add_argument("-c", "--config", type=Path, default=here / "config.json")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", type=Path, help="log file (tray mode default: mpc_discord_rpc.log next to the script)")
    p.add_argument("--no-tray", action="store_true", help="run in the console without a tray icon")
    p.add_argument("--check", action="store_true", help="run a step-by-step self test and exit")
    args = p.parse_args()

    tray = not (args.no_tray or args.check)
    no_console = sys.stderr is None  # pythonw / frozen windowed exe
    log_file = args.log_file or (here / "mpc_discord_rpc.log" if tray or no_console else None)
    handlers = []
    if log_file:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(log_file, maxBytes=512_000, backupCount=1, encoding="utf-8"))
    if not no_console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    if args.check:
        sys.exit(run_check(args.config))

    worker = Worker(args.config)
    if tray:
        try:
            run_tray(worker, log_file)
            return
        except Exception:
            log.exception("Tray icon unavailable; running without it.")
            if no_console:
                raise
    try:
        worker.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
