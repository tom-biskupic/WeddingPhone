"""
KS0835 telephone line interface module driver.

The module exposes five signals to the Pico; no SPI is involved:

  HOOK       (input)  – pulled low by the module when the handset is lifted
  RING_MODE  (output) – drive high to enable the ring circuitry
  FWD_REV    (output) – toggle at the ring frequency to modulate the ringer
  AUDIO_IN   (output) – analog audio from the Pico into the phone line
                        (driven by a PWM+RC-filter circuit, not wired here)
  AUDIO_OUT  (input)  – analog audio from the phone line to the Pico ADC
                        (read by the AudioIO class, not wired here)

Ring cadence (adjust constants to match local telephone standard):
  Australia / UK  – 400 ms on, 200 ms off, 400 ms on, 2 000 ms off
  USA / Canada    – 2 000 ms on, 4 000 ms off
"""

import utime
from machine import Pin, Timer


# Ring cadence in ms: list of (on_ms, off_ms) bursts per ring cycle
_CADENCE_AUS = [(400, 200), (400, 2000)]    # AU/UK double-burst
_CADENCE_USA = [(2000, 4000)]               # US single burst

# FWD/REV toggle period for the ringer (25 Hz ≈ 20 ms half-period)
_RING_HALF_PERIOD_MS = 20


class KS0835:
    def __init__(
        self,
        hook_pin:      int,
        ring_mode_pin: int,
        fwd_rev_pin:   int,
        cadence=_CADENCE_AUS,
        hook_active_low: bool = True,
    ):
        """
        hook_pin        – GPIO number connected to the module HOOK output
        ring_mode_pin   – GPIO number connected to the module RING_MODE input
        fwd_rev_pin     – GPIO number connected to the module FWD/REV input
        cadence         – ring cadence list; defaults to AU/UK pattern
        hook_active_low – True (default) if HOOK is active-low
        """
        pull = Pin.PULL_UP if hook_active_low else Pin.PULL_DOWN
        self._hook      = Pin(hook_pin, Pin.IN, pull)
        self._ring_mode = Pin(ring_mode_pin, Pin.OUT, value=0)
        self._fwd_rev   = Pin(fwd_rev_pin,   Pin.OUT, value=0)
        self._active_lo = hook_active_low
        self._cadence   = cadence
        self._ringing   = False

    # ------------------------------------------------------------------
    # Hook detection
    # ------------------------------------------------------------------

    def is_off_hook(self) -> bool:
        """Return True when the handset is lifted."""
        raw = self._hook.value()
        return (raw == 0) if self._active_lo else (raw == 1)

    def wait_off_hook(self, debounce_ms: int = 50) -> None:
        """Block until the handset is lifted (with debounce)."""
        while True:
            if self.is_off_hook():
                utime.sleep_ms(debounce_ms)
                if self.is_off_hook():
                    return
            utime.sleep_ms(5)

    def wait_on_hook(self, debounce_ms: int = 50) -> None:
        """Block until the handset is replaced (with debounce)."""
        while True:
            if not self.is_off_hook():
                utime.sleep_ms(debounce_ms)
                if not self.is_off_hook():
                    return
            utime.sleep_ms(5)

    # ------------------------------------------------------------------
    # Ringing
    # ------------------------------------------------------------------

    def ring(self, cycles: int = 2) -> None:
        """
        Ring the phone for the requested number of cadence cycles.
        Blocks for the full ring duration.  Returns early if the
        handset is lifted mid-ring.
        """
        self._ringing = True
        self._ring_mode.value(1)
        try:
            for _ in range(cycles):
                for on_ms, off_ms in self._cadence:
                    if not self._ringing or self.is_off_hook():
                        return
                    self._ring_burst(on_ms)
                    self._ring_mode.value(0)
                    utime.sleep_ms(off_ms)
                    self._ring_mode.value(1)
        finally:
            self.stop_ring()

    def stop_ring(self) -> None:
        """Immediately stop ringing."""
        self._ringing = False
        self._ring_mode.value(0)
        self._fwd_rev.value(0)

    def _ring_burst(self, duration_ms: int) -> None:
        """Toggle FWD/REV at ring frequency for duration_ms milliseconds."""
        end = utime.ticks_add(utime.ticks_ms(), duration_ms)
        state = 0
        while utime.ticks_diff(end, utime.ticks_ms()) > 0:
            self._fwd_rev.value(state)
            state ^= 1
            utime.sleep_ms(_RING_HALF_PERIOD_MS)
