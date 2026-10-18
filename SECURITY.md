# Security policy

## Supported versions

Only the latest release receives fixes. Security fixes go into a new
release of the current major version.

## Reporting a vulnerability

Please do **not** open an issue for security problems. Report them privately
through GitHub instead: on the
[Security tab](https://github.com/Tom-Joad/PanaControl/security), choose
**Report a vulnerability**.

## Scope notes

The web interface has no login: whoever reaches it controls the camera
(autofocus, start and stop recording, ISO) and sees the live view. Run it on
a trusted network only, or behind a reverse proxy with authentication. Of
particular interest:

- a way to reach the camera or other hosts through the app beyond the
  documented routes
- camera details (address, UDN, serial number, session ID) ending up in
  responses or logs where they don't belong
- a way to leave the camera's remote session occupied from outside, which
  locks the camera until it is switched off and on

The container starts as root only to switch to `PUID:PGID`, runs the app as
that unprivileged user, and is meant to run with a read-only root file
system and all capabilities dropped but `SETUID` and `SETGID`.

## Supply chain

Images are built by GitHub Actions from version tags only. Third-party
actions are pinned to commit SHAs, the base image to a digest. Dependencies
are pinned and checked with `pip-audit`, and the repository is scanned with
`gitleaks`. Images are scanned with Trivy and signed keylessly with cosign.
To verify an image (after `docker login ghcr.io`, the image is private):

```bash
cosign verify ghcr.io/tom-joad/panacontrol:latest \
  --certificate-identity-regexp 'https://github.com/Tom-Joad/PanaControl/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```
