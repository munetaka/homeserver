# Security Policy

This is a personal project that runs a sensor and power-monitoring stack on a
Raspberry Pi in the author's home. It is not distributed as a package, and
there are no versioned releases.

## Supported versions

Only the latest commit on `main` is maintained. Fixes are not backported.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/munetaka/homeserver/security/advisories/new)
(Security tab → "Report a vulnerability"). Do not open a public issue or pull
request for a security problem.

Helpful details include:

- the affected file and line, or the workflow name
- what an attacker could do, and under what conditions
- steps to reproduce, if you have them

## In scope

- Code in this repository (`src/`, `scripts/`, `deploy/`)
- GitHub Actions workflows and Dependabot configuration under `.github/`
- Credentials or other secrets accidentally committed to the repository or its history

## Out of scope

- The author's home network, devices, and hosts. Do not scan, probe, or attempt
  to access them; this repository describes them only for the author's own use.
- Vulnerabilities in third-party dependencies (for example `bleak`, `requests`,
  Grafana, VictoriaMetrics). Please report those to the upstream project.
  Dependabot tracks published advisories for this repository.

## What to expect

This project is maintained by one person in their spare time. You can expect
an acknowledgement within about a week. Confirmed issues are fixed on `main` and,
where useful, disclosed through a GitHub security advisory with credit to the
reporter unless you prefer to stay anonymous.
