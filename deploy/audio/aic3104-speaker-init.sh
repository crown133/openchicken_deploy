#!/bin/sh
# Speaker only. Do not change capture routing or microphone gain.
# Mixer ranges verified against the vendored tlv320aic3x.c:
# PCM 102 = ALSA 80% (-12.5 dB); line DAC 118 = 0 dB; line output gain 0 = 0 dB.
set -eu
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if amixer -c aic3104 info >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ "$ready" = 1 ] || { printf '%s\n' 'aic3104 sound card not found' >&2; exit 1; }

# Mute the output while setting the playback path; unmute only after success.
amixer -c aic3104 cset name='Line Playback Switch' off,off >/dev/null
trap 'amixer -c aic3104 cset name="Line Playback Switch" off,off >/dev/null 2>&1 || true' 0
amixer -c aic3104 cset name='PCM Playback Volume' 102,102 >/dev/null
amixer -c aic3104 cset name='Line DAC Playback Volume' 118,118 >/dev/null
amixer -c aic3104 cset name='Line Playback Volume' 0,0 >/dev/null
amixer -c aic3104 cset name='Line Playback Switch' on,on >/dev/null
trap - 0
printf '%s\n' 'aic3104 speaker ready: ALSA 80% (-12.5 dB), line gain 0 dB; no sound played'
