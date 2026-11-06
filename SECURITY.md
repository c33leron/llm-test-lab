# Security

The intentionally vulnerable variants run against isolated synthetic databases. Keep test data synthetic, not production records or real secrets.

For a reproducible bug, include the version, a small synthetic case, expected behavior and the actual state/event evidence. Remove secrets and personal data before sharing anything.

There is no private reporting channel configured yet. For a sensitive finding, ask the maintainer for a private channel without posting the exploit or affected data publicly. If a real credential has leaked, revoke or rotate it; editing a log is not enough.

The lab is not a sandbox for untrusted Python code or dependencies. See [the security boundaries](docs/security.md) before adding profiles, tools or checks.
