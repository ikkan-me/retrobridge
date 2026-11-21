# Security

## Reporting a vulnerability

Please **do not open a public issue** for a security vulnerability.
Use the repository's *Security → Report a vulnerability* tab, or contact the
maintainer privately.

Include when possible:

- the affected version (`retrobridge --version`);
- reproduction steps;
- the estimated impact (file read/write, command execution, RPC secret leak,
  etc.).

## Scope

RetroBridge runs locally with the user's privileges and handles paths supplied
in its configuration. Sensitive areas include:

- the Transmission RPC password, read from `RETROBRIDGE_RPC_PASSWORD`;
- external conversions (`chdman`, `dolphin-tool`);
- library, download, and SD mount paths.
