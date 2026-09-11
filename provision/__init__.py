import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENV_DIR = _REPO_ROOT / ".venv"
_lib_dir = _VENV_DIR / "lib"
if _lib_dir.is_dir():
    for _py_dir in sorted(_lib_dir.glob("python3.*"), reverse=True):
        _site_packages = _py_dir / "site-packages"
        if _site_packages.is_dir() and str(_site_packages) not in sys.path:
            sys.path.insert(0, str(_site_packages))
            break
