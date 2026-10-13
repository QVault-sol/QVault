"""Narration via a running VoiceStudio backend (https://github.com/debpalash/VoiceStudio).

1. Designs a narrator voice with VoxCPM2 (Apache-2.0) from a text description.
2. Saves that take as a voice profile, so every line is spoken with the same voice.
3. Renders each narration line with that profile and checks that the output is real audio.

Env:
  VOICESTUDIO_URL   backend address (default http://localhost:3900)
  OMNIVOICE_API_KEY backend API key (Docker server mode requires it)
  VOICE_DESCRIPTION optional override of the narrator description
Writes web/video/out/voice/<segment>.wav; exits non-zero on any failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "voice"
BASE = os.environ.get("VOICESTUDIO_URL", "http://localhost:3900").rstrip("/")
KEY = os.environ.get("OMNIVOICE_API_KEY", "")
ENGINE = "voxcpm2"
SEED = 7
DESCRIPTION = os.environ.get(
    "VOICE_DESCRIPTION",
    "A calm, confident male narrator in his thirties. Clear American English, warm and trustworthy, "
    "measured pace, like a product keynote. Clean studio recording.",
)
ANCHOR = ("Security should not depend on luck. This is a short sample of the narrator voice, "
          "recorded once and reused for every line of the demo.")

os.environ.setdefault("DEVNET_KEYPAIR", "[0]")  # record.py reads it at import time
sys.path.insert(0, str(HERE))
from record import NARRATION, speech  # noqa: E402


def headers(extra=None):
    h = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    h.update(extra or {})
    return h


def call(method, path, body=None, ctype=None, timeout=3600):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=headers({"Content-Type": ctype} if ctype else None))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def wait_ready(limit=1800):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            st, _ = call("GET", "/health", timeout=10)
            if st == 200:
                print(f"::notice::VoiceStudio ready after {time.time() - t0:.0f}s")
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(10)
    raise SystemExit("::error::VoiceStudio backend did not become healthy")


def probe_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def tts(text: str, path: Path, **extra):
    body = {"model": ENGINE, "input": text, "response_format": "wav", "language": "en", "seed": SEED, **extra}
    for attempt in range(3):
        try:
            t0 = time.time()
            st, data = call("POST", "/v1/audio/speech", json.dumps(body).encode(), "application/json")
            path.write_bytes(data)
            secs = probe_seconds(path)
            if st == 200 and secs > 0.3:
                print(f"  ✔ {path.name}: {secs:.1f}s audio in {time.time() - t0:.0f}s")
                return secs
            print(f"::warning::{path.name}: status {st}, {len(data)} bytes, not valid audio")
        except urllib.error.HTTPError as e:
            print(f"::warning::{path.name}: HTTP {e.code} {e.read()[:300]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"::warning::{path.name}: {e}")
        time.sleep(15 * (attempt + 1))
    raise SystemExit(f"::error::speech generation failed for {path.name}")


def create_profile(anchor: Path) -> str:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("name", "QVault narrator"), ("ref_text", ANCHOR), ("language", "Auto"), ("kind", "clone")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="ref_audio"; filename="anchor.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + anchor.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    st, data = call("POST", "/profiles", b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout=300)
    pid = json.loads(data).get("id")
    if not pid:
        raise SystemExit(f"::error::profile creation returned {data[:300]!r}")
    print(f"::notice::voice profile {pid} created from the designed voice")
    return pid


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    wait_ready()
    anchor = OUT / "_anchor.wav"
    print("Designing the narrator voice with VoxCPM2 …")
    tts(ANCHOR, anchor, description=DESCRIPTION)
    profile = create_profile(anchor)
    total = 0.0
    for sid, text in NARRATION.items():
        total += tts(speech(text), OUT / f"{sid}.wav", voice=profile)
    print(f"::notice::VoiceStudio narration ready: {len(NARRATION)} lines, {total:.0f}s")


if __name__ == "__main__":
    main()
