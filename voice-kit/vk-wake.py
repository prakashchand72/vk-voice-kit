#!/usr/bin/env python3
"""
vk-wake — hands-free wake-word listener for the vk voice kit.

Continuously listens on the mic. When the wake word "Sebastian" is heard it
plays a beep (so you know recording started), then records until ~0.7s of
trailing silence (or 15s max), writes the raw audio to /tmp/vk-hold.wav, and
triggers the SAME `vk hold` pipeline via Hammerspoon.

The wake word is detected with Vosk FULL transcription (using the language
model) plus fuzzy matching against common pronunciations, which is far more
reliable than strict keyword-spotting for a real human voice.

Usage:
  vk-wake            run the listener (foreground)
  vk-wake --once     run one wake->record cycle then exit (for testing)
  vk-wake --test[=N] calibration: print what the mic hears for N secs (default 10)
  vk-wake --stop     stop a running listener (writes a stop flag)
"""

import os
import sys
import time
import queue
import json
import struct
import subprocess

HOME = os.path.expanduser("~")
KIT = os.path.join(HOME, ".voice-kit")
MODEL_DIR = os.path.join(KIT, "models", "vosk-model-small-en-us-0.15")
HOLD_WAV = "/tmp/vk-hold.wav"
STOP_FLAG = os.path.join(KIT, "wake-stop")
STATE_FILE = os.path.join(KIT, "wake-state")
START_SOUND = os.path.join(KIT, "sounds", "start.wav")

WAKE_WORD = "sebastian"
SAMPLE_RATE = 16000
BLOCK_MS = 30
SILENCE_SECS = 3.0         # trailing silence that ends a recording
MIN_RECORD_SECS = 0.6      # don't finalize before this (avoids cutting off)
MAX_RECORD_SECS = 15.0     # hard cap on a single utterance
WAKE_COOLDOWN = 1.5        # ignore re-trigger for 1.5s after a cycle
VAD_PEAK = 8000            # int16 amplitude above which we count as "speech" (above ambient ~3987, below speech ~21000)
RESET_SECS = 600.0          # reset the recognizer periodically to stay bounded

# Common pronunciations/misspellings so a natural voice still triggers.
WAKE_VARIANTS = (
    "sebastian", "sabastian", "sebastien", "sebastin", "sebastion",
    "sebastician", "sebastion",
)


def wake_in(text):
    t = text.lower().replace("'", "")
    return any(v in t for v in WAKE_VARIANTS)


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(KIT, "wake.log"), "a") as f:
        f.write(line + "\n")


def set_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(s)
    except Exception:
        pass


