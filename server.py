#!/usr/bin/env python3
"""
Shared game-state server for a LAN event.

Run on ONE computer:
    python3 server.py                  # port 8000, no admin PIN
    python3 server.py 8080 --pin 1234  # custom port, admin page needs PIN 1234

Then open the printed address on every device on the same Wi-Fi.
Only the Python standard library is used - nothing to install.

Game rules (teams, items, commands and their effect rotations) live in
game_config.json. They can be edited there (restart the server) or live from
the admin page (/?admin).

How syncing works: devices send small actions ("team 3 uses Forest"). The
server applies them one at a time under a lock, so simultaneous presses never
clobber each other, then pushes the new state to every device via
Server-Sent Events. Everything is saved to game_state.json after each change.
"""

import argparse
import copy
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "game_state.json"
MAX_LOG = 300
MAX_UNDO = 200

ap = argparse.ArgumentParser(description="Event game server")
ap.add_argument("port", nargs="?", type=int, default=8000)
ap.add_argument("--pin", default="", help="PIN required for admin actions (undo, reset, edits)")
ap.add_argument("--config", default=str(ROOT / "game_config.json"), help="rules file")
ARGS = ap.parse_args()
CONFIG_FILE = Path(ARGS.config)

ROTATION_MODES = ("per_command", "team_wide")


# ---------------------------------------------------------------------------
# Config validation - turns the JSON rules into a normalized structure
# ---------------------------------------------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "x"


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    cfg = {
        "title": str(raw.get("title", "Game")),
        "rotation": raw.get("rotation", "per_command"),
        "allow_negative": bool(raw.get("allow_negative", False)),
        "show_next_effect": bool(raw.get("show_next_effect", False)),
        "return_after_seconds": max(0, int(raw.get("return_after_seconds", 15))),
    }
    if cfg["rotation"] not in ROTATION_MODES:
        raise ValueError(f"rotation must be one of {ROTATION_MODES}")

    teams = raw.get("teams") or []
    if not isinstance(teams, list) or not teams:
        raise ValueError("'teams' must be a non-empty list")
    cfg["teams"] = []
    for i, t in enumerate(teams):
        if isinstance(t, str):
            t = {"name": t}
        cfg["teams"].append({"name": str(t.get("name") or f"Team {i + 1}"),
                             "color": str(t.get("color") or "#868e96")})

    items = raw.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("'items' must be a non-empty list")
    cfg["items"], ids = [], set()
    for it in items:
        if isinstance(it, str):
            it = {"id": _slug(it), "name": it}
        iid = str(it.get("id") or _slug(it.get("name", "")))
        if iid in ids:
            raise ValueError(f"duplicate item id '{iid}'")
        ids.add(iid)
        cfg["items"].append({"id": iid, "name": str(it.get("name") or iid), "icon": str(it.get("icon", ""))})

    start = raw.get("start_inventory") or {}
    for k in start:
        if k not in ids:
            raise ValueError(f"start_inventory: unknown item '{k}'")
    cfg["start_inventory"] = {i: int(start.get(i, 0)) for i in ids}

    cmds = raw.get("commands") or []
    if not isinstance(cmds, list) or not cmds:
        raise ValueError("'commands' must be a non-empty list")
    cfg["commands"], cids = [], set()
    for c in cmds:
        name = str(c.get("name") or "").strip()
        if not name:
            raise ValueError("every command needs a 'name'")
        cid = str(c.get("id") or _slug(name))
        if cid in cids:
            raise ValueError(f"duplicate command id '{cid}'")
        cids.add(cid)
        effects = c.get("effects") or []
        if not isinstance(effects, list) or not effects:
            raise ValueError(f"command '{name}' needs at least one effect")
        norm = []
        for e in effects:
            change = e.get("change") or {}
            for k in change:
                if k not in ids:
                    raise ValueError(f"command '{name}': unknown item '{k}'")
            norm.append({"text": str(e.get("text", "")),
                         "change": {k: int(v) for k, v in change.items() if int(v) != 0}})
        cfg["commands"].append({"id": cid, "name": name, "effects": norm})
    return cfg


