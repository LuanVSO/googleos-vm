#!/usr/bin/env bash
# Checks the Linux host for everything install.sh and run/launch.py need.
# Never installs anything or asks for sudo; prints what is missing.
set -u
cd "$(dirname "$0")"

ok=1
fail() { echo "  MISSING: $1"; ok=0; }

echo "Host checks"
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || fail "an x86_64 Linux host"

if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then echo "  /dev/kvm: ok"; else fail "read/write access to /dev/kvm (add yourself to the kvm group)"; fi

flags=$(grep -m1 '^flags' /proc/cpuinfo)
for f in gfni avx_vnni sha_ni; do
    case " $flags " in *" $f "*) ;; *) fail "CPU flag $f (the guest userspace needs GFNI, AVX-VNNI and SHA-NI)";; esac
done

tools="python3 curl unzip qemu-system-x86_64 qemu-img qemu-io swtpm mkfs.erofs dump.erofs debugfs lz4 modinfo modprobe nm readelf objcopy ninja pkg-config tar gcc"
for t in $tools; do command -v "$t" >/dev/null 2>&1 || fail "$t"; done

python3 -c 'import venv, ensurepip' 2>/dev/null || fail "python3 venv/ensurepip (Mesa build environment)"

if qemu-system-x86_64 -display help 2>/dev/null | grep -q '^gtk\|^sdl'; then :; else fail "a QEMU build with the gtk or sdl display"; fi
qemu-system-x86_64 -device help 2>/dev/null | grep -q 'virtio-vga-gl' || fail "a QEMU build with virtio-vga-gl (virglrenderer)"

avail_gb=$(df -Pk . | awk 'NR==2 {print int($4/1024/1024)}')
[ "$avail_gb" -ge 80 ] || fail "80 GB free disk space (have ${avail_gb} GB)"

if [ "$ok" = 1 ]; then echo "All prerequisites found."; else
    echo
    echo "Fedora: sudo dnf install qemu-system-x86 qemu-img swtpm erofs-utils e2fsprogs lz4 kmod binutils ninja-build pkgconf-pkg-config gcc unzip curl"
    echo "Debian/Ubuntu: sudo apt install qemu-system-x86 qemu-utils swtpm erofs-utils e2fsprogs lz4 kmod binutils ninja-build pkg-config gcc unzip curl python3-venv"
    exit 1
fi
