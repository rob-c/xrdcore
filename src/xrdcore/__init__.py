"""xrdcore: the small, dependency-free core every grid storage client shares.

What lives here is what xrdclient, xgfalclient and xrdarc each needed and
none of them owns - nothing protocol-specific, nothing that needs a third-party
package to import:

* :mod:`.safexml` - XML parsing that refuses every ``<!DOCTYPE``, so no
  entity can be declared, expanded or fetched;
* :mod:`.checksum`, :mod:`.crc32c`, :mod:`.crc64` - the checksum algorithms
  storage servers report, behind one incremental, ``hashlib``-shaped surface;
* :mod:`.log` - credential redaction for logs and terminals, per-level
  muting, and a command-line tool's ``-v`` handler;
* :mod:`.cancel` - cancel scopes and cancel hooks, so every layer looks at
  the same "has the caller given up?";
* :mod:`.pipeline` - the bounded, read-ahead byte pump every streamed copy
  ends in;
* :mod:`.retry` - the one exponential, capped, full-jitter backoff, and
  HTTP ``Retry-After``;
* :mod:`.report` - the versioned ``storage-client-report`` JSON/XML document
  every tool's ``--json``/``--xml`` writes;
* :mod:`.errors` - the exception vocabulary xrdcore, xrdsec and xrdhttp raise;
* :mod:`.fanout` - jobs run side by side on threads, the first failure
  stopping the rest and raised as itself;
* :mod:`.sockets` - exactly-N-bytes reads from a framed stream socket;
* :mod:`.cli` - the exit statuses every command-line tool shares, the
  wrapper that turns Ctrl-C and a closed pipe into them, and the one-line
  handler for every other failure.

Importing this package imports none of them: each is loaded when first
named, so ``import xrdcore`` costs nothing a ``--help`` would notice.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from . import (
        cancel,
        checksum,
        cli,
        crc32c,
        crc64,
        errors,
        fanout,
        log,
        pipeline,
        report,
        retry,
        safexml,
        sockets,
    )

__all__ = [
    "__version__",
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
]

_SUBMODULES = frozenset(__all__) - {"__version__"}


def __getattr__(name: str) -> ModuleType:
    """Import a submodule the first time it is named as an attribute."""
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """The public names, including submodules not yet imported."""
    return sorted(__all__)
