# Changelog

Notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.4.1] - Unreleased

No changes of its own: released with the 0.4.1 chain (xrdsec, xrdclient and xgfalclient fixes, xrdarc's production fixes), whose packages share one version.

## [0.4.0] - 2026-10-19

### Added

- First release: the shared core extracted from xrdclient 0.4.0 so that
  xrdclient, xgfalclient and xrdarc stop importing each other's private
  modules.
- `xrdcore.safexml` (was `xrdclient._xml`).
- `xrdcore.checksum`, `xrdcore.crc32c`, `xrdcore.crc64` (were
  `xrdclient.crypto.checksum`, `.crc32c`, `.crc64`). The pure-Python CRC-32C
  is public as `crc32c_py`; `Digest` names either kind of digest. XRootD's
  4 KiB page framing (`pack_pages`, `unpack_pages`, `page_span`) stays with
  the XRootD client.
- `xrdcore.checksum.ChecksumInfo`: the one `algorithm:value` type for a
  reported checksum (was `xrdclient.types.ChecksumInfo` and a look-alike in
  `xrdhttp.metalink`), so checksums from every protocol compare equal.
- `xrdcore.log` (was `xrdclient._log`). `get_logger()` keeps the caller's
  logger name instead of forcing an `xrdclient.` prefix; the filter is public
  as `RedactingFilter`/`FILTER`.
- `xrdcore.cancel` (was `xrdclient._cancel`); its context variable is now the
  one every library looks at.
- `xrdcore.pipeline` (was `xrdclient.copy._pipeline`).
- `xrdcore.report` (was `xrdclient.cli._output`), with `dumps()` and
  `register_text_type()` in place of its dependency on `XRootDURL`.
- `xrdcore.retry` (was `xrdclient.retry`, née `_retry`): `ceiling`, `delay`,
  `pause`, `use_rng` and `retry_after`.
- `xrdcore.errors`: `XrdError`, `ProtocolError`, `MetalinkError`,
  `ConnectionError`, `TransientError`, `TimeoutError`, `AuthenticationError`,
  `CredentialError`, `TokenExpiredError`, `InsecureCredentialError`.
- Release readiness: the suite gates coverage at 100 % line and branch
  (`fail_under = 100`), with the CRC-32C accelerator probe, the safe-XML
  guard (every encoding, both ElementTree parsers, hostile documents) and the
  report (both formats, captured streams, exit-code policy) tested here
  rather than in xrdclient; the wheel ships `py.typed` (PEP 561).
- xrdclient 0.4.0 is built on this package: its old module names are aliases
  of these modules, and its shared error names are these classes.
- `xrdcore.fanout` (was `xrdclient._fanout`): `run_all()` and `Stopped`, the
  thread fan-out behind xrdclient's bulk download, parallel copy and
  multi-replica reader, xgfalclient's parallel ranged HTTP download (which
  had its own pool) and xrdarc's `get` (likewise). One exception object
  raised by several jobs is now reported once, not noted on itself.
- `xrdcore.cli`: the exit statuses xrdarc, xrdwms and the storage clients'
  tools share (`ExitCode`: 0, 1, 2, 70, 130, 141, from xrdarc's
  `cli._common.ExitCode` and xrdwms's `cli.ExitCode`), `extends()` checking
  a tool's own table keeps them, `run()` (Ctrl-C → 130, `BrokenPipeError` →
  a quiet 141, everything else to the tool's handler) and `silence_stdout()`
  (was xrdarc's `cli._common.silence_stdout` and xgfalclient's
  `cli._base._silence_stdout`). `report.run_cli` takes its 130 from it.
- `xrdcore.errors.rebuild`, the unpickler the clients' own exceptions reduce
  through, is public (it was `_rebuild`, which xrdclient imported anyway).
- `xrdcore.cli.Fatal`, the handler `run()` hands any other failure to (was
  xrdarc's `cli._fatal` and xrdwms's `cli._fatal`, near copies): an `error`
  record under `--json`/`--xml`, one redacted `prog: message` line on
  stderr, the status from the tool's exception table (`exit_code()`, rows of
  `ExitRule`: a class and a code, or a function of the exception), and a
  bug's traceback logged at DEBUG instead of printed; `Fatal.interrupted`
  says `prog: interrupted` for `run`'s Ctrl-C. xrdclient's `cli.fail` is one.
- `xrdcore.log.configure()`, `verbosity_level()`, `VERBOSITY` and
  `Configured`: a command-line tool's `-v` handler (was built separately by
  xrdarc's `cli._common`, xrdclient's `cli.configure_logging` and
  xgfalclient's `cli._base._Logging`; xrdwms had none). One handler on the
  loggers named, on a given stream or whatever `sys.stderr` is at each
  record, with `FILTER`'s redaction and muting on every record it writes
  (third-party loggers included), replacing its own earlier handler rather
  than adding a second; `Configured.close()` undoes it. Its first argument
  is a level, not a `-v` count, because the tools count on different scales
  (`verbosity_level(count, scale)`) and xrdarc's `-d LEVEL` names one.
