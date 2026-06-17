"""Basic raw-log fixture provider."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from core import BaseFixture


# Assets are bundled with the package (copied from the bublik-release publishing
# docs), so the basic fixture is self-contained and needs no bublik checkout.
ASSETS = Path(__file__).resolve().parent / "assets"
RAW_LOG = ASSETS / "example_raw.log"
CONVERTER = ASSETS / "example_converter.py"


class BasicFixture(BaseFixture):
    name = "basic"
    project = "bublik-e2e"

    def generate(self, output_dir: Path, pretty: bool) -> None:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)

        command = [
            sys.executable,
            str(CONVERTER),
            str(RAW_LOG),
            "-o",
            str(output_dir),
            "--project",
            self.project,
        ]
        if pretty:
            command.append("--pretty")

        completed = subprocess.run(
            command,
            cwd=ASSETS,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Converter failed:\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )


fixture = BasicFixture()
