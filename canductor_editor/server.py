"""canductor_editor.server — scenario editor HTTP service (stdlib asyncio).

Serves the single-page editor UI plus the scenario CRUD/validation API over a
directory of scenario JSON files (the same documents the canductor worker
consumes, `canductor/scenarios/*.json`).

API:
  GET  /                        editor UI (web/index.html)
  GET  /health
  GET  /api/scenarios           {dir, names}
  GET  /api/scenario?name=N     {name, doc, mtime}
  POST /api/validate            {doc} -> {errors}
  POST /api/scenario            {name, doc, overwrite?} -> {ok, bytes, errors}
  POST /api/scenario/new        {name, doc?} -> created (409 if exists)
  POST /api/scenario/duplicate  {name, from}
  POST /api/scenario/delete     {name}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import parse as urlparse

from . import __version__

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KINDS = ("state", "action", "setup")
_PHASES = ("quiet", "action", "after")
BUS_ALIASES = ("hs", "ms")  # the system has exactly two buses: HS-CAN and MS-CAN (one adapter each)
_HEX = re.compile(r"^[0-9A-Fa-f]+$")
_RULE_KEYS = (
    "quiet_s", "after_s", "repeats", "pause_s",
    "action_min_s", "action_max_s", "state_max_s",
)
_BUS_OVERRIDE_KEYS = ("can_baud", "std_only", "variable")
_RULE_DEFAULTS: dict[str, float] = {
    "quiet_s": 5.0, "after_s": 5.0, "repeats": 3.0, "pause_s": 10.0,
    "action_min_s": 5.0, "action_max_s": 15.0, "state_max_s": 120.0,
}


class EditorError(Exception):
    """HTTP-layer error (bad name, missing file, ...)."""


class ScenarioStore:
    """CRUD over `<root>/<name>.json` scenario documents (atomic writes)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    @staticmethod
    def check_name(name: str) -> str:
        if not name or not NAME_RE.match(name):
            raise EditorError(f"bad scenario name {name!r} (letters, digits, . _ -, max 64)")
        return name

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def get(self, name: str) -> dict[str, Any]:
        path = self._path(self.check_name(name))
        if not path.is_file():
            raise EditorError(f"scenario {name!r} not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise EditorError(f"scenario {name!r} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise EditorError(f"scenario {name!r} must be a JSON object")
        return data

    def _write(self, name: str, doc: dict[str, Any]) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(doc, ensure_ascii=False, indent=1)
        path = self._path(name)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=f".{name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.chmod(tmp, 0o644)  # mkstemp gives 0600; scenario files should be world-readable
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(payload.encode("utf-8"))

    def save(self, name: str, doc: dict[str, Any], overwrite: bool = True) -> tuple[int, list[str]]:
        """Validate then write; returns (bytes, leftover warnings)."""
        self.root.mkdir(parents=True, exist_ok=True)
        if self._path(self.check_name(name)).is_file() and not overwrite:
            raise EditorError(f"scenario {name!r} already exists")
        if not isinstance(doc, dict):
            raise EditorError("document must be a JSON object")
        errors = validate(doc, name)
        if errors:
            raise EditorError("invalid scenario: " + "; ".join(errors))
        doc = dict(doc)
        doc["name"] = name
        return self._write(name, doc), []

    def create(self, name: str, doc: dict[str, Any] | None = None, force: bool = False) -> int:
        self.check_name(name)
        if self._path(name).is_file() and not force:
            raise EditorError(f"scenario {name!r} already exists")
        doc = doc if isinstance(doc, dict) else new_scenario(name)
        return self._write(name, doc)

    def duplicate(self, dst: str, src: str) -> int:
        data = self.get(src)
        data = dict(data)
        data["name"] = dst
        return self._write(self.check_name(dst), data)

    def delete(self, name: str) -> None:
        path = self._path(self.check_name(name))
        if not path.is_file():
            raise EditorError(f"scenario {name!r} not found")
        path.unlink()

    def mtime(self, name: str) -> float:
        path = self._path(self.check_name(name))
        return path.stat().st_mtime if path.is_file() else 0.0


def new_scenario(name: str) -> dict[str, Any]:
    """Template for a fresh scenario (canductor defaults: hs 500k / ms 125k)."""
    return {
        "name": name,
        "title": f"Scenario {name}",
        "version": "1.0",
        "cars": {},
        "buses": {
            "hs": {"label": "HS-CAN 500 kbit/s", "can_baud": 500_000, "std_only": True,
                   "point": "OBD-II: pin 6 (CAN_H), pin 14 (CAN_L)"},
            "ms": {"label": "MS-CAN 125 kbit/s", "can_baud": 125_000, "std_only": True,
                   "point": "body / HVAC"},
        },
        "rules": dict(_RULE_DEFAULTS),
        "known_ids": {},
        "warnings": [],
        "stages": [{"id": "1", "title": "First stage", "warn": "", "checklist": [], "tests": []}],
    }

def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(doc: dict[str, Any], name: str = "") -> list[str]:
    """Schema validation, mirroring canductor.worker.scenario (returns issues).

    Additionally round-trips the document through canductor's own loader when
    it is importable, so anything saved here is guaranteed to load there.
    """
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]

    cars = doc.get("cars", {}) or {}
    buses = doc.get("buses", {}) or {}
    if not isinstance(cars, dict):
        errs.append("cars must be an object of alias -> {label}")
    if not isinstance(buses, dict):
        errs.append("buses must be an object of alias -> {label, can_baud}")
        buses = {}
    car_keys = list(cars.keys()) if isinstance(cars, dict) else []
    bus_keys = list(buses.keys()) if isinstance(buses, dict) else []

    for alias, desc in buses.items():
        if alias not in BUS_ALIASES:
            errs.append(f"bus {alias}: only {list(BUS_ALIASES)} are supported (HS and MS, one adapter each)")
        if not isinstance(desc, dict):
            errs.append(f"bus {alias}: description must be an object")
            continue
        rate = desc.get("can_baud")
        if rate is not None and (not _is_int(rate) or rate <= 0):
            errs.append(f"bus {alias}: can_baud must be a positive integer")
        for k in ("label", "point"):
            if k in desc and not isinstance(desc[k], str):
                errs.append(f"bus {alias}: {k} must be a string")

    rules = doc.get("rules", {})
    if rules is not None and not isinstance(rules, dict):
        errs.append("rules must be an object")
        rules = {}
    if isinstance(rules, dict):
        for k, v in rules.items():
            if not _is_num(v):
                errs.append(f"rules.{k} must be a number")

    warnings = doc.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        errs.append("warnings must be a list of strings")

    known = doc.get("known_ids", {})
    if not isinstance(known, dict):
        errs.append("known_ids must be an object of car -> [{id, params}]")
        known = {}
    for car, entries in known.items():
        if car_keys and car not in car_keys:
            errs.append(f"known_ids: car {car!r} not in cars")
        if not isinstance(entries, list):
            errs.append(f"known_ids[{car}]: must be a list")
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("id"):
                errs.append(f"known_ids[{car}]: each entry needs an id")
                continue
            params = e.get("params", [])
            if not isinstance(params, list) or any(
                not isinstance(p, dict) for p in params
            ):
                errs.append(f"known_ids[{car}][{e.get('id')}]: params must be a list of objects")

    stages = doc.get("stages", [])
    if not isinstance(stages, list):
        errs.append("stages must be a list")
        return errs
    seen: set[str] = set()
    for st in stages:
        if not isinstance(st, dict):
            errs.append("stage must be an object")
            continue
        sid = st.get("id")
        if not isinstance(sid, str) or not sid:
            errs.append("stage: id must be a non-empty string")
            continue
        ck = st.get("checklist", [])
        if not isinstance(ck, list) or any(not isinstance(c, str) for c in ck):
            errs.append(f"stage {sid}: checklist must be a list of strings")
        tests = st.get("tests", [])
        if not isinstance(tests, list):
            errs.append(f"stage {sid}: tests must be a list")
            continue
        for t in tests:
            if not isinstance(t, dict):
                errs.append(f"stage {sid}: test must be an object")
                continue
            tid = t.get("id")
            if not isinstance(tid, str) or not tid:
                errs.append(f"stage {sid}: test id must be a non-empty string")
                continue
            if tid in seen:
                errs.append(f"duplicate test id {tid}")
            seen.add(tid)
            kind = t.get("kind", "action")
            if kind not in _KINDS:
                errs.append(f"test {tid}: invalid kind {kind!r} (expected one of {list(_KINDS)})")
            for key, keys, kind_ in (("car", car_keys, "cars"), ("bus", bus_keys, "buses")):
                vals = t.get(key)
                vals = [vals] if isinstance(vals, str) else vals
                if not isinstance(vals, list) or not vals:
                    errs.append(f"test {tid}: {key} must be a non-empty list")
                elif any(not isinstance(v, str) or v not in keys for v in vals):
                    bad = [v for v in vals if not isinstance(v, str) or v not in keys]
                    errs.append(f"test {tid}: {key} {bad} not in {kind_}")
            dur = t.get("duration", 10)
            if isinstance(dur, dict):
                m = dur.get("max", dur.get("min"))
                if not _is_num(m) or m <= 0:
                    errs.append(f"test {tid}: duration needs a positive numeric min/max")
                if "open" in dur and not isinstance(dur.get("open"), bool):
                    errs.append(f"test {tid}: duration.open must be a boolean")
            elif dur != "open-ended":
                if not _is_num(dur) or dur <= 0:
                    errs.append(f"test {tid}: duration must be a positive number, {{min, max}}, or 'open-ended'")
            for key_ in ("title", "do", "file", "warn", "note"):
                v = t.get(key_)
                if v is not None and not isinstance(v, str):
                    errs.append(f"test {tid}: {key_} must be a string")
            checks = t.get("checks", [])
            if not isinstance(checks, list):
                errs.append(f"test {tid}: checks must be a list")
            else:
                for c in checks:
                    if not isinstance(c, str) or not _is_hex_id(c):
                        errs.append(f"test {tid}: check {c!r} must be a hex id (e.g. '0x201')")
            ov = t.get("bus_override")
            if ov is not None:
                if not isinstance(ov, dict):
                    errs.append(f"test {tid}: bus_override must be an object")
                else:
                    for k, v in ov.items():
                        if k not in _BUS_OVERRIDE_KEYS:
                            errs.append(f"test {tid}: bus_override key {k!r} (expected {list(_BUS_OVERRIDE_KEYS)})")
                        elif k == "can_baud" and (not _is_int(v) or v <= 0):
                            errs.append(f"test {tid}: bus_override.can_baud must be a positive integer")
                        elif k in ("std_only", "variable") and not isinstance(v, bool):
                            errs.append(f"test {tid}: bus_override.{k} must be a boolean")
            txs = t.get("tx", [])
            if not isinstance(txs, list):
                errs.append(f"test {tid}: tx must be a list")
            else:
                for i, p in enumerate(txs):
                    if not isinstance(p, dict):
                        errs.append(f"test {tid}: tx[{i}] must be an object")
                        continue
                    if "bus" in p:
                        busv = p["bus"]
                        if not isinstance(busv, str) or busv not in bus_keys:
                            errs.append(f"test {tid}: tx[{i}].bus {busv!r} not in buses")
                    hz = p.get("hz", 10)
                    if not _is_num(hz) or hz <= 0:
                        errs.append(f"test {tid}: tx[{i}].hz must be a positive number")
                    ph = p.get("phases")
                    if ph is not None and (not isinstance(ph, list) or any(x not in _PHASES for x in ph)):
                        errs.append(f"test {tid}: tx[{i}].phases must be a subset of {list(_PHASES)}")
                    frames = p.get("frames", [])
                    if not isinstance(frames, list):
                        errs.append(f"test {tid}: tx[{i}].frames must be a list")
                    else:
                        for j, fr in enumerate(frames):
                            if not isinstance(fr, dict):
                                errs.append(f"test {tid}: tx[{i}].frames[{j}] must be an object")
                                continue
                            fid = fr.get("id")
                            if not _is_id(fid):
                                errs.append(f"test {tid}: tx[{i}].frames[{j}].id must be an integer or '0x..' string")
                            data = fr.get("data")
                            if data is not None:
                                if isinstance(data, str):
                                    d = data.replace(" ", "")
                                    if not d or len(d) > 16 or not _HEX.match(d):
                                        errs.append(f"test {tid}: tx[{i}].frames[{j}].data must be hex, max 8 bytes")
                                elif isinstance(data, list):
                                    if len(data) > 8 or any(not _is_int(b) or not 0 <= b <= 255 for b in data):
                                        errs.append(f"test {tid}: tx[{i}].frames[{j}].data must be <=8 bytes 0..255")
                                else:
                                    errs.append(f"test {tid}: tx[{i}].frames[{j}].data must be hex string or byte list")
    if car_keys and (missing := [k for k, v in cars.items() if not (isinstance(v, dict) and v.get("label"))]):
        errs.append(f"cars: missing label for {missing}")

    # Authoritative round-trip through the canductor loader, when importable.
    try:
        import tempfile as _tf
        from canductor.worker.scenario import ScenarioError, load_scenario
    except ImportError:
        return errs
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(doc, tf, ensure_ascii=False)
        tmp = tf.name
    try:
        load_scenario(tmp)
    except (ScenarioError, ValueError, OSError) as e:
        errs.append(f"canductor loader rejected the scenario: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return errs


def _is_id(v: object) -> bool:
    return _is_int(v) or (isinstance(v, str) and (
        _HEX.match(v) or (v.lower().startswith("0x") and _HEX.match(v[2:]))))


def _is_hex_id(v: str) -> bool:
    return bool(_HEX.match(v)) or (v.lower().startswith("0x") and bool(_HEX.match(v[2:])))


# --- HTTP service (stdlib asyncio) ------------------------------------------


class EditorService:
    """HTTP plumbing: static UI + JSON api over the ScenarioStore."""

    def __init__(self, store: ScenarioStore, host: str = "127.0.0.1", port: int = 8120) -> None:
        self.store = store
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._started_at = time.time()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        try:
            self.port = self._server.sockets[0].getsockname()[1]
        except (AttributeError, IndexError):
            pass

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _json_body(self, obj: dict[str, Any], code: int = 200) -> bytes:
        data = json.dumps(obj, ensure_ascii=False).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  409: "Conflict", 500: "Internal Server Error"}.get(code, "Error")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode() + data

    def _html_body(self) -> bytes:
        idx = Path(__file__).parent / "web" / "index.html"
        if idx.is_file():
            raw = idx.read_bytes()
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(raw)}\r\n"
                "Connection: close\r\n\r\n"
            )
            return head.encode() + raw
        return self._json_body({"error": "web/index.html missing"}, 404)

    async def _read_request(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(8192)
            if not chunk:
                return None
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ", 2)
        except ValueError:
            return None
        headers: dict[str, str] = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        try:
            n = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return None
        if n > 16 * 1024 * 1024:  # sane cap for the JSON API (localhost)
            return None
        body = rest
        while len(body) < n:
            chunk = await reader.read(n - len(body))
            if not chunk:
                return None
            body += chunk
        return {"method": method.upper(), "path": path, "body": body}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_request(reader)
            if req is None:
                return
            p = urlparse.unquote(req["path"].split("?")[0]).rstrip("/") or "/"
            qs = req["path"].split("?", 1)
            query = {k: v[0] for k, v in urlparse.parse_qs(qs[1]).items()} if len(qs) > 1 else {}
            method = req["method"]
            body: Any = {}
            if req["body"]:
                try:
                    body = json.loads(req["body"].decode() or "{}")
                except ValueError:
                    writer.write(self._json_body({"error": "bad json"}, 400))
                    return
            if not isinstance(body, dict):
                body = {}
            if p == "/" and method == "GET":
                writer.write(self._html_body())
            elif p == "/health" and method == "GET":
                writer.write(self._json_body({
                    "ok": True, "version": __version__,
                    "dir": str(self.store.root),
                    "scenarios": self.store.names(),
                    "uptime": round(time.time() - self._started_at, 1),
                }))
            elif p == "/api/scenarios" and method == "GET":
                writer.write(self._json_body({"dir": str(self.store.root), "names": self.store.names()}))
            elif p == "/api/scenario" and method == "GET":
                name = query.get("name", "")
                writer.write(self._json_body({
                    "name": name, "doc": self.store.get(name), "mtime": self.store.mtime(name),
                }))
            elif p == "/api/validate" and method == "POST":
                doc = body.get("doc")
                writer.write(self._json_body({"errors": validate(doc if isinstance(doc, dict) else {}, body.get("name", ""))}))
            elif p == "/api/scenario" and method == "POST":
                name = body.get("name", "")
                doc = body.get("doc")
                try:
                    nbytes, _ = self.store.save(name, doc if isinstance(doc, dict) else {},
                                                overwrite=bool(body.get("overwrite", True)))
                except EditorError as e:
                    writer.write(self._json_body({"error": str(e)}, 409 if "already exists" in str(e) else 400))
                else:
                    writer.write(self._json_body({"ok": True, "name": name, "bytes": nbytes}))
            elif p == "/api/scenario/new" and method == "POST":
                name = body.get("name", "")
                doc = body.get("doc")
                try:
                    self.store.create(name, doc if isinstance(doc, dict) else None, force=bool(body.get("force", False)))
                except EditorError as e:
                    writer.write(self._json_body({"error": str(e)}, 400))
                else:
                    writer.write(self._json_body({"ok": True, "name": name}))
            elif p == "/api/scenario/duplicate" and method == "POST":
                try:
                    self.store.duplicate(body.get("name", ""), body.get("from", ""))
                except EditorError as e:
                    writer.write(self._json_body({"error": str(e)}, 400))
                else:
                    writer.write(self._json_body({"ok": True}))
            elif p == "/api/scenario/delete" and method == "POST":
                try:
                    self.store.delete(body.get("name", ""))
                except EditorError as e:
                    writer.write(self._json_body({"error": str(e)}, 400))
                else:
                    writer.write(self._json_body({"ok": True}))
            else:
                writer.write(self._json_body({"error": f"no endpoint {method} {p}"}, 404))
        except (ConnectionResetError, BrokenPipeError):
            pass
        except EditorError as e:
            try:
                writer.write(self._json_body({"error": str(e)}, 404))
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            import traceback
            try:
                writer.write(self._json_body({"error": f"{type(e).__name__}: {e}",
                                             "traceback": traceback.format_exc()}, 500))
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def default_scenarios_dir() -> Path:
    """SCENARIOS_DIR env, else <repo>/canductor/scenarios next to the editor."""
    env = os.environ.get("SCENARIOS_DIR")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent          # .../canductor_editor/canductor_editor
    sibling = here.parent.parent / "canductor" / "scenarios"
    if sibling.is_dir():
        return sibling
    cwd = Path.cwd() / "canductor" / "scenarios"
    if cwd.is_dir():
        return cwd
    return Path("scenarios")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="canductor-editor",
                                 description="web editor for canductor test scenarios")
    ap.add_argument("--version", action="version", version=f"canductor-editor {__version__}")
    ap.add_argument("--scenarios", type=Path, default=None, help="scenarios directory (default: autodetected)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8120)
    args = ap.parse_args(argv)

    root = args.scenarios if args.scenarios is not None else default_scenarios_dir()
    root.mkdir(parents=True, exist_ok=True)
    svc = EditorService(ScenarioStore(root), host=args.host, port=args.port)

    async def _run() -> None:
        await svc.start()
        print(f"CANDuctor scenario editor: {svc.base_url}")
        print(f"scenarios dir: {svc.store.root}  ({len(svc.store.names())} scenario(s))")
        try:
            await asyncio.sleep(3600 * 24 * 365)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await svc.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
