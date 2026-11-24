"""App launcher (Apps scene): a fixed list of PC apps/commands the phone can start.

Works on Linux and Windows. The list comes from ``config.json`` -> ``"apps"``.
Each entry is one of:

* ``"firefox"``: an installed app, by name.
  - Linux: a desktop-entry id, matched against ``*.desktop`` files in the XDG
    application dirs (``"spotify"``, ``"org.kde.dolphin"``; snap/flatpak names
    like ``firefox_firefox.desktop`` match too).
  - Windows: a Start Menu shortcut name (``"Spotify"``, ``"Visual Studio Code"``,
    ``"File Explorer"``), case-insensitive; a unique prefix is enough.
* ``{"app": "<as above>", "name": "Short name", "icon": "..."}``: same, with the
  label and/or icon overridden (``"desktop"`` / ``"shortcut"`` are accepted
  aliases for ``"app"``).
* ``{"name": "Lock", "cmd": ["loginctl", "lock-session"]}``: a fixed command
  (argv list, run without a shell).
* ``{"name": "Teams", "open": "msteams:"}``: a URI / file / ``shell:`` target,
  opened like a double-click (``os.startfile`` on Windows, ``xdg-open`` on Linux).
  Store apps without a Start Menu shortcut: ``"shell:AppsFolder\\<AppID>"``
  (PowerShell ``Get-StartApps`` lists the AppIDs).

Any entry may carry ``"os": "linux"`` or ``"os": "windows"`` to only show up on
that system, so one list can serve both. ``"icon"`` is an icon-theme name
(Linux) or a file path: .png/.svg/.ico, or an .exe/.dll to extract from
(Windows, needs pywin32 + Pillow from requirements-windows.txt).

With no ``"apps"`` key a default set is built from common apps that are installed.

Security: the phone can only ask to launch an entry *by its index id*; it never
sends a command or a path. The launch endpoint also requires an ``X-Desko``
header so a random web page in a desktop browser can't trigger it cross-origin.
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

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

DEFAULT_APPS_LINUX = [
    "firefox", "microsoft-edge", "google-chrome", "chromium", "spotify",
    "code", "org.kde.dolphin", "org.kde.konsole", "org.kde.kate", "thunderbird",
    "org.kde.systemmonitor", "org.kde.spectacle",
]
DEFAULT_APPS_WINDOWS = [
    "Firefox", "Google Chrome", "Microsoft Edge", "Spotify", "Visual Studio Code",
    "File Explorer", "Windows Terminal", "Discord", "Steam", "Outlook", "Microsoft Teams",
    "Notepad",
]
DEFAULT_MAX = 11
LOCK_LINUX = {"name": "Lock", "cmd": ["loginctl", "lock-session"], "icon": "system-lock-screen"}
LOCK_WINDOWS = {"name": "Lock", "cmd": ["rundll32.exe", "user32.dll,LockWorkStation"]}

# [{id, name, kind: desktop|shortcut|cmd|open, target, icon_path? | icon_bytes?+icon_mime?}]
ITEMS: list[dict] = []


# =============================================================================
# Linux: XDG .desktop files + icon themes
# =============================================================================


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


def _load_linux(wanted: list) -> list[dict]:
    files = _desktop_files()
    items, icon_names = [], set()
    for entry in wanted:
        item = None
        if "app" in entry:
            path = _match_desktop(entry["app"], files)
            info = _read_desktop(path) if path else None
            if info is None:
                log.info("apps: no .desktop file for %r -- skipped", entry["app"])
                continue
            item = {"name": entry.get("name") or info["name"], "icon": entry.get("icon") or info["icon"],
                    "kind": "desktop", "target": path}
        elif "cmd" in entry:
            item = {"name": entry.get("name") or entry["cmd"][0], "icon": entry.get("icon") or "",
                    "kind": "cmd", "target": entry["cmd"]}
        elif "open" in entry:
            item = {"name": entry.get("name") or entry["open"], "icon": entry.get("icon") or "",
                    "kind": "open", "target": entry["open"]}
        if item:
            items.append(item)
            icon_names.add(item["icon"])
    icons = _resolve_icons(icon_names)
    for it in items:
        ic = it.pop("icon")
        it["icon_path"] = ic if os.path.isabs(ic) and os.path.isfile(ic) else icons.get(ic)
    return items


# =============================================================================
# Windows: Start Menu shortcuts (.lnk / .url) + exe icon extraction
# =============================================================================
def _start_menu_dirs() -> list[str]:
    dirs = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")
            if os.path.isdir(p):
                dirs.append(p)
    return dirs


def _shortcuts() -> dict[str, str]:
    """lower-case shortcut name -> path (user Start Menu wins over all-users)."""
    found: dict[str, str] = {}
    for d in _start_menu_dirs():
        for path in glob.glob(os.path.join(d, "**", "*.*"), recursive=True):
            stem, ext = os.path.splitext(os.path.basename(path))
            if ext.lower() not in (".lnk", ".url") or "uninstall" in stem.lower():
                continue
            found.setdefault(stem.lower(), path)
    return found


def _match_shortcut(name: str, found: dict[str, str]) -> str | None:
    key = name.lower()
    if key in found:
        return found[key]
    pref = sorted(k for k in found if k.startswith(key))
    if pref:
        return found[pref[0]]
    return None


def _lnk_icon_source(path: str) -> str | None:
    """File to take a shortcut's icon from: its IconLocation, else its target."""
    if path.lower().endswith(".url"):
        cp = configparser.RawConfigParser(interpolation=None, strict=False)
        try:
            cp.read(path, encoding="utf-8")
            return cp.get("InternetShortcut", "IconFile", fallback=None)
        except Exception:
            return None
    try:
        import win32com.client  # pywin32

        sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
        loc = (sc.IconLocation or "").rsplit(",", 1)[0].strip()
        loc = os.path.expandvars(loc) if loc else ""
        if loc and os.path.isfile(loc):
            return loc
        target = os.path.expandvars(sc.TargetPath or "")
        return target if target and os.path.isfile(target) else None
    except Exception:
        return None


