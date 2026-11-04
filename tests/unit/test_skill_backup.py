"""Exercise the standalone backup script without user data or network access."""

import os
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup-trader-skills.py"


def test_backup_contains_skill_files_not_profile_secrets(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    skill = profile / "skills" / "trading" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("learned lesson")
    (profile / ".env").write_text("secret=not-for-backup")
    backup = tmp_path / "backups"
    assert SCRIPT.is_file(), "skill backup script must exist"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--profile-home", str(profile), "--backup-dir", str(backup)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    archives = list(backup.glob("skills-*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["skills/trading/example/SKILL.md"]
        assert archive.read(archive.namelist()[0]) == b"learned lesson"


def test_backup_does_not_follow_symlinks(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("lesson")
    secret = tmp_path / "secret"
    secret.write_text("must not copy")
    (skills / "linked.txt").symlink_to(secret)
    backup = tmp_path / "backups"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--profile-home", str(profile), "--backup-dir", str(backup)],
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(next(backup.glob("skills-*.zip"))) as archive:
        assert archive.namelist() == ["skills/SKILL.md"]


def test_backup_retains_only_requested_number_of_archives(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_text("lesson")
    backup = tmp_path / "backups"
    backup.mkdir()
    old = backup / "skills-20000101T000000000000Z.zip"
    old.write_bytes(b"old backup")
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile-home",
            str(profile),
            "--backup-dir",
            str(backup),
            "--keep",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    assert len(list(backup.glob("skills-*.zip"))) == 1
    assert not old.exists()


@pytest.mark.parametrize("failure", ["write", "scan"])
def test_failed_backup_never_publishes_or_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    profile = tmp_path / "profile"
    skills = profile / "skills"
    nested = skills / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("lesson")
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "skills-20000101T000000000000Z.zip"
    old.write_bytes(b"previous successful archive")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--profile-home",
            str(profile),
            "--backup-dir",
            str(backups),
            "--keep",
            "1",
        ],
    )
    real_scan = os.scandir

    def scan(path):
        if Path(path) == nested:
            raise PermissionError("injected scan failure")
        return real_scan(path)

    def write(*args, **kwargs):
        raise OSError("injected write failure")

    if failure == "scan":
        monkeypatch.setattr(os, "scandir", scan)
    else:
        monkeypatch.setattr(zipfile.ZipFile, "write", write)
    with pytest.raises(OSError):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert list(backups.iterdir()) == [old]
    assert old.read_bytes() == b"previous successful archive"
