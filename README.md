# googlevm

Runs a real Googlebook OS image (x86_64, Android 17) in a QEMU/KVM VM on a Linux host, with a virtual GPU (Mesa VirGL GLES, plus Venus Vulkan).

It is not an emulator image or a generic Android build. The install script downloads Google's own x86_64 recovery image, and at boot time swaps the hardware-specific bits for virtual ones. This is a Linux/x86_64 port of the approach in [gbos-vm](https://github.com/skylartaylor/gbos-vm), which targets the ARM64 image on Apple Silicon.

> Not affiliated with Google. It is not a verified Googlebook. The VM uses a software KeyMint, so keys are not hardware-backed; don't sign in with accounts you care about.

## Quick start

```bash
git clone <this repo> googlevm
cd googlevm
./install.sh
python3 run/launch.py
```

`install.sh` runs `prereqs.sh`, downloads the recovery image (about 7.7 GB, 19 GB extracted), downloads the kernel modules, Cuttlefish files, Android NDK and Mesa source, and builds the Mesa graphics libraries (several minutes, one time). It wants about 80 GB of free space and never asks for `sudo`. Re-running it skips finished steps. `python3 run/launch.py` also does the preparation on demand, so it works without `install.sh` as long as `work/image/` holds the image.

The first boot to the setup screen takes about a minute.

## What you need

- A **Linux x86_64 host** with **KVM** (`/dev/kvm` readable and writable by you).
- A CPU with **GFNI, AVX-VNNI and SHA-NI** (Intel 12th gen or newer, or recent AMD such as Zen 5).
- About 16 GB of RAM (the VM gets 8 GB by default).
- QEMU with `virtio-vga-gl` (virglrenderer) and the `gtk` or `sdl` display, `swtpm`, `erofs-utils`, `e2fsprogs` (`debugfs`), `lz4`, `kmod`, `binutils`, `ninja`, `pkg-config`, `gcc`, `python3` with `venv`, `curl`, `unzip`.

Run `./prereqs.sh` to see where you stand; it prints Fedora and Debian install lines for what is missing.

## Using it

```bash
python3 run/launch.py                 # prepare if needed, then boot
python3 run/launch.py --fullscreen    # guest resolution follows the window size
python3 run/launch.py --memory 12G --cpus 6
python3 run/launch.py --prepare-only  # build boot files without starting QEMU
```

The QEMU monitor is attached to the terminal; type `quit` to stop the VM. Serial output and mirrored logcat go to `work/build/serial.log`.

Other options: `--image PATH`, `--workdir PATH`, `--data-disk PATH`, `--data-disk-size 32G`, `--no-tpm`, `--boot-disk-bus virtio|nvme`, `--debug-init`, `--no-vm-keymint`, `--insecure-vm-userdata`.

The original image is never modified. It is attached in QEMU snapshot mode on top of a disposable qcow2 overlay, so system changes are discarded on exit. Your `/data` lives on a separate qcow2 disk, `work/build/data-disk.qcow2`. Encryption keys live in the discarded overlay, so the data disk is recreated on every `--vm-keymint` launch.

### Updating

After a `git pull`, run `./install.sh` again. The vendor build is cached by fingerprint and is rebuilt when the scripts or graphics libraries change.

### Layout

| Path | Purpose |
|---|---|
| `install.sh`, `prereqs.sh` | Host checks, image download, first-time preparation |
| `run/launch.py` | Extracts boot files, builds the initramfs, starts QEMU |
| `image/vm_keymint.py` | Rebuilds the vendor partition in the overlay (KeyMint, composer, Mesa, props) |
| `image/mesa_build.py` | Downloads the NDK and Mesa and builds the guest graphics libraries |
| `work/` | Everything generated or downloaded (gitignored): `image/`, `build/` |

## Virtual hardware

