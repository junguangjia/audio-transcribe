#!/bin/sh
if [ -d "$HOME/Applications/AudioTranscribe.app" ]; then
    exec /usr/bin/open "$HOME/Applications/AudioTranscribe.app"
fi
audio_code_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
if [ -d "$audio_code_root/native/build/AudioTranscribe.app" ]; then
    exec /usr/bin/open "$audio_code_root/native/build/AudioTranscribe.app"
fi
printf '%s\n' 'Build the app with .venv/bin/python scripts/build-app.py first.' >&2
exit 1
