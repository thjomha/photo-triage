#!/usr/bin/env bash
# Downloads the two small face models used by OpenCV (about 38 MB in total).
#   YuNet  finds faces          (face_detection_yunet_2023mar.onnx)
#   SFace  recognises faces     (face_recognition_sface_2021dec.onnx)
set -euo pipefail

DIR="${FACES_MODELS:-$HOME/photo-triage-data/models}"
BASE="https://github.com/opencv/opencv_zoo/raw/main/models"
mkdir -p "$DIR"

fetch() {  # fetch <url> <file> <minimum size in bytes>
  local url="$1" file="$DIR/$2" min="$3"
  if [ -s "$file" ] && [ "$(stat -c %s "$file")" -gt "$min" ]; then echo "have $2"; return; fi
  echo "downloading $2 ..."
  wget -q -O "$file" "$url"
  if [ "$(stat -c %s "$file")" -le "$min" ]; then
    echo "ERROR: $2 is too small ($(stat -c %s "$file") bytes), the download probably failed." >&2
    rm -f "$file"; exit 1
  fi
}

fetch "$BASE/face_detection_yunet/face_detection_yunet_2023mar.onnx" face_detection_yunet_2023mar.onnx 100000
fetch "$BASE/face_recognition_sface/face_recognition_sface_2021dec.onnx" face_recognition_sface_2021dec.onnx 5000000
echo "Models are in $DIR"
