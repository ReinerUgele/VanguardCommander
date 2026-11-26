# Security Policy

Vanguard Commander manages servers with **root privileges**. Security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** in this repository.

Include what you found, how to reproduce it and which version you used (F1 shows the version).
You will get an answer as soon as possible; fixes are released as a new version with credit if you like.

## Security model (summary)

* SSH is the only network entry point. Host keys are pinned on first connect (fingerprint confirmation);
  a changed host key aborts the connection.
* The server daemon listens on `127.0.0.1` only and is reached through an SSH channel – no open port.
* Every daemon request is signed with HMAC-SHA256 over all fields, with a time window and a one-time nonce;
  every response is signed as well.
* The daemon secret lives only on the server (`~/.vanguard/daemon.secret`, mode 0600).
* **Important:** anyone who can log in as that SSH user can read the secret and therefore act as root –
  exactly like any member of the `docker` group. Protect the SSH account accordingly (keys, no password login).
* Local passwords are stored in an AES-256-GCM vault (scrypt key derivation). "Remember on this computer"
  uses Windows DPAPI / macOS Keychain / Linux Secret Service and is opt-in.
* Accident protection: critical files (`/etc/shadow`, sudoers, SSH host keys …) are never written and system
  directories (`/`, `/etc`, `/usr`, `/home/<user>` …) are never deleted – enforced by client and daemon.

## Supported versions

Only the latest release receives fixes during the beta phase.
