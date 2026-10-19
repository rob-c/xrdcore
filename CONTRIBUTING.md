# Contributing to xrdcore

xrdcore is the dependency-free bottom layer of the storage-client family:
xrdsec, xrdhttp, xrdclient, xgfalclient and xrdarc all import it, and every
one of their `--help` screens loads it. Changes are welcome when they keep the
three properties it is built around: it needs nothing but the standard library
to import (the `fast` extra only accelerates CRC-32C); it holds only what more
than one package of the family needs and nothing protocol-specific; and it
depends on no other package of the family, ever.

## Development setup

Python 3.11 through 3.14 are supported. xrdcore has no dependencies, so a
checkout on its own is a complete development environment:

```console
$ python3 -m venv .venv
$ .venv/bin/python -m pip install -U pip
$ .venv/bin/python -m pip install -e '.[dev,fast]'
$ .venv/bin/pytest -q -n auto
```

`pyproject.toml` puts `src` on the test path, so a suite run from the shared
workspace virtual environment needs nothing installed. The suite is hermetic.
Run it once without the `fast` extra as well: the CRC-32C tests then cover the
pure-Python path, and CI runs both.
`mypy` wants the `fast` extra installed: google-crc32c ships its own types,
and the accelerated path is checked against them.

## Definition of done

A change is ready when all of these pass, as CI runs them:

```console
$ ruff check src tests
$ ruff format --check src tests
$ mypy
$ complexipy
$ pytest -q -p no:cacheprovider -n auto --cov --cov-report=term-missing:skip-covered
$ python -m build && twine check --strict dist/*
$ python ../xrd-devtools/src/xrddevtools/check_dist.py contents xrdcore dist
$ xrddevtools conformance --repo .
```

- **Coverage is 100 % of lines and branches** (`fail_under = 100`). Reach it
  with a test that makes the case happen - a hostile document, a cancelled
  copy, a missing accelerator - not with an exclusion.
- **The code standard** (below) holds for every line: nothing suppressed,
  nothing ignored, no snapshot. Fix the code rather than silencing a finding.
- **Types are `mypy --strict`.** The package ships `py.typed`; its annotations
  are part of the API of every package above it.
