# Security Policy

## Supported Version

Only the latest tagged `1.x` release receives security fixes.

## Report a Vulnerability

Do not include cookies, session values, raw platform IDs, private media, database
files, absolute user paths, or private logs in a public issue. Contact the repository
maintainer privately using the security-reporting channel configured on the Git host.
Include the affected version, a sanitized reproduction, impact, and whether the issue
can alter credentials, private files, SQLite state, or publication targets.

## Security Boundaries

- Credentials belong only in the private instance `config` directory.
- AI workers receive only exported bounded packets and schemas.
- The CLI owns SQLite, identity, validation, rendering, publication, and acceptance.
- Login, synchronization, analysis/download, canary, and publication require explicit
  confirmation.
- The project does not support bypassing CAPTCHA, signatures, rate limits, login
  controls, or platform protections.

Before publishing a fork, scan the working tree and complete Git history for secrets,
absolute paths, raw IDs, databases, media, model files, logs, and generated knowledge.
