"""Load the authentication blueprint recovered from this project's supplied CPython 3.14 bytecode.

The uploaded ZIP's editable routes/auth.py was empty. The included bytecode is retained
as a compatibility fallback so the existing login, signup, logout and password-reset
behavior is preserved rather than replaced with a speculative rewrite. This file is
ordinary editable Python; the recovered authentication implementation itself remains
version-bound to the accompanying CPython 3.14 .pyc until the original source can be restored.
"""
from importlib.machinery import SourcelessFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import sys

# These route functions are inside the recovered code object and not visible to AST tools.
__auth_binary_endpoints__ = (
    "splash",
    "choose_role",
    "login",
    "signup",
    "logout",
    "forgot_password",
    "verify_otp",
    "new_password",
)

_BINARY_MODULE_NAME = "routes._auth_recovered"
_BINARY_MODULE_PATH = Path(__file__).with_name("_auth_recovered.pyc")
_EXPECTED_PYC_MAGIC = bytes.fromhex("2b0e0d0a")  # CPython 3.14 artifact from the submitted ZIP

if not _BINARY_MODULE_PATH.is_file():
    raise ImportError(
        "Sivdhi authentication backup is missing. Restore routes/_auth_recovered.pyc "
        "from the supplied project archive or restore the original routes/auth.py source."
    )

_pyc_bytes = _BINARY_MODULE_PATH.read_bytes()
if not _pyc_bytes.startswith(_EXPECTED_PYC_MAGIC):
    raise ImportError(
        "Sivdhi's recovered auth bytecode is not the expected CPython 3.14 build. "
        "Restore the matching bytecode or replace this loader with the original source."
    )

_loader = SourcelessFileLoader(_BINARY_MODULE_NAME, str(_BINARY_MODULE_PATH))
_spec = spec_from_loader(_BINARY_MODULE_NAME, _loader)
if _spec is None:
    raise ImportError("Could not load the recovered Sivdhi authentication module.")

_recovered = module_from_spec(_spec)
sys.modules[_BINARY_MODULE_NAME] = _recovered
try:
    _loader.exec_module(_recovered)
except Exception:
    sys.modules.pop(_BINARY_MODULE_NAME, None)
    raise

if not hasattr(_recovered, "auth_bp"):
    sys.modules.pop(_BINARY_MODULE_NAME, None)
    raise ImportError("Recovered authentication module does not export auth_bp.")

auth_bp = _recovered.auth_bp

# Keep compatibility for any existing project code that imports these helpers.
for _name in (
    "clear_reset_session",
    "_client_ip",
    "_valid_email",
    "_forgot_password_url",
    "_valid_mobile",
    "_strong_password",
    "_hash_reset_otp",
):
    if hasattr(_recovered, _name):
        globals()[_name] = getattr(_recovered, _name)
