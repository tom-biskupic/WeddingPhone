#!/usr/bin/env bash
set -euo pipefail

_DEFAULT_TEXT="Hello! Thank you for coming to Kirrily and Jack's wedding. Please leave us a message after the tone."
TEXT="${1:-$_DEFAULT_TEXT}"
VOICE="${2:-en-AU-NatashaNeural}"
OUTPUT="${3:-greeting.wav}"

source=$(mktemp --suffix=.wav)
trap 'rm -f "$source"' EXIT

echo "Synthesising with voice: $VOICE"
edge-tts --rate=+25% --voice "$VOICE" --text "$TEXT" --write-media "$source"

echo "Converting to 8-bit mono 8 kHz and adding beep..."
ffmpeg -y \
    -i "$source" \
    -f lavfi -i "sine=frequency=1000:duration=0.5" \
    -filter_complex \
        "[0:a]aresample=8000,aformat=sample_fmts=u8:channel_layouts=mono,apad=pad_dur=0.4[speech];
         [1:a]aresample=8000,aformat=sample_fmts=u8:channel_layouts=mono[beep];
         [speech][beep]concat=n=2:v=0:a=1[out]" \
    -map "[out]" -acodec pcm_u8 "$OUTPUT"

echo "Saved to $OUTPUT"
