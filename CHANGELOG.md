# Changelog

## 6.2.0 Beta 2

* Fix: cancelling a download in SSH-only mode froze until the connection was closed. paramiko requested the
  whole file in advance, so the cancel had to wait for the rest of it (for a 90 GB model: minutes, and RAM).
  Read-ahead is now limited, and a cancel cuts the SFTP channel at once (upload too)
* Fix: Esc in the transfer dialog only hid the window while the transfer kept running; it now cancels.
  The dialog shows "Breche ab …" until the transfer has really stopped

## 6.2.0 Beta 1 – first public release

Complete rewrite of the former internal tool (v5).

* Native desktop app for Windows, macOS and Linux (PyQt6), no external `ssh` binary needed
* SSH key login incl. one-click key creation and installation on the server; SSH agent support
* Encrypted password vault (AES-256-GCM), optional "remember on this computer" (DPAPI / Keychain / Secret Service)
* Server daemon deployed automatically as Docker container when Docker is available; SSH-only fallback otherwise
* Dual-pane file manager: drives/places, drag & drop (also from Explorer/Finder), rename, editor that keeps
  owner, permissions, symlinks and line endings, protection against deleting system directories
* Docker: containers, live logs, image pull, **Docker Compose** discovery (running projects, container labels,
  file search) with Pull & Up / Up / Restart / Down and live output
* Real SSH terminals (PTY) in tabs, correct box-drawing characters, sudo password helper
* Live log windows (containers, compose, daemon, journal)
* Server status in the header (CPU, RAM, disk, load)
* Custom buttons for server commands, 28 presets, per-profile buttons, confirmation for risky commands
* UFW firewall tab, keyboard shortcuts (F1), log file, crash protection
* Privacy mode 🕶️ (Ctrl+Shift+P) for screenshots: masks names, hosts, IPs, users and local paths in the display
