# Security

LLM Test Lab intentionally contains ownership-check-disabled variants **inside synthetic per-attempt databases**. This is a teaching feature, not permission to weaken host controls or use real data.

Do not post API keys, `.env`, credentials, cookies or real personal data in issues. A useful report contains a minimal synthetic case, versions, expected boundary, observed action, independent state/audit proof, and redacted evidence. Rotate any real credential that was exposed; editing a log is not revocation.

There is no public security inbox configured yet. Until the owner provides a private reporting channel, do not publish sensitive exploit details or secrets in a public issue. Ordinary synthetic correctness bugs can be described without production data.

The process tree and SQLite isolation are not a sandbox against malicious local Python code or the computer owner. Static reports escape untrusted content and use no external resources. Profiles and custom trusted code should be reviewed before use. Live TLS/endpoint/model settings must not be relaxed to obtain a green test.

See [threat model](docs/security.md) and [current release gaps](docs/release-readiness.md).
