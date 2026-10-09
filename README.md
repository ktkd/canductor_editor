# canductor-editor

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/deps-none-orange)


A web editor for [canductor](https://github.com/ktkd/canductor) test scenarios:
build and maintain scenario documents (`canductor/scenarios/*.json`) in the
browser instead of hand-editing JSON — the same files the canductor worker
consumes via `canductor run -s NAME`.

The UI follows the canductor web UI visual language (same palette, panels,
badges, compact tables). Stdlib-only, Python ≥ 3.10, no external dependencies.

## What you can do

* **Scenarios** — list the files in the scenarios directory, create a new one
  from the template, duplicate or delete an existing one.
* **Scenario** — title, version, warnings; the file name is the scenario id.
* **Stages & tests** — the full sequence editor:
  * stages with id, title, warning, checklist items;
  * per test: id, title, action (`do`), type (`state` / `action` / `setup`),
    car(s) and bus(es) (from the scenario definitions), duration
    (fixed seconds / min–max / open-ended), log file base, warning, note,
    expected IDs (`checks`), variable encoding, per-test bus override
    (can_baud / std_only / variable);
  * **TX patterns** (bench ECU injection): bus, hz, phases (quiet/action/after),
    frames (id + hex data) — add/remove in place.
* **Cars / Buses / Known IDs** — the vehicle table, the fixed pair of buses
  (HS 500 kbit/s and MS 125 kbit/s only — one adapter each, no other buses),
  and the per-car signal reference (id, byte offsets, decoding formulas).
* **Session rules** — quiet/after/pause/repeats/action bounds in one place.
* **Source** — live JSON view of the document: format, apply the text back to
  the editor, reload from disk. Saving goes through the server-side validator
  (schema checks plus a round-trip through canductor's own scenario loader),
  so anything saved is guaranteed to load in `canductor run`.

## Run

Requirements: Linux, Python ≥ 3.10. The editor edits the scenarios directory
of a canductor checkout (auto-detected as the sibling `canductor/scenarios`),
or any explicit directory.

```bash
# from a canductor checkout or from the canductor_editor directory:
./bin/editor                     # or: python3 -m canductor_editor
# or:
pip install .                    # provides the `canductor-editor` script
canductor-editor --port 8120
```

Options: `--scenarios DIR` (or env `SCENARIOS_DIR`), `--host`, `--port`
(default 8120). Then open the printed URL in a browser; the first scenario
file is opened automatically (`?scenario=NAME` to pick another).

## Tests

```bash
python3 tests/test_api.py        # validation, store CRUD, HTTP round-trip
```

## Layout

```
canductor_editor/
  bin/editor                    # bash wrapper
  canductor_editor/
    server.py                   # store + validator + asyncio HTTP + CLI
    web/index.html              # the single-page editor (same style as canductor UI)
  tests/test_api.py
```

## Contact

* Website: <https://ktkd.ru>
* E-mail: <ktk@ktkd.ru>
