"""End-to-end browser test of the web app against real devnet.

Uses the demo-wallet path with a funded keypair (env DEVNET_KEYPAIR, JSON byte array).
Serves web/public locally, then: create vault → deposit SOL → mint test tokens →
deposit tokens → send SOL → send tokens. Screenshots go to web/test/screens/.
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
RECIPIENT = "Vote111111111111111111111111111111111111111"  # any existing account works as a demo recipient

srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "-d", str(PUBLIC)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)


def fail(msg):
    print("::error::" + msg.replace("\n", " "))
    raise SystemExit(1)


def settle(pg, what, timeout=180_000):
    pg.wait_for_function("!document.body.classList.contains('busy')", timeout=timeout)
    err = pg.locator("#error:not([hidden])")
    if err.count():
        fail(f"{what}: {err.inner_text()}")
    if pg.locator(".step.err").count():
        fail(f"{what}: {pg.locator('.step.err').first.inner_text()}")
    print(f"::notice::✔ {what}")


def select_containing(pg, sel, text):
    value = pg.eval_on_selector(sel, "(s, t) => [...s.options].find(o => o.text.includes(t))?.value", text)
    if not value:
        fail(f"no option containing {text!r} in {sel}")
    pg.select_option(sel, value)


try:
  try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        pg.on("pageerror", lambda e: print("pageerror:", e))
        pg.add_init_script(f"localStorage.setItem('qvault.burner', {json.dumps(json.dumps(KEY))})")
        pg.goto("http://localhost:8765/")
        pg.screenshot(path=SHOTS / "1-landing.png", full_page=True)

        pg.click("#demo-btn")
        pg.wait_for_selector("#app:not([hidden])")
        settle(pg, "wallet connected")

        pg.click("#create-btn")
        settle(pg, "vault created")
        pg.click("#hide-code")

        pg.fill("#dep-amount", "0.05")
        pg.click("#dep-form button[type=submit]")
        settle(pg, "deposited 0.05 SOL")

        pg.click("#mint-btn")
        settle(pg, "minted test tokens")

        select_containing(pg, "#dep-asset", "qUSD")
        pg.fill("#dep-amount", "10")
        pg.click("#dep-form button[type=submit]")
        settle(pg, "deposited 10 qUSD")
        pg.screenshot(path=SHOTS / "2-funded-vault.png", full_page=True)

        select_containing(pg, "#send-asset", "SOL")
        pg.fill("#send-to", RECIPIENT)
        pg.fill("#send-amount", "0.01")
        pg.click("#send-form button[type=submit]")
        settle(pg, "sent 0.01 SOL quantum-safe", timeout=300_000)

        select_containing(pg, "#send-asset", "qUSD")
        pg.fill("#send-to", RECIPIENT)
        pg.fill("#send-amount", "2")
        pg.click("#send-form button[type=submit]")
        settle(pg, "sent 2 qUSD quantum-safe", timeout=300_000)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=SHOTS / "3-after-sends.png", full_page=True)

        links = pg.eval_on_selector_all(".step a", "as => as.map(a => a.closest('li').innerText.split('\\n')[0] + ' | ' + a.href)")
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
