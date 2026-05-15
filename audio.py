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
  Core 1 – time-critical ADC sampling at a fixed rate (_sampler_thread).
  Core 0 – record_wav() streams completed chunks to the SD card.

Chunks travel Core 1 → Core 0 through a _LockedFIFO (protected by a
_thread lock).  Consumed chunks are returned to a free-buffer pool so no
heap allocation occurs during recording.
"""

import _thread
import math
import utime
import struct
from machine import PWM, ADC, Pin
from debug import log

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


def _mk_loop_buf(f1: float, f2: float, rate: int, f3: float = 0) -> bytearray:
    """
    Build the shortest cyclic PCM buffer that completes an integer number
    of cycles for all supplied frequencies.  Capped at 400 samples so
    computation stays fast even for awkward DTMF pairs.
    8-bit unsigned samples, amplitude 110/128 for headroom.
    """
    g = _gcd(rate, int(round(f1)))
    if f2:
        g = _gcd(g, int(round(f2)))
    if f3:
        g = _gcd(g, int(round(f3)))
    n = min(rate // g, 400)
    n = max(n, 40)

    freqs = [f for f in (f1, f2, f3) if f]
    buf = bytearray(n)
    for i in range(n):
        s = sum(math.sin(2.0 * math.pi * f / rate * i) for f in freqs)
        buf[i] = int(s / len(freqs) * 110) + 128
    return buf


# ---------------------------------------------------------------------------
# Recording infrastructure
# ---------------------------------------------------------------------------

_CHUNK_SAMPLES = 4096   # ADC samples per buffer chunk (~0.5 s at 8 kHz)
_NUM_CHUNKS    = 4      # pre-allocated chunks; covers ~2 s of write latency


class _LockedFIFO:
    """Thread-safe FIFO queue protected by a _thread lock."""

    def __init__(self, initial=()):
        self._lock = _thread.allocate_lock()
        self._q    = list(initial)

    def put(self, item):
        self._lock.acquire()
        self._q.append(item)
        self._lock.release()

    def get(self):
        """Return the next item, or None if the queue is empty."""
        self._lock.acquire()
        item = self._q.pop(0) if self._q else None
        self._lock.release()
        return item


def _sampler_thread(args):
    """
    Core 1 entry point.

    Draws free chunk buffers from free_q, fills them with ADC samples at a
    fixed period, and posts (buf, n) tuples onto data_q.  Releases done_lock
    when sampling ends so that record_wav() knows to stop draining data_q.
    """
    adc, period_us, max_samples, hook, free_q, data_q, done_lock = args

    total         = 0
    on_hook_count = 0
    hook_tick     = 0
    stop          = False

    while not stop:
        # Draw a free buffer; spin-wait if the writer hasn't returned one yet.
        buf = None
        while buf is None:
            buf = free_q.get()

        n         = 0
        chunk_len = len(buf)

        while n < chunk_len and total < max_samples:
            t = utime.ticks_us()
            buf[n] = adc.read_u16() >> 8
            n += 1
            total += 1

            hook_tick -= 1
            if hook_tick <= 0:
                hook_tick = 80
                if not hook.is_off_hook():
                    on_hook_count += 1
                    if on_hook_count >= 5:
                        stop = True
                        break
                else:
                    on_hook_count = 0

            while utime.ticks_diff(utime.ticks_us(), t) < period_us:
                pass

        if n:
            data_q.put((buf, n))

        if total >= max_samples:
            stop = True

    done_lock.release()  # signal record_wav() that sampling is complete


class AudioIO:
    WRITE_CHUNK = 4096  # bytes per SD write call – keep a multiple of 512

    def __init__(self, pwm_pin: int, adc_pin: int, sample_rate: int = 8000):
        self._rate      = sample_rate
        self._period_us = 1_000_000 // sample_rate
        self._pwm_pin   = pwm_pin

        # PWM carrier at 250 kHz; 8-bit resolution (duty 0–255 scaled to 0–65535)
        self._pwm = PWM(Pin(pwm_pin))
        self._pwm.freq(250_000)
        self._pwm.duty_u16(32768)   # mid-rail idle  (silence = 128 << 8)

        self._adc = ADC(Pin(adc_pin))

    # ------------------------------------------------------------------
    # Telephone tones
    # ------------------------------------------------------------------

    def _play_tone(self, f1: float, f2: float, duration_ms: int, f3: float = 0) -> None:
        """
        Play a one-, two-, or three-frequency tone for duration_ms via PWM.
        Uses a short cyclic buffer to avoid large allocations.
        """
        cycle     = _mk_loop_buf(f1, f2, self._rate, f3)
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
        """Australian dial tone: 400 + 425 + 450 Hz continuous."""
        self._play_tone(400.0, 425.0, duration_ms, f3=450.0)

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
                    log("audio: {} is not a valid WAV file".format(filename))
                    return
                file_rate = struct.unpack_from("<I", hdr, 24)[0]
                period_us = 1_000_000 // file_rate
                bits      = struct.unpack_from("<H", hdr, 34)[0]
                if bits != 8:
                    log("audio: {} is {}-bit; only 8-bit supported".format(filename, bits))
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
            log("audio: cannot play {}: {}".format(filename, e))
        finally:
            self._pwm.duty_u16(32768)   # return to mid-rail

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _apply_lowpass(self, buf, n_samples: int) -> None:
        """4th-order Butterworth LPF at 3 kHz (fs=8 kHz), two cascaded biquads, in-place."""
        # Coefficients from pre-warped bilinear transform
        # Stage 1: Q = 1.3066
        B0, B1, B2 = 0.6719, 1.3436, 0.6719
        A1, A2     = 1.1130, 0.5741
        # Stage 2: Q = 0.5412
        C0, C1, C2 = 0.5163, 1.0325, 0.5163
        D1, D2     = 0.8554, 0.2097

        v0  = float(buf[0])
        x1 = x2 = y1 = y2 = v0   # stage 1 delay lines
        u1 = u2 = w1 = w2 = v0   # stage 2 delay lines

        for i in range(n_samples):
            x  = float(buf[i])
            s  = B0*x  + B1*x1 + B2*x2 - A1*y1 - A2*y2
            x2, x1 = x1, x
            y2, y1 = y1, s
            t  = C0*s  + C1*u1 + C2*u2 - D1*w1 - D2*w2
            u2, u1 = u1, s
            w2, w1 = w1, t
            v = int(t + 0.5)
            buf[i] = 0 if v < 0 else (255 if v > 255 else v)

    def record_wav(self, filename: str, max_seconds: int, hook) -> None:
        """
        Record audio to a WAV file using dual-core streaming.

        Core 1 (_sampler_thread) samples the ADC at a fixed rate and posts
        chunk buffers onto data_q.  This method (Core 0) drains data_q and
        writes each chunk to the SD card, returning the buffer to free_q for
        reuse.  A done_lock (held here, released by the sampler) signals when
        sampling has finished so this method can stop draining.

        The WAV header is written as a placeholder first, then patched in-place
        via seek(0) once the total sample count is known.
        """
        chunk_pool = [bytearray(_CHUNK_SAMPLES) for _ in range(_NUM_CHUNKS)]
        free_q     = _LockedFIFO(chunk_pool)
        data_q     = _LockedFIFO()

        # done_lock: acquired (locked) here; sampler releases when done.
        # Main polls done_lock.acquire(False) — succeeds only after release.
        done_lock = _thread.allocate_lock()
        done_lock.acquire()

        args = (
            self._adc, self._period_us, max_seconds * self._rate, hook,
            free_q, data_q, done_lock,
        )
        _thread.start_new_thread(_sampler_thread, (args,))
        log("audio: recording started, streaming to {}".format(filename))

        total_written = 0
        sampler_done  = False

        try:
            with open(filename, "wb") as f:
                f.write(b'\x00' * 44)   # placeholder WAV header; patched below

                while True:
                    item = data_q.get()

                    if item is not None:
                        buf, n = item
                        mv     = memoryview(buf)
                        offset = 0
                        while offset < n:
                            end = min(offset + self.WRITE_CHUNK, n)
                            f.write(mv[offset:end])
                            offset = end
                        total_written += n
                        free_q.put(buf)   # return buffer to pool
                        continue

                    # Queue was empty.
                    if sampler_done:
                        break   # sampler done and queue fully drained
                    if done_lock.acquire(False):
                        sampler_done = True   # loop once more to drain any final items
                        continue
                    utime.sleep_us(500)

                # Patch the WAV header now that the total size is known.
                f.seek(0)
                f.write(_wav_header(total_written, self._rate))

        except OSError as e:
            log("audio: write error: {}".format(e))

        log("audio: saved {} s -> {}".format(total_written // self._rate, filename))


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
