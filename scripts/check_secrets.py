import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if not shutil.which("gitleaks"):
        raise SystemExit("Install Gitleaks first: brew install gitleaks")
    paths = sorted(
        set(
            subprocess.check_output(
                ("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"),
                cwd=ROOT,
            )
            .decode()
            .split("\0")
        )
        - {""}
    )
    ignored = subprocess.run(
        ("git", "check-ignore", "--no-index", "--stdin"),
        input="\n".join(paths),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if ignored.returncode not in (0, 1):
        raise SystemExit("Could not verify Git ignore rules.")
    if ignored.stdout:
        raise SystemExit(f"Refusing to publish tracked, ignored files:\n{ignored.stdout}")
    subprocess.run(
        ("gitleaks", "git", "--redact", "--no-banner", "--log-opts=--all --full-history", "."),
        check=True,
        cwd=ROOT,
    )
    with tempfile.TemporaryDirectory(prefix="ds-source-audit-") as temporary:
        for name in paths:
            source = ROOT / name
            if source.is_symlink():
                raise SystemExit(f"Review source symlink before publishing: {name}")
            if source.is_file():
                destination = Path(temporary) / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        subprocess.run(
            ("gitleaks", "dir", "--redact", "--no-banner", temporary), check=True, cwd=ROOT
        )
    print(f"Secret checks passed for Git history and {len(paths)} publishable paths.")


if __name__ == "__main__":
    main()
