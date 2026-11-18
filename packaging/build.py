"""Build the native Windows or macOS offline installer."""

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS = ROOT / "integrations"
sys.path.insert(0, str(INTEGRATIONS))
from common import REVISION  # noqa: E402
from runtime_guard import MANIFEST, file_hashes  # noqa: E402

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"


def run(*command, capture=False, env=None):
    result = subprocess.run(command, cwd=ROOT, env=env, check=True, text=True,
                            encoding="utf-8", capture_output=capture)
    return result.stdout.strip() if capture else None


def tool(name, override=None):
    path = Path(override).resolve() if override else shutil.which(name)
    if not path or not Path(path).is_file():
        raise SystemExit(f"Build tool not found: {override or name}")
    return Path(path).resolve()


def compiler(override):
    if override:
        return tool("ISCC.exe", override)
    if found := shutil.which("ISCC.exe"):
        return Path(found)
    roots = (Path(os.environ["LOCALAPPDATA"]) / "Programs",
             Path(os.getenv("ProgramFiles(x86)", os.environ["ProgramFiles"])))
    for folder in roots:
        for pattern in ("Inno Setup *", "Inno Setup"):
            for candidate in sorted(folder.glob(pattern), reverse=True):
                if (candidate / "ISCC.exe").is_file():
                    return candidate / "ISCC.exe"
    raise SystemExit("Install Inno Setup or provide --iscc PATH (Windows).")


def install_deps(target, requirements, python, env, existing_env, arch):
    existing = existing_env / ("Scripts/python.exe" if WINDOWS else "bin/python")
    if existing.is_file():
        try:
            run(existing, "-I", "-B", ROOT / "packaging" / "copy_dependencies.py",
                requirements, target, arch, env=env, capture=True)
            print(f"Reused locked libraries for {target.name}", flush=True)
            return
        except subprocess.CalledProcessError:
            pass
    command = ["uv", "pip", "install", "--no-deps", "--link-mode", "copy", "--python", python,
               "--target", target, "-r", requirements]
    try:
        run(*command, "--offline", env=env, capture=True)
    except subprocess.CalledProcessError:
        run(*command, env=env)


def export_deps(project, target, *, frozen=False):
    run("uv", "export", "--project", project, "--frozen" if frozen else "--locked",
        "--no-dev", "--no-emit-project", "--output-file", target, capture=True)


def copy_native(source, target, arch):
    if MAC:
        run("lipo", "-verify_arch", arch, source)
        libraries = run("otool", "-L", source, capture=True).splitlines()
        for entry in libraries:
            if entry.startswith("\t") and not entry.strip().startswith(("/usr/lib/", "/System/")):
                raise SystemExit(f"Use a standalone build of {source.name}: {entry.strip()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def stage_windows_cpp_runtime(target):
    system = Path(os.environ["SystemRoot"]) / "System32"
    if not (system / "msvcp140.dll").is_file():
        raise SystemExit("The build requires Microsoft Visual C++ x64 Redistributable.")
    names = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
             "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll",
             "vcruntime140_threads.dll", "concrt140.dll", "vcomp140.dll")
    for name in names:
        library = system / name
        if library.is_file():
            shutil.copy2(library, target / name)


def archive_crawler(destination, scratch, checkout):
    if not checkout.exists():
        checkout = scratch / "MediaCrawler"
        run("git", "clone", "--filter=blob:none", "--no-checkout",
            "https://github.com/NanmiCoder/MediaCrawler.git", checkout)
    try:
        run("git", "-C", checkout, "cat-file", "-e", f"{REVISION}^{{commit}}", capture=True)
    except subprocess.CalledProcessError:
        run("git", "-C", checkout, "fetch", "--no-tags", "origin", REVISION)
    archive = scratch / "crawler.zip"
    run("git", "-C", checkout, "archive", "--format=zip", "--output", archive, REVISION)
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)
    (destination / MANIFEST).write_text(json.dumps({
        "revision": REVISION, "files": file_hashes(destination),
    }, sort_keys=True), encoding="utf-8")


