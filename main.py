"""
Wedding Phone – MicroPython application for the Raspberry Pi Pico.

Flow
────
  1. Idle – wait for a guest to lift the handset (off-hook).
  2. Off-hook – open audio path; short pause for line to settle.
  3. Speak a greeting using SAM TTS synthesis.
  4. Record the guest's message to a new WAV file on the SD card.
  5. Hang-up – clean up, return to idle.

Hardware summary
────────────────
  KS0835 board
    HOOK      → GP6   (input, active-low: low = off-hook)
    RING_MODE → GP7   (output: high = ring enabled)
    FWD/REV   → GP8   (output: toggled at ring frequency)
    AUDIO_OUT → GP26  (ADC0: analog audio from phone line)
    AUDIO_IN  ← GP15  (PWM → RC filter → phone line)

  SD card (SPI1)
    SCK  → GP10
    MOSI → GP11
    MISO → GP12
    CS   → GP13

RC filter for AUDIO_IN
    GP15 ──┤1 kΩ├──┬── to KS0835 AUDIO_IN
                   │
                 39 nF
                   │
                  GND
    (fc ≈ 4.1 kHz; adequate for 8 kHz telephony audio)

ADC bias for AUDIO_OUT
    3V3 ──┤10 kΩ├──┬── to KS0835 AUDIO_OUT
                   ├──┤100 nF├── to GP26
                  10 kΩ
                   │
                  GND
    (biases AC audio signal to 1.65 V so it sits in the Pico ADC range)
"""

import machine
import utime
import os

from machine import Pin, SPI

import sdcard
from ks0835  import KS0835
from audio   import AudioIO

# ======================================================================
# Pin assignments  –  adjust if your wiring differs
# ======================================================================

# KS0835 interface
HOOK_PIN      = 6
RING_MODE_PIN = 7
FWD_REV_PIN   = 8

# Audio
AUDIO_OUT_PWM = 15   # PWM → RC filter → KS0835 AUDIO_IN
AUDIO_IN_ADC  = 26   # ADC0 ← KS0835 AUDIO_OUT (DC-biased to ~1.65 V)

# SD card (SPI1)
SD_SPI_BUS  = 1
SD_SCK_PIN  = 10
SD_MOSI_PIN = 11
SD_MISO_PIN = 12
SD_CS_PIN   = 13

# ======================================================================
# Application settings
# ======================================================================

GREETING_TEXT = (
    "Hello! Thank you for coming to our wedding. "
    "Please leave us a message after the tone."
)
GREETING_VOICE = "old_man"   # sam | robot | elf | old_man | whisper | etc.

# Telephone sound sequence played immediately after the handset is lifted.
# Simulates: dial tone → subscriber dialling → ringback → greeting.
DIAL_TONE_MS   = 1200        # duration of the initial dial tone
DTMF_SEQUENCE  = "0425"      # digits to "dial" (customise freely)
DTMF_TONE_MS   = 100         # on-time per DTMF digit
DTMF_GAP_MS    = 80          # silence between DTMF digits
RING_BURSTS    = 1           # how many AU ring cadences before greeting

ENABLE_SD      = False                # set True to mount SD and record messages

RECORDING_DIR  = "/sd/recordings"
SAMPLE_RATE    = 8000                 # Hz
MAX_REC_SECS   = 60                   # maximum guest message length

# ======================================================================
# Helpers
# ======================================================================

def _mount_sd() -> None:
    spi = SPI(
        SD_SPI_BUS,
        baudrate=10_000_000,
        polarity=0, phase=0,
        sck=Pin(SD_SCK_PIN),
        mosi=Pin(SD_MOSI_PIN),
        miso=Pin(SD_MISO_PIN),
    )
    cs   = Pin(SD_CS_PIN, Pin.OUT, value=1)
    card = sdcard.SDCard(spi, cs)
    vfs  = os.VfsFat(card)
    os.mount(vfs, "/sd")
    print("SD card mounted")


def _ensure_recording_dir() -> None:
    try:
        os.stat(RECORDING_DIR)
    except OSError:
        os.mkdir(RECORDING_DIR)


def _next_filename() -> str:
    """Return /sd/recordings/msg_NNNN.wav with an incrementing counter."""
    try:
        nums = [
            int(f[4:-4])
            for f in os.listdir(RECORDING_DIR)
            if f.startswith("msg_") and f.endswith(".wav")
        ]
        n = max(nums) + 1 if nums else 1
    except Exception:
        n = 1
    return f"{RECORDING_DIR}/msg_{n:04d}.wav"


# ======================================================================
# Main
# ======================================================================

def main() -> None:
    print("Wedding Phone – starting")

    if ENABLE_SD:
        _mount_sd()
        _ensure_recording_dir()

    slic     = KS0835(HOOK_PIN, RING_MODE_PIN, FWD_REV_PIN)
    audio_io = AudioIO(AUDIO_OUT_PWM, AUDIO_IN_ADC, SAMPLE_RATE)

    print("Ready – waiting for guests")

    while True:
        # ── IDLE: wait for off-hook ──────────────────────────────────
        slic.wait_off_hook()
        print("Handset lifted")

        # ── DIAL TONE → DTMF → RINGBACK ─────────────────────────────
        utime.sleep_ms(200)                          # line-settle pause
        audio_io.play_dial_tone(DIAL_TONE_MS)
        audio_io.play_dtmf(DTMF_SEQUENCE, DTMF_TONE_MS, DTMF_GAP_MS)
        audio_io.play_ring_tone(RING_BURSTS)

        # ── GREETING ────────────────────────────────────────────────
        print("Playing greeting")
        audio_io.speak(GREETING_TEXT, GREETING_VOICE)

        # If the guest hung up during the greeting, go back to idle
        if not slic.is_off_hook():
            print("Handset replaced during greeting")
            continue

        # ── RECORD ──────────────────────────────────────────────────
        if ENABLE_SD:
            filename = _next_filename()
            print(f"Recording → {filename}")
            audio_io.record_wav(filename, MAX_REC_SECS, slic)
        else:
            print("SD disabled – skipping recording, waiting for hang-up")
            while slic.is_off_hook():
                utime.sleep_ms(100)

        # ── HANG-UP ─────────────────────────────────────────────────
        print("Message done – waiting for next guest\n")
        utime.sleep_ms(200)


if __name__ == "__main__":
    main()
