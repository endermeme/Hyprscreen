#!/usr/bin/env python3
"""Display and workspace configuration for Hyprland Lua."""

import argparse
import copy
import curses
import fcntl
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "hyprscreen"
DB = ROOT / "displays.json"
MON = ROOT / "monitors.lua"
WS = ROOT / "workspaces.lua"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / (
    "hyprscreen-" + str(os.getuid())
)
SCRIPT = Path(__file__).resolve()
BLOCK = 5


def run(*args):
    p = subprocess.run(["hyprctl", *args], text=True, capture_output=True, timeout=8)
    if p.returncode:
        raise RuntimeError(p.stderr or p.stdout)
    return p.stdout.strip()


def query(name):
    return json.loads(run(name, "-j"))


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save(data):
    atomic(DB, json.dumps(data, indent=2) + "\n")


def read():
    return json.loads(DB.read_text()) if DB.exists() else {"outputs": {}}


def live():
    return [
        m
        for m in query("monitors")
        if not m.get("disabled") and m.get("mirrorOf", "none") == "none"
    ]


def discover(data, monitors):
    outputs = data["outputs"]
    for m in sorted(monitors, key=lambda m: m["name"]):
        name = m["name"]
        if name not in outputs:
            outputs[name] = {
                "base": max([v["base"] for v in outputs.values()], default=-5) + 5,
                "mode": f"{m['width']}x{m['height']}@{m['refreshRate']:.3f}",
                "x": m["x"],
                "y": m["y"],
                "scale": m["scale"],
                "transform": m.get("transform", 0),
                "bitdepth": 10 if "2101010" in m.get("currentFormat", "") else 8,
            }
        outputs[name]["description"] = m.get("description", "")
    return data


def render(data, monitors):
    lines = [
        "-- Managed by hyprscreen. Run hyprscreen to edit.",
        'hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })',
    ]
    for name, c in data["outputs"].items():
        lines.append(
            "hl.monitor({ output = %s, mode = %s, position = %s, scale = %s, transform = %d, bitdepth = %d })"
            % (
                json.dumps(name),
                json.dumps(c["mode"]),
                json.dumps(f"{c['x']}x{c['y']}"),
                c["scale"],
                c["transform"],
                c["bitdepth"],
            )
        )
    atomic(MON, "\n".join(lines) + "\n")
    rules = [
        "-- Five independent workspaces per connected display; managed by hyprscreen."
    ]
    for m in monitors:
        base = data["outputs"][m["name"]]["base"]
        for slot in range(1, 6):
            rules.append(
                'hl.workspace_rule({ workspace = "%d", monitor = %s, persistent = true%s })'
                % (
                    base + slot,
                    json.dumps(m["name"]),
                    ", default = true" if slot == 1 else "",
                )
            )
    atomic(WS, "\n".join(rules) + "\n")


def move_window(address, workspace):
    run(
        "dispatch",
        'hl.dsp.window.move({ window = %s, workspace = "%d", follow = false })'
        % (json.dumps("address:" + address), workspace),
    )


