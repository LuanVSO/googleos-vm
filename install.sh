#!/usr/bin/env bash
# Downloads Google's x86_64 Googlebook OS recovery image and prepares everything
# needed to boot it under QEMU/KVM. Safe to re-run; finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE_URL="https://dl.google.com/device/recovery/lapis-user/16452207/recovery.zip"
IMAGE_NAME="android-desktop_signed_recovery_image.bin"
IMAGE_DIR="work/image"

./prereqs.sh
mkdir -p "$IMAGE_DIR"

if [ ! -f "$IMAGE_DIR/$IMAGE_NAME" ]; then
    zip="$IMAGE_DIR/recovery.zip"
    echo "Downloading the recovery image (about 7.7 GB)..."
    curl -L -C - --fail -o "$zip" "$IMAGE_URL"
    echo "Extracting $IMAGE_NAME (about 19 GB)..."
    unzip -o -j "$zip" "$IMAGE_NAME" -d "$IMAGE_DIR"
    rm -f "$zip"
fi

echo "Preparing boot files, vendor overlay inputs and the Mesa graphics stack..."
python3 run/launch.py --prepare-only

echo
echo "Done. Start the VM with:  python3 run/launch.py"
