# xrdcore

The small, dependency-free core shared by
[xrdclient](https://github.com/rob-c/xrdclient),
[xgfalclient](https://github.com/rob-c/xgfalclient) and
[xrdarc](https://github.com/rob-c/xrdarc). Nothing protocol-specific lives
here, and nothing that needs a third-party package to import.

| Module | What it is |
|---|---|
| `xrdcore.safexml` | `fromstring()` that refuses every `<!DOCTYPE`, so no entity can be declared, expanded or fetched (`UnsafeXML`). |
| `xrdcore.checksum` | `new("adler32")`, `checksum_bytes`, `checksum_file`, `algorithms()`: one incremental, `hashlib`-shaped surface over Adler-32, CRC-32, CRC-32C, CRC-64/XZ, CRC-64/NVME, MD5 and the SHAs. |
| `xrdcore.crc32c` | CRC-32C, hardware-assisted with the `fast` extra (`google-crc32c`), else `crc32c_py`. |
| `xrdcore.crc64` | CRC-64/XZ and CRC-64/NVME, pure Python. |
| `xrdcore.log` | `redact()` and `get_logger()`: credential redaction for logs and terminals, and per-level `mute()`; `configure()` and `verbosity_level()`, a command-line tool's `-v` handler with that redaction. |
| `xrdcore.cancel` | `CancelScope` (context-variable cancellation) and `watching()` (cancel hooks). |
| `xrdcore.pipeline` | `pump()` and `ReadAhead`: the bounded, read-ahead byte pump every streamed copy ends in. |
| `xrdcore.retry` | `delay()`/`pause()`: the one exponential, capped, deadline-aware, full-jitter backoff; `retry_after()` reads HTTP `Retry-After` in both spellings. |
| `xrdcore.report` | The versioned `storage-client-report` document behind every tool's `--json`/`--xml`; `parse()` reads either format back as the one JSON tree. |
| `xrdcore.errors` | The exception vocabulary xrdcore, xrdsec and xrdhttp raise. Clients join it: a client's same-meaning names *are* these classes (`xrdclient.XRootDError is XrdError`), and its own errors subclass them, by multiple inheritance where they are also a builtin. |
| `xrdcore.fanout` | `run_all()`: jobs run side by side on threads, the first failure raised as itself (the rest noted on it), queued jobs never started and running ones told to stop (`Stopped`); each job sees the caller's `contextvars`. `Pool`: a long-lived, named pool for work that is not a fan-out, made on first use and again after a fork. |
| `xrdcore.sockets` | `recv_exact()`: exactly N bytes from a framed stream socket, or `ShortRead` (an `OSError`) saying how far it got. |
| `xrdcore.cli` | `ExitCode`, the exit statuses every tool shares (0, 1, 2, 70, 130, 141), `extends()` for a tool's own table, `run()` (Ctrl-C → 130, a closed pipe → a quiet 141), `Fatal` (any other failure: one redacted `prog: message` line, its status from the tool's `exit_code()` table, a bug's traceback at DEBUG) and `silence_stdout()`. |

`import xrdcore` imports no submodule; each loads when first named.

```python
from xrdcore import checksum, safexml

digest = checksum.new("adler32")
digest.update(b"hello world\n")
digest.hexdigest()            # '1e720467'

safexml.fromstring(b"<!DOCTYPE x><x/>")   # raises UnsafeXML
```

## Development

In the workspace every package is installed editable into the shared
interpreter, so the tests import the source tree. On a fresh checkout:

```sh
cd xrdcore
python -m pip install -e '.[dev,fast]'
python -m pytest -q -n 6 --cov=xrdcore      # 100 % line + branch, enforced (fail_under)
ruff check src tests && ruff format --check src tests
mypy                                        # strict, over src and tests
complexipy                                  # no function above 15, no snapshot
python -m build && twine check dist/*       # the wheel ships py.typed
```

Without the `fast` extra the CRC-32C tests still pass: they cover the
pure-Python path, and the accelerated one through a stand-in module.

## Licence

LGPL-3.0-or-later; see `LICENSE` and `COPYING`.
