#!/usr/bin/env bash
# Radio-degrade the clean clips into scenarios/laguardia/audio/{mild,medium,brutal}/
# and build one stepped-on overlap clip, then write audio/manifest.json.
#
#   scripts/audio/degrade.sh
#
# Chain per level: band-pass (high-pass + low-pass), compression, pink noise
# (plus a short squelch burst at key-up and release), resample to 8 kHz mono.
# Needs ffmpeg/ffprobe and clean clips from scripts/audio/generate.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUDIO="$ROOT/scenarios/laguardia/audio"
CLEAN="$AUDIO/clean"
# Overlap: t06 (TRUCK1, the unclear call) stepped on by t07 (AC8646) 1.2 s in.
OVER_A=t06 OVER_B=t07 OVER_DELAY_MS=1200
OVERLAP_ID="overlap_${OVER_A}_${OVER_B}"

command -v ffmpeg >/dev/null || { echo "degrade.sh: ffmpeg not found (install ffmpeg)"; exit 1; }
shopt -s nullglob
clips=("$CLEAN"/t*.wav)
if [ ${#clips[@]} -eq 0 ]; then
  echo "degrade.sh: no clean clips in $CLEAN."
  echo "  Run: uv run python scripts/audio/generate.py   (or add --offline for placeholder voices)"
  exit 1
fi

# level -> "hp lp compressor_threshold ratio makeup noise_amp squelch_amp drive"
params() {
  case "$1" in
    mild)   echo "300 3000 0.125 3 2 0.06 0.15 1.0" ;;
    medium) echo "350 2800 0.0625 6 4 0.2 0.4 1.6" ;;
    brutal) echo "450 2400 0.03125 12 8 0.55 0.9 3.0" ;;
  esac
}

degrade() {  # $1=in.wav $2=out.wav $3=level
  local in="$1" out="$2" hp lp thr ratio makeup noise squelch drive dur
  read -r hp lp thr ratio makeup noise squelch drive <<<"$(params "$3")"
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$in")
  local total tail
  total=$(awk -v d="$dur" 'BEGIN{printf "%.3f", d+0.3}')
  tail=$(awk -v d="$dur" 'BEGIN{printf "%.3f", d+0.18}')
  # Voice padded 0.15 s each side; noise runs the full length; squelch bursts
  # (louder noise) on the first and last 0.12 s like a key-up/release.
  ffmpeg -hide_banner -loglevel error -y -i "$in" \
    -f lavfi -i "anoisesrc=color=pink:amplitude=$noise:sample_rate=22050:seed=7" \
    -f lavfi -i "anoisesrc=color=white:amplitude=$squelch:sample_rate=22050:seed=11" \
    -filter_complex "
      [0:a]aformat=sample_rates=22050:channel_layouts=mono,adelay=150,apad=pad_dur=0.15,
           highpass=f=$hp:poles=2,highpass=f=$hp:poles=2,lowpass=f=$lp:poles=2,lowpass=f=$lp:poles=2,
           volume=$drive,acompressor=threshold=$thr:ratio=$ratio:attack=5:release=80:makeup=$makeup,
           alimiter=limit=0.9[v];
      [1:a]atrim=0:$total,highpass=f=$hp,lowpass=f=$lp[n];
      [2:a]atrim=0:$total,highpass=f=$hp,lowpass=f=$lp,
           volume='if(lt(t,0.12)+gt(t,$tail),1,0)':eval=frame[s];
      [v][n][s]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95,
           aresample=8000,aformat=sample_fmts=s16:channel_layouts=mono[out]" \
    -map "[out]" "$out"
}

# Stepped-on overlap from the clean clips (both voices at once).
ffmpeg -hide_banner -loglevel error -y -i "$CLEAN/$OVER_A.wav" -i "$CLEAN/$OVER_B.wav" \
  -filter_complex "[1:a]adelay=$OVER_DELAY_MS,volume=0.9[b];[0:a][b]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95" \
  -ac 1 -ar 22050 -sample_fmt s16 "$CLEAN/$OVERLAP_ID.wav"
clips+=("$CLEAN/$OVERLAP_ID.wav")

for level in mild medium brutal; do
  mkdir -p "$AUDIO/$level"
  rm -f "$AUDIO/$level"/*.wav
  for c in "${clips[@]}"; do
    degrade "$c" "$AUDIO/$level/$(basename "$c")" "$level"
  done
  echo "degrade.sh: $level -> ${#clips[@]} clips"
done

cd "$ROOT"
OVERLAP_ID="$OVERLAP_ID" OVER_A="$OVER_A" OVER_B="$OVER_B" OVER_DELAY_MS="$OVER_DELAY_MS" \
  uv run --quiet python scripts/audio/manifest.py