def _win_icon(src: str | None) -> tuple[bytes, str] | None:
    """Icon bytes from a png/svg/ico file, or extracted from an exe/dll."""
    if not src:
        return None
    src = os.path.expandvars(src)
    if not os.path.isfile(src):
        return None
    ext = os.path.splitext(src)[1].lower()
    if ext in (".png", ".svg", ".ico", ".jpg", ".jpeg"):
        try:
            with open(src, "rb") as f:
                return f.read(2_000_000), mimetypes.guess_type(src)[0] or "image/png"
        except OSError:
            return None
    if ext in (".exe", ".dll"):
        from . import procs  # reuses the Processes scene's pywin32/Pillow extractor

        png = procs._extract_icon(src)
        return (png, "image/png") if png else None
    return None


def _load_windows(wanted: list) -> list[dict]:
    try:
        import pythoncom  # COM must be initialised on this (worker) thread for WScript.Shell

        pythoncom.CoInitialize()
    except Exception:
        pythoncom = None
    try:
        found = _shortcuts()
        items = []
        for entry in wanted:
            item, icon_src = None, entry.get("icon")
            if "app" in entry:
                path = _match_shortcut(entry["app"], found)
                if path is None:
                    log.info("apps: no Start Menu shortcut for %r -- skipped", entry["app"])
                    continue
                item = {"name": entry.get("name") or os.path.splitext(os.path.basename(path))[0],
                        "kind": "shortcut", "target": path}
                icon_src = icon_src or _lnk_icon_source(path)
            elif "cmd" in entry:
                item = {"name": entry.get("name") or entry["cmd"][0], "kind": "cmd", "target": entry["cmd"]}
                if not icon_src:
                    exe = shutil.which(entry["cmd"][0])
                    icon_src = exe if exe and exe.lower().endswith(".exe") else None
            elif "open" in entry:
                item = {"name": entry.get("name") or entry["open"], "kind": "open", "target": entry["open"]}
            if item:
                ic = _win_icon(icon_src)
                if ic:
                    item["icon_bytes"], item["icon_mime"] = ic
                items.append(item)
        return items
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


