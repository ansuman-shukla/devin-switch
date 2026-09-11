import argparse
import hashlib
import platform
import subprocess
import tarfile
import tempfile
from pathlib import Path

VERSION = "8.30.1"


def install(destination: Path) -> None:
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}[platform.machine()]
    archive_name = f"gitleaks_{VERSION}_{platform.system().lower()}_{architecture}.tar.gz"
    checksums_name = f"gitleaks_{VERSION}_checksums.txt"
    with tempfile.TemporaryDirectory(prefix="ds-gitleaks-") as temporary:
        root = Path(temporary)
        subprocess.run(
            (
                "gh",
                "release",
                "download",
                f"v{VERSION}",
                "--repo",
                "gitleaks/gitleaks",
                "--pattern",
                archive_name,
                "--pattern",
                checksums_name,
                "--dir",
                str(root),
            ),
            check=True,
        )
        checksums = {
            name: digest
            for digest, name in (
                line.split() for line in (root / checksums_name).read_text().splitlines()
            )
        }
        with (root / archive_name).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != checksums[archive_name]:
                raise SystemExit("Gitleaks download failed checksum verification.")
        destination.mkdir(parents=True, exist_ok=True)
        binary = destination / "gitleaks"
        if binary.exists():
            raise SystemExit("Refusing to replace an existing Gitleaks executable.")
        with tarfile.open(root / archive_name) as archive:
            with archive.extractfile("gitleaks") as handle:
                binary.write_bytes(handle.read())
        binary.chmod(0o755)
    subprocess.run((str(binary), "version"), check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install a verified Gitleaks release using gh.")
    parser.add_argument("destination", type=Path)
    install(parser.parse_args().destination.resolve())
