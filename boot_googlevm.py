#!/usr/bin/env python3
"""Prepare and boot the local Android desktop recovery image under QEMU/KVM."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mesa_build
import vm_keymint


SECTOR_SIZE = 512
CI_BUILD_ID = "16302624"
CI_TARGET = "kernel_virt_x86_64"
STORAGE_MODULE_TARGET = "kernel_x86_64"
KERNEL_RELEASE = "6.18.21-android17-5-g3938c8d97142-ab16302624"
CI_MODULE_NAMES = (
    "virtio-gpu.ko",
    "virtio_dma_buf.ko",
    "virtio_input.ko",
    "virtio_net.ko",
    "net_failover.ko",
    "failover.ko",
    "virtio_snd.ko",
    "virtio-rng.ko",
)
STORAGE_MODULE_NAMES = (
    "virtio_pci_legacy_dev.ko",
    "virtio_pci_modern_dev.ko",
    "virtio_pci.ko",
    "virtio_blk.ko",
)
MODULE_NAMES = STORAGE_MODULE_NAMES + CI_MODULE_NAMES
SYMVERS_NAME = "vmlinux.symvers"
REQUIRED_CPU_FLAGS = {"gfni", "avx_vnni", "sha_ni"}
CI_LIST_URL = "https://ci.android.com/api/buildartifacts/list"


class BootError(RuntimeError):
    """An actionable error in image preparation or VM startup."""


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def read_exact(file_obj, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0:
        raise BootError(f"Invalid {label} range.")
    file_obj.seek(offset)
    data = file_obj.read(size)
    if len(data) != size:
        raise BootError(f"Image ended while reading {label}.")
    return data


def read_gpt_partitions(image: Path) -> dict[str, tuple[int, int]]:
    image_size = image.stat().st_size
    with image.open("rb") as source:
        header = read_exact(source, SECTOR_SIZE, SECTOR_SIZE, "GPT header")
        if header[:8] != b"EFI PART":
            raise BootError("The input image does not contain a GPT header.")
        entries_lba = struct.unpack_from("<Q", header, 72)[0]
        entry_count, entry_size = struct.unpack_from("<II", header, 80)
        if not 1 <= entry_count <= 4096 or entry_size < 128 or entry_size > 4096:
            raise BootError("The GPT partition table has invalid dimensions.")
        table_size = entry_count * entry_size
        if entries_lba * SECTOR_SIZE + table_size > image_size:
            raise BootError("The GPT partition table extends beyond the image.")
        table = read_exact(
            source, entries_lba * SECTOR_SIZE, table_size, "GPT partition table"
        )

    partitions: dict[str, tuple[int, int]] = {}
    for index in range(entry_count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        if entry[:16] == b"\0" * 16:
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        if last_lba < first_lba:
            raise BootError("The GPT contains a partition with invalid bounds.")
        name = entry[56:128].decode("utf-16le", errors="strict").split("\0", 1)[0]
        if name:
            if name in partitions:
                raise BootError(f"The GPT contains duplicate partition {name!r}.")
            offset = first_lba * SECTOR_SIZE
            size = (last_lba - first_lba + 1) * SECTOR_SIZE
            if offset + size > image_size:
                raise BootError(f"Partition {name!r} extends beyond the image.")
            partitions[name] = (offset, size)
    return partitions


GSC_STARTUP_ORIGINAL = b"/system/bin/gscutil --direct-gsc"
GSC_STARTUP_REPLACEMENT = b"/system/bin/true    --direct-gsc"


def super_extents(image: Path, partitions, name: str) -> list[tuple[int, int]]:
    """Return absolute (byte offset, byte length) extents of a logical partition."""
    super_offset, _ = partitions["super"]
    with image.open("rb") as source:
        header = read_exact(source, super_offset + 12288, 256, "LP metadata header")
        if struct.unpack_from("<I", header, 0)[0] != 0x414C5030:
            raise BootError("The super partition has no LP metadata.")
        header_size, tables_size = struct.unpack_from("<I", header, 8)[0], struct.unpack_from("<I", header, 44)[0]
        part_off, part_num, part_size = struct.unpack_from("<III", header, 80)
        ext_off, ext_num, ext_size = struct.unpack_from("<III", header, 92)
        tables = read_exact(
            source, super_offset + 12288 + header_size, tables_size, "LP tables"
        )
    for index in range(part_num):
        entry = tables[part_off + index * part_size :][:part_size]
        entry_name = entry[:36].split(b"\0", 1)[0].decode("ascii", "replace")
        if entry_name != name:
            continue
        first, count = struct.unpack_from("<II", entry, 40)
        extents = []
        for extent_index in range(first, first + count):
            raw = tables[ext_off + extent_index * ext_size :][:ext_size]
            sectors, target_type, target = struct.unpack_from("<QIQ", raw, 0)
            if target_type != 0:
                raise BootError(f"Unsupported LP extent type in {name}.")
            extents.append((super_offset + target * SECTOR_SIZE, sectors * SECTOR_SIZE))
        return extents
    raise BootError(f"Logical partition {name} not found in super.")


def gsc_startup_patch(image: Path, partitions) -> tuple[int, bytes]:
    """Locate gscutil.rc in system_a and return (offset, patched bytes)."""
    with image.open("rb") as source:
        for offset, length in super_extents(image, partitions, "system_a"):
            source.seek(offset)
            remaining, position, carry = length, offset, b""
            while remaining > 0:
                chunk = source.read(min(remaining, 64 << 20))
                if not chunk:
                    break
                data = carry + chunk
                hit = data.find(GSC_STARTUP_ORIGINAL)
                if hit != -1:
                    return position - len(carry) + hit, GSC_STARTUP_REPLACEMENT
                carry = data[-len(GSC_STARTUP_ORIGINAL) :]
                position += len(chunk)
                remaining -= len(chunk)
    raise BootError("Could not find the gscutil startup command in system_a.")


def write_preformatted_userdata(overlay: Path, workdir: Path, partition: tuple[int, int]) -> None:
    """Write a fresh ext4 filesystem into userdata so init mounts it without
    formatting it (formatting triggers vold's keystore-dependent setup)."""
    start, size = partition
    filesystem = workdir / "userdata.ext4"
    filesystem.unlink(missing_ok=True)
    with filesystem.open("wb") as handle:
        handle.truncate(size)
    run_tool(
        [
            "mke2fs", "-q", "-F", "-t", "ext4", "-b", "4096",
            "-O", "has_journal,extent,huge_file,dir_nlink,extra_isize,metadata_csum,"
            "quota,project,verity,encrypt",
            "-E", "quotatype=usrquota:grpquota:prjquota,lazy_itable_init=1,discard",
            "-L", "userdata", str(filesystem),
        ]
    )
    chunk_limit = 16 << 20
    commands: list[str] = []
    pieces: list[Path] = []
    with filesystem.open("rb") as handle:
        position = 0
        while True:
            try:
                data_start = os.lseek(handle.fileno(), position, os.SEEK_DATA)
            except OSError:
                break
            data_end = os.lseek(handle.fileno(), data_start, os.SEEK_HOLE)
            for chunk_start in range(data_start, data_end, chunk_limit):
                length = min(chunk_limit, data_end - chunk_start)
                handle.seek(chunk_start)
                body = handle.read(length)
                if not any(body):
                    continue
                piece = workdir / f"userdata-piece-{len(pieces)}.bin"
                piece.write_bytes(body)
                pieces.append(piece)
                commands.extend(["-c", f"write -s {piece} {start + chunk_start} {length}"])
            position = data_end
    try:
        if commands:
            subprocess.run(
                ["qemu-io", "-f", "qcow2", *commands, str(overlay)],
                check=True, stdout=subprocess.DEVNULL,
            )
    finally:
        for piece in pieces:
            piece.unlink(missing_ok=True)
        filesystem.unlink(missing_ok=True)


def write_vm_vendor(
    image: Path, overlay: Path, workdir: Path, partitions, graphics: Path | None = None
) -> None:
    extents = super_extents(image, partitions, "vendor_a")
    if len(extents) != 1:
        raise BootError("vendor_a is not a single extent; cannot replace it.")
    start, length = extents[0]
    commands: list[str] = []
    scratch: list[Path] = []
    try:
        vendor = vm_keymint.build_vendor(image, start, workdir, graphics, data_disk=True)
        size = vendor.stat().st_size
        if size > length:
            super_start, super_size = partitions["super"]
            start, new_length, slots = vm_keymint.plan_vendor_relocation(
                image, super_start, super_size, size
            )
            for index, (position, data) in enumerate(slots):
                piece = workdir / f"lp-slot-{index}.bin"
                piece.write_bytes(data)
                scratch.append(piece)
                commands += ["-c", f"write -s {piece} {position} {len(data)}"]
        commands += ["-c", f"write -s {vendor} {start} {size}"]
        subprocess.run(
            ["qemu-io", "-f", "qcow2", *commands, str(overlay)],
            check=True, stdout=subprocess.DEVNULL,
        )
    except vm_keymint.KeyMintError as error:
        raise BootError(f"Could not build the software-KeyMint vendor: {error}") from error
    finally:
        for piece in scratch:
            piece.unlink(missing_ok=True)


def build_overlay(
    image: Path, workdir: Path, partitions, preformat_userdata: bool = False,
    software_keymint: bool = False,
) -> Path:
    offset, replacement = gsc_startup_patch(image, partitions)
    overlay = workdir / "boot-overlay.qcow2"
    overlay.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(image), "-F", "raw", str(overlay)],
            check=True,
        )
        patch = workdir / "gsc-patch.bin"
        patch.write_bytes(replacement)
        subprocess.run(
            ["qemu-io", "-f", "qcow2", "-c", f"write -s {patch} {offset} {len(replacement)}", str(overlay)],
            check=True, stdout=subprocess.DEVNULL,
        )
        if preformat_userdata:
            write_preformatted_userdata(overlay, workdir, partitions["userdata"])
        if software_keymint:
            try:
                runtime = mesa_build.build_mesa_runtime(workdir)
            except mesa_build.MesaBuildError as error:
                raise BootError(f"Could not set up guest graphics: {error}") from error
            write_vm_vendor(image, overlay, workdir, partitions, runtime)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise BootError(f"Could not build the boot overlay (needs qemu-img/qemu-io): {error}") from error
    return overlay


