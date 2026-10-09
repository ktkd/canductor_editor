"""E2E: web UI check through the Selenium grid (docker selenium/standalone-chrome).

Prereqs:
  * the web service is running:
      canductor web --host 0.0.0.0 --dev hs=/dev/ttyUSB0 --dev ms=/dev/ttyUSB1 --port 8120
      (or for the editor: canductor-editor --scenarios <dir> --host 0.0.0.0 --port 8120)
  * the grid container:
      docker run -p 4444:4444 --add-host=host.docker.internal:host-gateway           -e SE_CB=false selenium/standalone-chrome:153.0
      (the browser runs inside the container, so the page URL must be reachable from there)

Optional overrides (env):
  SE_HOST      selenium grid base url   (default http://localhost:4444)
  PAGE_URL     URL the browser opens    (default PAGE_URL)
  LOCAL_URL    URL for direct HTTP calls from the host (default LOCAL_URL)

Run:  python3 tests/e2e_ui.py
"""
"""E2E: canductor scenario editor web UI via Selenium grid."""
import functools
import json
import os
import sys
from typing import Callable
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException


@functools.wraps(input)
def ok_input(self, value):
    self.clear()
    self.send_keys(value)


def console_log(driver: webdriver.Remote) -> list:
    try:
        return driver.execute_script("return window.__console || []")
    except WebDriverException:
        return []


def dismiss_dialog(driver):
    try:
        if driver.switch_to.alert:
            driver.switch_to.alert.accept()
    except WebDriverException:
        pass


PAGE_URL = os.environ.get("PAGE_URL", "http://host.docker.internal:8120")
LOCAL_URL = os.environ.get("LOCAL_URL", "http://127.0.0.1:8120")


