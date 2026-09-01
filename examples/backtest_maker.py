"""Run the credential-free Maker vertical slice."""

from pathlib import Path
from tempfile import TemporaryDirectory

from py000_nautilus.app import run_maker_simulated_example

with TemporaryDirectory(prefix="py000-maker-") as directory:
    print(run_maker_simulated_example(Path(directory) / "maker.state"))
