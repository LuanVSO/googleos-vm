"""Software KeyMint for the VM.

The Googlebook image expects hardware-backed Trusty KeyMint, which QEMU cannot
provide, so keystore2 never registers and boot stalls. This follows the approach
of the gbos-vm project (skylartaylor/gbos-vm, image/make_mica_security_port.py):
rebuild the vendor erofs with the AOSP software KeyMint taken from Google's
public Cuttlefish x86_64 build, and mount vendor without verified boot.

The recovery image is never modified; the rebuilt vendor partition is written
into the disposable qcow2 overlay.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

CUTTLEFISH_BUILD = "16373615"
CUTTLEFISH_ZIP = f"aosp_cf_x86_64_only_phone-img-{CUTTLEFISH_BUILD}.zip"
CUTTLEFISH_PAGE = (
    f"https://ci.android.com/builds/submitted/{CUTTLEFISH_BUILD}/"
    f"aosp_cf_x86_64_only_phone-userdebug/latest/{CUTTLEFISH_ZIP}"
)
KEYMINT_APEX = "/apex/com.android.hardware.keymint.rust_nonsecure.apex"
BOOT_APEX = "/apex/com.android.hardware.boot.apex"
COMPOSER_APEX = "/apex/com.android.hardware.graphics.composer.drm_hwcomposer.apex"
MTIME = 1230768000

# The virtio tablet reports absolute coordinates; Android only handles those as a
# touchscreen (the host cursor stays visible in the SDL window).
TABLET_IDC = b"""device.internal = 1
touch.deviceType = touchScreen
"""

KEYMINT_RC = b"""# AOSP software KeyMint: not hardware-backed.
service vendor.keymint-default /vendor/bin/hw/android.hardware.security.keymint-service
    class early_hal
    user nobody
    setenv LD_LIBRARY_PATH /vendor/lib64/vm_keymint:/vendor/lib64
"""


LOG_RC = b"""# Mirror framework and keystore logs to the serial console for diagnosis.
service vm-diagnostic-log /system/bin/sh -c "exec /system/bin/logcat -b all -v threadtime *:S keystore2:W SystemServer:I AndroidRuntime:E"
    class core
    user shell
    group shell log readproc
    seclabel u:r:shell:s0
    console ttyS0

on init
    start vm-diagnostic-log
