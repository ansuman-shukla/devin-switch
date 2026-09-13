import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="Native model tests require macOS and Swift",
)


@pytest.fixture(scope="module")
def model_tests(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path_factory.mktemp("macos-model") / "AppModelTests"
    result = subprocess.run(
        (
            "swiftc",
            "-parse-as-library",
            "-D",
            "APP_MODEL_TESTS",
            "-target",
            f"{platform.machine()}-apple-macos14.0",
            "-framework",
            "SwiftUI",
            "-framework",
            "AppKit",
            "-framework",
            "Vision",
            str(root / "macos/DevinSwitch.swift"),
            str(root / "macos/AccountViews.swift"),
            str(root / "tests/AppModelTests.swift"),
            "-o",
            str(executable),
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


@pytest.mark.parametrize(
    "scenario",
    [
        "quiet-polling",
        "unchanged-state",
        "external-default",
        "usage-race",
        "action-race",
        "errors",
        "preferences",
        "timers",
        "wake",
        "row-labels",
        "combined-quota",
        "combined-quota-exhaustion",
        "combined-quota-render",
        "adaptive-layout",
        "workspace-controls",
        "navigation",
        "rename-display",
        "rename-errors",
        "display-name-decoding",
    ],
)
def test_app_model(model_tests: Path, scenario: str, tmp_path: Path) -> None:
    result = subprocess.run(
        (str(model_tests), scenario, str(tmp_path)), capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
