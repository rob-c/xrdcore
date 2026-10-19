"""The error vocabulary every library on the stack raises.

xrdcore, xrdsec and xrdhttp raise these and nothing client-specific, so a
library never has to know which client called it. A client *joins* this
hierarchy rather than wrapping it:

* where a client's error means what one here means, the client's name is
  bound to *this* class. xrdclient does that for all of them
  (``xrdclient.XRootDError is XrdError``, ``xrdclient.errors.CredentialError
  is CredentialError``, ...), so ``except`` written against the client's name
  catches what any library raises, with nothing translated or re-raised. A
  client subclass the other way round would not: ``except`` catches a class
  and what is below it, and the library's class is not below the client's;
* the client's own errors - XRootD's ``kXR_*`` server errors, gfal2's
  ``GError`` - derive from these, by multiple inheritance where they also
  need a builtin or a base of the client's, so code that only knows the
  libraries (``except XrdError``) catches the clients' errors too.

The builtins are shadowed on purpose, as the clients shadow them:
:class:`ConnectionError` and :class:`TimeoutError` each *subclass* their
builtin, so either spelling of ``except`` catches them.

What must not break: every exception here pickles with all its attributes
(``__reduce__`` + :func:`rebuild`), because errors cross process boundaries
under fsspec/dask and :mod:`concurrent.futures`. A new exception with
keyword-only or extra state needs its own ``__reduce__``.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any

__all__ = [
    "AuthenticationError",
    "ConnectionError",
    "CredentialError",
    "InsecureCredentialError",
    "MetalinkError",
    "ProtocolError",
    "TimeoutError",
    "TokenExpiredError",
    "TransientError",
    "XrdError",
    "rebuild",
]


class XrdError(Exception):
    """Base of every error xrdcore, xrdsec and xrdhttp raise."""


#: What ``__reduce__`` returns here: :func:`rebuild` and the arguments it is called with.
_Reduced = tuple[Callable[..., BaseException], tuple[object, ...]]


def rebuild(
    cls: Callable[..., BaseException], args: tuple[object, ...], kwargs: dict[str, Any]
) -> BaseException:
    """Unpickle an exception that carries more than ``args``: ``cls(*args, **kwargs)``.

    ``BaseException.__reduce__`` replays only ``args``, so an exception with
    keyword-only or extra state returns ``rebuild, (type(self), args,
    kwargs)`` from its own ``__reduce__``. Public because the clients' own
    exceptions (xrdclient's ``ServerError`` family, ...) pickle through it
    too: one function, so a pickle written by any layer loads in any other.
    """
    return cls(*args, **kwargs)


class ProtocolError(XrdError):
    """A peer answered with something malformed, unexpected or unsupported."""


class MetalinkError(ProtocolError, ValueError):
    """A Metalink descriptor is unsafe, malformed, or unusable."""


class ConnectionError(XrdError, builtins.ConnectionError):
    """The transport failed."""


class TransientError(ConnectionError):
    """A retryable failure. Records progress so a caller can resume."""

    def __init__(self, message: str, *, attempts: int = 0, committed: int = 0) -> None:
        """Record the failure and how far the operation got.

        ``attempts`` is how many tries were made before giving up; ``committed``
        is the byte offset known to be durable at the far end (for an upload, the
        position the server acknowledged), so a caller can resume from there
        instead of starting over.
        """
        super().__init__(message)
        self.attempts = attempts
        self.committed = committed

    def __reduce__(self) -> _Reduced:
        """Pickle with ``attempts`` and ``committed`` intact.

        ``BaseException.__reduce__`` replays only ``args``, so a failure raised in
        a worker process would arrive in the parent with its progress record
        reset to zero.
        """
        state = {"attempts": self.attempts, "committed": self.committed}
        return rebuild, (type(self), (self.args[0],), state)


class TimeoutError(TransientError, builtins.TimeoutError):
    """An operation exceeded its deadline.

    A timeout is a :class:`TransientError` because that is what it is - the
    request may well succeed on the next attempt - so code that retries on
    ``TransientError`` catches it, and code that cares specifically about
    slowness can still catch the builtin :class:`TimeoutError`.
    """


class AuthenticationError(XrdError):
    """Logging in failed: no usable credential, or the peer refused it."""


class CredentialError(AuthenticationError):
    """A credential was missing, unreadable, or malformed."""


class TokenExpiredError(CredentialError):
    """A bearer token is expired; the server would have rejected it."""


class InsecureCredentialError(CredentialError, builtins.PermissionError):
    """A credential file that is not private to this user, and so is not used.

    Raised for a proxy, a bearer-token file or a keytab that is not a regular
    file, belongs to another user, or that group or others may read or write
    - the same refusals ``XrdSecgsi``, ``XrdSecsss`` and the grid's own tools
    make, because a secret anyone else can read is no longer a secret, and
    one anyone else can write is somebody else's credential. The message
    names the file and the command that fixes it; :attr:`path` is the file.
    ``except PermissionError`` catches it too.
    """

    def __init__(self, message: str, path: str = "") -> None:
        """``path`` names the offending file, kept apart from the message for programs."""
        # The plain one-argument form: ``OSError`` given an errno or a
        # filename prefixes ``[Errno ...]`` to the sentence it is meant to be.
        super().__init__(message)
        self.path = path

    def __reduce__(self) -> _Reduced:
        """Pickle with :attr:`path` intact."""
        return rebuild, (type(self), (self.args[0],), {"path": self.path})
