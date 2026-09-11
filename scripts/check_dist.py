import argparse
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent


def safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    forbidden = {
        ".git",
        ".venv",
        "__pycache__",
        "credentials.toml",
        "accounts",
        "shared",
        "runs",
        "handoffs",
    }
    if any(part in forbidden or part.startswith(".env") for part in path.parts):
        raise ValueError(f"Private or generated archive content: {name}")
    if path.suffix in {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".pyc",
        ".log",
    }:
        raise ValueError(f"Private or generated archive content: {name}")
    return path


def verify(directory: Path) -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    version = project["project"]["version"]
    wheel = directory / f"devin_switch-{version}-py3-none-any.whl"
    source = directory / f"devin_switch-{version}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="ds-distribution-") as temporary:
        root = Path(temporary)
        unpacked = root / "unpacked"
        with zipfile.ZipFile(wheel) as archive:
            names = {item.filename for item in archive.infolist()}
            assert f"devin_switch-{version}.dist-info/licenses/LICENSE" in names
            assert f"devin_switch-{version}.dist-info/licenses/NOTICE" in names
            for item in archive.infolist():
                path = safe_member(item.filename)
                assert path.parts[0] in {"devin_switch", f"devin_switch-{version}.dist-info"}
                assert not stat.S_ISLNK(item.external_attr >> 16)
                if not item.is_dir():
                    destination = unpacked / "wheel" / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
        with tarfile.open(source) as archive:
            prefix = f"devin_switch-{version}"
            names = {item.name for item in archive.getmembers()}
            assert {f"{prefix}/LICENSE", f"{prefix}/NOTICE", f"{prefix}/README.md"} <= names
            allowed = set(project["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"])
            for item in archive.getmembers():
                path = safe_member(item.name)
                assert path.parts[0] == prefix
                assert item.isdir() or item.isfile()
                if item.isfile():
                    assert path.parts[1] in allowed | {"PKG-INFO"}
                    destination = unpacked / "source" / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(item) as handle:
                        destination.write_bytes(handle.read())
        if not shutil.which("gitleaks"):
            raise SystemExit("Install Gitleaks first: brew install gitleaks")
        subprocess.run(("gitleaks", "dir", "--redact", "--no-banner", str(unpacked)), check=True)
        environment = {
            **os.environ,
            "UV_TOOL_DIR": str(root / "tools"),
            "UV_TOOL_BIN_DIR": str(root / "bin"),
            "DS_HOME": str(root / "state"),
        }
        subprocess.run(
            ("uv", "tool", "install", "--python", "3.11", str(wheel.resolve())),
            check=True,
            env=environment,
            cwd=root,
        )
        python = root / "tools/devin-switch/bin/python"
        for module in ("cli", "desktop", "sessions", "usage", "handoff"):
            subprocess.run(
                (str(python), "-c", f"import devin_switch.{module}"),
                check=True,
                cwd=root,
                env=environment,
            )
        subprocess.run((str(root / "bin/ds"), "--help"), check=True, env=environment, cwd=root)
        subprocess.run((str(root / "bin/ds"), "list"), check=True, env=environment, cwd=root)
        subprocess.run(
            (str(root / "bin/ds"), "switch", "--setup"), check=True, env=environment, cwd=root
        )
        subprocess.run(
            (
                str(python),
                "-c",
                "from pathlib import Path; from devin_switch.handoff import "
                "enabled; assert enabled(Path.cwd())",
            ),
            check=True,
            env=environment,
            cwd=root,
        )
    print("Distributions passed: contents, licenses, secret scan, and isolated uv tool install.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit and install built Python distributions.")
    parser.add_argument("directory", type=Path, nargs="?", default=ROOT / "dist")
    verify(parser.parse_args().directory.resolve())