def load_config_file():
    return validate_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Game engine
# ---------------------------------------------------------------------------

class Game:
    def __init__(self):
        self.cond = threading.Condition()
        self.config = load_config_file()
        self.state = self.initial_state()
        self.log, self.undo, self.version = [], [], 0
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.state, self.log, self.version = data["state"], data.get("log", []), data.get("version", 0)
                print(f"Resuming saved game from {STATE_FILE.name}")
            except Exception as e:
                print(f"Could not read {STATE_FILE.name} ({e}); starting fresh")
        self.sync_with_config()

    # ----- state helpers -----
    def new_team(self, i):
        return {"id": i, "name": "", "color": "", "inventory": dict(self.config["start_inventory"]),
                "step": 0, "pos": {}, "uses": 0}

    def initial_state(self):
        st = {"teams": [self.new_team(i) for i in range(len(self.config["teams"]))]}
        self.state = st
        self.sync_with_config()
        return st

    def sync_with_config(self):
        """Keep team list/items in line with the config (after a rules edit)."""
        teams = self.state["teams"]
        while len(teams) < len(self.config["teams"]):
            teams.append(self.new_team(len(teams)))
        del teams[len(self.config["teams"]):]
        for t, tc in zip(teams, self.config["teams"]):
            t["name"], t["color"] = tc["name"], tc["color"]
            for it in self.config["items"]:
                t["inventory"].setdefault(it["id"], 0)

    def team(self, payload):
        try:
            return self.state["teams"][int(payload["team"])]
        except (KeyError, TypeError, ValueError, IndexError):
            raise ValueError("invalid team")

    def command(self, cid):
        for c in self.config["commands"]:
            if c["id"] == cid:
                return c
        raise ValueError(f"unknown command '{cid}'")

    def rotation_index(self, team, cmd):
        raw = team["step"] if self.config["rotation"] == "team_wide" else team["pos"].get(cmd["id"], 0)
        return raw % len(cmd["effects"])

    def item_label(self, iid):
        for it in self.config["items"]:
            if it["id"] == iid:
                return f"{it['icon']} {it['name']}".strip()
        return iid

    def describe(self, change):
        return ", ".join(f"{v:+d} {self.item_label(k)}" for k, v in change.items()) or "no change"

    # ----- actions -----
    def act_do(self, p):
        """A team uses a command: apply its current effect, then rotate."""
        team, cmd = self.team(p), self.command(p.get("command"))
        idx = self.rotation_index(team, cmd)
        effect = cmd["effects"][idx]
        inv = team["inventory"]
        if not self.config["allow_negative"]:
            for k, d in effect["change"].items():
                if inv.get(k, 0) + d < 0:
                    raise ValueError(f"Not enough {self.item_label(k)}: need {-d}, have {inv.get(k, 0)}")
        for k, d in effect["change"].items():
            inv[k] = inv.get(k, 0) + d
        if self.config["rotation"] == "team_wide":
            team["step"] += 1
        else:
            team["pos"][cmd["id"]] = team["pos"].get(cmd["id"], 0) + 1
        team["uses"] += 1
        msg = f"{team['name']} → {cmd['name']}: {self.describe(effect['change'])}"
        result = {"team": team["id"], "command": cmd["id"], "command_name": cmd["name"],
                  "text": effect["text"], "change": effect["change"],
                  "position": idx + 1, "of": len(cmd["effects"])}
        return msg, result

    def act_adjust(self, p):
        team = self.team(p)
        item, delta = str(p.get("item")), int(p.get("delta", 0))
        if item not in team["inventory"] or delta == 0:
            raise ValueError("invalid item or delta")
        team["inventory"][item] += delta
        return f"[admin] {team['name']} {delta:+d} {self.item_label(item)}", None

    def act_set_config(self, p):
        self.config = validate_config(p.get("config"))
        write_json(CONFIG_FILE, self.config)
        self.sync_with_config()
        return "[admin] Rules updated", None

    def act_reset(self, p):
        self.state = self.initial_state()
        self.log = []
        return "[admin] Game reset", None

    PUBLIC = {"do": "act_do"}
    ADMIN = {"adjust": "act_adjust", "set_config": "act_set_config", "reset": "act_reset"}

    def apply(self, action, payload, device, pin):
        payload = payload or {}
        device = str(device or "?")[:30]
        with self.cond:
            if action in self.ADMIN or action == "undo":
                if ARGS.pin and str(pin) != ARGS.pin:
                    raise PermissionError("wrong admin PIN")
            if action == "undo":
                if not self.undo:
                    raise ValueError("nothing to undo")
                self.state, self.log, old_cfg = self.undo.pop()
                if old_cfg != self.config:
                    self.config = old_cfg
                    write_json(CONFIG_FILE, self.config)
                return self._commit(device, "[admin] ↶ undid last change", None)
            name = self.PUBLIC.get(action) or self.ADMIN.get(action)
            if not name:
                raise ValueError(f"unknown action '{action}'")
            before = (copy.deepcopy(self.state), list(self.log), self.config)
            try:
                msg, result = getattr(self, name)(payload)
            except (ValueError, KeyError, TypeError) as e:
                self.state, self.log, self.config = before
                raise ValueError(str(e))
            self.undo.append(before)
            del self.undo[:-MAX_UNDO]
            return self._commit(device, msg, result)

    def _commit(self, device, msg, result):
        self.log.append({"t": time.time(), "device": device, "msg": msg})
        del self.log[:-MAX_LOG]
        self.version += 1
        write_json(STATE_FILE, {"state": self.state, "log": self.log, "version": self.version})
        self.cond.notify_all()
        return result

    # ----- what devices get to see -----
    def public_snapshot(self):
        cfg = {k: v for k, v in self.config.items() if k != "commands"}
        cfg["commands"] = [{"id": c["id"], "name": c["name"], "effect_count": len(c["effects"])}
                           for c in self.config["commands"]]
        teams = []
        for t in self.state["teams"]:
            t = dict(t)
            t["rotation"] = {c["id"]: self.rotation_index(t, c) for c in self.config["commands"]}
            if self.config["show_next_effect"]:
                t["next"] = {c["id"]: c["effects"][t["rotation"][c["id"]]] for c in self.config["commands"]}
            teams.append(t)
        return {"version": self.version, "config": cfg, "state": {"teams": teams},
                "log": self.log[-60:], "pin_required": bool(ARGS.pin)}

    def wait_change(self, seen, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout=timeout)
            return self.public_snapshot()


