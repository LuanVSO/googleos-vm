# Boot the Android desktop recovery image

`boot_googlevm.py` prepares the local raw recovery image for QEMU/KVM. It extracts
the kernel and ramdisk fragments without modifying the image, downloads the
matching `kernel_virt_x86_64` modules from Android CI build `16302624`, checks
their published checksums, vermagic, and symbol CRCs. It downloads the signed
`virtio_pci` and `virtio_blk` modules from the `kernel_x86_64` target of the
same Android CI build, verifies their published checksums, and requires module
signer metadata. All modules are checked for matching vermagic and symbol CRCs
before being added to a generated VM initramfs.

## Requirements

- https://dl.google.com/device/recovery/lapis-user/16452207/recovery.zip
- Linux with `/dev/kvm` accessible to your user
- A CPU with GFNI, AVX-VNNI, and SHA-NI (12th-generation Intel or newer is
  expected; some AMD CPUs may not provide the needed AVX-VNNI instruction)
- `python3`, `lz4`, `modinfo`, `modprobe`, `nm`, `readelf`, `objcopy`,
  `qemu-img`, and `qemu-system-x86_64`
- Also: `swtpm`, `qemu-io`, `erofs-utils` (mkfs.erofs, dump.erofs), `debugfs`,
  `ninja`, `pkg-config`, `tar`, and a host C toolchain
- Network access on the first run. Only the original `.bin` is needed in this
  folder; the launcher downloads the kernel/modules (ci.android.com), the
  Cuttlefish KeyMint/composer files, the Android NDK r28c and Mesa 26.2.4
  (checksum-pinned), and builds the Mesa virgl GLES and Venus Vulkan libraries once into
  `googlevm-build/mesa-runtime` (several minutes, ~1GB of downloads).

## Usage

From this folder:

```sh
python3 boot_googlevm.py --prepare-only
python3 boot_googlevm.py
```

To debug early init/module loading, use `python3 boot_googlevm.py --debug-init`.
This adds verbose kernel logging and disables parallel module loading so the
serial log more clearly identifies which module load is failing. Debug output
is saved to `googlevm-build/serial.log`.

The image's hardware-backed Trusty KeyMint service is not available in a
standard QEMU VM. The opt-in
`python3 boot_googlevm.py --insecure-vm-userdata` removes userdata encryption
and block-checkpoint flags from the generated first-stage fstab only. The
original signed recovery image is unchanged, and guest writes to it remain
discarded when QEMU exits. This does not provide a replacement KeyMint service;
the supplied image may still stall while `vold` waits for KeyMint. The mode also
pre-formats userdata as ext4 in the disposable overlay, which lets init mount
`/data`, but the vendor fstab still triggers `vdc cryptfs encryptFstab`, which
blocks on keystore2 (verified: Android still does not reach zygote). Do not use
this mode for sensitive data.

The Android image disk is presented as NVMe by default (`--boot-disk-bus virtio`
to change) because the boot-control HAL's SELinux policy denies `vd_device`
block nodes.

A swtpm-backed TPM 2.0 (`tpm-tis`) is attached by default so `/dev/tpm0` exists
(`--no-tpm` disables it; swtpm must be installed). Its state and command log
are under `googlevm-build/tpm/`.

The first command prepares files under `googlevm-build/`; the second boots the
VM and exposes the QEMU monitor in the terminal. Serial output is saved to
`googlevm-build/serial.log`. The original recovery `.bin` is attached as a
virtio block device in QEMU snapshot mode, so guest writes to it are discarded
when QEMU exits. A separate persistent 128 GiB qcow2 data disk is created at
`googlevm-build/data-disk.qcow2` and attached as a second virtio block device.
Override its path or initial size with `--data-disk PATH` and
`--data-disk-size 32G`.

Use `--image PATH`, `--workdir PATH`, `--memory 12G`, or `--cpus 6` to override
defaults. The VM boots the `boot_a` kernel and uses `boot_a`'s matching
`init_boot_a` and platform `vendor_boot_a` ramdisks. The downloaded modules are
added to the generated initramfs; the signed recovery image itself is not
repacked or altered. The launcher also passes `androidboot.slot_suffix=_a`,
matching `boot_a` so first-stage fstab entries with `slotselect` resolve to the
correct partitions. Because QEMU starts the kernel directly instead of using
the device bootloader, the launcher also computes and passes the matching
`vbmeta_a` digest and size required by Android's AVB initialization. This keeps
AVB verification enabled while allowing first-stage filesystem mounts to use
the image's signed metadata.

This follows the linked bring-up notes. A successful kernel boot does not
guarantee that all Android desktop userspace components will work in QEMU.

## `--vm-keymint`: booting to the Android UI

The image only ships a Trusty KeyMint, which needs hardware a plain QEMU VM does
not have, so keystore2 never starts and boot stalls. Following the
[gbos-vm](https://github.com/skylartaylor/gbos-vm) approach,
`python3 boot_googlevm.py` (`--vm-keymint` is on by default; `--no-vm-keymint` disables it) rebuilds `vendor_a` inside the
throwaway qcow2 overlay (the original `.bin` is never modified) with:

- Google's software KeyMint and the stock AOSP boot-control HAL, taken from
  Cuttlefish build 16373615 (about 1.1 GB, downloaded once to
  `googlevm-build/cuttlefish/`); the StrongBox apex is disabled on the kernel
  command line;
- Mesa VirGL GLES libraries (`googlevm-build/mesa-runtime/`) in place of ANGLE
  over the Intel Vulkan driver, with matching graphics properties, so
  SurfaceFlinger works on QEMU's virtio-gpu;
- `/vendor` mounted without AVB (its hash no longer matches), and `/data` moved
  to the virtio data disk because the image's own 4 GiB userdata partition is
  filled by the first-boot odex copy;
- logcat mirrored to `serial.log`.

Because `/data` encryption keys live in the discarded `/metadata` overlay, the
data disk is recreated on every `--vm-keymint` launch. If vendor no longer
fits its partition it is relocated into free space in `super` with re-hashed
LP metadata. Status: the guest reaches `sys.boot_completed=1` and the launcher
runs. The Mesa libraries were built with the Android NDK (r28c) from Mesa
26.2.4 using `-Dgallium-drivers=virgl -Dvulkan-drivers=virtio (Venus, installed as vendor lib64/hw/vulkan.virtio.so, ro.hardware.vulkan=virtio) -Dplatforms=android`.

### Display (hwcomposer)

The stock `hwc3-service.drm` rejects virtio-gpu planes (no `COLOR_PIPELINE`
property), so `--vm-keymint` swaps in AOSP's generic drm_hwcomposer from the
Cuttlefish apex and binary-patches its color-pipeline check
(`skip_color_pipeline_check` in `vm_keymint.py`). `ro.vendor.hwcomposer.mode=client`
is set. For speed the guest logcat sent to the serial log is kept minimal
(serial output is slow) and `--cpus` defaults to 8.

### Window size and pointer

The guest display resolution follows the SDL window size (virtio-gpu reports
it through EDID), so the guest starts tiny if the window is small. Maximize
the window, or pass `--fullscreen`, to get the host resolution. The pointer is
a `virtio-mouse-pci` (relative).
