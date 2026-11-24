"""System volume collector + control for Linux (PulseAudio / PipeWire via ``pactl``).

Publishes the same ``volume`` section as the Windows pycaw collector
(``{"level": 0-100, "muted": bool}``) and services the ``set:<n>`` / ``mute``
commands from the Music-scene slider. Reads back every tick, so the slider also
follows changes made on the PC (media keys, KDE volume applet).

Degrades silently: without ``pactl`` the section stays null and the control hides.
"""
import asyncio
import logging
import os
import re
import shutil
import time

log = logging.getLogger("desko.volume")

SINK = "@DEFAULT_SINK@"
CMD_TICK_SEC = 0.1


async def _pactl(*args) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},  # keep XDG_RUNTIME_DIR to reach the audio server
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 3.0)
        return out.decode("utf-8", "replace") if proc.returncode == 0 else None
    except Exception as e:
        log.debug("pactl %s failed: %r", args, e)
        return None


async def _read() -> dict | None:
    vol = await _pactl("get-sink-volume", SINK)
    mute = await _pactl("get-sink-mute", SINK)
    if vol is None or mute is None:
        return None
    levels = [int(x) for x in re.findall(r"(\d+)%", vol)]
    if not levels:
        return None
    return {"level": round(sum(levels) / len(levels)), "muted": "yes" in mute.lower()}


async def start(state, config, session=None) -> None:
    if shutil.which("pactl") is None:
        log.info("pactl not found -- volume control disabled")
        return
    log.info("volume collector started (pactl)")
    interval = max(1.0, float(config.get("poll", {}).get("volume_sec", 1.0)))
    last_pub = None
    next_read = 0.0
    while True:
        changed = False
        while True:
            try:
                cmd = state.volume_commands.get_nowait()
            except Exception:
                break
            if cmd == "mute":
                await _pactl("set-sink-mute", SINK, "toggle")
            elif cmd.startswith("set:"):
                try:
                    lvl = max(0, min(100, int(float(cmd.split(":", 1)[1]))))
                except ValueError:
                    continue
                await _pactl("set-sink-volume", SINK, f"{lvl}%")
                if lvl > 0:
                    await _pactl("set-sink-mute", SINK, "0")
            changed = True
        if changed:
            next_read = 0.0

        if time.monotonic() >= next_read:
            next_read = time.monotonic() + interval
            payload = await _read()
            if payload is not None and payload != last_pub:
                last_pub = payload
                state.set_section("volume", payload)
        await asyncio.sleep(CMD_TICK_SEC)