"""


# apexd reads this from the kernel command line (ro.boot.vendor.apex.<name>); the
# StrongBox apex would otherwise declare a KeyMint device that keystore2 requires.
DISABLE_STRONGBOX_ARG = "androidboot.vendor.apex.com.android.hardware.keymint.strongbox.desktop=none"


class KeyMintError(RuntimeError):
    pass


def _run(arguments: list[str], **kwargs) -> bytes:
    try:
        return subprocess.run(
            arguments, check=True, capture_output=True, **kwargs
        ).stdout
    except FileNotFoundError as error:
        raise KeyMintError(f"Missing required utility: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip()
        raise KeyMintError(f"{arguments[0]} failed: {detail or error.returncode}") from error


def _unsparse(source: Path, dest: Path) -> None:
    with source.open("rb") as f, dest.open("wb") as g:
        magic, major, _, header_size, chunk_size, block, blocks, chunks, _ = struct.unpack(
            "<IHHHHIIII", f.read(28)
        )
        if magic != 0xED26FF3A or major != 1:
            raise KeyMintError("super.img is not an Android sparse image.")
        f.read(header_size - 28)
        for _ in range(chunks):
            kind, _, count, total = struct.unpack_from("<HHII", f.read(chunk_size))
            length = count * block
            if kind == 0xCAC1:
                while length:
                    data = f.read(min(length, 8 << 20))
                    g.write(data)
                    length -= len(data)
            elif kind == 0xCAC2:
                fill = f.read(4)
                if fill == bytes(4):
                    g.seek(length, 1)
                else:
                    pattern = fill * (1 << 18)
                    while length:
                        step = min(length, len(pattern))
                        g.write(pattern[:step])
                        length -= step
            elif kind == 0xCAC3:
                g.seek(length, 1)
            elif kind == 0xCAC4:
                f.read(4)
            else:
                raise KeyMintError(f"Unknown sparse chunk type {kind:#x}.")
        g.truncate(blocks * block)


def _partition_offset(raw: Path, name: str) -> int:
    u32 = lambda data, offset: struct.unpack_from("<I", data, offset)[0]
    with raw.open("rb") as f:
        f.seek(4096)
        geometry = f.read(4096)
        f.seek(12288)
        metadata = f.read(u32(geometry, 40))
    header_size = u32(metadata, 8)
    tables = metadata[header_size : header_size + u32(metadata, 44)]

    def table(offset: int):
        start, count, size = struct.unpack_from("<III", metadata, offset)
        return [tables[start + i * size : start + (i + 1) * size] for i in range(count)]

    extents = [struct.unpack_from("<QIQI", e) for e in table(92)]
    for partition in table(80):
        if partition[:36].split(b"\0")[0].decode() == name:
            _, first, count, _ = struct.unpack_from("<IIII", partition, 36)
            if count != 1:
                raise KeyMintError(f"{name} has more than one extent.")
            return extents[first][2] * 512
    raise KeyMintError(f"{name} not found in the Cuttlefish super image.")


def fetch_keymint_files(cache: Path) -> dict[str, bytes]:
    """Return the software KeyMint binary, its libcrypto and its VINTF fragments."""
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / "keymint-files.json"
    names = {
        "service": "keymint-service",
        "libcrypto": "libcrypto.so",
        "boot": "boot-service.default",
        "composer": "hwc3-service.drm",
        "reporter": "drm_hwcomposer_atom_reporter.so",
        "keymint.xml": "android.hardware.security.keymint-service.xml",
        "secureclock.xml": "android.hardware.security.secureclock-service.xml",
        "sharedsecret.xml": "android.hardware.security.sharedsecret-service.xml",
    }
    if manifest.is_file() and all((cache / n).is_file() for n in names.values()):
        return {key: (cache / n).read_bytes() for key, n in names.items()}

    archive = cache / CUTTLEFISH_ZIP
    if not archive.is_file():
        print(f"Downloading Cuttlefish build {CUTTLEFISH_BUILD} (about 1.1 GB)...")
        with urllib.request.urlopen(CUTTLEFISH_PAGE, timeout=60) as response:
            page = response.read(1 << 20).decode()
        match = re.search(r"var JSVariables = (\{.*?\});", page)
        url = json.loads(match.group(1))["artifactUrl"] if match else ""
        if not url:
            raise KeyMintError("ci.android.com did not return a download URL.")
        part = archive.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as out:
            while chunk := response.read(8 << 20):
                out.write(chunk)
        part.rename(archive)

    sparse = cache / "super.img"
    raw = cache / "super.raw"
    payload = cache / "keymint-payload.img"
    try:
        with zipfile.ZipFile(archive) as z:
            sparse.write_bytes(z.read("super.img"))
        _unsparse(sparse, raw)
        vendor = _partition_offset(raw, "vendor_a")
        package = _run(
            ["dump.erofs", f"--offset={vendor}", f"--path={KEYMINT_APEX}", "--cat", str(raw)]
        )
        payload.write_bytes(zipfile.ZipFile(io.BytesIO(package)).read("apex_payload.img"))
        boot_package = _run(
            ["dump.erofs", f"--offset={vendor}", f"--path={BOOT_APEX}", "--cat", str(raw)]
        )
        composer_package = _run(
            ["dump.erofs", f"--offset={vendor}", f"--path={COMPOSER_APEX}", "--cat", str(raw)]
        )
        composer_payload = cache / "composer-payload.img"
        composer_payload.write_bytes(
            zipfile.ZipFile(io.BytesIO(composer_package)).read("apex_payload.img")
        )
        boot_payload = cache / "boot-payload.img"
        boot_payload.write_bytes(
            zipfile.ZipFile(io.BytesIO(boot_package)).read("apex_payload.img")
        )

        def cat(path: str) -> bytes:
            data = _run(["debugfs", "-R", f"cat {path}", str(payload)])
            if not data:
                raise KeyMintError(f"{path} is empty in the KeyMint payload.")
            return data

        results = {
            "keymint-service": cat("/bin/hw/android.hardware.security.keymint-service.nonsecure"),
            "libcrypto.so": cat("/lib64/libcrypto.so"),
            "hwc3-service.drm": _run(
                ["debugfs", "-R", "cat /bin/hw/android.hardware.composer.hwc3-service.drm", str(composer_payload)]
            ),
            "drm_hwcomposer_atom_reporter.so": _run(
                ["debugfs", "-R", "cat /lib64/drm_hwcomposer_atom_reporter.so", str(composer_payload)]
            ),
            "boot-service.default": _run(
                ["debugfs", "-R", "cat /bin/hw/android.hardware.boot-service.default", str(boot_payload)]
            ),
        }
        for key in ("keymint", "secureclock", "sharedsecret"):
            name = f"android.hardware.security.{key}-service.xml"
            results[name] = cat(f"/etc/vintf/{name}")
    finally:
        for temporary in (sparse, raw, payload, cache / "boot-payload.img", cache / "composer-payload.img"):
            temporary.unlink(missing_ok=True)
    for name, data in results.items():
        (cache / name).write_bytes(data)
    manifest.write_text(
        json.dumps({n: hashlib.sha256(d).hexdigest() for n, d in results.items()}, indent=1)
    )
    return {key: results[n] for key, n in names.items()}


def _inode_metadata(image: Path, path: str, offset: int) -> dict:
    text = _run(["dump.erofs", f"--offset={offset}", f"--path={path}", str(image)]).decode()
    nid = int(re.search(r"NID: (\d+)", text)[1])
    with image.open("rb") as f:
        def read(position: int, size: int) -> bytes:
            f.seek(offset + position)
            return f.read(size)

        sb = read(1024, 128)
        block = 1 << sb[12]
        meta, xattr_base = struct.unpack_from("<II", sb, 40)
        position = meta * block + nid * 32
        inode = read(position, 64)
        fmt, xattr_count, mode = struct.unpack_from("<HHH", inode)
        extended = fmt & 1
        uid, gid = struct.unpack_from("<II" if extended else "<HH", inode, 24)
        epoch = struct.unpack_from("<Q", sb, 24)[0]
        mtime = (
            struct.unpack_from("<Q", inode, 32)[0]
            if extended
            else epoch + struct.unpack_from("<I", inode, 12)[0]
        )
        xattrs: dict[str, bytes] = {}

        def entry(at: int) -> int:
            name_len, index, value_len = struct.unpack("<BBH", read(at, 4))
            body = read(at + 4, name_len + value_len)
            prefix = {1: "user.", 4: "trusted.", 6: "security."}.get(index)
            if prefix is None:
                raise KeyMintError(f"Unsupported xattr prefix {index} on {path}.")
            xattrs[prefix + body[:name_len].decode()] = body[name_len:]
            return (4 + name_len + value_len + 3) & ~3

        if xattr_count:
            at = position + (64 if extended else 32)
            header = read(at, 12)
            shared = header[4]
            for i in range(shared):
                entry(xattr_base * block + 4 * struct.unpack("<I", read(at + 12 + i * 4, 4))[0])
            end = at + 12 + (xattr_count - 1) * 4
            at += 12 + shared * 4
            while at < end:
                at += entry(at)
    return {"mode": mode, "uid": uid, "gid": gid, "mtime": mtime, "xattrs": xattrs}


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc


GRAPHICS_FILES = {
    "lib64/egl/libEGL_virgl.so": "libEGL_virgl.so",
    "lib64/egl/libGLESv1_CM_virgl.so": "libGLESv1_CM_virgl.so",
    "lib64/egl/libGLESv2_virgl.so": "libGLESv2_virgl.so",
    "lib64/libgallium_dri.so": "libgallium_dri.so",
    "lib64/hw/vulkan.virtio.so": "vulkan.virtio.so",
}
# Mesa VirGL GLES for SurfaceFlinger/HWUI instead of ANGLE over the Intel Vulkan driver.
GRAPHICS_PROPS = {
    "ro.hardware.egl": "virgl",
    "ro.hardware.vulkan": "virtio",
    "ro.hwui.use_vulkan": "false",
    "debug.renderengine.backend": "skiaglthreaded",
    "debug.renderengine.vulkan": "false",
    "debug.renderengine.graphite_desktop_optin": "false",
    "debug.hwui.renderer": "skiagl",
    "ro.gfx.angle.supported": "false",
    "ro.surface_flinger.has_wide_color_display": "false",
    "ro.surface_flinger.has_HDR_display": "false",
    "ro.vendor.hwcomposer.mode": "client",
    "ro.vendor.hwc.drop_drm_master": "0",
}


def build_vendor(
    image: Path,
    vendor_offset: int,
    workdir: Path,
    graphics: Path | None = None,
    data_disk: bool = False,
) -> Path:
    """Return a vendor erofs containing the software KeyMint. Cached in workdir."""
    output = workdir / "vm-vendor.erofs"
    stamp = workdir / "vm-vendor.stamp"
    fingerprint = f"{image.stat().st_size}:{vendor_offset}:{Path(__file__).stat().st_mtime_ns}"
    fingerprint += f":dd{int(data_disk)}"
    if graphics is not None:
        fingerprint += ":" + ",".join(
            str((graphics / n).stat().st_mtime_ns) for n in sorted(GRAPHICS_FILES.values())
        )
    if output.is_file() and stamp.is_file() and stamp.read_text() == fingerprint:
        return output

    files = fetch_keymint_files(workdir / "cuttlefish")
    entries: dict[str, tuple[bytes, str, int]] = {
        "bin/hw/android.hardware.security.keymint-service": (
            files["service"], "hal_keymint_default_exec", 0o755,
        ),
        "lib64/vm_keymint/libcrypto.so": (files["libcrypto"], "vendor_file", 0o644),
        "etc/init/vm-software-keymint.rc": (KEYMINT_RC, "vendor_configs_file", 0o644),
        # The stock service calls ChromeOS firmware to mark the slot booted; without it
        # system_server reboots with "Checkpoint commit failed".
        "bin/hw/android.hardware.boot-service.android-desktop": (
            files["boot"], "hal_bootctl_default_exec", 0o755,
        ),
    }
    entries["usr/idc/QEMU_Virtio_Tablet.idc"] = (TABLET_IDC, "vendor_configs_file", 0o644)
    entries["etc/init/vm-diagnostic-log.rc"] = (LOG_RC, "vendor_configs_file", 0o644)
    contexts = "etc/selinux/vendor_file_contexts"
    contexts_meta = _inode_metadata(image, "/" + contexts, vendor_offset)
    contexts_body = _run(
        ["dump.erofs", f"--offset={vendor_offset}", f"--path=/{contexts}", "--cat", str(image)]
    )
    entries[contexts] = (
        contexts_body + b"\n/dev/ttyS0 u:object_r:console_device:s0\n"
        + b"/sys/devices/pci0000:00/0000:00:08\\.0(/.*)? u:object_r:sysfs_gpu:s0\n"
        + (b"/dev/block/vda u:object_r:userdata_block_device:s0\n" if data_disk else b""),
        contexts_meta["xattrs"]["security.selinux"].decode().split(":")[2],
        contexts_meta["mode"] & 0o7777,
    )
    if data_disk:
        fstab = "etc/fstab.android-desktop"
        fstab_meta = _inode_metadata(image, "/" + fstab, vendor_offset)
        fstab_body = _run(
            ["dump.erofs", f"--offset={vendor_offset}", f"--path=/{fstab}", "--cat", str(image)]
        )
        entries[fstab] = (
            userdata_on_data_disk(fstab_body),
            fstab_meta["xattrs"]["security.selinux"].decode().split(":")[2],
            fstab_meta["mode"] & 0o7777,
        )
    if graphics is not None:
        # The stock composer insists on a COLOR_PIPELINE plane property that virtio-gpu
        # lacks, so no display pipeline is ever created. AOSP's generic DRM composer works.
        entries["bin/hw/android.hardware.composer.hwc3-service.drm"] = (
            skip_color_pipeline_check(files["composer"]),
            "hal_graphics_composer_default_exec", 0o755,
        )
        entries["lib64/drm_hwcomposer_atom_reporter.so"] = (
            files["reporter"], "same_process_hal_file", 0o644,
        )
        for target, source in GRAPHICS_FILES.items():
            entries[target] = ((graphics / source).read_bytes(), "same_process_hal_file", 0o644)
        props_meta = _inode_metadata(image, "/build.prop", vendor_offset)
        props_text = _run(
            ["dump.erofs", f"--offset={vendor_offset}", "--path=/build.prop", "--cat", str(image)]
        ).decode()
        kept = [l for l in props_text.splitlines() if l.split("=", 1)[0] not in GRAPHICS_PROPS]
        kept += ["# Mesa VirGL for the virtual GPU."]
        kept += [f"{k}={v}" for k, v in GRAPHICS_PROPS.items()]
        entries["build.prop"] = (
            ("\n".join(kept) + "\n").encode(),
            props_meta["xattrs"]["security.selinux"].decode().split(":")[2],
            props_meta["mode"] & 0o7777,
        )
    for key in ("keymint", "secureclock", "sharedsecret"):
        entries[f"etc/vintf/manifest/vm-android.hardware.security.{key}-service.xml"] = (
            files[f"{key}.xml"], "vendor_configs_file", 0o644,
        )

    with image.open("rb") as f:
        f.seek(vendor_offset + 1024)
        superblock = f.read(128)
        if superblock[:4] != bytes.fromhex("e2e1f5e0"):
            raise KeyMintError("vendor_a is not an erofs filesystem.")
        original_size = struct.unpack_from("<I", superblock, 36)[0] * 4096
        f.seek(vendor_offset)
        output.write_bytes(f.read(original_size))

    overlay = workdir / "vm-vendor-overlay.tar"
    existing = [".", "bin", "bin/hw", "etc", "etc/init", "etc/selinux", "etc/vintf", "etc/vintf/manifest", "lib64", "lib64/hw", "usr", "usr/idc"]
    with tarfile.open(overlay, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in existing + ["lib64/vm_keymint"] + (["lib64/egl"] if graphics else []):
            if name in ("lib64/vm_keymint", "lib64/egl"):
                meta = {
                    "mode": 0o40755, "uid": 0, "gid": 2000, "mtime": MTIME,
                    "xattrs": {"security.selinux": b"u:object_r:vendor_file:s0"},
                }
            else:
                meta = _inode_metadata(
                    image, "/" if name == "." else "/" + name, vendor_offset
                )
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = meta["mode"] & 0o7777
            info.uid, info.gid, info.mtime = meta["uid"], meta["gid"], meta["mtime"]
            info.pax_headers = (
                {}
                if name == "."
                else {"SCHILY.xattr." + k: v.decode() for k, v in meta["xattrs"].items()}
            )
            tar.addfile(info)
        for name, (body, label, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(body), mode, MTIME
            info.pax_headers = {"SCHILY.xattr.security.selinux": f"u:object_r:{label}:s0"}
            tar.addfile(info, io.BytesIO(body))

    _run([
        "mkfs.erofs", "--incremental=data", "--tar=f", "-x0", "-b4096",
        f"-T{MTIME}", "-zlz4hc", str(output), str(overlay),
    ])

    # Incremental tar import drops the root inode's xattr table reference; restore it.
    root_nid = struct.unpack_from("<H", superblock, 14)[0]
    meta_block = struct.unpack_from("<I", superblock, 40)[0]
    root = meta_block * 4096 + root_nid * 32
    with image.open("rb") as f:
        f.seek(vendor_offset + root)
        old = f.read(48)
    with output.open("r+b") as f:
        f.seek(root)
        new = f.read(48)
        if old[2:4] != new[2:4] or old[0] & 1 or new[0] & 1:
            raise KeyMintError("Unexpected root inode layout; cannot restore its xattrs.")
        f.seek(root + 32)
        f.write(old[32:48])
        f.seek(1024)
        block = bytearray(f.read(3072))
        block[4:8] = bytes(4)
        f.seek(1028)
        f.write(struct.pack("<I", _crc32c(bytes(block))))

    _run(["fsck.erofs", str(output)])
    for name, (body, _, _) in entries.items():
        stored = _run(["dump.erofs", f"--path=/{name}", "--cat", str(output)])
        if stored != body:
            raise KeyMintError(f"Verification of {name} in the rebuilt vendor failed.")
    overlay.unlink(missing_ok=True)
    stamp.write_text(fingerprint)
    return output


USERDATA_BLOCK = b"/dev/block/by-name/userdata"
DATA_DISK_BLOCK = b"/dev/block/vda"


def userdata_on_data_disk(fstab: bytes) -> bytes:
    """Point /data at the virtio data disk (same-size rewrite). The image's own
    userdata partition is 4 GiB, which the first-boot odex copy fills completely."""
    pattern = re.compile(re.escape(USERDATA_BLOCK) + rb"( +/data )")
    pad = b" " * (len(USERDATA_BLOCK) - len(DATA_DISK_BLOCK))
    patched, count = pattern.subn(lambda m: DATA_DISK_BLOCK + pad + m.group(1), fstab)
    if count < 1:
        raise KeyMintError("No /data fstab entry found to move to the data disk.")
    return patched


def skip_color_pipeline_check(composer: bytes) -> bytes:
    """Make the DRM composer accept planes without COLOR_PIPELINE (virtio-gpu has none).

    The kernel advertises the color-pipeline client cap, after which the composer
    rejects every plane lacking the property. Turn the guarding `jne` into a `jmp`."""
    offset, original = 0x2FA69, bytes.fromhex("0f8534030000")
    if composer[offset : offset + 6] != original:
        raise KeyMintError("Unexpected DRM composer build; cannot patch the plane check.")
    patched = bytearray(composer)
    patched[offset : offset + 6] = bytes.fromhex("e935030000") + b"\x90"
    return bytes(patched)


def drop_vendor_avb(platform_ramdisk: bytes) -> bytes:
    """Mount /vendor without dm-verity (its hash no longer matches); same size rewrite."""
    needle = re.compile(rb"^(vendor /vendor [^\n]*?),avb=vbmeta", re.M)
    patched, count = needle.subn(lambda m: m.group(1) + b" " * len(b",avb=vbmeta"), platform_ramdisk)
    if count < 1:
        raise KeyMintError("The first-stage fstab has no vendor avb entry to relax.")
    return patched


def plan_vendor_relocation(
    image: Path, super_start: int, super_size: int, vendor_size: int
) -> tuple[int, int, list[tuple[int, bytes]]]:
    """Place vendor_a in unused space of the super partition.

    Returns (absolute byte offset, length, [(absolute offset, new LP metadata slot)]).
    Every metadata slot is re-hashed after pointing vendor_a's extent at the new
    location; nothing is written here.
    """
    u32 = lambda data, offset: struct.unpack_from("<I", data, offset)[0]
    writes: list[tuple[int, bytes]] = []
    ranges: list[tuple[int, int]] = []
    with image.open("rb") as f:
        f.seek(super_start + 4096)
        geometry = f.read(4096)
        geometry_body = bytearray(geometry[: u32(geometry, 4)])
        geometry_body[8:40] = bytes(32)
        if hashlib.sha256(geometry_body).digest() != geometry[8:40]:
            raise KeyMintError("LP geometry checksum mismatch.")
        slot_size, slot_count, block = struct.unpack_from("<III", geometry, 40)
        extent_fields = []
        for slot in range(slot_count * 2):
            position = super_start + 12288 + slot * slot_size
            f.seek(position)
            metadata = bytearray(f.read(slot_size))
            if u32(metadata, 0) != 0x414C5030:
                raise KeyMintError("LP metadata slot has a bad magic.")
            header_size = u32(metadata, 8)
            header = bytearray(metadata[:header_size])
            header[12:44] = bytes(32)
            if hashlib.sha256(header).digest() != metadata[12:44]:
                raise KeyMintError("LP metadata header checksum mismatch.")
            tables_size = u32(metadata, 44)
            if hashlib.sha256(metadata[header_size : header_size + tables_size]).digest() != metadata[48:80]:
                raise KeyMintError("LP metadata tables checksum mismatch.")
            part_off, part_count, part_size = struct.unpack_from("<III", metadata, 80)
            ext_off, ext_count, ext_size = struct.unpack_from("<III", metadata, 92)
            for i in range(ext_count):
                sectors, kind, target, _ = struct.unpack_from(
                    "<QIQI", metadata, header_size + ext_off + i * ext_size
                )
                if kind != 0:
                    raise KeyMintError("Unsupported LP extent type.")
                if sectors:
                    ranges.append((super_start + target * 512, super_start + (target + sectors) * 512))
            found = []
            for i in range(part_count):
                at = header_size + part_off + i * part_size
                if metadata[at : at + 36].split(b"\0")[0] == b"vendor_a":
                    first, count = struct.unpack_from("<II", metadata, at + 40)
                    if count != 1:
                        raise KeyMintError("vendor_a has more than one extent.")
                    found.append(header_size + ext_off + first * ext_size)
            if len(found) != 1:
                raise KeyMintError("vendor_a not found in an LP metadata slot.")
            extent_fields.append((position, metadata, header_size, tables_size, found[0]))

    align = 1 << 20
    start = (max(end for _, end in ranges) + align - 1) // align * align
    length = (vendor_size + block - 1) // block * block
    if start + length > super_start + super_size:
        raise KeyMintError("No room in super for the rebuilt vendor partition.")
    for position, metadata, header_size, tables_size, extent in extent_fields:
        struct.pack_into("<QIQI", metadata, extent, length // 512, 0, (start - super_start) // 512, 0)
        metadata[48:80] = hashlib.sha256(metadata[header_size : header_size + tables_size]).digest()
        metadata[12:44] = bytes(32)
        metadata[12:44] = hashlib.sha256(metadata[:header_size]).digest()
        writes.append((position, bytes(metadata)))
    return start, length, writes