# =============================================================================
# Shared
# =============================================================================
def _normalise(entry) -> dict | None:
    """Config entry -> {"app"|"cmd"|"open", name?, icon?}; None if invalid or for another OS."""
    if isinstance(entry, str):
        entry = {"app": entry}
    if not isinstance(entry, dict):
        return None
    want_os = str(entry.get("os") or "").lower()
    if want_os and not ((want_os == "windows" and IS_WINDOWS) or (want_os == "linux" and IS_LINUX)):
        return None
    out = {k: str(entry[k]) for k in ("name", "icon") if entry.get(k)}
    app = entry.get("app") or entry.get("desktop") or entry.get("shortcut")
    if isinstance(app, str) and app:
        out["app"] = app
    elif isinstance(entry.get("cmd"), list) and entry["cmd"]:
        out["cmd"] = [str(x) for x in entry["cmd"]]
    elif isinstance(entry.get("open"), str) and entry["open"]:
        out["open"] = entry["open"]
    else:
        return None
    return out


def load(config: dict) -> list[dict]:
    """Resolve the configured apps. Blocking (filesystem walk); call off-loop."""
    if not (IS_LINUX or IS_WINDOWS):
        return []
    wanted = config.get("apps")
    if not isinstance(wanted, list):
        if IS_WINDOWS:
            found = _shortcuts()
            wanted = [a for a in DEFAULT_APPS_WINDOWS if _match_shortcut(a, found)][:DEFAULT_MAX] + [LOCK_WINDOWS]
        else:
            files = _desktop_files()
            wanted = [a for a in DEFAULT_APPS_LINUX if _match_desktop(a, files)][:DEFAULT_MAX] + [LOCK_LINUX]
    entries = [e for e in (_normalise(x) for x in wanted) if e]
    items = _load_windows(entries) if IS_WINDOWS else _load_linux(entries)
    for i, it in enumerate(items):
        it["id"] = str(i)
    log.info("apps: %d launcher entries", len(items))
    return items


def _icon_version(it: dict) -> str:
    """Cache-buster for the icon URL: ids are list positions, so when the list
    changes the same id can point at a different icon (and the phone caches
    icons for a day)."""
    if it.get("icon_bytes"):
        return hashlib.sha1(it["icon_bytes"]).hexdigest()[:10]
    path = it.get("icon_path")
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        key = path
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def public(items: list[dict]) -> dict:
    return {"items": [{"id": i["id"], "name": i["name"],
                       "icon": bool(i.get("icon_path") or i.get("icon_bytes")),
                       "iconVer": _icon_version(i)} for i in items]}


def icon(app_id: str) -> tuple[bytes, str] | None:
    for it in ITEMS:
        if it["id"] != app_id:
            continue
        if it.get("icon_bytes"):
            return it["icon_bytes"], it.get("icon_mime") or "image/png"
        if it.get("icon_path"):
            try:
                with open(it["icon_path"], "rb") as f:
                    data = f.read(2_000_000)
            except OSError:
                return None
            return data, mimetypes.guess_type(it["icon_path"])[0] or "application/octet-stream"
    return None


def _spawn(argv: list[str]) -> None:
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              cwd=os.path.expanduser("~"))
    if IS_WINDOWS:
        # Own process group, no console window, not killed with the server.
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def launch(app_id: str) -> bool:
    for it in ITEMS:
        if it["id"] != app_id:
            continue
        kind, target = it["kind"], it["target"]
        try:
            if kind == "desktop":
                _spawn(["gio", "launch", target] if shutil.which("gio")
                       else ["gtk-launch", os.path.basename(target)[:-8]])
            elif kind == "shortcut":
                os.startfile(target)  # type: ignore[attr-defined]  # Windows only
            elif kind == "open":
                if IS_WINDOWS:
                    os.startfile(os.path.expandvars(target))  # type: ignore[attr-defined]
                else:
                    _spawn(["xdg-open", target])
            else:
                argv = [os.path.expandvars(a) for a in target] if IS_WINDOWS else target
                _spawn(argv)
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
