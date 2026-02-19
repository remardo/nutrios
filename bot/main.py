"""Compatibility wrapper.

Legacy scripts still call `python bot/main.py`.
The actual bot implementation lives in repo root: `main.py`.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_root_main():
    root_main = Path(__file__).resolve().parents[1] / "main.py"
    spec = spec_from_file_location("nutrios_root_main", root_main)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {root_main}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    _load_root_main().main()
