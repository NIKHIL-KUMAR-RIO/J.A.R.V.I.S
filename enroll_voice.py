"""
Voice Enrollment Tool for JARVIS (V2 - with volume check and silence trimming).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
import numpy as np
import sounddevice as sd

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.speaker_id import SpeakerIdentifier, DEFAULT_THRESHOLD

SAMPLE_RATE = 16000
SAMPLE_DURATION = 4.5  # seconds


def record_clip(prompt: str, label: str) -> np.ndarray:
    while True:
        print(f"\n" + "=" * 55)
        print(f" {label}")
        print("=" * 55)
        print(f"Read this sentence aloud clearly and naturally:")
        print(f"\n  👉 \"{prompt}\"\n")
        input("Press [ENTER] when ready to speak...")

        print("🎙️ Get ready: 3...", end="", flush=True)
        time.sleep(0.7)
        print(" 2...", end="", flush=True)
        time.sleep(0.7)
        print(" 1...", end="", flush=True)
        time.sleep(0.7)
        print(" 👉 SPEAK NOW!")

        num_samples = int(SAMPLE_RATE * SAMPLE_DURATION)
        audio = sd.rec(num_samples, samplerate=SAMPLE_RATE, channels=1, dtype="float32")

        # Visual progress
        steps = 16
        for _ in range(steps):
            time.sleep(SAMPLE_DURATION / steps)
            print("🔴", end="", flush=True)
        sd.wait()
        print(" Done!")

        waveform = audio.flatten()
        peak = float(np.max(np.abs(waveform)))
        rms = float(np.sqrt(np.mean(waveform ** 2)))

        # Visual volume meter
        meter_len = int(min(30, peak * 60))
        meter = "█" * meter_len + "░" * (30 - meter_len)
        print(f"Volume Level: [{meter}] (peak: {peak:.2f})")

        if peak < 0.03 or rms < 0.005:
            print("\n⚠️  Microphone volume is TOO LOW or no speech was heard!")
            print("   Please speak closer to your microphone or check microphone volume in Windows.")
            retry = input("Press [ENTER] to re-record this sample, or 's' to skip: ").strip().lower()
            if retry != 's':
                continue

        return waveform


def main():
    print("""
==========================================================
       JARVIS VOICE BIOMETRICS ENROLLMENT (CAM++)
==========================================================
We will record 3 sentences so Jarvis learns your voice.
Tips for the best result:
  • Speak at your normal talking distance and volume.
  • Wait for '👉 SPEAK NOW!' before starting each sentence.
""")

    sid = SpeakerIdentifier()
    if not sid.is_ready:
        print(f"❌ Error: Model not found at {sid.model_path}")
        sys.exit(1)

    prompts = [
        "Hey Jarvis, check my system diagnostics and report status.",
        "Jarvis, open my workspace and show me today's schedule.",
        "Hey Jarvis, turn on voice recognition and listen for commands.",
    ]

    samples = []
    for i, p in enumerate(prompts, 1):
        samples.append(record_clip(p, f"Sample {i}/3"))

    print("\n⏳ Analyzing vocal resonance patterns...")
    ok, msg, consistencies = sid.enroll(samples, sample_rate=SAMPLE_RATE)
    if not ok:
        print(f"❌ Enrollment failed: {msg}")
        sys.exit(1)

    if consistencies:
        avg_con = int(np.mean(consistencies) * 100)
        print(f"📊 Vocal Consistency Score: {avg_con}%")
        for idx, c in enumerate(consistencies, 1):
            print(f"   • Pair comparison {idx}: {int(c * 100)}% match")

    print(f"\n✅ {msg}")
    print(f"💾 Voice profile saved to: {sid.profile_path}")

    # Interactive live verification test
    print("\n" + "=" * 55)
    print(" LIVE VERIFICATION TEST")
    print("=" * 55)
    print("Now let's test that Jarvis recognizes your voice!")

    while True:
        test_clip = record_clip("Hey Jarvis, are you able to hear my voice right now?", "Live Test")
        is_match, score = sid.verify(test_clip, sample_rate=SAMPLE_RATE, threshold=DEFAULT_THRESHOLD)
        score_pct = int(score * 100)
        th_pct = int(DEFAULT_THRESHOLD * 100)

        print("-" * 55)
        if is_match:
            print(f"🎉 VERIFIED! Jarvis recognizes you!")
            print(f"   Match Score: {score_pct}% (Required: {th_pct}%)")
            print("   Jarvis is now locked to your voice.")
        else:
            print(f"⚠️  Verification Score: {score_pct}% (Required: {th_pct}%)")
            print("   The score was below threshold.")
            if score_pct < 30:
                print("   (A score under 30% usually means silence was recorded or mic volume was too low).")
        print("-" * 55)

        again = input("\nTest another phrase? (Press ENTER to test, or type 'done' to finish): ").strip().lower()
        if again in ("done", "q", "quit", "n", "no"):
            break

    print("\nAll set! Your voice is enrolled.")


if __name__ == "__main__":
    main()
