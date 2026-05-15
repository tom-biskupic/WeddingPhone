# Wedding Phone

A MicroPython guestbook for Raspberry Pi Pico that turns a vintage telephone into a voice message recorder for weddings and events.

When a guest lifts the handset, the phone plays a short sequence of telephone sounds (dial tone, DTMF digits, ring-back), then plays a recorded greeting and captures their message to a WAV file on an SD card. While idle, the phone rings periodically to attract guests' attention.

## Hardware

- **Raspberry Pi Pico**
- **KS0835 telephone line interface module** — drives the ringer and handles the hook switch; bridges the Pico to the phone line
- **Vintage telephone** (any analogue handset with a standard RJ11 interface)
- **MicroSD card** (FAT32 formatted) on SPI1
- RC filter on the PWM audio output (820 Ω + 47 nF to ground, 100 nF DC-blocking cap to line)
- Voltage-divider bias circuit on the ADC audio input (2 × 10 kΩ to 3.3 V, 100 nF AC coupling cap)

> **Schematic:** coming soon.

### Pin assignments

| Signal | Pin | Direction | Notes |
|---|---|---|---|
| HOOK | GP6 | IN | High when handset is lifted |
| RING_MODE | GP7 | OUT | High to enable ring circuit |
| FWD/REV | GP8 | OUT | Toggled at ring frequency |
| AUDIO_IN | GP15 | OUT | PWM → RC filter → phone line |
| AUDIO_OUT | GP26 | IN | ADC0 ← phone line (DC-biased to ~1.65 V) |
| SD SCK | GP10 | OUT | SPI1 |
| SD MOSI | GP11 | OUT | SPI1 |
| SD MISO | GP12 | IN | SPI1 |
| SD CS | GP13 | OUT | SPI1 |

## Software

All code runs on MicroPython for RP2040. Recording uses both cores: Core 1 samples the ADC at a fixed 8 kHz rate while Core 0 streams completed chunks to the SD card, so the sample clock never stalls on a slow write.

| File | Purpose |
|---|---|
| `main.py` | Application entry point and main loop |
| `audio.py` | PWM playback, DTMF/tone generation, dual-core ADC recording |
| `ks0835.py` | KS0835 driver — hook detection and ring control |
| `sdcard.py` | SPI SD card block driver |
| `debug.py` | Timestamped logging to serial and optionally to `/sd/debug.log` |

## Getting started

### 1. Install MicroPython

Flash the [MicroPython RP2040 UF2](https://micropython.org/download/RPI_PICO/) onto the Pico.

### 2. Install `mpremote`

```bash
pip install mpremote
```

### 3. Generate the greeting

Edit the default text in `makegreeting.sh` if needed, then run:

```bash
./makegreeting.sh
```

This synthesises speech with [edge-tts](https://github.com/rany2/edge-tts) (install with `pip install edge-tts`) and converts the output to 8-bit mono 8 kHz WAV with a 1 kHz beep appended.

To use a custom text or voice:

```bash
./makegreeting.sh "Your custom greeting here." en-AU-WilliamNeural
```

### 4. Deploy to the Pico

```bash
./deploy.sh
```

This copies all Python files and `greeting.wav` to the Pico and resets it.

## Configuration

Key settings are constants at the top of `main.py`:

| Constant | Default | Description |
|---|---|---|
| `ENABLE_SD` | `True` | Mount SD card and save recordings |
| `MAX_REC_SECS` | `60` | Maximum message length in seconds |
| `GREETING_FILE` | `"greeting.wav"` | Path to the greeting audio file |
| `DIAL_TONE_MS` | `1200` | Duration of the simulated dial tone |
| `DTMF_SEQUENCE` | `"0425"` | Digits "dialled" after the handset is lifted |
| `RING_BURSTS` | `1` | Ring-back cadences played before the greeting |
| `IDLE_RING_MIN_S` | `480` | Minimum idle time before ringing (8 min) |
| `IDLE_RING_MAX_S` | `720` | Maximum idle time before ringing (12 min) |
| `IDLE_RING_CYCLES` | `2` | AU cadence cycles per idle ring burst |
| `TEST_STARTUP_RING` | `True` | Ring once 3 s after boot — **set `False` before the event** |

## Recordings

Messages are saved as 8-bit mono 8 kHz PCM WAV files to `/sd/recordings/msg_NNNN.wav`, numbered sequentially. Copy them off the SD card after the event and play with any audio player (or convert with ffmpeg).

## Ring cadence

The Australian ring signal used for idle ringing is:

```
400 ms ON · 200 ms OFF · 400 ms ON · 2000 ms OFF  (repeat)
```

The phone rings for `IDLE_RING_CYCLES` cycles then stops, regardless of whether the handset is lifted. If the handset is lifted mid-ring the phone answers immediately.
