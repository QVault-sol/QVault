"""Records the QVault demo video automatically against Solana devnet.

Title slides + the live web app (demo wallet), narrated by an open-source TTS voice
(Piper) with burned-in captions. Output: out/qvault-demo.mp4 and out/qvault-demo.srt.

Env:
  DEVNET_KEYPAIR  funded devnet keypair (JSON byte array) used as the demo wallet
  PIPER_MODEL     optional path to a Piper .onnx voice; without it the video has captions only
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
WEB = HERE.parent
OUT = HERE / "out"
AUDIO = OUT / "audio"
W, H = 1280, 720
KEY = json.loads(os.environ["DEVNET_KEYPAIR"])

NARRATION = {
    "title": "This is QVault: a quantum-safe vault for SOL and SPL tokens on Solana.",
    "problem": "On Solana, your address is your Ed25519 public key. It is visible to everyone, "
               "and a large enough quantum computer could compute the private key behind it.",
    "idea": "QVault keeps funds in a program account with no private key on chain. It opens only with a "
            "Winternitz one-time signature. That is hash-based, built on SHA-256, and verified by our program.",
    "landing": "Let's try it live on Solana devnet, with the demo wallet that runs in the browser.",
    "create": "First, the browser creates a vault from a random recovery code. That code never leaves this device.",
    "dep_sol": "We deposit some devnet SOL from the normal wallet.",
    "dep_tok": "And an SPL test token. SOL and tokens sit in the same vault.",
    "send_sol": "Now a withdrawal. The browser computes an 840-byte Winternitz signature. It fixes the recipient, "
                "the amount, the token and the next vault. The program re-hashes it on chain. Then permissionless "
                "steps pay out, and everything else rotates to a fresh vault under a new key.",
    "send_tok": "The token works the same way, signed with the next one-time key.",
    "log": "Every step is a real devnet transaction, with an explorer link to verify it.",
    "diff": "Winternitz vaults already exist on Solana, but they hold SOL only. "
            "QVault protects SOL, SPL tokens and Token-2022 in one vault.",
    "biz": "The business model is simple: a zero point one percent fee, only on withdrawals to third parties. "
           "Moving funds into your own next vault is free. We start with protocol and DAO treasuries, "
           "then wallet integrations.",
    "outro": "QVault is open source, with attack tests and a devnet pipeline that runs this exact flow in a real "
             "browser. It is not audited yet, so it runs on devnet only. An external audit comes before any "
             "mainnet launch.",
}


# ───────────── voice ─────────────
def synthesize() -> dict[str, float]:
    """Returns duration per segment in seconds; writes WAVs when a Piper voice is available."""
    AUDIO.mkdir(parents=True, exist_ok=True)
    model = os.environ.get("PIPER_MODEL")
    durations: dict[str, float] = {}
    voice = None
    if model and Path(model).exists():
        try:
            from piper import PiperVoice
            voice = PiperVoice.load(model)
        except Exception as e:  # noqa: BLE001
            print(f"::warning::Piper voice unavailable ({e}); captions only")
    for sid, text in NARRATION.items():
        path = AUDIO / f"{sid}.wav"
        if voice is not None:
            with wave.open(str(path), "wb") as wf:
                if hasattr(voice, "synthesize_wav"):
                    voice.synthesize_wav(text, wf)
                else:
                    voice.synthesize(text, wf)
            with wave.open(str(path), "rb") as wf:
                durations[sid] = wf.getnframes() / wf.getframerate()
        else:
            durations[sid] = len(text.split()) / 2.6  # reading pace for captions
    return durations


# ───────────── recording helpers ─────────────
CURSOR_JS = """
(() => {
  if (window.__cur) return;
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;left:-40px;top:-40px;width:22px;height:22px;border-radius:50%;' +
    'background:rgba(185,131,42,.85);border:2px solid #0f2a44;z-index:99;pointer-events:none;' +
    'transition:left .55s cubic-bezier(.3,.7,.2,1),top .55s cubic-bezier(.3,.7,.2,1),transform .15s;transform:translate(-50%,-50%)';
  document.documentElement.appendChild(c);
  window.__cur = c;
  window.__move = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; };
  window.__press = () => { c.style.transform = 'translate(-50%,-50%) scale(.7)'; setTimeout(() => c.style.transform = 'translate(-50%,-50%)', 180); };
})();
"""

CAPTION_JS = """
(text) => {
  let el = document.getElementById('__cap');
  if (!text) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement('div'); el.id = '__cap'; el.className = 'caption';
    el.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:98;max-width:1040px;' +
      'padding:12px 20px;border-radius:8px;background:rgba(15,42,68,.94);color:#fff;font:500 21px/1.35 Archivo,system-ui,sans-serif;text-align:center';
    document.documentElement.appendChild(el); }
  el.textContent = text;
}
"""


class Director:
    def __init__(self, page, durations):
        self.page = page
        self.dur = durations
        self.t0 = time.monotonic()
        self.cues: list[tuple[str, float, float]] = []
        self.pending_end = 0.0

    def now(self) -> float:
        return time.monotonic() - self.t0

    def say(self, sid: str, hold: bool = True):
        self.wait_voice()
        start = self.now()
        d = self.dur[sid]
        self.cues.append((sid, start, start + d))
        self.page.evaluate(CAPTION_JS, NARRATION[sid])
        self.pending_end = start + d + 0.35
        if hold:
            self.wait_voice()

    def wait_voice(self):
        rest = self.pending_end - self.now()
        if rest > 0:
            time.sleep(rest)
        if self.pending_end:
            try:
                self.page.evaluate(CAPTION_JS, "")
            except Exception:  # noqa: BLE001 - page may be navigating
                pass
            self.pending_end = 0.0

    def goto(self, url: str):
        self.page.goto(url)
        self.page.evaluate(CURSOR_JS)
        self.page.wait_for_timeout(250)

    def click(self, selector: str):
        el = self.page.locator(selector).first
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        self.page.evaluate(CURSOR_JS)
        self.page.evaluate(f"window.__move({box['x'] + box['width'] / 2}, {box['y'] + box['height'] / 2})")
        time.sleep(0.65)
        self.page.evaluate("window.__press()")
        el.click()

    def type(self, selector: str, text: str):
        self.click(selector)
        self.page.locator(selector).fill("")
        self.page.locator(selector).type(text, delay=70)

    def settle(self, what: str, timeout: int = 300_000):
        self.page.wait_for_function("!document.body.classList.contains('busy')", timeout=timeout)
        err = self.page.locator("#error:not([hidden])")
        if err.count():
            raise SystemExit(f"::error::{what}: {err.inner_text()}")
        print(f"::notice::✔ {what} at {self.now():.1f}s")

    def pick_chip(self, box: str, label: str, tries: int = 30):
        for _ in range(tries):
            loc = self.page.locator(f"#{box} label.chip", has_text=label)
            if loc.count():
                self.click(f"#{box} label.chip:has-text('{label}')")
                return
            time.sleep(2)
        raise SystemExit(f"::error::no {label} chip in #{box}")


def record(durations) -> tuple[Path, list]:
    raw = OUT / "raw"
    shutil.rmtree(raw, ignore_errors=True)
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8770", "-d", str(WEB)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    base = "http://localhost:8770"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(raw),
                                record_video_size={"width": W, "height": H}, device_scale_factor=1)
            ctx.add_init_script(f"localStorage.setItem('qvault.burner', {json.dumps(json.dumps(KEY))})")
            page = ctx.new_page()
            d = Director(page, durations)

            for scene in ("title", "problem", "idea"):
                d.goto(f"{base}/video/slides.html?scene={scene}")
                d.say(scene)

            d.goto(f"{base}/public/")
            d.say("landing")
            d.click("#demo-btn")
            page.wait_for_selector("#app:not([hidden])")
            d.settle("connected")

            d.say("create", hold=False)
            d.click("#create-btn")
            d.settle("vault created")
            page.locator("#recovery").evaluate("el => el.open = false")
            d.wait_voice()

            d.say("dep_sol", hold=False)
            d.pick_chip("dep-assets", "SOL")
            d.type("#dep-amount", "0.05")
            d.click("#dep-form button[type=submit]")
            d.settle("deposited SOL")
            d.wait_voice()

            d.say("dep_tok", hold=False)
            d.click("#mint-btn")
            d.settle("minted qUSD")
            d.pick_chip("dep-assets", "qUSD")
            d.type("#dep-amount", "10")
            d.click("#dep-form button[type=submit]")
            d.settle("deposited qUSD")
            d.wait_voice()

            d.click("#tab-send")
            d.pick_chip("send-assets", "SOL")
            d.click("#to-self")
            d.type("#send-amount", "0.01")
            d.say("send_sol", hold=False)
            d.click("#send-form button[type=submit]")
            d.settle("sent SOL")
            time.sleep(1.2)
            d.wait_voice()

            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            time.sleep(0.8)
            d.pick_chip("send-assets", "qUSD")
            d.click("#to-self")
            d.type("#send-amount", "2")
            d.say("send_tok", hold=False)
            d.click("#send-form button[type=submit]")
            d.settle("sent qUSD")
            time.sleep(1.2)
            d.wait_voice()

            page.locator("#log-h").scroll_into_view_if_needed()
            page.evaluate("document.getElementById('log-h').scrollIntoView({behavior: 'smooth', block: 'start'})")
            d.say("log")

            for scene in ("diff", "biz", "outro"):
                d.goto(f"{base}/video/slides.html?scene={scene}")
                d.say(scene)
            time.sleep(1.0)

            video = page.video
            cues = d.cues
            ctx.close()
            path = Path(video.path())
            b.close()
            return path, cues
    finally:
        srv.terminate()


def srt(cues, path: Path):
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    with open(path, "w") as f:
        for i, (sid, a, b) in enumerate(cues, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{NARRATION[sid]}\n\n")


def mux(video: Path, cues, out: Path):
    have_audio = all((AUDIO / f"{sid}.wav").exists() for sid, _, _ in cues) and os.environ.get("PIPER_MODEL")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    if have_audio:
        for sid, _, _ in cues:
            cmd += ["-i", str(AUDIO / f"{sid}.wav")]
        parts = [f"[{i + 1}:a]aresample=48000,adelay={int(a * 1000)}:all=1[a{i}]" for i, (_, a, _) in enumerate(cues)]
        mix = "".join(f"[a{i}]" for i in range(len(cues))) + f"amix=inputs={len(cues)}:normalize=0:duration=longest[aout]"
        cmd += ["-filter_complex", ";".join(parts + [mix]), "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-map", "0:v", "-an"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def main():
    OUT.mkdir(exist_ok=True)
    durations = synthesize()
    video, cues = record(durations)
    srt(cues, OUT / "qvault-demo.srt")
    mux(video, cues, OUT / "qvault-demo.mp4")
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                   str(OUT / "qvault-demo.mp4")], capture_output=True, text=True).stdout.strip() or 0)
    print(f"::notice::Video ready: {length:.0f} s, {(OUT / 'qvault-demo.mp4').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
