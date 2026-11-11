#!/usr/bin/env bash
# Builds a synthetic 16:9 test video with REAL (synthesised) French and English speech.
# Used to exercise transcription, signal detection and rendering without any copyrighted footage.
#   scripts/make-fixture.sh [output.mp4]
set -euo pipefail
out="${1:-$(cd "$(dirname "$0")/.." && pwd)/data/fixtures/sample.mp4}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$(dirname "$out")"

say_to() { # voice, file, text — falls back to the default voice if the requested one is missing
  say -v "$1" -o "$tmp/$2.aiff" "$3" 2>/dev/null || say -o "$tmp/$2.aiff" "$3"
}
say_to Thomas a "Bon les gars, on est sur le serveur FiveM. Je prends la voiture et on file vers le commissariat."
say_to Thomas b "Attends, attends, il y a un policier juste devant. Non non non, freine, freine !"
say_to Daniel c "Oh my god, you just crashed into the police car. That was the worst escape I have ever seen."
say_to Thomas d "Mais non, c'est lui qui a reculé ! Monsieur l'agent, je vous jure que c'est un malentendu."
say_to Daniel e "Sir, step out of the vehicle. You are under arrest for destroying my lunch."
say_to Thomas f "Son déjeuner ? Il m'arrête pour un sandwich ! C'est le meilleur serveur RP de ma vie."

# Timeline (seconds): speech lines separated by pauses, with a loud impact at 21s (the "crash").
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=64" \
  -i "$tmp/a.aiff" -i "$tmp/b.aiff" -i "$tmp/c.aiff" -i "$tmp/d.aiff" -i "$tmp/e.aiff" -i "$tmp/f.aiff" \
  -f lavfi -i "anoisesrc=color=brown:duration=0.6:amplitude=0.9" \
  -f lavfi -i "anoisesrc=color=pink:duration=64:amplitude=0.004" \
  -filter_complex "
    [1:a]adelay=3000:all=1[a];[2:a]adelay=13000:all=1[b];[3:a]adelay=24000:all=1[c];
    [4:a]adelay=34000:all=1[d];[5:a]adelay=44000:all=1[e];[6:a]adelay=53000:all=1[f];
    [7:a]adelay=21000:all=1,afade=t=out:st=21.2:d=0.4[hit];
    [a][b][c][d][e][f][hit][8:a]amix=inputs=8:normalize=0:duration=longest,aresample=48000[aout];
    [0:v]drawbox=x='mod(t*220,1280)':y=300:w=120:h=70:color=red@0.9:t=fill,
         drawbox=enable='between(t,21,22)':x=0:y=0:w=1280:h=720:color=white@0.5:t=fill[vout]" \
  -map "[vout]" -map "[aout]" -t 64 -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k "$out"
echo "$out"