def play_beep():
    """Play the start chime so the user knows recording has begun."""
    if os.path.exists(START_SOUND):
        subprocess.Popen(["afplay", START_SOUND],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def trigger_hermes():
    """Ask Hammerspoon to run the vk hold pipeline on /tmp/vk-hold.wav."""
    try:
        subprocess.run(["hs", "-c", "vkWakeHold()"],
                       capture_output=True, timeout=10)
        return True
    except Exception as e:
        log("trigger failed: %s" % e)
        return False


def main():
    import numpy as np
    import sounddevice as sd
    from vosk import Model, KaldiRecognizer

    if not os.path.isdir(MODEL_DIR):
        log("model not found at %s" % MODEL_DIR)
        sys.exit(1)

    model = Model(MODEL_DIR)

    # --test: calibration mode, print everything the mic hears for N seconds
    test_arg = next((a for a in sys.argv if a.startswith("--test")), None)
    if test_arg is not None:
        test_secs = 10
        if "=" in test_arg:
            test_secs = int(test_arg.split("=")[1])
        rec = KaldiRecognizer(model, SAMPLE_RATE)
        log("CALIBRATION: listening for %ds. Say \"Sebastian\" and watch for it below." % test_secs)
        q = queue.Queue()

        def cb(indata, frames, time_info, status):
            q.put(bytes(indata))

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=int(SAMPLE_RATE * BLOCK_MS / 1000),
            dtype="int16", channels=1, callback=cb, device=None,
        ):
            end = time.time() + test_secs
            while time.time() < end:
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    t = res.get("text", "").strip()
                    if t:
                        mark = "  <<< WAKE" if wake_in(t) else ""
                        log("heard: '%s'%s" % (t, mark))
                else:
                    res = json.loads(rec.PartialResult())
                    t = res.get("partial", "").strip()
                    if t:
                        print("  partial: '%s'" % t, flush=True)
        log("calibration done")
        return

    # FULL transcription (language model), not strict keyword-spotting. Full
    # transcription is far more robust at recognizing a real human saying
    # "Sebastian" than a grammar restricted to the single keyword.
    rec = KaldiRecognizer(model, SAMPLE_RATE)

    mic = "default"
    try:
        out = subprocess.run([os.path.join(KIT, "vk"), "mic-resolve"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out and out != "❌":
            mic = out
    except Exception:
        pass

    log("wake-word listener starting (wake word: '%s', mic: %s)" % (WAKE_WORD, mic))
    set_state("listening")

    q = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(bytes(indata))

    armed = True
    recording = False
    rec_buf = bytearray()
    last_sound = 0.0
    last_wake = 0.0
    last_reset = 0.0
    cycle_start = 0.0
    had_speech = False

    def reset():
        nonlocal armed, recording, rec_buf, last_sound, had_speech
        armed = True
        recording = False
        rec_buf = bytearray()
        last_sound = 0.0
        had_speech = False
        set_state("listening")

    def finalize():
        nonlocal recording
        # If the wake word fired but no real command was spoken (e.g. the
        # recording hit the max-duration cap on silence), discard it instead
        # of sending a garbage/empty command to Hermes.
        if not had_speech:
            log("no speech after wake, discarding (%d bytes)" % len(rec_buf))
            reset()
            return
        log("finalizing (%d bytes)" % len(rec_buf))
        # write a proper WAV file (16-bit mono 16kHz) so `vk hold` can read it.
        # Raw PCM without a RIFF header is unreadable and caused stale transcription.
        data = bytes(rec_buf)
        n = len(data)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + n, b"WAVE",
            b"fmt ", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
            b"data", n,
        )
        with open(HOLD_WAV, "wb") as f:
            f.write(header)
            f.write(data)
        reset()
        trigger_hermes()

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=int(SAMPLE_RATE * BLOCK_MS / 1000),
        dtype="int16", channels=1, callback=callback, device=None,
    ):
        last_reset = time.time()
        while True:
            if os.path.exists(STOP_FLAG):
                os.remove(STOP_FLAG)
                log("stop flag seen, exiting")
                set_state("off")
                break

            try:
                data = q.get(timeout=1.0)
            except queue.Empty:
                continue

            if armed:
                # periodic reset keeps the full-transcription recognizer bounded
                if time.time() - last_reset >= RESET_SECS:
                    rec.Reset()
                    last_reset = time.time()

                if rec.AcceptWaveform(data):
                    res = json.loads(rec.Result())
                    text = res.get("text", "").strip()
                else:
                    res = json.loads(rec.PartialResult())
                    text = res.get("partial", "").strip()

                if text and wake_in(text):
                    now = time.time()
                    if now - last_wake >= WAKE_COOLDOWN:
                        last_wake = now
                        log("WAKE: '%s' -> beep + recording" % text)
                        play_beep()
                        armed = False
                        recording = True
                        rec_buf = bytearray()   # fresh: no wake word
                        last_sound = now
                        cycle_start = now
                        had_speech = False
                        set_state("recording")
                        rec.Reset()             # stop re-detecting the keyword
            elif recording:
                # capture audio + VAD on EVERY block
                rec_buf += data
                peak = np.abs(np.frombuffer(data, dtype=np.int16)).max()
                if peak > VAD_PEAK:
                    last_sound = time.time()
                    had_speech = True

                elapsed = time.time() - cycle_start
                if elapsed >= MIN_RECORD_SECS and time.time() - last_sound >= SILENCE_SECS:
                    log("silence detected")
                    finalize()
                    if "--once" in sys.argv:
                        log("--once mode, exiting")
                        break
                elif elapsed >= MAX_RECORD_SECS:
                    log("max duration reached")
                    finalize()
                    if "--once" in sys.argv:
                        log("--once mode, exiting")
                        break


if __name__ == "__main__":
    main()