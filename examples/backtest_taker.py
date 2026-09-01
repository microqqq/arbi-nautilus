"""Run the credential-free Taker vertical slice."""

from pathlib import Path
from tempfile import TemporaryDirectory

from py000_nautilus.app import run_simulated_example

with TemporaryDirectory(prefix="py000-nautilus-") as directory:
    print(run_simulated_example(Path(directory) / "taker.state.json"))
