"""Records the QVault demo video automatically against Solana devnet.

Title slides + the live web app (demo wallet), narrated by an open-source TTS voice
(Kokoro, Piper as fallback) with burned-in captions. Output: out/qvault-demo.mp4 and out/qvault-demo.srt.

Env:
  DEVNET_KEYPAIR  funded devnet keypair (JSON byte array) used as the demo wallet
  KOKORO_VOICE    optional Kokoro voice (default af_heart); Piper (PIPER_MODEL) is the fallback
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
PAYER = json.loads(os.environ["DEVNET_KEYPAIR"])
RPC = "https://api.devnet.solana.com"


def fresh_wallet(sol: float = 0.15) -> list[int]:
    """New demo wallet for the recording, funded from the devnet payer (no faucet needed)."""
    import base64
    import urllib.request
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.hash import Hash
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction

    def rpc(method, params):
        req = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                     {"Content-Type": "application/json"})
        for i in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read())
                if "error" not in res:
                    return res["result"]
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2 * (i + 1))
        raise SystemExit(f"::error::RPC {method} failed")

    payer = Keypair.from_bytes(bytes(PAYER))
    kp = Keypair()
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    msg = Message.new_with_blockhash([transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=kp.pubkey(),
                                                              lamports=int(sol * 1e9)))], payer.pubkey(), bh)
    sig = rpc("sendTransaction", [base64.b64encode(bytes(Transaction([payer], msg, bh))).decode(), {"encoding": "base64"}])
    for _ in range(60):
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            print(f"::notice::Demo wallet {kp.pubkey()} funded with {sol} SOL")
            return list(bytes(kp))
        time.sleep(1)
    raise SystemExit("::error::funding the demo wallet did not confirm")


KEY = None  # set in main()

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
# How the voice should pronounce terms that captions show in written form.
SAY = [("QVault", "Q-Vault"), ("Ed25519", "E-D 2-5-5-1-9"), ("SHA-256", "SHA 256"), ("SPL", "S-P-L"),
       ("Token-2022", "Token 2022"), ("840-byte", "840 byte"), ("DAO", "DAO"), ("devnet", "dev-net")]


def speech(text: str) -> str:
    for a, b in SAY:
        text = text.replace(a, b)
    return text


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def _kokoro():
    """Kokoro-82M (Apache-2.0): natural neural voice, runs on CPU."""
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    pipe = KPipeline(lang_code="a")  # American English
    voice = os.environ.get("KOKORO_VOICE", "af_heart")

    def say(text: str, path: Path):
        chunks = []
        for r in pipe(text, voice=voice, speed=1.0):
            audio = getattr(r, "audio", None)
            if audio is None:
                audio = r[2]
            chunks.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
        sf.write(str(path), np.concatenate(chunks), 24000, subtype="PCM_16")
    return say


def _piper():
    from piper import PiperVoice
    v = PiperVoice.load(os.environ["PIPER_MODEL"])

    def say(text: str, path: Path):
        with wave.open(str(path), "wb") as wf:
            (v.synthesize_wav if hasattr(v, "synthesize_wav") else v.synthesize)(text, wf)
    return say


def synthesize() -> dict[str, float]:
    """Returns duration per segment in seconds and writes one WAV per segment.

    Tries Kokoro first, then Piper; without either the video gets captions only."""
    AUDIO.mkdir(parents=True, exist_ok=True)
    say = None
    for name, factory in (("kokoro", _kokoro), ("piper", _piper)):
        try:
            say = factory()
            print(f"::notice::voice engine: {name}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"::warning::{name} unavailable: {e}")
    durations: dict[str, float] = {}
    for sid, text in NARRATION.items():
        path = AUDIO / f"{sid}.wav"
        if say is not None:
            say(speech(text), path)
            durations[sid] = _wav_seconds(path)
        else:
            path.unlink(missing_ok=True)
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
    el.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:98;max-width:1240px;' +
      'padding:14px 26px;border-radius:10px;background:rgba(15,42,68,.94);color:#fff;font:500 27px/1.35 Archivo,system-ui,sans-serif;text-align:center';
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
                picked = self.page.eval_on_selector(f"#{box}", "b => b.querySelector('input:checked')?.closest('label')?.innerText || ''")
                if label in picked:
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
            # Render at 1600x900 and scale the recording to 1280x720: more of the app fits on screen.
            ctx = b.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(raw),
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
    have_audio = all((AUDIO / f"{sid}.wav").exists() for sid, _, _ in cues)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    if have_audio:
        for sid, _, _ in cues:
            cmd += ["-i", str(AUDIO / f"{sid}.wav")]
        parts = [f"[{i + 1}:a]aresample=48000,adelay={int(a * 1000)}:all=1[a{i}]" for i, (_, a, _) in enumerate(cues)]
        mix = ("".join(f"[a{i}]" for i in range(len(cues))) + f"amix=inputs={len(cues)}:normalize=0:duration=longest,"
               "apad,loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        cmd += ["-filter_complex", ";".join(parts + [mix]), "-map", "0:v", "-map", "[aout]", "-shortest", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def plan(cues, total: float, min_gap: float = 3.0, keep: float = 0.8, speed: float = 6.0):
    """Segments (start, end, factor): stretches without narration play at `speed`x,
    keeping `keep` seconds of real time at both ends so the motion stays readable."""
    spans = sorted((a, b) for _, a, b in cues)
    segs, t = [], 0.0
    for a, b in spans + [(total, total)]:
        if a - t > min_gap:
            segs += [(t, t + keep, 1.0), (t + keep, a - keep, speed), (a - keep, a, 1.0)]
        elif a > t:
            segs.append((t, a, 1.0))
        if b > a:
            segs.append((a, min(b, total), 1.0))
        t = max(t, b)
    return [(x, y, f) for x, y, f in segs if y - x > 0.05]


def remap(t: float, segs) -> float:
    out = 0.0
    for a, b, f in segs:
        if t >= b:
            out += (b - a) / f
        else:
            return out + max(0.0, t - a) / f
    return out


def tighten(src: Path, cues, dst: Path):
    """Speeds up silent waiting for the blockchain; returns cues on the new timeline.

    Each segment is encoded on its own and joined with the concat demuxer, which keeps memory low."""
    segs = []
    for a, b, f in plan(cues, duration(src)):  # merge neighbours with the same speed
        if segs and segs[-1][2] == f and abs(segs[-1][1] - a) < 1e-6:
            segs[-1] = (segs[-1][0], b, f)
        else:
            segs.append((a, b, f))
    work = dst.parent / "segments"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    listing = []
    for i, (a, b, f) in enumerate(segs):
        tempo, r = [], f
        while r > 2.0:  # atempo accepts 0.5–2.0 per stage
            tempo.append("atempo=2.0")
            r /= 2.0
        tempo.append(f"atempo={r:.4f}")
        out = work / f"s{i:03}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(src),
                        "-filter:v", f"setpts=PTS/{f}", "-filter:a", ",".join(tempo), "-r", "30",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(out)], check=True)
        listing.append(f"file '{out.name}'")
    (work / "list.txt").write_text("\n".join(listing) + "\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
                    "-c", "copy", "-movflags", "+faststart", str(dst)], check=True)
    return [(sid, remap(a, segs), remap(b, segs)) for sid, a, b in cues]


def main():
    global KEY
    OUT.mkdir(exist_ok=True)
    KEY = fresh_wallet()
    durations = synthesize()
    video, cues = record(durations)
    full = OUT / "full.mp4"
    mux(video, cues, full)
    cues = tighten(full, cues, OUT / "qvault-demo.mp4")
    srt(cues, OUT / "qvault-demo.srt")
    length = duration(OUT / "qvault-demo.mp4")
    print(f"::notice::Video ready: {length:.0f} s, {(OUT / 'qvault-demo.mp4').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