| Device | Notes |
|---|---|
| CPU/RAM | KVM, `--cpus 8`, `--memory 8G` |
| Disk | Image as NVMe (the boot-control HAL's SELinux policy only allows nvme/sd/mmc), data disk as virtio-blk |
| GPU | `virtio-vga-gl` with `blob=true,venus=true`, pinned to PCI `00:08.0` (the vendor SELinux label is tied to that address) |
| Input | `qemu-xhci` at `00:10.0` with `usb-kbd`, `usb-mouse`, `usb-tablet` |
| Network | `-net nic,model=virtio -net user,hostfwd=tcp::2222-:22` (NAT; host port 2222 to guest port 22) |
| Audio | `virtio-sound-pci` on a null backend, plus a PipeWire `ich9-intel-hda`/`hda-micro` (the guest kernel has no HDA driver, so this is inert) |
| TPM | swtpm-backed TPM 2.0 (`tpm-tis`), state in `work/build/tpm/` |

The guest display resolution follows the SDL/GTK window size through EDID, so maximize the window or use `--fullscreen`.

## System structure

The image starts as Google's unmodified recovery download. Googlebook OS expects hardware a VM lacks (a TPM with Trusty, a specific GPU), so the launcher:

- Extracts the kernel and ramdisk fragments from the image without changing it, and downloads matching `kernel_virt_x86_64` modules (and signed `virtio_pci`/`virtio_blk`) from Android CI build `16302624`, verifying checksums, vermagic and symbol CRCs, then adds them to a generated initramfs.
- Passes the `boot_a` slot, and computes the `vbmeta_a` digest so Android's AVB setup works with direct kernel boot.
- Rebuilds `vendor_a` inside the overlay (`--vm-keymint`, on by default) with:
  - Google's software KeyMint and the stock AOSP boot-control HAL, from Cuttlefish build 16373615 (about 1.1 GB, downloaded once). The StrongBox apex is disabled on the kernel command line.
  - AOSP's generic drm_hwcomposer, binary-patched to skip the `COLOR_PIPELINE` plane check that virtio-gpu lacks (`skip_color_pipeline_check`).
  - Mesa 26.2.4 built with the Android NDK r28c: VirGL GLES (`libEGL_virgl` and friends, `ro.hardware.egl=virgl`) and the Venus Vulkan driver (`vulkan.virtio.so`, `ro.hardware.vulkan=virtio`).
  - `/vendor` mounted without AVB (its hash no longer matches), and `/data` moved to the virtio data disk because the image's own 4 GiB userdata is filled by the first-boot odex copy.
  - Logcat mirrored to the serial log, kept minimal because serial output is slow.
- Relocates vendor into free space in `super` with re-hashed LP metadata if it no longer fits.

SurfaceFlinger and the UI render through Mesa GLES, not Vulkan.

## What doesn't work yet

- Whether Android actually gets a network lease on `eth0` has not been confirmed.
- No hardware-backed keys, battery, or working HDA audio (the guest kernel has no battery or HDA driver).
- Non-fatal noise in the logs: Trusty secretkeeper/weaver/authgraph aborts, audio/EC HAL errors, a timberslide crash, and `vrend` "RegionSampling" errors on the host.
- `--insecure-vm-userdata` only removes userdata encryption flags from the first-stage fstab and does not make the stock Trusty-dependent boot work; it is superseded by `--vm-keymint`. Do not use it for sensitive data.

## Notes

- `--skip-setup` marks provisioning complete and disables the setup wizard (offline desktop).
- `python3 tests/soak.py [runs] [timeout]` boots repeatedly as a smoke test.
- `GOOGLEVM_WORK` overrides the work directory.
- Ported from gbos-vm: quiet absent-hardware services (gscd, Trusty secretkeeper) and a trimmed diagnostic logcat filter.
- Not ported: macOS-specific pieces (viewer, Metal/MoltenVK patches, SELinux shm policy). Credits: skylartaylor/gbos-vm, Mesa, Cuttlefish. No LICENSE file is included; add one if you want.
