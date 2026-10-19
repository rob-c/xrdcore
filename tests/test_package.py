"""Package-level contracts: lazy import, independence, error pickling."""

from __future__ import annotations

import pickle
import subprocess
import sys
from pathlib import Path

import pytest

import xrdcore
from xrdcore import errors

SRC = Path(__file__).resolve().parents[1] / "src"


def test_version_and_dir():
    assert xrdcore.__version__
    assert "report" in dir(xrdcore)
    with pytest.raises(AttributeError):
        _ = xrdcore.nonexistent


def test_the_package_is_marked_typed():
    """PEP 561: without ``py.typed`` a client's mypy treats every import as ``Any``."""
    assert (SRC / "xrdcore" / "py.typed").is_file()


def _imported_after(statement: str) -> set[str]:
    code = f"import sys; {statement}; print('\\n'.join(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); {code}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def test_importing_the_package_imports_no_submodule():
    loaded = _imported_after("import xrdcore")
    assert not {m for m in loaded if m.startswith("xrdcore.") and m != "xrdcore._version"}


def test_the_report_is_cheap():
    loaded = _imported_after("import xrdcore.report")
    assert not {"ssl", "cryptography", "http.client", "socket"} & loaded


@pytest.mark.parametrize(
    "module",
    [
        "cancel",
        "checksum",
        "cli",
        "crc32c",
        "crc64",
        "errors",
        "fanout",
        "log",
        "pipeline",
        "report",
        "retry",
        "safexml",
        "sockets",
    ],
)
def test_no_client_package_is_ever_imported(module):
    loaded = _imported_after(f"import xrdcore.{module}")
    assert not {m for m in loaded if m.split(".")[0] in {"xrdclient", "xgfalclient", "xrdarc"}}


def test_hierarchy():
    assert issubclass(errors.TimeoutError, TimeoutError)
    assert issubclass(errors.TimeoutError, errors.TransientError)
    assert issubclass(errors.ConnectionError, ConnectionError)
    assert issubclass(errors.InsecureCredentialError, PermissionError)
    assert issubclass(errors.InsecureCredentialError, errors.CredentialError)
    assert issubclass(errors.MetalinkError, ValueError)
    assert issubclass(errors.TokenExpiredError, errors.AuthenticationError)


@pytest.mark.parametrize(
    "exc",
    [
        errors.TransientError("lost", attempts=3, committed=4096),
        errors.TimeoutError("slow", attempts=1),
        errors.InsecureCredentialError("chmod 600 /tmp/x", "/tmp/x"),
        errors.ProtocolError("bad frame"),
    ],
)
def test_errors_pickle_with_their_state(exc):
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is type(exc) and str(back) == str(exc)
    assert vars(back) == vars(exc)


def test_rebuild_is_the_public_unpickler_a_client_reduces_through():
    """Clients' own exceptions name ``errors.rebuild`` in their ``__reduce__``: it is public."""
    assert "rebuild" in errors.__all__
    assert errors.TransientError("lost").__reduce__()[0] is errors.rebuild
    back = errors.rebuild(errors.InsecureCredentialError, ("chmod",), {"path": "/p"})
    assert isinstance(back, errors.InsecureCredentialError) and back.path == "/p"
