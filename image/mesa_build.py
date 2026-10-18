"""Build the Mesa VirGL GLES libraries for Android x86_64 on first use.

Downloads the Android NDK toolchain and the Mesa source, cross-compiles the four
libraries the VM vendor image needs, and keeps only the stripped results.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

MESA_VERSION = "26.2.4"
MESA_URL = f"https://archive.mesa3d.org/mesa-{MESA_VERSION}.tar.xz"
MESA_SHA256 = "bce5f7fbebb934373b86c999a064d52fb5065878dc57f287f95346648ec832e9"
NDK_NAME = "android-ndk-r28c"
NDK_URL = f"https://dl.google.com/android/repository/{NDK_NAME}-linux.zip"
NDK_SHA256 = "dfb20d396df28ca02a8c708314b814a4d961dc9074f9a161932746f815aa552f"
TOOLCHAIN = "toolchains/llvm/prebuilt/linux-x86_64"
PYTHON_PACKAGES = ("meson", "mako", "pyyaml", "packaging", "setuptools")

# Built library (relative to the meson build dir) -> file name in the runtime dir.
OUTPUTS = {
    "src/egl/libEGL_virgl.so": "libEGL_virgl.so",
    "src/mesa/glapi/es1api/libGLESv1_CM_virgl.so": "libGLESv1_CM_virgl.so",
    "src/mesa/glapi/es2api/libGLESv2_virgl.so": "libGLESv2_virgl.so",
    "src/gallium/targets/dri/libgallium_dri.so": "libgallium_dri.so",
    "src/virtio/vulkan/libvulkan_virtio.so": "vulkan.virtio.so",
}

MESON_OPTIONS = (
    "--buildtype=release",
    "-Dforce_fallback_for=libdrm,expat",
    "-Dallow-fallback-for=libdrm",
    "-Dplatforms=android",
    "-Dplatform-sdk-version=35",
    "-Dandroid-stub=true",
    "-Dandroid-libbacktrace=disabled",
    "-Dllvm=disabled",
    "-Dglx=disabled",
    "-Dgbm=disabled",
    "-Dvideo-codecs=",
    "-Dgallium-va=disabled",
    "-Dzstd=disabled",
    "-Dgallium-drivers=virgl",
    "-Dvulkan-drivers=virtio",
    "-Degl=enabled",
    "-Dgles1=enabled",
    "-Dgles2=enabled",
    "-Dopengl=true",
    "-Degl-lib-suffix=_virgl",
    "-Dgles-lib-suffix=_virgl",
)

REQUIRED_TOOLS = ("ninja", "pkg-config", "tar")


class MesaBuildError(RuntimeError):
    pass


def runtime_ready(runtime: Path) -> bool:
    return all((runtime / name).is_file() for name in OUTPUTS.values())


def _download(url: str, destination: Path, sha256: str) -> None:
    if destination.is_file() and _sha256(destination) == sha256:
        return
    print(f"Downloading {url}")
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
    except OSError as error:
        raise MesaBuildError(f"Could not download {url}: {error}") from error
    if _sha256(partial) != sha256:
        partial.unlink(missing_ok=True)
        raise MesaBuildError(f"Checksum mismatch for {url}.")
    partial.replace(destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_toolchain(archive: Path, destination: Path) -> None:
    prefix = f"{NDK_NAME}/{TOOLCHAIN}/"
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if not info.filename.startswith(prefix):
                continue
            target = destination / info.filename
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                target.unlink(missing_ok=True)
                target.symlink_to(bundle.read(info).decode())
                continue
            with bundle.open(info) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            target.chmod(mode & 0o777 or 0o644)


def _run(command: list[str], **kwargs) -> None:
    result = subprocess.run(command, **kwargs)
    if result.returncode != 0:
        raise MesaBuildError(f"{command[0]} failed with exit code {result.returncode}.")


def build_mesa_runtime(workdir: Path) -> Path:
    """Return the directory holding the Mesa libs, building them if absent."""
    runtime = workdir / "mesa-runtime"
    if runtime_ready(runtime):
        return runtime
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise MesaBuildError(
            "Building the Mesa graphics libraries needs: " + ", ".join(missing)
        )

    build_root = workdir / "mesa-build"
    build_root.mkdir(parents=True, exist_ok=True)
    print("Building Mesa VirGL for the guest (one time, takes several minutes)...")

    ndk_zip = build_root / f"{NDK_NAME}-linux.zip"
    mesa_tar = build_root / f"mesa-{MESA_VERSION}.tar.xz"
    _download(NDK_URL, ndk_zip, NDK_SHA256)
    _download(MESA_URL, mesa_tar, MESA_SHA256)

    toolchain = build_root / NDK_NAME / TOOLCHAIN
    if not (toolchain / "bin/x86_64-linux-android35-clang").exists():
        shutil.rmtree(build_root / NDK_NAME, ignore_errors=True)
        _extract_toolchain(ndk_zip, build_root)
    source = build_root / f"mesa-{MESA_VERSION}"
    if not (source / "meson.build").is_file():
        shutil.rmtree(source, ignore_errors=True)
        _run(["tar", "-xJf", str(mesa_tar), "-C", str(build_root)])

    venv = build_root / "venv"
    if not (venv / "bin/meson").exists() or not (venv / ".packages").exists():
        shutil.rmtree(venv, ignore_errors=True)
        _run([sys.executable, "-m", "venv", str(venv)])
        _run([str(venv / "bin/python"), "-m", "pip", "install", "-q", *PYTHON_PACKAGES])
        (venv / ".packages").write_text(" ".join(PYTHON_PACKAGES))

    pkgconfig = build_root / "android-pkgconfig"
    pkgconfig.mkdir(exist_ok=True)
    cross = build_root / "android-x86_64.cross"
    cross.write_text(
        f"""[constants]
ndk = '{toolchain}'
[binaries]
c = ndk / 'bin/x86_64-linux-android35-clang'
cpp = [ndk / 'bin/x86_64-linux-android35-clang++', '-fno-exceptions', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '--start-no-unused-arguments', '-static-libstdc++', '--end-no-unused-arguments']
ar = ndk / 'bin/llvm-ar'
strip = ndk / 'bin/llvm-strip'
c_ld = 'lld'
cpp_ld = 'lld'
pkg-config = '{shutil.which("pkg-config")}'
[host_machine]
system = 'android'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
[properties]
needs_exe_wrapper = true
pkg_config_libdir = '{pkgconfig}'
"""
    )

    environment = dict(os.environ)
    environment["PATH"] = f"{venv / 'bin'}:{environment['PATH']}"
    environment["CCACHE_DISABLE"] = "1"
    build = build_root / "build"
    if not (build / "build.ninja").is_file():
        shutil.rmtree(build, ignore_errors=True)
        _run(
            [
                str(venv / "bin/meson"), "setup", str(build), str(source),
                "--cross-file", str(cross), *MESON_OPTIONS,
            ],
            env=environment,
        )
    _run(["ninja", "-C", str(build)], env=environment)

    staging = workdir / "mesa-runtime.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for built, name in OUTPUTS.items():
        output = staging / name
        shutil.copy2(build / built, output)
        _run([str(toolchain / "bin/llvm-strip"), str(output)])
    shutil.rmtree(runtime, ignore_errors=True)
    staging.rename(runtime)
    shutil.rmtree(build_root, ignore_errors=True)
    print(f"Mesa VirGL libraries ready in {runtime}")
    return runtime
