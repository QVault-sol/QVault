"""End-to-end browser test of the web app against real devnet.

Uses the demo-wallet path with a funded keypair (env DEVNET_KEYPAIR, JSON byte array).
Serves web/public locally, then:
  1. manual path: create vault → deposit SOL → mint test tokens → deposit tokens → send SOL → send tokens
  2. judge path: fresh browser, one click on "Run the full demo"
Screenshots go to web/test/screens/. Progress and failures are reported as GitHub annotations.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PUBLIC = HERE.parent / "public"
SHOTS = HERE / "screens"
SHOTS.mkdir(exist_ok=True)
KEY = json.loads(os.environ["DEVNET_KEYPAIR"])
INIT = f"localStorage.setItem('qvault.burner', {json.dumps(json.dumps(KEY))})"

srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "-d", str(PUBLIC)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)


def fail(msg):
    print("::error::" + msg.replace("\n", " "))
    raise SystemExit(1)


def settle(pg, what, timeout=300_000):
    pg.wait_for_function("!document.body.classList.contains('busy')", timeout=timeout)
    err = pg.locator("#error:not([hidden])")
    if err.count():
        fail(f"{what}: {err.inner_text()}")
    if pg.locator(".step.err").count():
        fail(f"{what}: {pg.locator('.step.err').first.inner_text()}")
    print(f"::notice::✔ {what}")


def chip(pg, box, label):
    for _ in range(30):  # token accounts show up on public RPC with a short delay
        loc = pg.locator(f"#{box} label.chip", has_text=label)
        if loc.count():
            loc.first.click()
            return
        pg.wait_for_timeout(2000)
    fail(f"no {label!r} chip in #{box}")


def phases_ok(pg, what):
    states = pg.eval_on_selector_all(".phases li", "ls => ls.map(l => l.className)")
    if states != ["ok"] * 4:
        fail(f"{what}: withdrawal phases {states}")


try:
  try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.on("pageerror", lambda e: print("pageerror:", e))
        pg.add_init_script(INIT)
        pg.goto("http://localhost:8765/")
        pg.screenshot(path=SHOTS / "1-landing.png", full_page=True)

        pg.click("#demo-btn")
        pg.wait_for_selector("#app:not([hidden])")
        settle(pg, "wallet connected")

        pg.click("#create-btn")
        settle(pg, "vault created")

        chip(pg, "dep-assets", "SOL")
        pg.fill("#dep-amount", "0.05")
        pg.click("#dep-form button[type=submit]")
        settle(pg, "deposited 0.05 SOL")

        pg.click("#mint-btn")
        settle(pg, "minted test tokens")

        chip(pg, "dep-assets", "qUSD")
        pg.fill("#dep-amount", "10")
        pg.click("#dep-form button[type=submit]")
        settle(pg, "deposited 10 qUSD")
        pg.screenshot(path=SHOTS / "2-funded-vault.png", full_page=True)

        pg.click("#tab-send")
        chip(pg, "send-assets", "SOL")
        pg.click("#to-self")
        pg.fill("#send-amount", "0.01")
        pg.click("#send-form button[type=submit]")
        settle(pg, "sent 0.01 SOL quantum-safe")
        phases_ok(pg, "SOL withdrawal")

        chip(pg, "send-assets", "qUSD")
        pg.click("#to-self")
        pg.fill("#send-amount", "2")
        pg.click("#send-form button[type=submit]")
        settle(pg, "sent 2 qUSD quantum-safe")
        phases_ok(pg, "token withdrawal")
        pg.wait_for_timeout(1500)
        pg.screenshot(path=SHOTS / "3-after-sends.png", full_page=True)

        links = pg.eval_on_selector_all(".step a", "as => as.map(a => a.closest('li').innerText.split('\\n')[0] + ' | ' + a.href)")

        # The judge path: a fresh browser, one click on "Run the full demo".
        ctx2 = b.new_context(viewport={"width": 1280, "height": 900})
        pg2 = ctx2.new_page()
        pg2.on("pageerror", lambda e: print("pageerror:", e))
        pg2.add_init_script(INIT)
        pg2.goto("http://localhost:8765/")
        pg2.click("#demo-btn")
        pg2.wait_for_selector("#app:not([hidden])")
        settle(pg2, "guided demo: wallet connected")
        pg2.click("#tab-demo")
        pg2.click("#demo-run")
        settle(pg2, "guided demo: SOL + qUSD in and out", timeout=600_000)
        if "Guided demo complete" not in pg2.inner_text("#log"):
            fail("guided demo did not report completion")
        done = pg2.eval_on_selector_all("#demo-steps li", "ls => ls.map(l => l.className)")
        if done != ["ok"] * 6:
            fail(f"guided demo checklist {done}")
        pg2.screenshot(path=SHOTS / "4-guided-demo.png", full_page=True)
        ctx2.close()

        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write("### Web app on devnet (browser test)\n")
                for l in reversed(links):
                    text, href = l.rsplit(" | ", 1)
                    f.write(f"- [{text.strip()}]({href})\n")
        b.close()
  except SystemExit:
    raise
  except Exception as e:  # surface unexpected errors as annotations
    fail(f"{type(e).__name__}: {e}"[:900])
finally:
    srv.terminate()
