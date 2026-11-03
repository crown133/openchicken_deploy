#!/bin/sh
# OpenChicken MK1 analog MEMS microphone, tested on 2026-10-25.
# The generic AIC3X driver calls the AIC3104 MIC2R input "Mic3R".
# Configure capture only; do not start recording or alter speaker controls.
set -eu
amixer -c aic3104 info >/dev/null
amixer -c aic3104 cset name='PGA Capture Switch' off,off >/dev/null
trap 'amixer -c aic3104 cset name="PGA Capture Switch" off,off >/dev/null 2>&1 || true' 0
for side in Left Right; do
    if [ "$side" = Left ]; then inputs='Line1L Line1R Line2L Mic3L Mic3R'
    else inputs='Line1L Line1R Line2R Mic3L Mic3R'
    fi
    for input in $inputs; do
        value=off
        if [ "$input" = Mic3R ]; then value=on; fi
        amixer -c aic3104 cset "name=$side PGA Mixer $input Switch" "$value" >/dev/null
    done
done
amixer -c aic3104 cset name='AGC Switch' off,off >/dev/null
amixer -c aic3104 cset name='PGA Capture Volume' 40,40 >/dev/null
amixer -c aic3104 cset name='PGA Capture Switch' on,on >/dev/null
trap - 0
printf '%s\n' 'MK1 capture route ready: both channels +20 dB from MK1; recording not started'
