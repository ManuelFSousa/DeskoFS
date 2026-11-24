"""App launcher (Apps scene): a fixed list of PC apps/commands the phone can start.

The list comes from ``config.json`` -> ``"apps"``. Each entry is either:

* a string: a desktop-entry id, matched against ``*.desktop`` files in the XDG
  application dirs (``"firefox"``, ``"spotify"``, ``"org.kde.dolphin"``...);
  snap/flatpak names like ``firefox_firefox.desktop`` match too;
* ``{"desktop": "<id>", "name": "Short name"}``: same, with the label (and/or
  ``"icon"``) overridden;
* an object ``{"name": "Lock", "cmd": ["loginctl", "lock-session"], "icon": "system-lock-screen"}``
  for an arbitrary command (``cmd`` is an argv list, run without a shell).

With no ``"apps"`` key a default set is built from common apps that are installed.

Security: the phone can only ask to launch an entry *by its index id*; it never
sends a command or a path. The launch endpoint also requires an ``X-Desko``
header so a random web page in a desktop browser can't trigger it cross-origin.

Linux only for now (XDG .desktop + icon themes); elsewhere the section stays
null and the scene shows an empty-state hint.
"""
import configparser
import glob
import hashlib
import logging
import mimetypes
import os
import shutil
import subprocess
import sys

log = logging.getLogger("desko.apps")

DEFAULT_APPS = [
    "firefox", "microsoft-edge", "google-chrome", "chromium", "spotify",
    "code", "org.kde.dolphin", "org.kde.konsole", "org.kde.kate", "thunderbird",
    "org.kde.systemmonitor", "org.kde.spectacle",
]
DEFAULT_MAX = 11
LOCK_ENTRY = {"name": "Lock", "cmd": ["loginctl", "lock-session"], "icon": "system-lock-screen"}

ITEMS: list[dict] = []   # [{id, name, icon_path, desktop?, cmd?}]