GAME = Game()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/state":
            with GAME.cond:
                self._send(200, GAME.public_snapshot())
        elif url.path == "/config":  # full rules incl. effects, for the admin editor
            pin = parse_qs(url.query).get("pin", [""])[0]
            if ARGS.pin and pin != ARGS.pin:
                return self._send(403, {"error": "wrong admin PIN"})
            with GAME.cond:
                self._send(200, GAME.config)
        elif url.path == "/events":
            self._events()
        else:
            self._send(404, {"error": "not found"})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = None
        try:
            while True:
                snap = GAME.wait_change(seen, timeout=15)
                if snap["version"] != seen:
                    seen = snap["version"]
                    self.wfile.write(b"data: " + json.dumps(snap, ensure_ascii=False).encode() + b"\n\n")
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def do_POST(self):
        if urlparse(self.path).path != "/action":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            result = GAME.apply(body.get("action"), body.get("payload"), body.get("device"), body.get("pin", ""))
            self._send(200, {"ok": True, "result": result})
        except PermissionError as e:
            self._send(403, {"ok": False, "error": str(e)})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"ok": False, "error": str(e)})


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is actually sent
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler)
    server.daemon_threads = True
    base = f"http://{lan_ip()}:{ARGS.port}"
    print("=" * 56)
    print(f"  Player devices:   {base}")
    print(f"  Scoreboard:       {base}/?board")
    print(f"  Admin:            {base}/?admin" + ("   (PIN required)" if ARGS.pin else ""))
    print("  Ctrl+C to stop. Game is saved automatically.")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
