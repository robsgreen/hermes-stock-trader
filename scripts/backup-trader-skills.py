#!/usr/bin/env python3
"""Back up only a trader profile's skills, without an LLM or broker access."""

import argparse
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=30)
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    source = args.profile_home / "skills"
    if not source.is_dir():
        parser.error("profile skills directory does not exist")
    args.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = args.backup_dir / f"skills-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.zip"
    temporary = target.with_suffix(".partial")

    def scan_error(error: OSError) -> None:
        raise error

    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            os.chmod(temporary, 0o600)
            for directory, folders, filenames in os.walk(
                source, followlinks=False, onerror=scan_error
            ):
                folders[:] = sorted(
                    name for name in folders if not (Path(directory) / name).is_symlink()
                )
                for name in sorted(filenames):
                    file = Path(directory) / name
                    if not file.is_symlink():
                        archive.write(file, Path("skills") / file.relative_to(source))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("backup verification failed")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    for old in sorted(args.backup_dir.glob("skills-*.zip"), reverse=True)[args.keep :]:
        old.unlink()
    print(f"Skill backup saved: {target}")


if __name__ == "__main__":
    main()
