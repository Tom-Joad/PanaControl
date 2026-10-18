# CLAUDE.md

Working agreement for this repository.

## What the project is

Remote control for LUMIX cameras over the undocumented `cam.cgi` protocol:
trigger autofocus, start and stop video recording, show the live view.
Developed against a DC-GH6 with firmware 3.01.

The first target platform was a Windows desktop, developed and tested on
macOS, and the app now also runs as a container on a Linux NAS
(linuxserver.io base image, s6-overlay). The code stays platform-neutral: no
registry, no Windows APIs, no hard-wired path separators.

## Hard rules

**No environment values in the repository.** Not in code, comments,
examples, tests, log output or the commit history. That covers IP addresses,
subnets, UDNs, serial numbers, session IDs, device names, Wi-Fi names and
file paths with user names. Where an example is needed, use addresses
reserved for documentation from 192.0.2.0/24. `config.toml` is and stays
gitignored.

**One change per step.** For anything to do with connection setup, timing
or the protocol, change exactly one thing and then test. Several changes at
once make finding the cause impossible, and this camera's failure patterns
are ambiguous anyway.

**Don't roll back confirmed findings.** `docs/protocol.md` records what was
verified on the real device. Whoever wants to deviate from it verifies again
and updates the document; assumptions don't replace measurements.

## Conventions

* Everything in the repository is in English: identifiers, docstrings,
  comments, log lines, interface text, documentation. ASCII in code and
  comments.
* Few dependencies. Currently `httpx`, `fastapi` and `uvicorn`. No OpenCV, no
  Pillow: the camera delivers finished JPEGs, which are passed on unchanged.
* Python 3.11 as the minimum (`tomllib` from the standard library).
* Camera errors are reported as `LumixError`, not as a bare HTTP exception.
  The caller shouldn't have to know that HTTP is involved.
* Logging through the `logging` module, never `print` outside `probe.py`.
* The interface follows the Tom Joad Media design (`static/css/tokens.css`,
  `base.css`, fonts served by the app itself, never from a CDN).

## Pitfalls that have cost time before

The camera holds **exactly one** remote session. An aborted process leaves it
occupied, after which the camera answers every command with `err_busy` until
it is switched off and on. A clean shutdown is not a nicety but a
prerequisite for daily use. Every path that ends the app must send
`stopstream`. In the container, s6 gives the app `S6_SERVICES_GRACETIME`
after SIGTERM; keep it below the stop timeout of `docker stop`.

The header before the JPEG has **no** fixed length. 276, 324 and 340 bytes
were measured in the same run. The JPEG start is searched, never computed.

The mode change via `recmode` takes longer than a second. An HTTP timeout set
too short makes it fail, and afterwards the camera is at `err_busy`.