def stage_browsers(component, runtime):
    cache = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    default = (Path(os.environ["LOCALAPPDATA"]) / "ms-playwright" if WINDOWS
               else Path.home() / "Library" / "Caches" / "ms-playwright")
    cache = Path(cache).expanduser().resolve() if cache and cache != "0" else default
    target = runtime / "browsers"
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(target),
           "PLAYWRIGHT_SKIP_BROWSER_GC": "1"}
    command = [*component, "crawler", "-m", "playwright", "install", "chromium"]
    plan = run(*command, "--dry-run", capture=True, env=env)
    for line in plan.splitlines():
        if line.strip().startswith("Install location:"):
            destination = Path(line.split(":", 1)[1].strip())
            cached = cache / destination.name
            if destination.parent == target and (cached / "INSTALLATION_COMPLETE").is_file():
                shutil.copytree(cached, destination, symlinks=True)
    run(*command, env=env)
    links = target / ".links"
    if links.is_dir():
        shutil.rmtree(links)


def make_app(directory, version):
    app = directory / "CreatorRadar.app"
    contents = app / "Contents"
    folder = contents / "MacOS"
    folder.mkdir(parents=True)
    executable = folder / "CreatorRadar"
    executable.write_text(
        '#!/bin/sh\nroot="$(cd "$(dirname "$0")/../Resources" && pwd)" || exit 1\n'
        'exec "$root/runtime/python/bin/python3.12" -I -X utf8 -B '
        '"$root/packaging/component.py" backend "$root/packaging/launch.py" "$@"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.creatorradar.workstation", "CFBundleName": "CreatorRadar",
        "CFBundleDisplayName": "CreatorRadar", "CFBundleExecutable": "CreatorRadar",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }))
    return app, contents / "Resources"