def extract_android_v4_payload(
    image: Path, partition: tuple[int, int], payload_kind: str
) -> bytes:
    offset, partition_size = partition
    with image.open("rb") as source:
        header = read_exact(source, offset, 4096, f"{payload_kind} header")
        if header[:8] != b"ANDROID!":
            raise BootError(f"{payload_kind} is not an Android boot image.")
        version = struct.unpack_from("<I", header, 40)[0]
        if version != 4:
            raise BootError(f"{payload_kind} must use Android boot header v4.")
        kernel_size, ramdisk_size = struct.unpack_from("<II", header, 8)
        if payload_kind == "boot_a":
            if not kernel_size:
                raise BootError("boot_a does not contain a kernel.")
            payload_offset, payload_size = 4096, kernel_size
        else:
            if kernel_size or not ramdisk_size:
                raise BootError("init_boot_a has an unexpected kernel/ramdisk layout.")
            payload_offset, payload_size = 4096, ramdisk_size
        if payload_offset + payload_size > partition_size:
            raise BootError(f"{payload_kind} payload exceeds its partition.")
        return read_exact(
            source, offset + payload_offset, payload_size, f"{payload_kind} payload"
        )


def extract_vendor_boot(
    image: Path, partition: tuple[int, int]
) -> tuple[bytes, bytes, str]:
    offset, partition_size = partition
    with image.open("rb") as source:
        header = read_exact(source, offset, 4096, "vendor_boot_a header")
        if header[:8] != b"VNDRBOOT":
            raise BootError("vendor_boot_a is not a vendor boot image.")
        version, page_size = struct.unpack_from("<II", header, 8)
        if version != 4 or page_size == 0:
            raise BootError("vendor_boot_a must use Android vendor boot header v4.")
        ramdisk_size = struct.unpack_from("<I", header, 24)[0]
        dtb_size = struct.unpack_from("<I", header, 2092)[0]
        header_size = struct.unpack_from("<I", header, 2096)[0]
        table_size, entry_count, entry_size, bootconfig_size = struct.unpack_from(
            "<IIII", header, 2112
        )
        if entry_size < 108 or table_size != entry_count * entry_size:
            raise BootError("vendor_boot_a has an invalid ramdisk fragment table.")

        ramdisk_offset = align_up(header_size, page_size)
        dtb_offset = align_up(ramdisk_offset + ramdisk_size, page_size)
        table_offset = align_up(dtb_offset + dtb_size, page_size)
        bootconfig_offset = align_up(table_offset + table_size, page_size)
        if bootconfig_offset + bootconfig_size > partition_size:
            raise BootError("vendor_boot_a ramdisk table or bootconfig exceeds its partition.")

        table = read_exact(
            source, offset + table_offset, table_size, "vendor ramdisk table"
        )
        platform_fragment: bytes | None = None
        for index in range(entry_count):
            entry = table[index * entry_size : (index + 1) * entry_size]
            fragment_size, fragment_offset, fragment_type = struct.unpack_from(
                "<III", entry, 0
            )
            if fragment_type != 1:
                continue
            if platform_fragment is not None:
                raise BootError("vendor_boot_a has multiple platform ramdisk fragments.")
            if fragment_offset + fragment_size > ramdisk_size:
                raise BootError("A vendor ramdisk fragment exceeds the ramdisk area.")
            platform_fragment = read_exact(
                source,
                offset + ramdisk_offset + fragment_offset,
                fragment_size,
                "platform ramdisk fragment",
            )
        if platform_fragment is None:
            raise BootError("vendor_boot_a has no platform ramdisk fragment.")
        bootconfig = read_exact(
            source, offset + bootconfig_offset, bootconfig_size, "vendor bootconfig"
        )
        cmdline = header[28 : 28 + 2048].split(b"\0", 1)[0].decode(
            "ascii", errors="replace"
        )
    return platform_fragment, bootconfig, cmdline