- `xrdcore.fanout.Pool`: the long-lived thread pool for work that is not a
  fan-out (xrdclient's asyncio facade and pyxrootd callback dispatcher,
  xrdwms's backend threads and dask executor). Made on first use, sized
  then (the size may be a callable reading configuration), named, and made
  again in a forked child, whose copy of the parent's executor has no
  threads. With `run_all` it is where the chain makes its thread pools: the
  clients' lint bans `ThreadPoolExecutor` and names these two.
- `xrdcore.report.parse(text, output_format)`: the format's reader, giving
  back a finished document of either format as the one JSON tree (XML
  through `safexml`, encoded strings and keys decoded). It replaces the
  `xml_value`/`decode` helpers that xrdcore's, xrdclient's and xgfalclient's
  report tests each carried.
- `xrdcore.sockets`: `recv_exact(sock, count)`, the exactly-N-bytes read
  every framed stream protocol loops on, raising `ShortRead` (an
  `xrdcore.errors.ConnectionError`, so an `OSError`) with how far it got.
  It replaces the copies in xrdsec's KDC client and xgfalclient's dcap fake.

### Packaging

- CI (`.github/workflows/ci.yml`): ruff, `mypy --strict` and the family's
  take-over scorecard (`tests/test_scorecard.py`, measured with
  xrd-devtools' scorecard plugin; `complexipy` and `radon` joined the `dev`
  extra); the suite at 100 % on Linux and macOS, Python 3.11-3.14, with and
  without the `fast` extra; and a package job that builds the sdist and
  wheel, checks their contents (xrd-devtools' `check_dist.py`: no caches,
  coverage data or stray top-level files; `py.typed` present), installs the
  wheel into a clean environment and imports every public module from it.
- Publishing (`.github/workflows/publish.yml`): a `v<version>` tag builds,
  checks that tag, runtime version, wheel metadata and a dated changelog entry
  agree and that the wheel has no default dependency, and uploads to PyPI by
  trusted publishing. xrdcore is the first package of the 0.4.0 chain to be
  published; xrdclient's `docs/releasing.md` is the chain's runbook.
- `CONTRIBUTING.md` and `SECURITY.md`.
- The chain's tooling moved to the new
  [xrd-devtools](https://github.com/rob-c/xrd-devtools) repository, used from
  a checkout (it is not on PyPI and cannot be a dependency):
  `tests/test_scorecard.py` is now a few lines loading its scorecard plugin
  (found through pytest's `pythonpath`: `../xrd-devtools/src` or CI's
  `.dependencies/xrd-devtools/src`; it skips, saying so, without one), in
  place of the copy that located xrdclient's `tools/scorecard.py` through
  `XRDCLIENT_CHECKOUT`/`SCORECARD_REQUIRED`. `.github/scripts/check_dist.py`,
  identical in the three libraries, is xrd-devtools' `check_dist.py`; ruff,
  mypy and complexipy no longer list `.github/scripts`.
- CI (`.github/workflows/ci.yml`) calls xrd-devtools' reusable workflows -
  `static.yml` (ruff, mypy, complexipy, the scorecard, made required, and
  `xrddevtools conformance`), `tests.yml`, `package.yml` and `chain.yml` (the
  duplicate and conformance gates over all seven repositories) - with the
  candidates from its one candidates action; `publish.yml` checks
  xrd-devtools out to run `check_dist.py`. CI therefore needs
  `rob-c/xrd-devtools` to exist.

### Code health

- No grandfathered code: ruff's full rule set (`SIM` added) with nothing
  ignored, `mypy --strict` over the tests and release scripts as well as
  the package, complexipy at 15 with no snapshot, and every warning an
  error under pytest. The suppressions are gone (6 `type: ignore`, 1 `noqa`)
  and so is the untyped-import exemption for google-crc32c, which ships its
  own types. `errors`' pickling helpers are typed; the private
  `safexml._guard_expat` and `pipeline._abort` take any object, as they
  always looked at it by attribute only.
- Banned APIs (ruff `TID251`; CONTRIBUTING.md explains the list): xrdcore
  imports no third-party package, no protocol module (`http.client`,
  `ssl`) and nothing above it in the chain (xrdsec, xrdhttp, the clients).
  It owns `ThreadPoolExecutor` (`fanout`) and the XML parsers (`safexml`),
  which the other repositories ban in its favour.
- Release metadata is xrd-devtools' release gate:
  `tests/test_release_metadata.py` is `release_gate(__file__)` from
  `xrddevtools.pytest_plugin` - one version source (`_version.py`, read by
  hatch), a CHANGELOG led by exactly that version's well-formed section,
  project metadata and README that name this repository, and a release
  workflow built through the shared publish action.
- The publish workflow's build job is xrd-devtools' shared `publish`
  composite action (build, `twine check --strict`, the artifact or
  pre-flight checks, a clean install whose tag, runtime version and dated
  changelog section agree, the artifact upload); only the trusted-publishing
  upload to PyPI stays in this workflow's own job.
- The standard's settings in `pyproject.toml` (ruff rules, the banned-API
  table, mypy's strictness and allowed overrides, complexipy, coverage,
  pytest's warnings filter) and CONTRIBUTING's *The code standard*, *Banned
  APIs* table and *xrd-devtools* sections are managed blocks, written by
  `xrddevtools sync` from the standard kept once in xrd-devtools;
  `xrddevtools conformance` fails a hand edit between the markers. The
  repository's own keys and notes sit beside the blocks.
- `[project.urls]` (Homepage, Documentation, Repository, Issues,
  Changelog), as every other package of the chain has.