def _data_dirs() -> list[str]:
    home = os.path.expanduser("~")
    dirs = [os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local/share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs += [os.path.join(home, ".local/share/flatpak/exports/share"),
             "/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop"]
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _desktop_files() -> dict[str, str]:
    """basename-without-.desktop -> path (first match wins, XDG order)."""
    found: dict[str, str] = {}
    for d in _data_dirs():
        for path in glob.glob(os.path.join(d, "applications", "**", "*.desktop"), recursive=True):
            found.setdefault(os.path.basename(path)[:-8], path)
    return found


def _match_desktop(app_id: str, files: dict[str, str]) -> str | None:
    if app_id in files:
        return files[app_id]
    for name, path in files.items():   # snap: firefox_firefox, flatpak: com.spotify.Client
        if name.startswith(app_id + "_") or name.lower().endswith("." + app_id.lower()):
            return path
    return None


def _read_desktop(path: str) -> dict | None:
    cp = configparser.RawConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
        e = cp["Desktop Entry"]
    except Exception:
        return None
    if e.get("NoDisplay") == "true" and "Exec" not in e:
        return None
    return {"name": e.get("Name", os.path.basename(path)), "icon": e.get("Icon", "")}


def _icon_theme() -> str:
    for tool in ("kreadconfig6", "kreadconfig5"):
        if shutil.which(tool):
            try:
                out = subprocess.run([tool, "--group", "Icons", "--key", "Theme"],
                                     capture_output=True, text=True, timeout=3).stdout.strip()
                if out:
                    return out
            except Exception:
                pass
    return "breeze"


def _resolve_icons(names: set[str]) -> dict[str, str]:
    """Icon name -> best file. One walk over the relevant themes."""
    names = {n for n in names if n and not os.path.isabs(n)}
    if not names:
        return {}
    theme = _icon_theme()
    themes = [theme, theme.replace("-dark", ""), "breeze", "hicolor", "Adwaita"]
    roots = []
    for d in [os.path.expanduser("~/.icons")] + [os.path.join(x, "icons") for x in _data_dirs()]:
        for t in dict.fromkeys(themes):
            p = os.path.join(d, t)
            if os.path.isdir(p):
                roots.append(p)
    best: dict[str, tuple[int, str]] = {}
    for rank, root in enumerate(roots):
        for dirpath, _dirs, files in os.walk(root, followlinks=True):
            for f in files:
                stem, ext = os.path.splitext(f)
                if stem not in names or ext not in (".svg", ".png"):
                    continue
                # Prefer earlier themes, then scalable/large app icons.
                score = -rank * 1000
                score += 300 if ext == ".svg" else 0
                score += 100 if "/apps" in dirpath else 0
                for size in (256, 128, 96, 64, 48):
                    if f"{size}" in dirpath:
                        score += size // 4
                        break
                if stem not in best or score > best[stem][0]:
                    best[stem] = (score, os.path.join(dirpath, f))
    for n in names - set(best):
        for ext in (".svg", ".png"):
            p = os.path.join("/usr/share/pixmaps", n + ext)
            if os.path.isfile(p):
                best[n] = (0, p)
                break
    return {k: v[1] for k, v in best.items()}


def load(config: dict) -> list[dict]:
    """Resolve the configured apps. Blocking (filesystem walk); call off-loop."""
    if not sys.platform.startswith("linux"):
        return []
    files = _desktop_files()
    wanted = config.get("apps")
    if not isinstance(wanted, list):
        wanted = [a for a in DEFAULT_APPS if _match_desktop(a, files)][:DEFAULT_MAX] + [LOCK_ENTRY]

    items, icon_names = [], set()
    for entry in wanted:
        item = None
        if isinstance(entry, str):
            entry = {"desktop": entry}
        if isinstance(entry, dict) and isinstance(entry.get("desktop"), str):
            path = _match_desktop(entry["desktop"], files)
            info = _read_desktop(path) if path else None
            if info is None:
                log.info("apps: no .desktop file for %r -- skipped", entry["desktop"])
                continue
            item = {"name": str(entry.get("name") or info["name"]),
                    "icon": str(entry.get("icon") or info["icon"]), "desktop": path}
        elif isinstance(entry, dict) and isinstance(entry.get("cmd"), list) and entry["cmd"]:
            item = {"name": str(entry.get("name") or entry["cmd"][0]),
                    "icon": str(entry.get("icon") or ""),
                    "cmd": [str(x) for x in entry["cmd"]]}
        if item:
            item["id"] = str(len(items))
            items.append(item)
            icon_names.add(item["icon"])

    icons = _resolve_icons(icon_names)
    for it in items:
        ic = it.pop("icon")
        it["icon_path"] = ic if os.path.isabs(ic) and os.path.isfile(ic) else icons.get(ic)
    log.info("apps: %d launcher entries", len(items))
    return items


def _icon_version(path: str | None) -> str:
    """Cache-buster for the icon URL: ids are list positions, so when the list
    changes the same id can point at a different icon (and the phone caches
    icons for a day)."""
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        key = path
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def public(items: list[dict]) -> dict:
    return {"items": [{"id": i["id"], "name": i["name"], "icon": bool(i.get("icon_path")),
                       "iconVer": _icon_version(i.get("icon_path"))} for i in items]}


def icon(app_id: str) -> tuple[bytes, str] | None:
    for it in ITEMS:
        if it["id"] == app_id and it.get("icon_path"):
            try:
                with open(it["icon_path"], "rb") as f:
                    data = f.read(2_000_000)
            except OSError:
                return None
            return data, mimetypes.guess_type(it["icon_path"])[0] or "application/octet-stream"
    return None


def launch(app_id: str) -> bool:
    for it in ITEMS:
        if it["id"] != app_id:
            continue
        if "desktop" in it:
            argv = ["gio", "launch", it["desktop"]] if shutil.which("gio") else \
                   ["gtk-launch", os.path.basename(it["desktop"])[:-8]]
        else:
            argv = it["cmd"]
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             cwd=os.path.expanduser("~"))
            log.info("apps: launched %s", it["name"])
            return True
        except Exception as e:
            log.warning("apps: launching %s failed: %s", it["name"], e)
            return False
    return False


async def start(state, config, session=None) -> None:
    import asyncio

    try:
        ITEMS[:] = await asyncio.to_thread(load, config)
    except Exception as e:
        log.warning("apps: failed to load launcher entries: %r", e)
        ITEMS[:] = []
    state.set_section("apps", public(ITEMS))
