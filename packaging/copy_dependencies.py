"""Reuse locked wheels already installed in a build environment."""

import base64
import hashlib
import platform
import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement


def files_for(requirement):
    installed = distribution(requirement.name)
    version_matches = requirement.specifier.contains(installed.version, prereleases=True)
    if not version_matches or not installed.files:
        raise ValueError(f"Installed version does not match lock: {requirement.name}")
    for entry in installed.files:
        relative = Path(entry)
        # Entry-point launchers outside site-packages embed the build machine's Python path.
        if relative.is_absolute() or ".." in relative.parts or relative.suffix == ".pyc":
            continue
        source = Path(installed.locate_file(entry))
        if not source.is_file():
            raise ValueError(f"Installed wheel file is missing: {entry}")
        if entry.hash:
            with source.open("rb") as handle:
                digest = hashlib.file_digest(handle, entry.hash.mode).digest()
            if base64.urlsafe_b64encode(digest).decode().rstrip("=") != entry.hash.value:
                raise ValueError(f"Installed file does not match its wheel: {entry}")
        yield source, relative


def main():
    requirements, target, architecture = sys.argv[1:]
    if sys.version_info[:2] != (3, 12) or platform.machine().lower() != architecture:
        raise ValueError("Build environment architecture or Python version does not match")
    files = []
    for line in Path(requirements).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "\t")):
            continue
        requirement = Requirement(line.removesuffix("\\").strip())
        if requirement.marker is None or requirement.marker.evaluate():
            files.extend(files_for(requirement))
    for source, relative in files:
        destination = Path(target) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


if __name__ == "__main__":
    main()
