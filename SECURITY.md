# Security policy

## What this tool handles

The viewer and the maps parse **untrusted data**: SQLite databases, `-wal` files and JSON from a Phone Link package, and (for the cable / Bluetooth section) the live registry, setup logs and event logs of the PC it runs on. A crafted database or file can try to crash the parser, make it hang, exhaust memory, or make it write outside its working folder (for example through a blob name or a package name).

How the tool limits that:

- **It never writes to the evidence.** The input folder is copied first, and every copy of a steady source is re-hashed (SHA-256) against its source; a mismatch stops the load. Only the copies are opened, with SQLite `mode=ro` and `query_only`. The source folder is never opened by SQLite.
- **Folder links are not followed** when the Phone Link package is copied.
- **Secrets are never read:** Bluetooth link keys, `PlatformEncryptedKeyStorage.json` and certificate bodies.
- **CSV export is Excel-safe:** a cell that starts with `=`, `+`, `-`, `@`, a tab or a line break is prefixed with `'` unless it is a plain number.
- **Standard user only.** It needs no administrator rights and never asks for them.

Known limits are in the README: SQLECmd itself has no query timeout, writes `-shm`/`-wal` files beside its input and does not neutralise formula-like CSV cells (see *SQLECmd behaviours to know*), and running the viewer on a suspect PC writes its temporary copies to that PC (see *How evidence is handled*).

## Reporting a vulnerability

Please report a security problem **privately**, not in a public issue:

- use GitHub's **Report a vulnerability** button on the repository's *Security* tab ([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)), or
- if that is not available, open a public issue that only asks for a private contact, with no details.

Include the version or commit, what you ran, and what happened. **Build the reproducing file from synthetic data** (see [CONTRIBUTING.md](CONTRIBUTING.md)). Never send real evidence, real phone data or personal data, even privately.

## Supported versions

Only the latest commit on `main` receives fixes.