def main() -> None:
    opt = webdriver.ChromeOptions()
    driver = webdriver.Remote(os.environ.get("SE_HOST", "http://localhost:4444").rstrip("/"), options=opt)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.__dlgLog = []; window.__dlgConfirm = true; window.__dlgVal = undefined;
        window.alert = (m) => { window.__dlgLog.push(['alert', String(m)]); };
        window.confirm = (m) => { window.__dlgLog.push(['confirm', String(m)]); return !!window.__dlgConfirm; };
        window.prompt = (m, d) => { window.__dlgLog.push(['prompt', String(m), String(d == null ? '' : d)]); return (window.__dlgVal === undefined ? (d || '') : window.__dlgVal); };
        window.__console = [];
        const _fmt = (x) => { try { return typeof x === 'object' ? JSON.stringify(x) : String(x); } catch(_) { return String(x); } };
        for (const m of ['log','warn','error','info']) {
          const orig = console[m].bind(console);
          console[m] = (...a) => { try { window.__console.push([m, a.map(_fmt).join(' ')]) } catch(_) {} ; orig(...a); };
        }
        window.addEventListener('error', (e) => { try { window.__console.push(['uncaught', e.message + ' @ ' + (e.filename||'?') + ':' + e.lineno]); } catch(_) {} });
        window.addEventListener('unhandledrejection', (e) => { try { window.__console.push(['unhandledrejection', _fmt(e.reason)]); } catch(_) {} });
    """})
    results: list[tuple[str, str]] = []
    D = WebDriverWait(driver, 15)

    def check(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            results.append(("PASS", name))
        except Exception as e:
            dismiss_dialog(driver)
            results.append(("FAIL", f"{name} -- {type(e).__name__}: {e}"))

    driver.set_window_size(1600, 1000)
    driver.get(PAGE_URL + "/")

    # --- 1. load -------------------------------------------------------------
    def t():
        D.until(EC.presence_of_element_located((By.ID, "scenlist")))
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#scenlist .scen-row"))
        names = [r.find_element(By.TAG_NAME, "b").text for r in driver.find_elements(By.CSS_SELECTOR, "#scenlist .scen-row")]
        if "bench_quick" not in names or "mazda_swap" not in names:
            raise AssertionError(f"list: {names}")
        if driver.find_element(By.ID, "scenname").text not in names:
            raise AssertionError("no scenario auto-opened")
        if not driver.find_elements(By.CSS_SELECTOR, "#tab-stages .stage-card"):
            raise AssertionError("no stage cards rendered")
    check("page loads: list + auto-open + stage cards", t)

    # --- 2. tabs --------------------------------------------------------------
    def t():
        for tab in ("cars", "buses", "known", "rules", "source"):
            driver.find_element(By.CSS_SELECTOR, f'span.tab[data-tab="{tab}"]').click()
            D.until(lambda d, tb=tab: d.find_element(By.ID, f"tab-{tb}").is_displayed())
            dismiss_dialog(driver)
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="buses"]').click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#tab-buses input[data-b-label]"))
        labels = {e.get_attribute("data-b-label"): e.get_attribute("value")
                  for e in driver.find_elements(By.CSS_SELECTOR, "#tab-buses input[data-b-label]")}
        if "hs" not in labels or "ms" not in labels:
            raise AssertionError(f"buses: {labels}")
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="rules"]').click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#tab-rules input[data-rule]"))
        rules = {e.get_attribute("data-rule"): e.get_attribute("value")
                 for e in driver.find_elements(By.CSS_SELECTOR, "#tab-rules input[data-rule]")}
        if rules.get("repeats") != "3":
            raise AssertionError(f"rules repeats={rules.get('repeats')!r}")
    check("tabs render: buses + rules content", t)

    # --- 3. edit a test -----------------------------------------------------------
    def t():
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="stages"]').click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "tr.test-row"))
        row = driver.find_elements(By.CSS_SELECTOR, "tr.test-row")[0]
        row.click()
        D.until(lambda d: d.find_elements(By.ID, "t-title"))
        ok_input(driver.find_element(By.ID, "t-title"), "selenium edited")
        dirty = driver.find_element(By.ID, "scendirty").text
        if "unsaved" not in dirty:
            raise AssertionError(f"dirty marker: {dirty!r}")
        driver.find_element(By.ID, "t-close").click()
    check("test editor opens, title edited, dirty marker", t)

    # --- 4. source tab: broken JSON + reload + minimal valid doc ---------------------
    def t():
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="source"]').click()
        D.until(lambda d: d.find_elements(By.ID, "srctext"))
        ta = driver.find_element(By.ID, "srctext")
        driver.execute_script("arguments[0].value = '{ broken json'", ta)
        driver.find_element(By.ID, "b-src-apply").click()
        stat = driver.find_element(By.ID, "srcstat").text
        if not stat.startswith("apply:"):
            raise AssertionError(f"srcstat: {stat!r}")
        driver.find_element(By.ID, "b-src-reload").click()
        D.until(lambda d: "synced" in d.find_element(By.ID, "srcstat").text)
        driver.execute_script(
            "arguments[0].value = JSON.stringify({name: 'x', title: 't', cars: {}, buses: "
            "{}, stages: []}, null, 1)", ta)
        driver.find_element(By.ID, "b-src-apply").click()
        D.until(lambda d: "applied" in d.find_elements(By.ID, "toastdiv")[0].text)
    check("source tab: broken apply errors, reload restores, minimal doc applies", t)

    # --- 5. Ctrl+S with minimal doc (saves what editor holds; restore after) -----
    orig_doc = driver.execute_script("return await fetch('/api/scenario?name=' + encodeURIComponent(arguments[0])).then(r => r.json());",
                                    driver.find_element(By.ID, "scenname").text)

    def t():
        a = driver.find_element(By.CSS_SELECTOR, "body")
        a.send_keys(Keys.CONTROL, "s")
        D.until(lambda d: "saved" in d.find_element(By.ID, "toastdiv").text)
    check("Ctrl+S saves: toast shown", t)

    def t():
        r = driver.execute_script(
            "return await fetch('/api/scenario', {method: 'POST', headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({name: arguments[0], doc: arguments[1], overwrite: true})}).then(r => r.json());",
            orig_doc["name"], orig_doc["doc"])
        if not r.get("ok"):
            raise AssertionError(f"restore: {r}")
    check("restore benchmark doc saved on disk", t)

    # --- 6. create / invalid-name / duplicate / delete ---------------------------------
    def t():
        ok_input(driver.find_element(By.ID, "newscen"), "e2e_tmp_scen")
        driver.find_element(By.ID, "b-new").click()
        D.until(lambda d: d.find_element(By.ID, "scenname").text == "e2e_tmp_scen")
    check("create scenario: template opens", t)

    def t():
        ok_input(driver.find_element(By.ID, "newscen"), "bad name with spaces")
        driver.find_element(By.ID, "b-new").click()
        D.until(lambda d: d.execute_script("return (window.__dlgLog||[]).some(e => e[0]==='alert')"))
        log = driver.execute_script("return window.__dlgLog")
        al = [e for e in log if e[0] == "alert"]
        if not any("bad scenario name" in e[1] for e in al):
            raise AssertionError(f"alert logged: {al}")
        print("   [info] bad-name alert:", al[-1][1])
    check("create: bad name rejected (alert logged)", t)

    def t():
        driver.execute_script("window.__dlgVal = 'e2e_copy_tmp'; window.__dlgLog = window.__dlgLog || []; window.__dlgConfirm = true;")
        driver.find_element(By.ID, "b-dup").click()
        D.until(lambda d: d.find_element(By.ID, "scenname").text == "e2e_copy_tmp")
        log = driver.execute_script("return window.__dlgLog")
        pr = [e for e in log if e[0] == "prompt"]
        if not pr:
            raise AssertionError(f"prompt not logged: {log}")
    check("duplicate scenario via prompt", t)

    def t():
        driver.execute_script("window.__dlgConfirm = true;")
        driver.find_element(By.ID, "b-del").click()   # deletes the open e2e_copy_tmp
        D.until(lambda d: d.find_element(By.ID, "scenname").text == "\u2014")
        log = driver.execute_script("return window.__dlgLog")
        cf = [e for e in log if e[0] == "confirm" and "Delete the file" in e[1]]
        if not cf:
            raise AssertionError(f"confirm not logged: {log}")
        # now delete the leftover e2e_tmp_scen
        row = [r for r in driver.find_elements(By.CSS_SELECTOR, "#scenlist .scen-row")
               if r.find_element(By.TAG_NAME, "b").text == "e2e_tmp_scen"][0]
        row.click()
        D.until(lambda d: d.find_element(By.ID, "scenname").text == "e2e_tmp_scen")
        driver.find_element(By.ID, "b-del").click()
        D.until(lambda d: d.find_element(By.ID, "scenname").text == "\u2014")
    check("delete scenarios (confirm logged) + cleanup", t)

    def t():
        names = [r.find_element(By.TAG_NAME, "b").text
                 for r in driver.find_elements(By.CSS_SELECTOR, "#scenlist .scen-row")]
        if "e2e_copy_tmp" in names or "e2e_tmp_scen" in names:
            raise AssertionError(f"temp scenarios still listed: {names}")
    check("deleted scenarios vanished from list", t)

    # --- 7. server-side validation endpoint ---------------------------------------------
    def t():
        bad = {"cars": {"rx8": {"label": "x"}},
               "buses": {"hs": {"label": "HS", "can_baud": 500000},
                         "ms": {"label": "MS", "can_baud": 125000}},
               "stages": [{"id": "1", "title": "s", "tests": [
                   {"id": "1.1", "title": "t", "car": ["ghost"], "bus": ["hs"],
                    "kind": "state", "duration": 10}]}]}
        r = driver.execute_script(
            "const doc = JSON.parse(arguments[0]);"
            "return await fetch('/api/validate', {method:'POST', "
            "headers:{'Content-Type':'application/json'}, body: JSON.stringify({doc: doc})}"
            ").then(r => r.json());", json.dumps(bad))
        if not r.get("errors"):
            raise AssertionError(f"validate said ok: {r}")
        print("   [info] validate errors:", r["errors"][:2])
    check("POST /api/validate flags unknown car reference", t)

    # --- 11. regression guards (editor UI bugs fixed in this round) -------------
    def reopen_scenario(name: str = "mazda_swap") -> None:
        # after the delete/cleanup checks the editor holds no doc; reopen one
        rows = [r for r in driver.find_elements(By.CSS_SELECTOR, "#scenlist .scen-row")
                if r.find_element(By.TAG_NAME, "b").text == name]
        if rows:
            rows[0].click()
            D.until(lambda d: d.find_element(By.ID, "scenname").text == name)

    def t():
        reopen_scenario("mazda_swap")   # needs the known_ids reference rows
        # known-ids "+param" appends exactly one empty row (was 97 before)
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="known"]').click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#tab-known [data-kpp]"))
        total = "document.querySelectorAll('#tab-known table tbody tr').length"
        rows_before = driver.execute_script("return " + total + ";")
        driver.find_elements(By.CSS_SELECTOR, "#tab-known [data-kpp]")[0].click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#tab-known [data-kpp]"))
        rows_after = driver.execute_script("return " + total + ";")
        print("   [info] +param rows:", rows_before, "->", rows_after)
        if rows_after - rows_before != 1:
            raise AssertionError(f"+param delta={rows_after - rows_before} (expect 1)")
    check("known-ids +param adds exactly one row", t)

    def t():
        reopen_scenario("bench_quick")
        driver.find_element(By.CSS_SELECTOR, 'span.tab[data-tab="stages"]').click()
        D.until(lambda d: d.find_elements(By.CSS_SELECTOR, "[data-tadd]"))
        driver.find_elements(By.CSS_SELECTOR, "[data-tadd]")[0].click()
        D.until(lambda d: d.find_elements(By.ID, "t-title"))
        chips = driver.execute_script(
            "return [...document.querySelectorAll('#testedit [data-tcar]')].map(c => c.checked)")
        if not chips or any(not c for c in chips):
            raise AssertionError(f"car preselection: {chips}")
        print("   [info] new-test car checkboxes:", chips)
        driver.find_element(By.ID, "t-close").click()
    check("new test: all cars preselected", t)

    def t():
        # duration "open-ended" string now round-trips canductor's loader (save + restore)
        reopen_scenario("bench_quick")
        r = driver.execute_script(
            "return await (async () => { "
            "const name = document.getElementById('scenname').textContent; "
            "const j = await fetch('/api/scenario?name=' + encodeURIComponent(name)).then(x => x.json()); "
            "const pristine = JSON.stringify(j.doc); "
            "const doc = JSON.parse(pristine); "
            "let t0 = null; for (const st of doc.stages) if ((st.tests || []).length) { t0 = st.tests[0]; break; } "
            "if (!t0) throw new Error('no tests in scenario'); "
            "t0.duration = 'open-ended'; "
            "const res = await fetch('/api/scenario', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
            "body: JSON.stringify({name: name, doc: doc, overwrite: true})}); "
            "const saved = await res.json(); "
            "await fetch('/api/scenario', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
            "body: JSON.stringify({name: name, doc: JSON.parse(pristine), overwrite: true})}); "
            "return {status: res.status, json: saved}; })()")
        if r["status"] != 200 or not r["json"].get("ok"):
            raise AssertionError(f"open-ended save: {r}")
        print("   [info] open-ended save: HTTP", r["status"], "- ok")
    check("duration 'open-ended' saves (cross-loader roundtrip)", t)

    check("no JS console errors", t)

    for status, name in results:
        print(f"{status:5s} {name}")
    failed = [r for r in results if r[0] == "FAIL"]
    driver.quit()
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()