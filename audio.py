"""
Audio I/O for the Raspberry Pi Pico.

Playback  – 8-bit unsigned PCM WAV → PWM output → RC low-pass filter.
            Recommended filter: R=1kΩ, C=39nF (fc ≈ 4.1 kHz).
            Connect the filtered output to the KS0835 AUDIO_IN pin.

Recording – ADC input from the KS0835 AUDIO_OUT pin.
            The SLIC output must be DC-biased to ~1.65 V (half of 3.3 V)
            so the Pico ADC sees the full 0–3.3 V swing.
            A simple bias circuit: 2×10 kΩ voltage divider to 3.3 V,
            with the audio signal AC-coupled via a 100 nF capacitor.

Dual-core strategy:
  Core 1 – time-critical ADC sampling at a fixed rate.
  Core 0 – this module's record_wav() method; streams chunks to the SD card.

Both cores share two fixed-size bytearrays via a lightweight handshake
(no locks needed: only one writer and one reader per buffer at a time).
"""

import math
import utime
import struct
import _thread
from machine import PWM, ADC, Pin

# ---------------------------------------------------------------------------
# Tone generation helpers (module-level, independent of AudioIO)
# ---------------------------------------------------------------------------

# DTMF digit → (row_hz, column_hz)
_DTMF_FREQS = {
    '1': (697, 1209), '2': (697, 1336), '3': (697, 1477),
    '4': (770, 1209), '5': (770, 1336), '6': (770, 1477),
    '7': (852, 1209), '8': (852, 1336), '9': (852, 1477),
    '0': (941, 1336), '*': (941, 1209), '#': (941, 1477),
}


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _mk_loop_buf(f1: float, f2: float, rate: int) -> bytearray:
    """
    Build the shortest cyclic PCM buffer that completes an integer number
    of cycles for both f1 and f2 (or just f1 if f2 == 0).  Capped at
    400 samples so computation stays fast even for awkward DTMF pairs.
    8-bit unsigned samples, amplitude 110/128 for headroom.
    """
    if f2:
        g = _gcd(int(round(f1)), int(round(f2)))
    else:
        g = _gcd(rate, int(round(f1)))
    n = min(rate // g, 400)
    n = max(n, 40)

    buf = bytearray(n)
    a1  = 2.0 * math.pi * f1 / rate
    a2  = 2.0 * math.pi * f2 / rate if f2 else 0.0
    for i in range(n):
        s = math.sin(a1 * i)
        if f2:
            s = (s + math.sin(a2 * i)) * 0.5
        buf[i] = int(s * 110) + 128
    return buf


class AudioIO:
    # Each chunk = 0.5 s of audio.  Two chunks → one second of buffering.
    # Large enough to hide SD-write latency; small enough to fit in RAM.
    CHUNK = 4000   # samples  (0.5 s @ 8 kHz)

    def __init__(self, pwm_pin: int, adc_pin: int, sample_rate: int = 8000):
        self._rate      = sample_rate
        self._period_us = 1_000_000 // sample_rate   # µs between samples
        self._pwm_pin   = pwm_pin

        # PWM carrier at 250 kHz; 8-bit resolution (duty 0–255 scaled to 0–65535)
        self._pwm = PWM(Pin(pwm_pin))
        self._pwm.freq(250_000)
        self._pwm.duty_u16(32768)   # mid-rail idle  (silence = 128 << 8)

        self._adc = ADC(Pin(adc_pin))

        # Double-buffer for recording
        self._buf = [bytearray(self.CHUNK), bytearray(self.CHUNK)]
        # _fill_idx  : which buffer Core 1 is currently writing into
        # _write_idx : which buffer Core 0 should flush to SD
        self._fill_idx  = 0
        self._write_idx = 1

        # Handshake flags (written by one core, read by the other)
        # _buf_ready  – Core 1 → Core 0: a full buffer is waiting
        # _buf_acked  – Core 0 → Core 1: Core 0 has taken the buffer
        self._buf_ready = False
        self._buf_acked = False

        # Core 1 sets this True when it has finished sampling
        self._rec_done = False

    # ------------------------------------------------------------------
    # Speech synthesis (TTS)
    # ------------------------------------------------------------------

    def speak(self, text: str, voice: str = "sam") -> None:
        """
        Synthesise speech from text and play it through the audio output pin.

        Depends on the SAM TTS library for MicroPython:
            https://github.com/kevinmcaleer/sam
        Copy sam.py (and any bundled dependencies) onto the Pico alongside
        this file.

        voice choices: "sam", "robot", "elf", "old_man", "whisper",
                       "alien", "giant", "child", "stuffy"

        SAM uses RP2040 PIO for its output, so the PWM on the same pin must
        be released first; it is re-initialised afterwards.
        """
        self._pwm.deinit()
        try:
            from sam import SAM                         # type: ignore
            tts = SAM(pin=self._pwm_pin, voice=voice)
            tts.say(text)
        except ImportError:
            print(
                "audio: SAM library not found – "
                "install from https://github.com/kevinmcaleer/sam"
            )
        finally:
            # Restore PWM for WAV playback and the idle mid-rail state
            self._pwm = PWM(Pin(self._pwm_pin))
            self._pwm.freq(250_000)
            self._pwm.duty_u16(32768)

    # ------------------------------------------------------------------
    # Telephone tones
    # ------------------------------------------------------------------

    def _play_tone(self, f1: float, f2: float, duration_ms: int) -> None:
        """
        Play a one- or two-frequency tone for duration_ms via PWM.
        Uses a short cyclic buffer to avoid large allocations.
        """
        cycle     = _mk_loop_buf(f1, f2, self._rate)
        total     = self._rate * duration_ms // 1000
        period_us = self._period_us
        played    = 0
        while played < total:
            for b in cycle:
                if played >= total:
                    break
                t = utime.ticks_us()
                self._pwm.duty_u16(b << 8)
                while utime.ticks_diff(utime.ticks_us(), t) < period_us:
                    pass
                played += 1
        self._pwm.duty_u16(32768)

    def _play_silence(self, duration_ms: int) -> None:
        """Output mid-rail (silence) for duration_ms milliseconds."""
        self._pwm.duty_u16(32768)
        utime.sleep_ms(duration_ms)

    def play_dial_tone(self, duration_ms: int = 1500) -> None:
        """Australian dial tone: 425 Hz continuous."""
        self._play_tone(425.0, 0, duration_ms)

    def play_dtmf(
        self,
        digits:  str,
        tone_ms: int = 100,
        gap_ms:  int = 80,
    ) -> None:
        """
        Play a DTMF digit sequence.
        digits   – string of '0'–'9', '*', or '#'
        tone_ms  – on duration per digit
        gap_ms   – silence between digits (and after the last)
        """
        for ch in digits:
            freqs = _DTMF_FREQS.get(ch)
            if freqs:
                self._play_tone(freqs[0], freqs[1], tone_ms)
            self._play_silence(gap_ms)

    def play_ring_tone(self, bursts: int = 1) -> None:
        """
        Australian ringing tone: 400 Hz + 450 Hz.
        Cadence per burst: 400 ms ON · 200 ms OFF · 400 ms ON · 800 ms OFF.
        """
        for _ in range(bursts):
            self._play_tone(400.0, 450.0, 400)
            self._play_silence(200)
            self._play_tone(400.0, 450.0, 400)
            self._play_silence(800)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def play_wav(self, filename: str) -> None:
        """
        Play a WAV file via PWM.  Only 8-bit mono PCM is supported.
        Stereo and 16-bit files will play incorrectly; convert offline first
        (e.g. ffmpeg -i input.wav -ar 8000 -ac 1 -acodec pcm_u8 greeting.wav).
        """
        try:
            with open(filename, "rb") as f:
                hdr = f.read(44)
                if len(hdr) < 44 or hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                    print(f"audio: {filename} is not a valid WAV file")
                    return
                file_rate = struct.unpack_from("<I", hdr, 24)[0]
                period_us = 1_000_000 // file_rate
                bits      = struct.unpack_from("<H", hdr, 34)[0]
                if bits != 8:
                    print(f"audio: {filename} is {bits}-bit; only 8-bit supported")
                    return

                chunk = bytearray(256)
                while True:
                    n = f.readinto(chunk)
                    if not n:
                        break
                    for i in range(n):
                        t = utime.ticks_us()
                        self._pwm.duty_u16(chunk[i] << 8)
                        while utime.ticks_diff(utime.ticks_us(), t) < period_us:
                            pass
        except OSError as e:
            print(f"audio: cannot play {filename}: {e}")
        finally:
            self._pwm.duty_u16(32768)   # return to mid-rail

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_wav(self, filename: str, max_seconds: int, hook) -> None:
        """
        Stream audio from the ADC into a WAV file until:
          - max_seconds have elapsed, or
          - the handset is replaced (hook.is_off_hook() returns False).

        hook must expose an is_off_hook() method (a KS0835 instance).
        """
        self._buf_ready = False
        self._buf_acked = False
        self._rec_done  = False
        self._fill_idx  = 0
        self._write_idx = 1

        max_chunks = (max_seconds * self._rate) // self.CHUNK

        _thread.start_new_thread(self._adc_thread, (max_chunks, hook))

        total_samples = 0
        try:
            with open(filename, "wb") as f:
                f.write(bytes(44))          # placeholder header

                while not self._rec_done or self._buf_ready:
                    if self._buf_ready:
                        # Grab the index Core 1 just finished
                        idx = self._write_idx
                        self._buf_acked = True   # signal Core 1 we have it
                        self._buf_ready = False

                        f.write(self._buf[idx])
                        total_samples += self.CHUNK
                    else:
                        utime.sleep_ms(1)

            # Back-fill the WAV header with correct sizes
            with open(filename, "r+b") as f:
                f.seek(0)
                f.write(_wav_header(total_samples, self._rate))

            print(f"audio: saved {total_samples} samples "
                  f"({total_samples // self._rate} s) → {filename}")
        except OSError as e:
            print(f"audio: record error: {e}")

    def _adc_thread(self, max_chunks: int, hook) -> None:
        """Core 1: sample the ADC into the inactive buffer at a fixed rate."""
        chunks = 0
        pos    = 0
        buf    = self._buf[self._fill_idx]

        while chunks < max_chunks and hook.is_off_hook():
            t = utime.ticks_us()

            # 12-bit ADC → 8-bit unsigned PCM
            buf[pos] = self._adc.read_u16() >> 8
            pos += 1

            if pos >= self.CHUNK:
                # Buffer full: signal Core 0 to flush it
                self._write_idx = self._fill_idx
                self._buf_ready = True

                # Swap to the other buffer
                self._fill_idx = 1 - self._fill_idx
                buf = self._buf[self._fill_idx]
                pos = 0
                chunks += 1

                # Wait for Core 0 to acknowledge before we signal again
                while not self._buf_acked:
                    pass
                self._buf_acked = False

            # Spin-wait for the next sample slot
            while utime.ticks_diff(utime.ticks_us(), t) < self._period_us:
                pass

        self._rec_done = True


# ------------------------------------------------------------------
# WAV header helper
# ------------------------------------------------------------------

def _wav_header(
    num_samples: int,
    sample_rate: int = 8000,
    channels:    int = 1,
    bits:        int = 8,
) -> bytes:
    """Build a 44-byte WAV file header for the given parameters."""
    byte_rate   = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    data_size   = num_samples * channels * (bits // 8)
    riff_size   = 36 + data_size
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", riff_size,
        b"WAVE",
        b"fmt ", 16,
        1,              # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data", data_size,
    )
