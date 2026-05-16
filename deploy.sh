#!/usr/bin/env bash
set -euo pipefail

echo "Deploying Wedding Phone to Pico..."

mpremote cp sdcard.py ks0835.py audio.py main.py debug.py :
echo "  copied Python files"

greeting_count=0
for i in $(seq 1 10); do
    f="greeting${i}.wav"
    if [ -f "$f" ]; then
        mpremote cp "$f" ":${f}"
        echo "  copied $f"
        greeting_count=$((greeting_count + 1))
    fi
done
if [ -f greeting.wav ]; then
    mpremote cp greeting.wav :greeting.wav
    echo "  copied greeting.wav"
    greeting_count=$((greeting_count + 1))
fi
if [ "$greeting_count" -eq 0 ]; then
    echo "  warning: no greeting wav files found, skipping"
fi

mpremote reset
echo "Done – Pico is restarting"
