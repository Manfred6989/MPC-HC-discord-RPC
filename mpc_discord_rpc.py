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


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        log.warning("Config %s not found, using defaults.", path)
    env_id = os.environ.get("MPC_DISCORD_CLIENT_ID")
    if env_id:
        cfg["discord_client_id"] = env_id
    return cfg


class DiscordPresence:
    """Thin wrapper around pypresence that reconnects on failure."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.rpc = None

    def _connect(self) -> bool:
        from pypresence import Presence

        try:
            rpc = Presence(self.client_id)
            rpc.connect()
        except Exception as e:  # DiscordNotFound, InvalidPipe, timeouts, ...
            log.debug("Discord connect failed: %s", e)
            return False
        self.rpc = rpc
        log.info("Connected to Discord.")
        return True

    def ensure_connected(self) -> bool:
        return self.rpc is not None or self._connect()

    def _drop(self, err: Exception) -> None:
        log.warning("Lost connection to Discord (%s); will retry.", err)
        try:
            self.rpc.close()
        except Exception:
            pass
        self.rpc = None

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


def run(cfg: dict) -> None:
    client_id = str(cfg["discord_client_id"]).strip()
    if not client_id.isdigit():
        sys.exit(
            "discord_client_id is not set. Create an application at "
            "https://discord.com/developers/applications and put its "
            "Application ID in config.json (see README)."
        )

    discord = DiscordPresence(client_id)
    interval = max(float(cfg["poll_interval"]), 0.5)
    sent: dict | None = None  # what Discord currently shows
    last_push = float("-inf")
    mpc_was_up = None

    log.info("Watching MPC-HC at http://%s:%s/ ...", cfg["mpc_host"], cfg["mpc_port"])
    try:
        while True:
            status = fetch_status(cfg["mpc_host"], int(cfg["mpc_port"]))
            if (status is not None) != mpc_was_up:
                mpc_was_up = status is not None
                log.info("MPC-HC %s.", "detected" if mpc_was_up else "not reachable (is the web interface enabled?)")

            activity = build_activity(status, cfg) if status else None
            now = time.monotonic()

            changed = needs_update(sent, activity)
            stale = activity is not None and now - last_push >= REFRESH_INTERVAL
            if (changed or stale) and now - last_push >= MIN_UPDATE_INTERVAL:
                ok = discord.update(activity) if activity else discord.clear()
                if ok:
                    sent = activity
                    last_push = now
                    if changed:
                        log.info("Presence: %s", f"{activity['details']} - {activity['state']}" if activity else "cleared")
                elif activity:
                    sent = None  # retry on the next poll
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        discord.close()


def main() -> None:
    here = Path(getattr(sys, "frozen", False) and sys.executable or __file__).resolve().parent
    p = argparse.ArgumentParser(description="Discord Rich Presence for MPC-HC")
    p.add_argument("-c", "--config", type=Path, default=here / "config.json")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", type=Path, help="log to this file (useful with pythonw, which has no console)")
    args = p.parse_args()

    logging.basicConfig(
        filename=args.log_file,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    run(load_config(args.config))


if __name__ == "__main__":
    main()