def lz4_decompress(data: bytes, label: str) -> bytes:
    executable = shutil.which("lz4")
    if executable is None:
        raise BootError("Missing lz4. Install the lz4 command-line utility.")
    try:
        result = subprocess.run(
            [executable, "-d", "-c"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise BootError(f"Could not decompress {label} with lz4: {detail}") from error
    return result.stdout


def validate_newc(data: bytes, label: str) -> None:
    position = 0
    entries = 0
    while position + 110 <= len(data):
        header = data[position : position + 110]
        if header[:6] not in (b"070701", b"070702"):
            break
        try:
            values = [
                int(header[index : index + 8], 16)
                for index in range(6, 110, 8)
            ]
        except ValueError as error:
            raise BootError(f"{label} has an invalid cpio header.") from error
        file_size, name_size = values[6], values[11]
        name_start = position + 110
        name_end = name_start + name_size
        if name_size < 1 or name_end > len(data) or data[name_end - 1] != 0:
            raise BootError(f"{label} has an invalid cpio filename.")
        name = data[name_start : name_end - 1].decode("utf-8", errors="replace")
        data_start = align_up(name_end, 4)
        data_end = data_start + file_size
        if data_end > len(data):
            raise BootError(f"{label} has a truncated cpio entry.")
        entries += 1
        if name == "TRAILER!!!":
            if entries == 1:
                raise BootError(f"{label} contains no files.")
            return
        position = align_up(data_end, 4)
    raise BootError(f"{label} is not a complete newc cpio archive.")


def disable_userdata_encryption(platform_ramdisk: bytes) -> bytes:
    entry_name = b"first_stage_ramdisk/fstab.android-desktop"
    entry_count = 0
    data = bytearray(platform_ramdisk)
    position = 0

    while position + 110 <= len(data):
        header = data[position : position + 110]
        if header[:6] not in (b"070701", b"070702"):
            break
        try:
            fields = [
                int(header[index : index + 8], 16)
                for index in range(6, 110, 8)
            ]
        except ValueError as error:
            raise BootError("The platform ramdisk has an invalid cpio header.") from error
        file_size, name_size = fields[6], fields[11]
        name_start = position + 110
        name_end = name_start + name_size
        if name_size < 1 or name_end > len(data) or data[name_end - 1] != 0:
            raise BootError("The platform ramdisk has an invalid cpio filename.")
        name = bytes(data[name_start : name_end - 1])
        data_start = align_up(name_end, 4)
        data_end = data_start + file_size
        if data_end > len(data):
            raise BootError("The platform ramdisk has a truncated cpio entry.")

        if name == entry_name:
            entry_count += 1
            fstab = bytes(data[data_start:data_end])
            patched_lines: list[bytes] = []
            userdata_count = 0
            removed_encryption = False
            for line in fstab.splitlines(keepends=True):
                line_body = line.rstrip(b"\r\n")
                endings = line[len(line_body) :]
                columns = list(re.finditer(rb"\S+", line_body))
                if (
                    len(columns) < 5
                    or line_body[columns[1].start() : columns[1].end()] != b"/data"
                ):
                    patched_lines.append(line)
                    continue

                userdata_count += 1
                flags_column = columns[4]
                options = line_body[
                    flags_column.start() : flags_column.end()
                ].split(b",")
                removed_encryption |= any(
                    option.startswith((b"fileencryption=", b"keydirectory="))
                    for option in options
                )
                retained = [
                    option
                    for option in options
                    if not option.startswith((b"fileencryption=", b"keydirectory="))
                    and option != b"checkpoint=block"
                ]
                replacement = b",".join(retained)
                original_length = flags_column.end() - flags_column.start()
                if len(replacement) > original_length:
                    raise BootError("Could not safely rewrite userdata fstab flags.")
                line_body = (
                    line_body[: flags_column.start()]
                    + replacement
                    + b" " * (original_length - len(replacement))
                    + line_body[flags_column.end() :]
                )
                patched_lines.append(line_body + endings)

            if userdata_count != 1 or not removed_encryption:
                raise BootError(
                    "The first-stage fstab did not contain the expected encrypted "
                    "userdata entry; refusing the VM-only bypass."
                )
            patched_fstab = b"".join(patched_lines)
            if len(patched_fstab) != file_size:
                raise BootError("The VM-only fstab rewrite changed the archive size.")
            data[data_start:data_end] = patched_fstab

        if name == b"TRAILER!!!":
            if entry_count != 1:
                raise BootError(
                    "The platform ramdisk does not contain the expected first-stage fstab."
                )
            return bytes(data)
        position = align_up(data_end, 4)

    raise BootError(
        "The platform ramdisk does not contain the expected first-stage fstab."
    )


def make_newc(entries: list[tuple[str, bytes, int]]) -> bytes:
    archive = bytearray()

    def append_entry(name: str, body: bytes, mode: int, inode: int) -> None:
        encoded_name = name.encode("utf-8") + b"\0"
        fields = (
            inode,
            mode,
            0,
            0,
            1,
            0,
            len(body),
            0,
            0,
            0,
            0,
            len(encoded_name),
            0,
        )
        archive.extend(b"070701")
        archive.extend("".join(f"{field:08x}" for field in fields).encode("ascii"))
        archive.extend(encoded_name)
        archive.extend(b"\0" * ((-len(archive)) % 4))
        archive.extend(body)
        archive.extend(b"\0" * ((-len(archive)) % 4))

    for inode, (name, body, mode) in enumerate(entries, start=1):
        if name.startswith("/") or ".." in Path(name).parts:
            raise BootError(f"Refusing unsafe initramfs path {name!r}.")
        append_entry(name, body, mode, inode)
    append_entry("TRAILER!!!", b"", 0, len(entries) + 1)
    return bytes(archive)


def vbmeta_digest(image: Path, partition: tuple[int, int]) -> tuple[int, str]:
    offset, partition_size = partition
    header_size = 256
    with image.open("rb") as source:
        header = read_exact(source, offset, header_size, "vbmeta header")
        if header[:4] != b"AVB0":
            raise BootError("The vbmeta_a partition does not contain an AVB header.")
        auth_size, aux_size = struct.unpack_from(">QQ", header, 12)
        vbmeta_size = header_size + auth_size + aux_size
        if vbmeta_size > partition_size:
            raise BootError("The vbmeta image extends beyond the vbmeta_a partition.")
        vbmeta = read_exact(source, offset, vbmeta_size, "vbmeta image")
    return vbmeta_size, hashlib.sha256(vbmeta).hexdigest()


def android_cmdline(
    vendor_cmdline: str,
    bootconfig: bytes,
    vbmeta_size: int,
    vbmeta_sha256: str,
    debug_init: bool = False,
) -> str:
    bootconfig_tokens: list[str] = []
    for raw_line in bootconfig.decode("ascii", errors="replace").splitlines():
        line = raw_line.strip().strip("\0")
        if line.startswith("androidboot.") and "=" in line:
            bootconfig_tokens.append(line)
    bootconfig_keys = {token.split("=", 1)[0] for token in bootconfig_tokens}
    if debug_init:
        bootconfig_tokens = [
            token
            for token in bootconfig_tokens
            if token.split("=", 1)[0] != "androidboot.load_modules_parallel"
        ]
        bootconfig_keys.add("androidboot.load_modules_parallel")
    generated_bootconfig = {
        "androidboot.slot_suffix",
        "androidboot.vbmeta.hash_alg",
        "androidboot.vbmeta.size",
        "androidboot.vbmeta.digest",
    }
    bootconfig_tokens = [
        token
        for token in bootconfig_tokens
        if token.split("=", 1)[0] not in generated_bootconfig
    ]
    bootconfig_keys.update(generated_bootconfig)

    tokens: list[str] = []
    for token in vendor_cmdline.split():
        key = token.split("=", 1)[0]
        if (
            token == "bootconfig"
            or token == "kvm-intel.pkvm=1"
            or key == "fred"
            or key == "console"
            or key.startswith("xe.")
            or key.startswith("snd_")
            or key.startswith("snd.")
            or key == "androidboot.boot_devices"
            or key in bootconfig_keys
        ):
            continue
        tokens.append(token)
    tokens.extend(bootconfig_tokens)
    if debug_init:
        tokens.extend(
            (
                "androidboot.load_modules_parallel=false",
                "loglevel=8",
                "ignore_loglevel",
                "initcall_debug",
            )
        )
    tokens.append("fred=off")
    tokens.append("printk.devkmsg=on")
    tokens.extend(
        (
            "console=ttyS0,115200",
            "earlycon=uart8250,io,0x3f8,115200",
            "androidboot.boot_devices=pci0000:00/0000:00:04.0",
            "androidboot.slot_suffix=_a",
            "androidboot.vbmeta.hash_alg=sha256",
            f"androidboot.vbmeta.size={vbmeta_size}",
            f"androidboot.vbmeta.digest={vbmeta_sha256}",
        )
    )
    return " ".join(tokens)


def ci_artifact_metadata(
    target: str, required: tuple[str, ...], cache_dir: Path | None = None
) -> dict[str, tuple[int, str]]:
    cache = cache_dir / f"ci-artifacts-{CI_BUILD_ID}-{target}.json" if cache_dir else None
    if cache and cache.is_file():
        try:
            cached = {k: (v[0], v[1]) for k, v in json.loads(cache.read_text()).items()}
            if all(name in cached for name in required):
                return cached
        except (ValueError, IndexError, TypeError):
            pass
    artifacts = _fetch_ci_artifacts(target, required)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(artifacts))
    return artifacts


def _fetch_ci_artifacts(
    target: str, required: tuple[str, ...]
) -> dict[str, tuple[int, str]]:
    # Android CI serializes this protobuf request as JSON keyed by field number.
    request_body = json.dumps(
        {"1": CI_BUILD_ID, "2": target, "3": "latest"}
    ).encode("utf-8")
    request = urllib.request.Request(
        CI_LIST_URL,
        data=request_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.load(response)
            error = None
            break
        except urllib.error.HTTPError as http_error:
            error = http_error
            if http_error.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as other:
            error = other
        time.sleep(10 * (attempt + 1))
    if error is not None:
        raise BootError(
            f"Could not list Android CI artifacts for build {CI_BUILD_ID} "
            f"target {target}: {error}"
        ) from error
    if (
        not isinstance(payload, list)
        or not payload
        or not isinstance(payload[0], list)
    ):
        raise BootError("Android CI returned an unexpected artifact-list response.")

    artifacts: dict[str, tuple[int, str]] = {}
    for row in payload[0]:
        if (
            isinstance(row, list)
            and len(row) >= 4
            and isinstance(row[0], str)
            and isinstance(row[1], int)
            and isinstance(row[3], str)
        ):
            artifacts[row[0]] = (row[1], row[3].lower())
    missing = [name for name in required if name not in artifacts]
    if missing:
        raise BootError(
            f"Android CI build {CI_BUILD_ID} target {target} is missing required "
            "artifacts: "
            + ", ".join(missing)
        )
    return artifacts


def download_artifact(
    name: str,
    expected_size: int,
    expected_md5: str,
    destination: Path,
    target: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    def valid_existing(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as existing:
            for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_md5

    if valid_existing(destination):
        print(f"Using verified {name}")
        return

    url = (
        f"https://ci.android.com/builds/submitted/{CI_BUILD_ID}/{target}/"
        f"latest/raw/{urllib.parse.quote(name, safe='')}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "googlevm-boot/1.0"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.md5(usedforsecurity=False)
    actual_size = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open(
            "wb"
        ) as output:
            while chunk := response.read(1024 * 1024):
                actual_size += len(chunk)
                if actual_size > expected_size:
                    raise BootError(f"Android CI returned an oversized {name}.")
                digest.update(chunk)
                output.write(chunk)
    except BootError:
        temporary.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise BootError(f"Could not download {name} from Android CI: {error}") from error
    if actual_size != expected_size or digest.hexdigest() != expected_md5:
        temporary.unlink(missing_ok=True)
        raise BootError(
            f"Integrity check failed for {name}: got {actual_size} bytes, "
            f"MD5 {digest.hexdigest()}."
        )
    os.replace(temporary, destination)
    print(f"Downloaded and verified {name} ({actual_size} bytes)")


def run_tool(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as error:
        raise BootError(f"Missing required utility: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip()
        raise BootError(
            f"{arguments[0]} failed: {detail or error.returncode}"
        ) from error
    return result.stdout.strip()


def module_export_crcs(path: Path) -> dict[str, str]:
    sections = run_tool(["readelf", "--sections", "--wide", str(path)])
    section_names: dict[int, str] = {}
    for line in sections.splitlines():
        match = re.search(r"\[\s*(\d+)\]\s+(\S+)", line)
        if match:
            section_names[int(match.group(1))] = match.group(2)

    symbol_table = run_tool(["readelf", "--symbols", "--wide", str(path)])
    crc_offsets: dict[str, dict[str, int]] = {}
    for line in symbol_table.splitlines():
        columns = line.split()
        if len(columns) < 8 or not columns[0].endswith(":"):
            continue
        symbol = columns[7]
        if not symbol.startswith("__crc_") or not columns[6].isdigit():
            continue
        section = section_names.get(int(columns[6]), "")
        if section.startswith("__kcrctab"):
            crc_offsets.setdefault(section, {})[
                symbol[len("__crc_") :]
            ] = int(columns[1], 16)
    if not crc_offsets:
        return {}

    exports: dict[str, str] = {}
    for section, symbols in crc_offsets.items():
        try:
            result = subprocess.run(
                [
                    "objcopy",
                    "--dump-section",
                    f"{section}=/dev/stdout",
                    str(path),
                    os.devnull,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            detail = getattr(error, "stderr", b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise BootError(
                f"Could not read {section} exports from {path.name}: "
                f"{detail or error}"
            ) from error
        for symbol, offset in symbols.items():
            if offset + 4 <= len(result.stdout):
                crc = int.from_bytes(result.stdout[offset : offset + 4], "little")
                exports[symbol] = f"0x{crc:08x}"
    return exports


def validate_modules(
    module_dir: Path, symvers_path: Path
) -> tuple[list[str], dict[str, list[str]]]:
    symvers: dict[str, str] = {}
    for line in symvers_path.read_text(errors="replace").splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[0].startswith("0x"):
            symvers[columns[1]] = columns[0].lower()
    if not symvers:
        raise BootError("The downloaded vmlinux.symvers contains no symbol versions.")

    modules_by_name = {Path(name).stem.replace("-", "_"): name for name in MODULE_NAMES}
    module_exports: dict[str, str] = {}
    for name in MODULE_NAMES:
        for symbol, crc in module_export_crcs(module_dir / name).items():
            existing = module_exports.get(symbol)
            if existing is not None and existing != crc:
                raise BootError(f"Downloaded modules disagree on CRC for {symbol}.")
            module_exports[symbol] = crc

    dependencies: dict[str, list[str]] = {}
    for name in MODULE_NAMES:
        path = module_dir / name
        vermagic = run_tool(["modinfo", "-F", "vermagic", str(path)]).split()
        if not vermagic or vermagic[0] != KERNEL_RELEASE:
            found = vermagic[0] if vermagic else "missing"
            raise BootError(
                f"{name} has vermagic {found!r}; expected {KERNEL_RELEASE!r}."
            )
        if name in STORAGE_MODULE_NAMES:
            signer = run_tool(["modinfo", "-F", "signer", str(path)])
            if not signer:
                raise BootError(
                    f"{name} is unsigned; the kernel protects symbols required "
                    "by the virtio PCI transport."
                )

        imports = run_tool(["modprobe", "--dump-modversions", str(path)])
        for line in imports.splitlines():
            columns = line.split()
            if len(columns) != 2 or not columns[0].startswith("0x"):
                raise BootError(f"Could not parse symbol versions from {name}.")
            symbol, crc = columns[1], columns[0].lower()
            expected_crc = symvers.get(symbol, module_exports.get(symbol))
            if expected_crc is None:
                raise BootError(
                    f"{name} imports {symbol}, absent from vmlinux.symvers "
                    "and the downloaded module exports."
                )
            if expected_crc != crc:
                raise BootError(
                    f"{name} symbol CRC mismatch for {symbol}: "
                    f"{crc} != {expected_crc}."
                )

        depends = run_tool(["modinfo", "-F", "depends", str(path)])
        dependencies[name] = [
            modules_by_name[dependency.replace("-", "_")]
            for dependency in depends.split(",")
            if dependency.replace("-", "_") in modules_by_name
        ]

    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise BootError(f"Cyclic module dependency involving {name}.")
        visiting.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for module in MODULE_NAMES:
        visit(module)
    return ordered, dependencies


def prepare(
    image: Path,
    workdir: Path,
    debug_init: bool = False,
    insecure_userdata: bool = False,
    software_keymint: bool = False,
) -> tuple[Path, Path, str]:
    if not image.is_file():
        raise BootError(f"Recovery image not found: {image}")
    workdir.mkdir(parents=True, exist_ok=True)

    partitions = read_gpt_partitions(image)
    required_partitions = ("boot_a", "init_boot_a", "vendor_boot_a", "vbmeta_a")
    missing = [name for name in required_partitions if name not in partitions]
    if missing:
        raise BootError("Image is missing required partitions: " + ", ".join(missing))

    kernel = extract_android_v4_payload(image, partitions["boot_a"], "boot_a")
    if not kernel.startswith(b"MZ"):
        raise BootError("The boot_a kernel payload is not a bzImage (missing MZ header).")
    init_ramdisk = extract_android_v4_payload(
        image, partitions["init_boot_a"], "init_boot_a"
    )
    platform_blob, bootconfig, vendor_cmdline = extract_vendor_boot(
        image, partitions["vendor_boot_a"]
    )
    platform_ramdisk = lz4_decompress(platform_blob, "platform ramdisk")
    init_ramdisk = lz4_decompress(init_ramdisk, "init_boot ramdisk")
    validate_newc(platform_ramdisk, "platform ramdisk")
    validate_newc(init_ramdisk, "init_boot ramdisk")
    if insecure_userdata:
        platform_ramdisk = disable_userdata_encryption(platform_ramdisk)
        validate_newc(platform_ramdisk, "modified platform ramdisk")
        print(
            "WARNING: VM-only bypass disables userdata encryption and "
            "block checkpointing."
        )
    if software_keymint:
        try:
            platform_ramdisk = vm_keymint.drop_vendor_avb(platform_ramdisk)
            platform_ramdisk = vm_keymint.userdata_on_data_disk(platform_ramdisk)
        except vm_keymint.KeyMintError as error:
            raise BootError(str(error)) from error
        validate_newc(platform_ramdisk, "modified platform ramdisk")
        print("WARNING: --vm-keymint mounts /vendor without verified boot and keeps /data on the data disk.")
    vbmeta_size, vbmeta_sha256 = vbmeta_digest(image, partitions["vbmeta_a"])
    command_line = android_cmdline(
        vendor_cmdline, bootconfig, vbmeta_size, vbmeta_sha256, debug_init
    )
    if software_keymint:
        command_line += " " + vm_keymint.DISABLE_STRONGBOX_ARG

    module_dir = workdir / "modules"
    virtio_artifacts = ci_artifact_metadata(
        CI_TARGET, (*CI_MODULE_NAMES, SYMVERS_NAME), workdir
    )
    for name in (*CI_MODULE_NAMES, SYMVERS_NAME):
        size, digest = virtio_artifacts[name]
        download_artifact(name, size, digest, module_dir / name, CI_TARGET)
    storage_artifacts = ci_artifact_metadata(
        STORAGE_MODULE_TARGET, STORAGE_MODULE_NAMES, workdir
    )
    for name in STORAGE_MODULE_NAMES:
        size, digest = storage_artifacts[name]
        download_artifact(
            name, size, digest, module_dir / name, STORAGE_MODULE_TARGET
        )
    load_order, dependencies = validate_modules(module_dir, module_dir / SYMVERS_NAME)

    module_entries: list[tuple[str, bytes, int]] = [
        ("lib", b"", 0o040755),
        ("lib/modules", b"", 0o040755),
    ]
    for name in MODULE_NAMES:
        module_entries.append(
            (f"lib/modules/{name}", (module_dir / name).read_bytes(), 0o100644)
        )
    module_entries.extend(
        (
            (
                "lib/modules/modules.load",
                ("\n".join(load_order) + "\n").encode("ascii"),
                0o100644,
            ),
            (
                "lib/modules/modules.dep",
                "".join(
                    f"{name}:"
                    + "".join(f" {dependency}" for dependency in dependencies[name])
                    + "\n"
                    for name in load_order
                ).encode("ascii"),
                0o100644,
            ),
        )
    )
    modules_archive = make_newc(module_entries)
    initrd = gzip.compress(
        platform_ramdisk + init_ramdisk + modules_archive, compresslevel=6, mtime=0
    )
    kernel_path = workdir / "kernel"
    initrd_path = workdir / "initrd.img"
    kernel_path.write_bytes(kernel)
    initrd_path.write_bytes(initrd)
    (workdir / "cmdline.txt").write_text(command_line + "\n")
    print(
        f"Prepared kernel ({len(kernel)} bytes), initramfs ({len(initrd)} bytes), "
        f"and {len(MODULE_NAMES)} verified modules in {workdir}"
    )
    return kernel_path, initrd_path, command_line


def check_host() -> None:
    flags: set[str] = set()
    try:
        with Path("/proc/cpuinfo").open() as cpuinfo:
            for line in cpuinfo:
                if line.startswith(("flags", "Features")) and ":" in line:
                    flags.update(line.split(":", 1)[1].split())
                    break
    except OSError as error:
        raise BootError(f"Could not read CPU flags from /proc/cpuinfo: {error}") from error
    missing_flags = sorted(REQUIRED_CPU_FLAGS - flags)
    if missing_flags:
        raise BootError(
            "The host CPU lacks required userspace instructions: "
            + ", ".join(missing_flags)
            + ". The Android userspace needs GFNI, AVX-VNNI, and SHA-NI."
        )
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise BootError(
            "KVM is unavailable or /dev/kvm is not accessible to this user. "
            "Enable KVM access before launching."
        )
    required_tools = [
        "qemu-system-x86_64", "qemu-img", "qemu-io", "swtpm",
        "mkfs.erofs", "dump.erofs", "debugfs", "ninja", "pkg-config", "tar",
    ]
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing_tools:
        raise BootError("Missing required host tools: " + ", ".join(missing_tools))


def ensure_data_disk(path: Path, size: str) -> None:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise BootError("Missing qemu-img, required to create the writable data disk.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        run_tool([qemu_img, "create", "-f", "qcow2", str(path), size])
        print(f"Created persistent qcow2 data disk {path} ({size})")
        return
    try:
        info = json.loads(run_tool([qemu_img, "info", "--output=json", str(path)]))
    except json.JSONDecodeError as error:
        raise BootError(f"qemu-img returned invalid disk information for {path}.") from error
    if info.get("format") != "qcow2":
        raise BootError(f"Data disk {path} exists but is not a qcow2 image.")
    print(
        f"Using existing persistent qcow2 data disk {path} "
        f"({info.get('virtual-size', 0)} bytes)"
    )


def launch(
    image: Path,
    workdir: Path,
    kernel: Path,
    initrd: Path,
    command_line: str,
    memory: str,
    cpus: int,
    data_disk: Path,
    data_disk_size: str,
    boot_disk_bus: str = "nvme",
    tpm: bool = True,
    preformat_userdata: bool = False,
    software_keymint: bool = False,
    fullscreen: bool = False,
) -> int:
    if software_keymint and data_disk.exists():
        # /data lives here, but its encryption keys sit in the throwaway /metadata overlay,
        # so contents from a previous boot cannot be unlocked.
        data_disk.unlink()
    ensure_data_disk(data_disk, data_disk_size)
    boot_overlay = build_overlay(
        image, workdir, read_gpt_partitions(image), preformat_userdata,
        software_keymint,
    )
    serial_log = workdir / "serial.log"
    args = [
        "qemu-system-x86_64",
        "-machine",
        "q35,accel=kvm,memory-backend=mem1",
        "-cpu",
        "host",
        "-m",
        memory,
        "-smp",
        str(cpus),
        "-kernel",
        str(kernel),
        "-initrd",
        str(initrd),
        "-append",
        command_line,
        "-drive",
        f"file={boot_overlay},if=none,id=vm-disk,format=qcow2,snapshot=on",
        "-device",
        "nvme,drive=vm-disk,serial=googlevm0,addr=4.0"
        if boot_disk_bus == "nvme"
        else "virtio-blk-pci,drive=vm-disk,addr=4.0",
        "-drive",
        f"file={data_disk},if=none,id=data-disk,format=qcow2",
        "-device",
        "virtio-blk-pci,drive=data-disk,addr=5.0",
        "-device",
        "virtio-keyboard-pci",
        "-device",
        "virtio-tablet-pci",
        "-netdev",
        "user,id=net0",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-device",
        "virtio-rng-pci",
        "-audiodev",
        "driver=none,id=audio0",
        "-device",
        "virtio-sound-pci,audiodev=audio0",
        "-device",
        "virtio-vga-gl,hostmem=8G,blob=true,venus=true",
        "-vga",
        "none",
        "-display",
        "sdl,gl=on,show-cursor=on" + (",full-screen=on" if fullscreen else ""),
        "-object",
        "memory-backend-memfd,id=mem1,size=8G,share=on",
        "-serial",
        f"file:{serial_log}",
        "-monitor",
        "stdio",
        "-no-reboot",
    ]
    swtpm_process = None
    if tpm:
        tpm_dir = workdir / "tpm"
        tpm_dir.mkdir(exist_ok=True)
        tpm_socket = tpm_dir / "swtpm.sock"
        tpm_socket.unlink(missing_ok=True)
        try:
            swtpm_process = subprocess.Popen(
                [
                    "swtpm",
                    "socket",
                    "--tpm2",
                    "--tpmstate",
                    f"dir={tpm_dir}",
                    "--ctrl",
                    f"type=unixio,path={tpm_socket}",
                    "--log",
                    f"file={tpm_dir / 'swtpm.log'},level=20",
                ]
            )
        except FileNotFoundError as error:
            raise BootError("A TPM needs swtpm; install it or pass --no-tpm.") from error
        for _ in range(50):
            if tpm_socket.exists():
                break
            time.sleep(0.1)
        args.extend(
            [
                "-chardev",
                f"socket,id=chrtpm,path={tpm_socket}",
                "-tpmdev",
                "emulator,id=tpm0,chardev=chrtpm",
                "-device",
                "tpm-tis,tpmdev=tpm0",
            ]
        )
    print(f"Starting QEMU; serial output will be written to {serial_log}")
    print("QEMU monitor is attached to this terminal; enter 'quit' to stop the VM.")
    try:
        return subprocess.run(args, check=False).returncode
    except FileNotFoundError as error:
        raise BootError("Missing qemu-system-x86_64.") from error
    finally:
        if swtpm_process is not None:
            swtpm_process.terminate()


def parse_args() -> argparse.Namespace:
    workspace = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Prepare and boot the local Android desktop recovery image."
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=workspace / "android-desktop_signed_recovery_image.bin",
        help="raw recovery image (default: the image in this script's folder)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=workspace / "googlevm-build",
        help="directory for downloaded modules and generated boot files",
    )
    parser.add_argument("--memory", default="8G", help="VM memory (default: 8G)")
    parser.add_argument("--fullscreen", action="store_true", help="fullscreen window; the guest display follows the window size")
    parser.add_argument("--cpus", type=int, default=8, help="virtual CPUs (default: 8)")
    parser.add_argument(
        "--data-disk",
        type=Path,
        default=None,
        help="persistent qcow2 data disk (created if absent)",
    )
    parser.add_argument(
        "--data-disk-size",
        default="128G",
        help="size for a newly created data disk (default: 128G)",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="extract and build the VM initramfs without starting QEMU",
    )
    parser.add_argument(
        "--debug-init",
        action="store_true",
        help=(
            "disable parallel module loading and enable verbose kernel logs "
            "to diagnose first-stage init"
        ),
    )
    parser.add_argument(
        "--insecure-vm-userdata",
        action="store_true",
        help=(
            "disable userdata encryption and block checkpointing in the generated "
            "VM initramfs; guest userdata is unencrypted"
        ),
    )
    parser.add_argument(
        "--vm-keymint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "replace the hardware Trusty KeyMint with AOSP software KeyMint from "
            "Google's public Cuttlefish x86_64 build (downloads ~1.1 GB once) and "
            "mount /vendor without verified boot; keys are not hardware-backed. "
            "Also builds Mesa VirGL for the guest on first use (default: on)"
        ),
    )
    parser.add_argument(
        "--no-tpm",
        action="store_true",
        help="do not attach a swtpm-backed TPM 2.0 (tpm-tis)",
    )
    parser.add_argument(
        "--boot-disk-bus",
        choices=("virtio", "nvme"),
        default="nvme",
        help=(
            "bus for the Android image disk (default: nvme; the boot-control "
            "HAL SELinux policy only allows nvme/sd/mmc block devices)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image = args.image.expanduser().resolve()
    workdir = args.workdir.expanduser().resolve()
    if args.cpus < 1:
        raise BootError("--cpus must be at least 1.")
    if not args.prepare_only:
        check_host()
    kernel, initrd, command_line = prepare(
        image, workdir, args.debug_init, args.insecure_vm_userdata,
        args.vm_keymint,
    )
    if args.prepare_only:
        return 0
    return launch(
        image,
        workdir,
        kernel,
        initrd,
        command_line,
        args.memory,
        args.cpus,
        (args.data_disk or workdir / "data-disk.qcow2").expanduser().resolve(),
        args.data_disk_size,
        args.boot_disk_bus,
        not args.no_tpm,
        args.insecure_vm_userdata,
        args.vm_keymint,
        args.fullscreen,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BootError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
