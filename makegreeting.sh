#!/usr/bin/env bash
set -euo pipefail

VOICE="${1:-en-AU-NatashaNeural}"

GREETINGS=(
    "greeting1.wav=Hello! Thank you for coming to Kirrily and Jack's wedding. Please leave us a message after the tone."
    "greeting2.wav=You have reached the Kirrily and Jack hotline. Please leave your message after the tone. Or after your third glass of champagne. Whichever comes first."
    "greeting3.wav=Hi! Kirrily and Jack can't come to the phone right now because they are currently getting married. Leave them some advice for survival."
    "greeting4.wav=Warning: This message is being recorded for quality, training, and blackmail purposes. Please leave your marital advice or gossip now."
    "greeting5.wav=Welcome to Kirrily and Jack's final escape room. The door is locked. The contract is signed. There are no hints, and the timer is set for the next fifty years. Leave your advice on how to solve the first puzzle after the beep"
    "greeting6.wav=Thank you for calling. Kirrily and Jack have successfully escaped single life. To unlock the voicemail system, please state your name, your relation to the couple, and where Jack hid the keys to the motorbikes"
    "greeting7.wav=Welcome to the register. Kirrily is a school teacher, so this voicemail will be graded. Please speak clearly, use full sentences, and don't make Jack read the instructions."
    "greeting8.wav=Welcome to the audio register. Please leave a message for Kirrily and Jack. Note: All emotional and sentimental statements will be reviewed by Jack for cost-benefit analysis before being enjoyed."
    "greeting9.wav=Kirrily and Jack can't come to the phone, but if you hop 3 times, spin around, touch your nose, and say your name, then leave them a message after the beep, they might get back to you after the honeymoon."
    "greeting10.wav=Hi! You've reached the newlywed hotline. Kirrily and Jack are currently being showered with love, so leave a message with your best wishes, marriage advice, or blackmail material for later."
)

for PAIR in "${GREETINGS[@]}"; do
    OUTPUT="${PAIR%%=*}"
    TEXT="${PAIR#*=}"

    source=$(mktemp)
    mv "$source" "${source}.wav"
    source="${source}.wav"

    cleanup() { rm -f "$source"; }
    trap cleanup EXIT

    echo "Synthesising: $OUTPUT"
    echo "  Voice: $VOICE"
    echo "  Text:  $TEXT"
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
    echo ""
done
