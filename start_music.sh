#!/bin/bash
set -euo pipefail

LOG=/root/VibeChat/logs/sonic-pi-headless.log
exec >>"$LOG" 2>&1

echo "==== $(date) starting headless music stack ===="

export DISPLAY=:1

cleanup() {
  echo "Stopping headless stack..."
  pkill -TERM -f "ffmpeg -loglevel" || true
  pkill -TERM -f "sonic-pi" || true
  pkill -TERM -f "jackd -d dummy" || true
  pkill -TERM -f "x11vnc -display :1" || true
  pkill -TERM -f "fluxbox" || true
  pkill -TERM -f "Xvfb :1" || true
  sleep 2
  pkill -KILL -f "ffmpeg -loglevel|sonic-pi|jackd -d dummy|x11vnc -display :1|fluxbox|Xvfb :1" || true
}

trap cleanup EXIT TERM INT

Xvfb :1 -screen 0 1280x800x24 &
sleep 2

DISPLAY=:1 openbox --sm-disable &
x11vnc -display :1 -nopw -listen localhost -forever -bg

jackd -d dummy -r 44100 -p 1024 &
sleep 3

DISPLAY=:1 sonic-pi &
echo "Waiting for Sonic Pi audio ports..."
for i in $(seq 1 60); do
  if jack_lsp 2>/dev/null | grep -Eq 'SuperCollider:out_1|Sonic Pi:out_1'; then
    echo "Sonic Pi JACK ports found."
    break
  fi
  sleep 1
done


while true; do
  echo "Starting ffmpeg SRT listener on :5000"
  ffmpeg -nostdin -loglevel warning -f jack -i ffmpeg -c:a aac -b:a 192k -ar 48000 -ac 2 -frame_size 1024: -f mpegts srt://0.0.0.0:5000?mode=listener &
  echo "ffmpeg exited, retrying in 2s"
  sleep 2
done &

echo "Waiting for ffmpeg JACK ports..."
for i in $(seq 1 30); do
  if jack_lsp 2>/dev/null | grep -q 'ffmpeg:input_1'; then
    echo "ffmpeg JACK ports found."
    break
  fi
  sleep 1
done

jack_connect "SuperCollider:out_1" "ffmpeg:input_1" || true
jack_connect "SuperCollider:out_2" "ffmpeg:input_2" || true

echo "Headless stack started."
while true; do
  sleep 30
done