def stage(args, root, scratch, arch):
    runtime = root / "runtime"
    scratch_temp = scratch / "temp"
    scratch_temp.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch_temp), "TMP": str(scratch_temp),
           "TEMP": str(scratch_temp)}
    suffix = ".exe" if WINDOWS else ""
    managed = run("uv", "python", "find", "--managed-python", "3.12", capture=True)
    source = Path(managed)
    if not source.is_file():
        raise SystemExit("Install a build runtime with uv python install 3.12")
    python_root = source.parent if WINDOWS else source.parent.parent
    python = runtime / "python" / ("python.exe" if WINDOWS else "bin/python3.12")
    print("Staging Python, backend, MediaCrawler and ASR libraries", flush=True)
    shutil.copytree(python_root, python.parent if WINDOWS else python.parent.parent, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "site-packages"))
    if WINDOWS:
        stage_windows_cpp_runtime(python.parent)
    for name in ("node", "ffmpeg", "ffprobe"):
        override = args.node if name == "node" else (
            args.ffmpeg_dir / (name + suffix) if args.ffmpeg_dir else None
        )
        copy_native(tool(name + suffix, override), runtime / ("node" if name == "node" else "media")
                    / (name + suffix), arch)
    for directory in ("backend/creator_radar", "backend/migrations"):
        shutil.copytree(ROOT / directory, root / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundled_integrations = root / INTEGRATIONS.name
    bundled_integrations.mkdir()
    for name in ("bridge.py", "crawler.py", "common.py", "cover_cache.py", "runtime_guard.py",
                 "asr_bridge.py"):
        shutil.copy2(INTEGRATIONS / name, bundled_integrations / name)
    (root / "packaging").mkdir()
    for name in ("component.py", "launch.py"):
        shutil.copy2(ROOT / "packaging" / name, root / "packaging" / name)
    crawler = runtime / "MediaCrawler"
    archive_crawler(crawler, scratch, args.crawler_checkout)
    for name, project, existing in (
        ("backend", ROOT / "backend", ROOT / "backend" / ".venv"),
        ("crawler", crawler, args.crawler_checkout / ".venv"),
    ):
        requirements = scratch / f"{name}-requirements.txt"
        export_deps(project, requirements, frozen=name == "crawler")
        install_deps(runtime / "packages" / name, requirements, python, env, existing, arch)
    install_deps(runtime / "packages" / "asr", INTEGRATIONS / "faster-whisper.lock", python, env,
                 ROOT / ".phase1" / "asr-env", arch)

    print("Staging Chromium and the pinned small model", flush=True)
    component = [python, "-I", "-X", "utf8", "-B", root / "packaging" / "component.py"]
    stage_browsers(component, runtime)
    run(*component, "asr", ROOT / "packaging" / "prepare_model.py", runtime / "models" / "small",
        env={**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"})
    shutil.copytree(ROOT / "web" / "dist", root / "web" / "dist")
    if args.sample:
        print("Verifying the bundled crawler and ASR with offline settings", flush=True)
        clean_path = (
            str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32")
            if WINDOWS else "/usr/bin:/bin"
        )
        run(*component, "backend", root / "packaging" / "launch.py", "--self-test", args.sample,
            "--data-dir", scratch / "smoke", env={**os.environ, "PATH": clean_path,
                                                  "HF_HUB_CACHE": str(scratch / "empty-hub")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--sample", type=Path, help="Run offline checks with local Chinese audio")
    parser.add_argument("--ffmpeg-dir", type=Path, help="Directory with standalone media tools")
    parser.add_argument("--node", type=Path, help="Path to a standalone Node executable")
    parser.add_argument("--iscc", type=Path, help="Path to Inno Setup's ISCC.exe")
    parser.add_argument("--crawler-checkout", type=Path, default=ROOT / ".phase1" / "MediaCrawler")
    args = parser.parse_args()
    arch = platform.machine().lower()
    if (WINDOWS and arch not in {"amd64", "x86_64"}) or (MAC and arch not in {"arm64", "x86_64"}):
        parser.error(f"Unsupported architecture: {arch}")
    if not (WINDOWS or MAC):
        parser.error("Build Windows installers on Windows and macOS installers on macOS")
    installer = None
    if not args.stage_only:
        installer = compiler(args.iscc) if WINDOWS else tool("productbuild")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("sample", "ffmpeg_dir", "node", "crawler_checkout"):
        if (value := getattr(args, name)) is not None:
            setattr(args, name, value.resolve())
    if args.sample and not args.sample.is_file():
        parser.error(f"Sample audio not found: {args.sample}")
    version = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))["version"]
    target = "windows-x64" if WINDOWS else f"macos-{arch}"
    npm = "npm.cmd" if WINDOWS else "npm"
    run(npm, "--prefix", ROOT / "web", "ci", "--no-audit", "--no-fund", "--prefer-offline")
    run(npm, "--prefix", ROOT / "web", "run", "build")

    with tempfile.TemporaryDirectory(prefix="build-", dir=args.output) as work:
        scratch = Path(work)
        directory = (Path(tempfile.mkdtemp(prefix=f"{target}-", dir=args.output))
                     if args.stage_only else scratch / "payload")
        if WINDOWS:
            app, resources = directory, directory
        else:
            app, resources = make_app(directory, version)
        stage(args, resources, scratch, arch)
        if args.stage_only:
            print(f"Staged: {app}", flush=True)
        elif WINDOWS:
            output_name = f"CreatorRadar-{version}-{target}"
            run(installer, "/Qp", f"/DVersion={version}", f"/DPayloadRoot={resources}",
                f"/DOutputRoot={args.output}", f"/DOutputName={output_name}",
                ROOT / "packaging" / "windows.iss")
            print(f"Installer: {args.output / (output_name + '.exe')}", flush=True)
        else:
            output = args.output / f"CreatorRadar-{version}-{target}.pkg"
            run(installer, "--component", app, "/Applications", output)
            print(f"Installer: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        raise SystemExit(f"Build command failed (exit {exc.returncode})") from None
