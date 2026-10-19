# Security

## Reporting a vulnerability

Mail <robert.andrew.currie@gmail.com> with a description and, if you have one,
a reproducer. Please do not open a public issue for anything that could expose
a credential in a log or a report, let a hostile document reach an XML entity
expander, or make a corrupted transfer pass its checksum. Expect an
acknowledgement within a few working days. A fix is released as a patch
version of every supported minor release; because the family's packages
require `xrdcore>=X.Y.0,<X.Y+1`, a patch release reaches xrdsec, xrdhttp and
the clients without a release of theirs (rebuild the RPM/DEB and Homebrew
bundles, which snapshot it).

Supported versions: the latest `0.x` minor release. Until 1.0, fixes are not
back-ported to older minors.

## What this library is trusted with

xrdcore holds no credential and opens no connection. What it is trusted with is
what every package above it routes through it:

- **XML from the network.** WebDAV `PROPFIND` answers, S3 listings and errors,
  Metalink, SRM SOAP, BDII answers, A-REX job descriptions and VOMS REST
  replies are all parsed by `xrdcore.safexml`. The documents are
  attacker-supplied.
- **Text that may contain credentials.** Log records, error messages and CLI
  reports carry URLs whose query strings may hold bearer tokens, S3 pre-signed
  signatures, EOS capabilities or macaroons. `xrdcore.log` redacts them.
- **Checksums that decide whether a transfer is accepted.** `xrdcore.checksum`
  computes the values compared with what a storage element reports.

## What the implementation guarantees

**No XML entity is ever declared, expanded or fetched.** `safexml.fromstring()`
refuses every `<!DOCTYPE` - the only place XML can declare an entity - *while*
the tree is built, from expat's start-of-declaration event, before the
internal subset is read. The check is expat's, so it is encoding-aware (a
UTF-16 DOCTYPE is caught), and it holds with and without ElementTree's C
accelerator. No external resource is ever read. A refused document raises
`UnsafeXML` (a `ValueError`).

**Credentials are redacted before they reach a log or a terminal.** Every
logger from `log.get_logger()` carries `RedactingFilter`, which interpolates a
record *before* redacting it, so a token is caught even when it only appears
once the format string and its arguments are joined. The patterns cover
`authz=` and `access_token` parameters, S3 and Azure/Google signatures, EOS
capabilities, macaroons and whole `Authorization` header values; they only
ever grow. `redact()` is the same function for text that is not a log record.

**Reports carry results, not secrets.** The `storage-client-report` JSON/XML
document (`xrdcore.report`) records what a tool did; its messages pass through
the same redaction, binary streams become bounded base64 records, and records
are spooled to disk rather than accumulated in memory.

**Checksums are the algorithms servers report, computed exactly.** Adler-32,
CRC-32, CRC-32C, CRC-64/XZ and CRC-64/NVME are integrity checks against
accidental corruption, pinned to their published check values; they are not
authentication. The accelerated CRC-32C (the `fast` extra) and the
pure-Python one give identical answers, and the suite checks both.

**Backoff is bounded and jittered.** `retry.delay()` is capped,
deadline-aware and fully jittered, so a failing server cannot synchronise a
fleet of clients into retrying in step. `retry_after()` only *reads* a
server's `Retry-After` (absent, negative or unparsable values give `None`);
whether to honour it, and how far, is the caller's policy - cap it with the
same `cap`, so a server asking for an hour cannot hang a batch job for one.

**No dependencies and no dynamic execution.** The default install depends on
nothing outside the standard library. Nothing in the package calls `eval`,
`exec` or `subprocess`; exceptions are picklable by design (they cross process
boundaries in the clients' worker pools), but nothing here ever unpickles.

## Limits you should know about

- **Redaction is pattern-based.** It catches the credential spellings the grid
  uses; a secret in a parameter name it does not know is printed. Report a
  missing spelling as a security issue.
- **`safexml` defends the parser, not the application.** A well-formed
  document without a DOCTYPE is parsed in full; size limits are the caller's.
- **Checksums are not signatures.** Anyone who can change the data can change
  its Adler-32 to match.