- **The take-over scorecard.** `tests/test_scorecard.py` loads the chain's
  scorecard plugin (`xrddevtools.pytest_plugin`) from an
  [xrd-devtools](https://github.com/rob-c/xrd-devtools) checkout and fails if
  any of the twelve measurements of `src` is worse than the median of 39
  widely used Python libraries. Do not lower the bar to land a change; split
  the function, group its options, or explain the code.
- **The chain's gates** pass: the scorecard above, the release-metadata gate
  (`tests/test_release_metadata.py`: one version source, a CHANGELOG led by
  that version, metadata that agrees), `xrddevtools conformance` (the
  standard, and every managed block as `xrddevtools sync` writes it) and
  `xrddevtools dupes` (no function copied into another repository) - all
  from [xrd-devtools](#xrd-devtools).
- **Documentation.** Every module, class and function has a docstring that
  explains purpose, contract, failure modes and why - not a restatement of the
  signature. The README's module table is the package's map; keep it current.
- **Changelog.** Add user-visible changes to [CHANGELOG.md](CHANGELOG.md) in
  the same pull request, and say which package a moved name came from.

## The code standard

<!-- >>> xrddevtools sync: code-standard (generated from xrd-devtools/src/xrddevtools/standard) >>> -->
Every repository of the chain is held to one code standard, and every line
of it is held to the standard: old code passes the same checks as new code.

- **Ruff** selects `E`, `F`, `W`, `I`, `UP`, `B`, `C4`, `SIM`, `RUF`, the
  high-confidence security checks `S102`, `S307`, `S506`, `S602`, `S608`,
  `S609`, `S701` and the banned-API rule `TID251` (see *Banned APIs*), with
  `ignore = []` and no per-file ignores, over `src`, `tests`; `ruff format`
  formats it, 100 columns wide.
- **mypy** is strict (Python 3.11) over `src`, `tests`. A test function may
  leave its fixtures unannotated where `pyproject.toml` allows it, but its
  body is still checked; only a named third-party module that ships no types
  may be imported untyped, with the reason beside it.
- **complexipy** allows no function above 15, over `src`, `tests`, with no
  snapshot of old offenders.
- **Coverage** is 100 % of lines and branches, reached by a test that makes
  each case happen. The only exclusions are `if TYPE_CHECKING:`,
  `@overload`, `raise NotImplementedError`, `\.\.\.$`, `if __name__ ==
  .__main__.:`; there is no `omit`.
- **Every warning is an error** (`filterwarnings = ["error"]`): ours are
  fixed, not filtered.
- **No suppression comment**: no `# noqa`, `# pragma: no cover`, `# fmt:
  off`, `# complexipy: ignore` or `# nosec`. A `# type: ignore[code]` only
  where it cannot be avoided (a test passing a wrongly typed value on
  purpose), with its error code and the reason beside it or just above.
- **No snapshot or baseline** of old findings, for any tool.

The settings behind these rules are the managed blocks of `pyproject.toml`
(between `# >>> xrddevtools sync` markers), and this section is one of the
managed blocks of this file (between `<!-- >>> xrddevtools sync` markers).
`xrddevtools sync` writes them all from the standard, kept once in
xrd-devtools (`src/xrddevtools/standard`); `xrddevtools conformance`, which
CI runs, fails if one is edited by hand. To change the standard, change it
there and run `xrddevtools sync --workspace ..` over every repository.
<!-- <<< xrddevtools sync: code-standard <<< -->

## Banned APIs

<!-- >>> xrddevtools sync: banned-apis (generated from xrd-devtools/src/xrddevtools/standard) >>> -->
Ruff's `TID251` rule refuses the APIs whose job another part of the chain
does, in every tree ruff checks (`src`, `tests`), with no per-file ignore
and no `noqa`; the list is `[tool.ruff.lint.flake8-tidy-imports.banned-api]`
in `pyproject.toml`, and each entry's message names what to use instead. A
need for a banned API is met by the library that owns it growing an API,
with its tests - never by an exception here.

| Banned | Use instead | Why |
|---|---|---|
| `urllib3`, `requests`, `httpx` | the chain has no third-party HTTP stack: use xrdhttp.session.Session (pooling, redirects, retries; TLS from xrdsec.tls) | one HTTP stack for the whole chain, with its TLS, redirect and retry rules |
| `urllib.request` | use xrdhttp.session.Session: urllib's opener has none of the chain's TLS, redirect or retry rules | urllib's opener knows none of the grid's TLS (CAs, CRLs, proxies) and follows redirects with credentials attached |
| `gssapi` | Kerberos GSS-API is xrdsec.krb5.gss (the xrdsec[krb5] extra), one wrapper for every client | one GSS-API wrapper, behind the `xrdsec[krb5]` extra |
| `krb5` | pykrb5 is not a dependency: credential caches are xrdsec.krb5.caches / xrdsec.krb5.ccache, GSS-API is xrdsec.krb5.gss | the pure-Python ccache, KCM and keyring readers replaced it |
| `botocore`, `boto3` | S3 signing and the S3 codec are xrdhttp.s3 (the xrdhttp[s3] extra); only xrdhttp.s3 imports botocore | S3 signing and the S3 codec, behind the `xrdhttp[s3]` extra |
| `http.client` | xrdcore speaks no protocol: HTTP is xrdhttp's (xrdhttp.session) | - |
| `ssl` | xrdcore speaks no protocol: TLS contexts are xrdsec.tls's | - |
| `cryptography` | xrdcore has no third-party dependency: cryptography belongs in xrdsec | - |
| `asn1crypto` | xrdcore has no third-party dependency: ASN.1 belongs in xrdsec (xrdsec.der) | - |
| `xrdsec`, `xrdhttp`, `xrdclient`, `xgfalclient`, `xrdarc`, `xrdwms` | - | they are built on this repository; the dependency direction never points up |
<!-- <<< xrddevtools sync: banned-apis <<< -->

What xrdcore owns, it does not ban: `concurrent.futures.ThreadPoolExecutor`
(`fanout.run_all` and `fanout.Pool` are built on it) and the
`xml.etree.ElementTree` parsers (`safexml` is built on them, and its tests
compare against them). The other repositories ban both, naming these
modules instead - so a new need there (another kind of pool, another kind
of XML input) is met by an API here, with its tests, not by an exception
there.

## xrd-devtools

<!-- >>> xrddevtools sync: devtools (generated from xrd-devtools/src/xrddevtools/standard) >>> -->
The chain's gates and tools - the take-over scorecard and the
release-metadata and maintainability gates, `xrddevtools conformance`,
`dupes` and `sync`, the release pre-flight and the packaging tools - live in
[xrd-devtools](https://github.com/rob-c/xrd-devtools). It is never uploaded
to PyPI and cannot be a dependency (PyPI refuses direct-URL requirements),
so it is used from a checkout:

- **A developer** keeps it beside this repository (`../xrd-devtools`, the
  workspace layout). pytest's `pythonpath` lists `../xrd-devtools/src`, so
  the gate tests find it with nothing installed; `pip install -e
  ../xrd-devtools` once gives the `xrddevtools` command.
- **CI** checks it out under `.dependencies/xrd-devtools` (the shared
  `candidates` action always does), which `pythonpath` lists too. It loads
  the plugin with `pytest -p xrddevtools.pytest_plugin`, which fails at
  start-up if it cannot be imported, and sets `XRD_DEVTOOLS_REQUIRED=1`, so
  a missing radon or complexipy fails rather than skips. This repository's
  workflows are xrd-devtools' reusable workflows and actions: CI runs only
  once `rob-c/xrd-devtools` exists.
- **Anywhere else** - a wheel-only test environment, a distribution build -
  `pip install -e` a checkout or put its `src` on `PYTHONPATH`. Without one,
  the gate tests (`tests/test_scorecard.py`,
  `tests/test_release_metadata.py`) skip, saying why.
<!-- <<< xrddevtools sync: devtools <<< -->

## Layering and compatibility

- **No dependencies.** `dependencies = []` in `pyproject.toml` is a contract:
  the publish workflow refuses a wheel with a default requirement. An
  accelerator goes behind an extra with a pure-Python fallback, as
  `google-crc32c` does.
- **Imports.** xrdcore never imports xrdsec, xrdhttp or a client, and
  `import xrdcore` imports no submodule; `tests/test_package.py` pins both.
- **Shared formats are API.** The `storage-client-report` document
  (`xrdcore.report`) is read by scripts written against any of the family's
  tools; a change that would make an existing consumer misread it bumps its
  `VERSION`. Redaction patterns (`xrdcore.log`) only ever grow.
- **Exceptions.** The clients join `xrdcore.errors` by identity or multiple
  inheritance (`xrdclient.XRootDError is XrdError`), so a rename, a new base
  class or a changed attribute is a change to their public API too. Add,
  deprecate, then remove; never change what an existing keyword means.

## Releasing

xrdcore is released first: everything else in the family requires it. The
whole chain's runbook is xrdclient's
[docs/releasing.md](https://github.com/rob-c/xrdclient/blob/main/docs/releasing.md)
("Releasing the 0.4.0 chain").

1. Set `__version__` in `src/xrdcore/_version.py` (the build reads it from
   there) and date the version's [CHANGELOG.md](CHANGELOG.md) entry.
2. Wait for CI on the release commit, then push an annotated `v<version>` tag.
   The publish workflow rebuilds the sdist and wheel, checks the artifact
   contents, that the tag, the runtime version, the wheel metadata and a dated
   changelog entry agree and that the wheel has no default dependency, and
   uploads to PyPI through trusted publishing. Never upload a local build.
3. Wait until `pip download --only-binary=:all: xrdcore==<version>` succeeds
   from a clean environment before tagging xrdsec or xrdhttp: their publish
   jobs install against this release from PyPI.
