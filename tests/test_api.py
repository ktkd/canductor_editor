"""Unit tests: scenario validation, store CRUD, and the HTTP API round-trip."""
import asyncio
import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canductor_editor import server as S


def _doc():
    return {
        "name": "t",
        "title": "T",
        "version": "1.0",
        "cars": {"rx8": {"label": "Mazda RX-8"}},
        "buses": {"hs": {"label": "HS", "can_baud": 500000, "std_only": True}},
        "rules": {"quiet_s": 5, "after_s": 5, "repeats": 3, "pause_s": 10,
                  "action_min_s": 5, "action_max_s": 15, "state_max_s": 120},
        "known_ids": {"rx8": [{"id": "0x201", "params": [{"name": "RPM", "bytes": "0-1", "formula": "UINT16*0.25"}]}]},
        "warnings": [],
        "stages": [
            {"id": "1", "title": "s1", "warn": "", "checklist": ["item"],
             "tests": [
                 {"id": "1.1.1", "kind": "state", "title": "t1", "do": "",
                  "car": ["rx8"], "bus": ["hs"], "duration": 30, "file": "a",
                  "checks": ["0x201"],
                  "tx": [{"bus": "hs", "hz": 10, "frames": [{"id": "0x123", "data": "01 02"}]}]},
                 {"id": "1.1.2", "kind": "action", "title": "t2",
                  "car": ["rx8"], "bus": ["hs"], "duration": {"min": 5, "max": 20},
                  "file": "b", "checks": [],
                  "bus_override": {"can_baud": 125000, "std_only": True, "variable": True}},
             ]},
        ],
    }


def test_template_valid():
    assert S.validate(S.new_scenario("x")) == []


def test_valid_doc():
    errs = S.validate(_doc())
    assert errs == [], errs


def test_validation_catches():
    d = _doc()
    d["stages"][0]["tests"].append(
        {"id": "1.1.1", "car": ["rx8"], "bus": ["nope"], "kind": "weird",
         "duration": -5, "checks": ["nothex"], "title": "dup"})
    errs = S.validate(d)
    joined = " | ".join(errs)
    assert "duplicate test id 1.1.1" in joined
    assert "not in buses" in joined
    assert "invalid kind" in joined
    assert "duration" in joined
    assert "check 'nothex'" in joined

    d2 = _doc()
    d2["buses"]["hs"]["can_baud"] = 0
    assert any("can_baud" in e for e in S.validate(d2))

    d3 = _doc()
    d3["stages"][0]["tests"][0]["tx"][0]["frames"].append({"id": "0x1", "data": "010203040506070809"})
    assert any("max 8 bytes" in e for e in S.validate(d3))

    d4 = _doc()
    d4["buses"]["lv"] = {"label": "LV", "can_baud": 100_000, "std_only": True}
    assert any("bus lv" in e and "supported" in e for e in S.validate(d4))


def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = S.ScenarioStore(os.path.join(td))
        nd = S.new_scenario("s1")
        store.create("s1", nd)
        assert store.names() == ["s1"]
        got = store.get("s1")
        assert got["name"] == "s1"
        nbytes, _ = store.save("s1", got)
        assert nbytes > 0
        store.duplicate("s2", "s1")
        assert store.names() == ["s1", "s2"]
        store.delete("s2")
        assert store.names() == ["s1"]
        try:
            store.save("s1", {"name": "s1", "stages": [{"id": "1", "tests": [{"id": "1.1", "car": [], "bus": []}]}]})
            raise AssertionError("expected validation error")
        except S.EditorError:
            pass


def test_http_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        svc = S.EditorService(S.ScenarioStore(os.path.join(td)), host="127.0.0.1", port=0)

        def _get(u: str) -> bytes:
            return urllib.request.urlopen(u, timeout=5).read()

        def _post(u: str, body: dict) -> bytes:
            req = urllib.request.Request(u, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            return urllib.request.urlopen(req, timeout=5).read()

        async def _run():
            loop = asyncio.get_running_loop()
            await svc.start()
            base = svc.base_url
            try:
                # health
                j = json.loads(await loop.run_in_executor(None, _get, base + "/health"))
                assert j["ok"] and j["scenarios"] == []
                # create + save + load
                j = json.loads(await loop.run_in_executor(None, _post, base + "/api/scenario", {"name": "demo", "doc": _doc()}))
                assert j["ok"], j
                raw = await loop.run_in_executor(None, _get, base + "/api/scenario?name=demo")
                loaded = json.loads(raw)
                assert loaded["doc"]["stages"][0]["tests"][0]["id"] == "1.1.1"
                # invalid save rejected (car/bus must be non-empty)
                try:
                    await loop.run_in_executor(None, _post, base + "/api/scenario",
                                                {"name": "demo",
                                                 "doc": {"name": "demo", "stages": [{"id": "1", "tests": [{"id": "1.1", "car": [], "bus": []}]}]}})
                    raise AssertionError("expected 400 for invalid doc")
                except AssertionError:
                    raise
                except urllib.error.HTTPError as e:
                    assert e.code == 400
                # delete
                j = json.loads(await loop.run_in_executor(None, _post, base + "/api/scenario/delete", {"name": "demo"}))
                assert j["ok"]
                assert svc.store.names() == []
                # ui page
                html = (await loop.run_in_executor(None, _get, base + "/")).decode()
                assert "scenario editor" in html
            finally:
                await svc.stop()

        asyncio.run(_run())


if __name__ == "__main__":
    failed = 0
    for name in dir():
        if name.startswith("test_") and callable(globals()[name]):
            try:
                globals()[name]()
                print(f"  ok  {name}")
            except Exception as e:
                failed += 1
                import traceback
                print(f"  FAIL {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)