"""Renders a short voice-sample video: several Kokoro voices read the same opening line,
with the voice name on screen, so a narrator can be picked by ear.

Writes web/video/out/voice-samples.mp4.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "samples"
VOICES = ["af_heart", "af_bella", "bf_emma", "am_michael", "bm_george", "am_fenrir"]
LINE = ("On Solana, your address is your public key. A big enough quantum computer could work out "
        "the private key behind it. Q-Vault keeps your tokens behind hash-based signatures instead.")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def main():
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    OUT.mkdir(parents=True, exist_ok=True)
    pipes = {}
    clips = []
    for n, voice in enumerate(VOICES, 1):
        lang = "b" if voice.startswith("b") else "a"
        pipe = pipes.setdefault(lang, KPipeline(lang_code=lang))
        audio = np.concatenate([
            (r.audio if getattr(r, "audio", None) is not None else r[2]).detach().cpu().numpy()
            for r in pipe(LINE, voice=voice, speed=1.0)
        ])
        wav = OUT / f"{voice}.wav"
        sf.write(str(wav), np.concatenate([np.zeros(6000), audio, np.zeros(12000)]), 24000, subtype="PCM_16")
        clip = OUT / f"{n:02d}-{voice}.mp4"
        label = f"Voice {n}  ·  {voice}"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x0f2a44:s=1280x720:r=30",
            "-i", str(wav), "-shortest",
            "-vf", f"drawtext=fontfile={FONT}:text='{label}':fontcolor=0xdce2e5:fontsize=64:x=(w-tw)/2:y=(h-th)/2",
            "-af", "aresample=48000,highpass=f=70,lowpass=f=12500,afftdn=nf=-30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", str(clip)], check=True)
        clips.append(clip)
        print(f"  ✔ {voice}")
    lst = OUT / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
    dst = HERE / "out" / "voice-samples.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    str(dst)], check=True)
    print(f"::notice::voice samples ready: {', '.join(VOICES)}")


if __name__ == "__main__":
    sys.exit(main())
