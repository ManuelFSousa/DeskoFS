"""Media collector for Linux, using MPRIS through ``playerctl``.

Publishes the same ``media`` section as the Windows GSMTC collector (media.py),
so the Music scene and lyrics work unchanged. Works with any MPRIS player:
Spotify, Firefox/Chromium/Edge tabs (YouTube Music...), VLC, mpv with mpris...

Player choice: the first player that is Playing, else the one we used last,
else the first one listed. Transport commands queued by the HTTP layer
(``state.media_commands``) are run with ``playerctl -p <player>``.

Degrades silently: without ``playerctl`` on PATH the collector is a no-op and
the Music scene stays hidden.
"""
import asyncio
import base64
import logging
import mimetypes
import os
import shutil
import time
from collections import OrderedDict
from urllib.parse import unquote, urlparse

from . import lyrics as lyrics_mod

log = logging.getLogger("desko.media")

ART_CAP = 400000          # bytes; skip larger covers (they're inlined as data URLs)
ART_CACHE_MAX = 64
ART_CACHE: "OrderedDict[str, str]" = OrderedDict()   # artUrl -> data URL ("" = failed)

POLL_SEC = 1.0            # how often playerctl is queried
CMD_TICK_SEC = 0.1        # how often the command queue is drained
PATCH_STEP_SEC = 0.2
LYRICS_RETRY_SEC = 12.0
NO_SESSION_GRACE_SEC = 10.0

SEP = "\x1f"
FIELDS = ["playerName", "status", "xesam:title", "xesam:artist", "xesam:album",
          "mpris:artUrl", "position", "mpris:length"]
FORMAT = SEP.join("{{%s}}" % f for f in FIELDS)


async def _run(*args, timeout=3.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "playerctl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace")
    except Exception as e:
        log.debug("playerctl %s failed: %r", args, e)
        return 1, ""


def _us_to_sec(v: str) -> float:
    try:
        return int(v) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


async def _list_players() -> list[dict]:
    rc, out = await _run("-a", "metadata", "--format", FORMAT)
    if rc != 0 and not out:
        return []
    players = []
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) != len(FIELDS):
            continue
        p = dict(zip(FIELDS, parts))
        if not p["xesam:title"]:
            continue
        players.append(p)
    return players


def _pick(players: list[dict], last_name: str | None) -> dict | None:
    if not players:
        return None
    for p in players:
        if p["status"] == "Playing":
            return p
    for p in players:
        if p["playerName"] == last_name:
            return p
    return players[0]


async def _art_data_url(url: str, session) -> str:
    if not url:
        return ""
    if url.startswith("data:"):
        return url if len(url) <= ART_CAP * 4 // 3 + 100 else ""
    cached = ART_CACHE.get(url)
    if cached is not None:
        ART_CACHE.move_to_end(url)
        return cached
    data, mime = b"", "image/jpeg"
    try:
        if url.startswith("file://"):
            path = unquote(urlparse(url).path)
            if os.path.getsize(path) <= ART_CAP:
                with open(path, "rb") as f:
                    data = f.read()
                mime = mimetypes.guess_type(path)[0] or mime
        elif url.startswith(("http://", "https://")) and session is not None:
            import aiohttp

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    # content.read(n) returns whatever is buffered (up to n),
                    # not the whole body -- loop to EOF or past the cap, else
                    # a half-downloaded JPEG ends up on the phone.
                    buf = bytearray()
                    while len(buf) <= ART_CAP:
                        chunk = await r.content.read(65536)
                        if not chunk:
                            break
                        buf += chunk
                    data = bytes(buf)
                    mime = (r.headers.get("Content-Type") or mime).split(";")[0]
    except Exception as e:
        log.debug("art fetch failed for %s: %r", url, e)
        return ""   # not cached: retried on the next poll
    res = ""
    if data and len(data) <= ART_CAP:
        res = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    ART_CACHE[url] = res
    while len(ART_CACHE) > ART_CACHE_MAX:
        ART_CACHE.popitem(last=False)
    return res


async def _execute(player: str, action: str) -> None:
    if action == "play_pause":
        await _run("-p", player, "play-pause")
    elif action == "next":
        await _run("-p", player, "next")
    elif action == "prev":
        await _run("-p", player, "previous")
    elif action.startswith("seek:"):
        secs = max(0.0, float(action.split(":", 1)[1]))
        await _run("-p", player, "position", "%.3f" % secs)


async def start(state, config, session) -> None:
    if shutil.which("playerctl") is None:
        log.info("playerctl not found -- media collector disabled (apt install playerctl)")
        return
    log.info("media collector started (MPRIS via playerctl)")
    # Floor: each poll spawns playerctl; the phone interpolates position between polls.
    poll = max(0.5, float(config.get("poll", {}).get("media_sec", POLL_SEC)))

    last_player = None
    last_key = None
    last_art = None
    last_playing = None
    last_pos = None
    last_media = None
    no_session_since = None
    lyrics_key = None
    lyrics_at = 0.0
    lyrics_tasks: set = set()
    next_poll = 0.0

    while True:
        # 1) Transport commands, applied straight away so the phone feels snappy.
        ran_cmd = False
        while True:
            try:
                action = state.media_commands.get_nowait()
            except Exception:
                break
            if last_player:
                await _execute(last_player, action)
                ran_cmd = True
        if ran_cmd:
            next_poll = 0.0  # re-read now to reflect the change

        if time.monotonic() < next_poll:
            await asyncio.sleep(CMD_TICK_SEC)
            continue
        next_poll = time.monotonic() + poll

        # 2) Read the current player.
        p = _pick(await _list_players(), last_player)
        if p is None:
            if no_session_since is None:
                no_session_since = time.monotonic()
            elif time.monotonic() - no_session_since > NO_SESSION_GRACE_SEC:
                if state.get("media") is not None:
                    state.set_section("media", None)
                last_key = last_art = last_playing = last_pos = last_media = None
            continue
        no_session_since = None
        last_player = p["playerName"]

        title, artist, album = p["xesam:title"], p["xesam:artist"], p["xesam:album"]
        key = f"{artist}||{title}"
        playing = p["status"] == "Playing"
        pos = _us_to_sec(p["position"])
        dur = _us_to_sec(p["mpris:length"])
        art = await _art_data_url(p["mpris:artUrl"], session)
        now = time.time()

        if key != last_key or art != last_art or playing != last_playing:
            last_key, last_art, last_playing, last_pos = key, art, playing, pos
            last_media = {
                "playing": playing,
                "title": title,
                "artist": artist,
                "album": album,
                "artDataUrl": art,
                "positionSec": pos,
                "durationSec": dur,
                "sourceApp": last_player,
                "updatedAt": now,
            }
            state.set_section("media", last_media)
        elif last_pos is None or abs(pos - last_pos) >= PATCH_STEP_SEC:
            last_pos = pos
            state.patch_section("media", {"positionSec": pos, "updatedAt": now, "playing": playing})

        # 3) Lyrics: fetch on track change, retry while still missing.
        cur = state.get("lyrics")
        have = cur is not None and cur.get("trackKey") == key
        due = key != lyrics_key or (time.monotonic() - lyrics_at) >= LYRICS_RETRY_SEC
        if not have and due and last_media is not None:
            lyrics_key, lyrics_at = key, time.monotonic()
            t = asyncio.create_task(lyrics_mod.fetch(state, session, key, artist, title, album, dur))
            lyrics_tasks.add(t)
            t.add_done_callback(lyrics_tasks.discard)