def sync():
    ROOT.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (ROOT / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (RUNTIME / "pending.json").exists():
            pending = json.loads((RUNTIME / "pending.json").read_text())
            if time.time() > pending.get("expires", 0) + 5:
                rollback()
            else:
                return
        monitors = live()
        if not monitors:
            return
        data = discover(read(), monitors)
        save(data)
        render(data, monitors)
        run("reload")
        for m in monitors:
            base = data["outputs"][m["name"]]["base"]
            for slot in range(1, 6):
                run(
                    "dispatch",
                    'hl.dsp.workspace.move({ workspace = "%d", monitor = %s })'
                    % (base + slot, json.dumps(m["name"])),
                )
        statefile = RUNTIME / "folded.json"
        records = json.loads(statefile.read_text()) if statefile.exists() else {}
        clients = {w["address"]: w for w in query("clients")}
        active = {m["name"] for m in monitors}
        for address, record in list(records.items()):
            w = clients.get(address)
            if not w or w["workspace"]["id"] != record["fallback"]:
                del records[address]
            elif record["output"] in active:
                move_window(address, record["origin"])
                del records[address]
        fallback = next(
            (m for m in monitors if m["name"].startswith("eDP")), monitors[0]
        )
        fallback_base = data["outputs"][fallback["name"]]["base"]
        for name, config in data["outputs"].items():
            if name in active:
                continue
            for address, w in clients.items():
                origin = w["workspace"]["id"]
                if config["base"] < origin <= config["base"] + BLOCK:
                    dest = fallback_base + origin - config["base"]
                    records[address] = {
                        "output": name,
                        "origin": origin,
                        "fallback": dest,
                    }
                    move_window(address, dest)
        atomic(statefile, json.dumps(records))
        for m in live():
            base = data["outputs"][m["name"]]["base"]
            if not base < m["activeWorkspace"]["id"] <= base + BLOCK:
                run(
                    "eval",
                    'hl.get_monitor(%s):set_workspace({ workspace = "%d" })'
                    % (json.dumps(m["name"]), base + 1),
                )


def workspace(action, slot):
    name = run("repl", "hl.get_monitor_at_cursor().name")
    monitors = live()
    data = read()
    if name not in data["outputs"]:
        sync()
        data = read()
    base = data["outputs"][name]["base"]
    m = next(m for m in monitors if m["name"] == name)
    current = (m["activeWorkspace"]["id"] - base - 1) % BLOCK
    target = (
        (current + (1 if slot == "next" else -1)) % BLOCK + 1
        if slot in ("next", "prev")
        else int(slot)
    )
    if not 1 <= target <= BLOCK:
        raise ValueError("Workspace must be 1..5")
    wid = str(base + target)
    if action == "focus":
        run(
            "eval",
            "hl.get_monitor(%s):set_workspace({ workspace = %s })"
            % (json.dumps(name), json.dumps(wid)),
        )
    else:
        run(
            "dispatch",
            "hl.dsp.window.move({ workspace = %s, follow = %s })"
            % (json.dumps(wid), "false" if action == "send" else "true"),
        )


def watch():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        sockpath = (
            Path(os.environ["XDG_RUNTIME_DIR"])
            / "hypr"
            / os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
            / ".socket2.sock"
        )
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(str(sockpath))
            deadline = time.monotonic() + 1
            buffer = ""
            while True:
                ready, _, _ = select.select(
                    [sock],
                    [],
                    [],
                    max(0, deadline - time.monotonic()) if deadline else None,
                )
                if ready:
                    chunk = sock.recv(8192)
                    if not chunk:
                        return
                    buffer += chunk.decode(errors="replace")
                    while "\n" in buffer:
                        event, buffer = buffer.split("\n", 1)
                        if event.startswith("monitorremoved"):
                            deadline = time.monotonic() + 5
                        elif event.startswith("monitoradded"):
                            deadline = time.monotonic() + 2
                if deadline and time.monotonic() >= deadline:
                    try:
                        sync()
                    except Exception as e:
                        print(e, file=sys.stderr, flush=True)
                    deadline = None


def rollback():
    pending = RUNTIME / "pending.json"
    if pending.exists():
        old = json.loads(pending.read_text())
        atomic(MON, old["monitors"])
        atomic(DB, old["database"])
        pending.unlink()
        run("reload")


def watchdog():
    time.sleep(22)
    pending = RUNTIME / "pending.json"
    if pending.exists() and json.loads(pending.read_text()).get("token") == sys.argv[2]:
        rollback()


def dimensions(c):
    w, h = map(int, c["mode"].split("@")[0].split("x"))
    if c["transform"] % 2:
        w, h = h, w
    return round(w / c["scale"]), round(h / c["scale"])


def put(win, y, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h - 1:
        win.addnstr(y, 0, text, max(0, w - 1), attr)


def prompt(win, title, default=""):
    win.timeout(-1)
    win.erase()
    put(win, 0, title)
    put(win, 2, "Enter: accept | blank: keep current | Esc: cancel")
    put(win, 4, "Current: " + str(default))
    put(win, 6, "> ")
    win.refresh()
    curses.curs_set(1)
    value = ""
    try:
        while True:
            k = win.getch()
            if k == 27:
                return str(default)
            if k in (10, 13):
                return value.strip() or str(default)
            if k in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
            elif 32 <= k < 127 and len(value) < 100:
                value += chr(k)
            win.move(6, 2)
            win.clrtoeol()
            win.addnstr(6, 2, value, max(1, win.getmaxyx()[1] - 4))
            win.refresh()
    finally:
        curses.curs_set(0)


def choose(win, title, options):
    pos = 0
    while True:
        win.erase()
        put(win, 0, title, curses.A_BOLD)
        height = max(1, win.getmaxyx()[0] - 4)
        start = max(0, pos - height + 1)
        for i in range(start, min(len(options), start + height)):
            put(win, 2 + i - start, options[i], curses.A_REVERSE if i == pos else 0)
        put(
            win,
            win.getmaxyx()[0] - 2,
            f"{pos + 1}/{len(options)} | Up/Down or J/K scroll | Enter select | Esc cancel",
        )
        win.refresh()
        k = win.getch()
        if k in (27, ord("q")):
            return None
        if k in (10, 13):
            return pos
        if k in (curses.KEY_DOWN, ord("j")):
            pos = (pos + 1) % len(options)
        elif k in (curses.KEY_UP, ord("k")):
            pos = (pos - 1) % len(options)


def apply(win, data):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    pending = RUNTIME / "pending.json"
    if pending.exists():
        raise RuntimeError("Another display change is awaiting confirmation")
    token = str(time.time_ns())
    atomic(
        pending,
        json.dumps(
            {
                "monitors": MON.read_text(),
                "database": DB.read_text(),
                "token": token,
                "expires": time.time() + 22,
            }
        ),
    )
    subprocess.Popen(
        [sys.executable, str(SCRIPT), "watchdog", token],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        monitors = live()
        current = read()
        current["outputs"].update(data["outputs"])
        data = discover(current, monitors)
        save(data)
        render(data, monitors)
        run("reload")
        errors = run("configerrors")
        if errors:
            raise RuntimeError(errors)
        deadline = time.monotonic() + 20
        win.timeout(200)
        while time.monotonic() < deadline:
            win.erase()
            put(
                win,
                1,
                "KEEP THESE DISPLAY SETTINGS? [y] Keep / any other key: restore",
                curses.A_BOLD,
            )
            put(
                win,
                3,
                f"Automatic rollback in {int(deadline - time.monotonic()) + 1} seconds",
            )
            for i, m in enumerate(live()):
                put(
                    win,
                    5 + i,
                    f"{m['name']}: {m['width']}x{m['height']} @ {m['refreshRate']:.2f}Hz, scale {m['scale']}",
                )
            win.refresh()
            k = win.getch()
            if k == ord("y"):
                pending.unlink(missing_ok=True)
                return "Saved and applied."
            if k != -1 and k != curses.KEY_RESIZE:
                break
        rollback()
        return "Previous settings restored."
    except Exception:
        rollback()
        raise
    finally:
        win.timeout(-1)


def mode_catalog(monitor, current):
    """Keep every advertised refresh; don't label custom values as supported."""
    catalog = {}
    for raw in monitor.get("availableModes", []):
        match = re.fullmatch(r"(\d+x\d+)@([\d.]+)(?:Hz)?", raw)
        if match:
            resolution, rate = match.groups()
            catalog.setdefault(resolution, {})[float(rate)] = True
    resolution, rate = current.split("@")
    rates = catalog.setdefault(resolution, {})
    if not any(abs(float(rate) - r) < 0.01 for r in rates):
        rates[float(rate)] = False
    return catalog


def select_mode(win, monitor, config, refresh_only=False):
    catalog = mode_catalog(monitor, config["mode"])
    resolution = config["mode"].split("@")[0]
    if not refresh_only:
        resolutions = sorted(
            catalog, key=lambda s: tuple(map(int, s.split("x"))), reverse=True
        )
        labels = [
            r
            + "   |   "
            + ", ".join(f"{v:g}" for v in sorted(catalog[r], reverse=True))
            + " Hz"
            for r in resolutions
        ]
        idx = choose(
            win,
            monitor["name"] + ": resolution (all Hz listed per resolution)",
            labels + ["Custom resolution + Hz..."],
        )
        if idx is None:
            return
        if idx == len(resolutions):
            value = prompt(
                win,
                "Custom mode (not verified by display): WIDTHxHEIGHT@HZ",
                config["mode"],
            )
            if not re.fullmatch(r"\d{3,5}x\d{3,5}@\d{1,3}(\.\d+)?", value):
                raise ValueError("Expected a mode like 1920x1080@60")
            config["mode"] = value
            return
        resolution = resolutions[idx]
    rates = sorted(catalog[resolution], reverse=True)
    labels = [
        f"{r:g} Hz"
        + (
            "  [reported by display]"
            if catalog[resolution][r]
            else "  [current/custom, not advertised]"
        )
        for r in rates
    ]
    idx = choose(
        win,
        monitor["name"] + " / " + resolution + ": refresh rate",
        labels + ["Custom Hz (not verified by display)..."],
    )
    if idx is None:
        return
    rate = (
        rates[idx]
        if idx < len(rates)
        else float(
            prompt(
                win,
                "Custom Hz: requires Apply + confirmation",
                config["mode"].split("@")[1],
            )
        )
    )
    if not 1 <= rate <= 999:
        raise ValueError("Refresh rate must be 1..999 Hz")
    config["mode"] = f"{resolution}@{rate:g}"


def layout_bounds(outputs, names):
    rects = [(outputs[n]["x"], outputs[n]["y"], *dimensions(outputs[n])) for n in names]
    return (
        min(x for x, y, w, h in rects),
        min(y for x, y, w, h in rects),
        max(x + w for x, y, w, h in rects),
        max(y + h for x, y, w, h in rects),
    )


def snap_screen(config, others, distance=160):
    """Snap to a non-overlapping adjacent edge, preserving near-edge alignment."""
    w, h = dimensions(config)
    candidates = []
    for ref in others:
        rw, rh = dimensions(ref)
        x, y = config["x"], config["y"]
        rx, ry = ref["x"], ref["y"]
        if min(y + h, ry + rh) > max(y, ry):
            for nx in (rx - w, rx + rw):
                if abs(nx - x) <= distance:
                    align = min(
                        (ry, ry + rh - h, round(ry + (rh - h) / 2)),
                        key=lambda a: abs(a - y),
                    )
                    candidates.append(
                        (abs(nx - x), nx, align if abs(align - y) <= distance else y)
                    )
        if min(x + w, rx + rw) > max(x, rx):
            for ny in (ry - h, ry + rh):
                if abs(ny - y) <= distance:
                    align = min(
                        (rx, rx + rw - w, round(rx + (rw - w) / 2)),
                        key=lambda a: abs(a - x),
                    )
                    candidates.append(
                        (abs(ny - y), align if abs(align - x) <= distance else x, ny)
                    )
    if candidates:
        _, config["x"], config["y"] = min(candidates)


def layout_editor(win, outputs, names, selected):
    staged = copy.deepcopy(outputs)
    original = copy.deepcopy(outputs)
    step = 10
    steps = [1, 2, 5, 10, 20, 50, 100, 200, 500]
    zoom = 1
    zoom_anchor = None
    reference = next((i for i in range(len(names)) if i != selected), selected)
    viewport = layout_bounds(staged, names)
    while True:
        height, width = win.getmaxyx()
        win.erase()
        put(
            win,
            0,
            "Tab select | arrows move | +/- step | hjkl: 1px | HJKL: 100px",
            curses.A_BOLD,
        )
        put(win, 1, "Z/X zoom in/out | F fit | Space snap | 1-4 place | N reference")
        put(
            win,
            2,
            "Enter: keep draft | Esc: cancel | U: reset draft (Apply later with A)",
        )
        name = names[selected]
        c = staged[name]
        put(
            win,
            3,
            f"Selected: {name}  Step: {step}px  Reference: {names[reference]}  Position: {c['x']},{c['y']}",
        )
        old = original[name]
        put(
            win,
            4,
            f"Delta: X {c['x'] - old['x']:+d}  Y {c['y'] - old['y']:+d} px | Zoom {zoom}x | Snap only on Space",
        )
        if height < 16 or width < 65:
            put(win, 5, "Enlarge terminal to at least 65 columns x 16 rows.")
        else:
            left, top, right, bottom = viewport
            canvas_w, canvas_h = width - 4, height - 9
            factor = (
                min(
                    (canvas_w - 4) / max(right - left, 1),
                    (canvas_h - 2) * 2 / max(bottom - top, 1),
                )
                * 0.88
            )
            if zoom > 1 and zoom_anchor is not None:
                factor *= zoom
                left, top = zoom_anchor
                ox = width / 2
                oy = 6 + canvas_h / 2
            else:
                ox = 2 + (canvas_w - (right - left) * factor) / 2
                oy = 6 + (canvas_h - (bottom - top) * factor / 2) / 2
            order = [i for i in range(len(names)) if i != selected] + [selected]
            for i in order:
                item = staged[names[i]]
                w, h = dimensions(item)
                x = round(ox + (item["x"] - left) * factor)
                y = round(oy + (item["y"] - top) * factor / 2)
                bw, bh = max(5, round(w * factor)), max(3, round(h * factor / 2))
                attr = (
                    curses.A_BOLD | curses.A_REVERSE
                    if i == selected
                    else curses.A_NORMAL
                )

                def draw(yy, xx, text):
                    if yy < 5 or yy >= height - 3 or xx >= width - 1:
                        return
                    if xx < 0:
                        text, xx = text[-xx:], 0
                    if text:
                        win.addnstr(yy, xx, text, max(0, width - xx - 1), attr)

                draw(y, x, "+" + "-" * (bw - 2) + "+")
                for yy in range(max(5, y + 1), min(height - 3, y + bh - 1)):
                    draw(yy, x, "|" + " " * (bw - 2) + "|")
                draw(y + bh - 1, x, "+" + "-" * (bw - 2) + "+")
                label = names[i][: bw - 2]
                draw(y + bh // 2, x + 1, label.center(bw - 2))
                if bh >= 5:
                    draw(y + bh // 2 + 1, x + 1, f"{w}x{h}"[: bw - 2].center(bw - 2))
            put(
                win,
                height - 3,
                "Z zooms at selected top-left corner. hjkl nudges exactly 1px; F shows all.",
            )
        win.refresh()
        k = win.getch()
        if k == 27:
            return
        if k in (10, 13):
            outputs.update(staged)
            return
        if k == 9:
            selected = (selected + 1) % len(names)
            if reference == selected:
                reference = (selected + 1) % len(names)
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            dx, dy = {
                curses.KEY_LEFT: (-step, 0),
                curses.KEY_RIGHT: (step, 0),
                curses.KEY_UP: (0, -step),
                curses.KEY_DOWN: (0, step),
            }[k]
            c["x"] += dx
            c["y"] += dy
        elif k in map(ord, "hjklHJKL"):
            key = chr(k)
            amount = 100 if key.isupper() else 1
            dx, dy = {
                "h": (-amount, 0),
                "j": (0, amount),
                "k": (0, -amount),
                "l": (amount, 0),
            }[key.lower()]
            c["x"] += dx
            c["y"] += dy
        elif k in (ord("+"), ord("=")):
            step = steps[min(len(steps) - 1, steps.index(step) + 1)]
        elif k == ord("-"):
            step = steps[max(0, steps.index(step) - 1)]
        elif k in (ord("z"), ord("Z")):
            if zoom == 1:
                zoom_anchor = (c["x"], c["y"])
            zoom = min(256, zoom * 2)
        elif k in (ord("x"), ord("X")):
            zoom = max(1, zoom // 2)
        elif k == ord(" "):
            snap_screen(c, [staged[n] for n in names if n != name], max(160, step))
        elif k in (ord("f"), ord("F")):
            zoom, zoom_anchor = 1, None
            viewport = layout_bounds(staged, names)
        elif k in (ord("u"), ord("U")):
            staged = copy.deepcopy(original)
            zoom, zoom_anchor = 1, None
            viewport = layout_bounds(staged, names)
        elif k in (ord("n"), ord("N")) and len(names) > 1:
            reference = (reference + 1) % len(names)
            if reference == selected:
                reference = (reference + 1) % len(names)
        elif k in (ord("1"), ord("2"), ord("3"), ord("4")) and reference != selected:
            ref = staged[names[reference]]
            w, h = dimensions(c)
            rw, rh = dimensions(ref)
            cx, cy = round(ref["x"] + (rw - w) / 2), round(ref["y"] + (rh - h) / 2)
            c["x"], c["y"] = [
                (ref["x"] - w, cy),
                (ref["x"] + rw, cy),
                (cx, ref["y"] - h),
                (cx, ref["y"] + rh),
            ][k - ord("1")]
            viewport = layout_bounds(staged, names)


def tui(win):
    curses.curs_set(0)
    sync()
    data = read()
    selected = 0
    message = "Every display has 5 independent workspaces. Changes are staged until A."
    while True:
        monitors = live()
        names = [m["name"] for m in monitors if m["name"] in data["outputs"]]
        win.erase()
        put(win, 0, "HYPRSCREEN  |  arrows/j/k select display", curses.A_BOLD)
        put(
            win,
            2,
            "Output          Resolution / Hz           Scale   Position      Workspaces",
        )
        if not names:
            raise RuntimeError(
                "No displays detected. Reopen after connecting a display."
            )
        selected %= len(names)
        for i, name in enumerate(names):
            c = data["outputs"][name]
            put(
                win,
                3 + i,
                f"{name:15} {c['mode']:25} {c['scale']:<7} {c['x']:>5},{c['y']:<5}  1-5 (IDs {c['base'] + 1}-{c['base'] + 5})",
                curses.A_REVERSE if i == selected else 0,
            )
        row = 5 + len(names)
        put(
            win,
            row,
            "[M] Resolution  [H] Refresh Hz  [S] Scale  [P] Visual layout  [X] Coordinates",
        )
        put(
            win,
            row + 1,
            "[O] Rotation  [A] Apply + save  [R] Rescan / discard edits  [Q] Quit",
        )
        selected_live = next(m for m in monitors if m["name"] == names[selected])
        selected_config = data["outputs"][names[selected]]
        resolution = selected_config["mode"].split("@")[0]
        catalog = mode_catalog(selected_live, selected_config["mode"])
        supported = [
            f"{r:g}"
            for r, reported in sorted(catalog[resolution].items(), reverse=True)
            if reported
        ]
        put(
            win,
            row + 3,
            resolution
            + ": reported Hz = "
            + (", ".join(supported) or "unknown; current mode is custom"),
        )
        if (
            not selected_live.get("description")
            and len(selected_live.get("availableModes", [])) <= 1
        ):
            put(
                win,
                row + 4,
                "Display identification unavailable: driver reports fallback modes only.",
            )
        put(win, row + 5, message)
        win.refresh()
        k = win.getch()
        if k in (ord("q"), ord("Q")):
            return
        if k in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(names)
            continue
        if k in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(names)
            continue
        name = names[selected]
        c = data["outputs"][name]
        try:
            if k in (ord("m"), ord("M"), ord("h"), ord("H")):
                m = next(m for m in monitors if m["name"] == name)
                select_mode(win, m, c, refresh_only=k in (ord("h"), ord("H")))
            elif k in (ord("s"), ord("S")):
                value = float(
                    prompt(win, "Scale (e.g. 1, 1.25, 1.5, 1.6, 2)", c["scale"])
                )
                if not 0.5 <= value <= 4:
                    raise ValueError("Scale must be 0.5..4")
                c["scale"] = value
            elif k in (ord("x"), ord("X")):
                value = prompt(
                    win, "Position X,Y (logical pixels)", f"{c['x']},{c['y']}"
                )
                c["x"], c["y"] = map(int, value.split(","))
            elif k in (ord("o"), ord("O")):
                idx = choose(
                    win,
                    "Rotation",
                    ["Normal", "90 degrees", "180 degrees", "270 degrees"],
                )
                if idx is not None:
                    c["transform"] = idx
            elif k in (ord("p"), ord("P")):
                layout_editor(win, data["outputs"], names, selected)
            elif k in (ord("a"), ord("A")):
                message = apply(win, data)
                data = read()
            elif k in (ord("r"), ord("R")):
                sync()
                data = read()
                message = "Rescanned connected displays; unsaved edits discarded."
            else:
                continue
            if k not in (ord("a"), ord("A"), ord("r"), ord("R")):
                message = "Staged. Press A to apply, R to discard."
        except Exception as e:
            message = str(e).replace("\n", " ")[:200]


def target_monitor(monitors, source_id, direction):
    monitors = [
        m
        for m in monitors
        if not m.get("disabled") and m.get("mirrorOf", "none") == "none"
    ]
    if len(monitors) < 2:
        return None
    source = next((m for m in monitors if m["id"] == source_id), None)
    if source is None:
        return None

    def bounds(m):
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2:
            w, h = h, w
        return m["x"], m["y"], m["x"] + w / m["scale"], m["y"] + h / m["scale"]

    sx, sy, sr, sb = bounds(source)
    candidates = []
    for m in monitors:
        if m["id"] == source_id:
            continue
        x, y, r, b = bounds(m)
        # Require overlap on the perpendicular axis, so diagonal screens
        # cannot steal an arrow intended for a directly adjacent screen.
        if direction == "left" and r <= sx + 1 and min(b, sb) > max(y, sy):
            score = sx - r
        elif direction == "right" and x >= sr - 1 and min(b, sb) > max(y, sy):
            score = x - sr
        elif direction == "up" and b <= sy + 1 and min(r, sr) > max(x, sx):
            score = sy - b
        elif direction == "down" and y >= sb - 1 and min(r, sr) > max(x, sx):
            score = y - sb
        else:
            continue
        candidates.append((score, m["id"], m))
    return min(candidates, key=lambda c: c[:2])[2] if candidates else None


def transfer(direction):
    if direction not in ("left", "right", "up", "down"):
        raise ValueError("Direction must be left, right, up or down")
    window = query("activewindow")
    if not window.get("address"):
        return
    target = target_monitor(live(), window.get("monitor"), direction)
    if target and target["activeWorkspace"]["id"] > 0:
        run(
            "dispatch",
            'hl.dsp.window.move({ window = %s, workspace = "%d" })'
            % (
                json.dumps("address:" + window["address"]),
                target["activeWorkspace"]["id"],
            ),
        )


def initialize():
    if run("repl", "type(hl.monitor)") != "function":
        raise RuntimeError(
            "Hyprscreen requires Hyprland with the Lua configuration API."
        )
    monitors = live()
    if not monitors:
        raise RuntimeError("No active displays found.")
    data = discover(read(), monitors)
    save(data)
    render(data, monitors)
    integration = ROOT / "hyprscreen.lua"
    integration.write_text(INTEGRATION)
    print("Add this after your other imports in hyprland.lua:")
    print("dofile(" + json.dumps(str(integration)) + ")")
    print("Remove conflicting monitor/workspace rules, then run hyprctl reload.")


INTEGRATION = r"""local home = os.getenv("HOME")
local config = os.getenv("XDG_CONFIG_HOME") or (home .. "/.config")
dofile(config .. "/hyprscreen/monitors.lua")
dofile(config .. "/hyprscreen/workspaces.lua")
local command = string.format("%q", home .. "/.local/bin/hyprscreen")
for slot = 1, 5 do
  local key = "code:" .. tostring(slot + 9)
  hl.unbind("SUPER + " .. key)
  hl.unbind("SUPER + SHIFT + " .. key)
  hl.bind("SUPER + " .. key, hl.dsp.exec_cmd(command .. " focus " .. slot))
  hl.bind("SUPER + SHIFT + " .. key, hl.dsp.exec_cmd(command .. " move " .. slot))
end
for _, direction in ipairs({ "left", "right", "up", "down" }) do
  local key = "SUPER + CTRL + ALT + " .. direction:upper()
  hl.unbind(key)
  hl.bind(key, hl.dsp.exec_cmd(command .. " transfer " .. direction))
end
hl.exec_cmd(command .. " watch")
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version="hyprscreen 0.1.0")
    parser.add_argument(
        "command",
        nargs="?",
        default="tui",
        choices=[
            "tui",
            "init",
            "sync",
            "watch",
            "rollback",
            "focus",
            "move",
            "send",
            "transfer",
            "watchdog",
        ],
    )
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    command = args.command
    if (
        command in ("focus", "move", "send", "transfer", "watchdog")
        and args.argument is None
    ):
        parser.error(command + " requires an argument")
    if command == "init":
        initialize()
    elif command == "tui":
        if not (ROOT / "hyprscreen.lua").exists():
            parser.error(
                "Run hyprscreen init and load its Lua file before opening the TUI."
            )
        curses.wrapper(tui)
    elif command in ("focus", "move", "send"):
        workspace(command, args.argument)
    elif command == "transfer":
        transfer(args.argument)
    elif command == "sync":
        sync()
    elif command == "watch":
        watch()
    elif command == "watchdog":
        watchdog()
    elif command == "rollback":
        rollback()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(str(error))
