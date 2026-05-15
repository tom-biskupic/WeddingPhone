#!/usr/bin/env bash
set -euo pipefail

echo "Deploying Wedding Phone to Pico..."

mpremote cp sdcard.py ks0835.py audio.py main.py debug.py :
echo "  copied Python files"

if [ -f greeting.wav ]; then
    mpremote cp greeting.wav :greeting.wav
    echo "  copied greeting.wav"
else
    echo "  warning: greeting.wav not found, skipping"
fi

mpremote reset
echo "Done – Pico is restarting"
