# Security policy

## Reporting a vulnerability

peekseed is a local, single-user tool, but security reports are still welcome.
Please **do not open a public issue** for a suspected vulnerability.

- Preferred: report privately through the repository's GitHub "Report a
  vulnerability" / private security-advisory flow once the public repository
  exists.
- Until then: contact the maintainers at
  `security@example.invalid` <!-- placeholder: replace with the real security contact before publishing -->.

Include, as applicable:

- the peekseed version (`peekseed version`) and Python/platform;
- the configuration that matters (`PEEKSEED_*` variables with secrets removed,
  whether the server was exposed beyond loopback);
- a description of the impact, reproduction steps and any proof-of-concept;
- whether you want public credit.

The maintainers aim to acknowledge reports within **5 business days** and to
agree a disclosure timeline with the reporter. Please give us a reasonable
window to release a fix before public disclosure; a 90-day coordinated
disclosure default is typical, shorter for actively exploited issues.

## Supported versions

Security fixes are made on the current release line.

| Version | Status | Supported |
|---|---|---|
| `0.1.0rcN` (release candidates) | current pre-release | yes |
| `0.1.0` (final) | planned | yes, once published |
| `< 0.1.0` | development history only | no |
| `0.2.x` | planned | yes, once published |

Release candidates are built from a pinned commit and are not yet on PyPI; if
you are testing an `rc` build, include the exact commit hash in your report
when you can.

## Threat model and scope

The threat model, defended/undefended boundaries, upload limits and environment
variables are documented in [docs/security.md](docs/security.md). In short,
peekseed assumes a **trusted local user on a trusted machine**:

- Loopback bind by default; no TLS termination, no accounts, no multi-tenancy.
- A browser write must come from a page you opened (Origin/Host checks), and
  `PEEKSEED_TOKEN` optionally gates mutating `/api/*` calls.
- Model links are SSRF-checked; API keys are stored mode 0600 and never
  returned to the browser.
- Uploads are bounded and confined to the dataset directory; `/pdf` is inline
  and read-only.

Out of scope by design: a compromised local account, a process that can already
read the workspace, deliberate exposure to an untrusted network without a
reverse proxy/token, and attacks that require code execution on the host.

## Dependency licences

The default dependency closure is permissively licensed; CI runs
`scripts/check_licenses.py` and fails on AGPL/GPL packages. The optional
`pymupdf`/`layout` extras pull in PyMuPDF, which is AGPL-3.0 or commercially
licensed and is **not** part of the default install — see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
