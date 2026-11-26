# Third-party software / Fremdsoftware

Vanguard Commander is licensed under the **GNU General Public License v3.0 or later** (see `LICENSE`).
The distributed executables bundle the following third-party components under their own licenses:

| Component | Purpose | License |
|---|---|---|
| [Python](https://www.python.org/) | Runtime | PSF License |
| [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) | GUI bindings | GPL v3 (or commercial) |
| [Qt 6](https://www.qt.io/) (PyQt6-Qt6) | GUI toolkit | LGPL v3 |
| [paramiko](https://www.paramiko.org/) | SSH / SFTP | LGPL v2.1 |
| [pyte](https://github.com/selectel/pyte) | Terminal emulation (VT100/xterm) | LGPL v3 |
| [wcwidth](https://github.com/jquast/wcwidth) | Character widths (used by pyte) | MIT |
| [cryptography](https://cryptography.io/) | Vault encryption, SSH keys | Apache 2.0 or BSD 3-Clause |
| [bcrypt](https://github.com/pyca/bcrypt) | SSH key passphrases | Apache 2.0 |
| [PyNaCl](https://github.com/pyca/pynacl) | Ed25519 (used by paramiko) | Apache 2.0 |
| [cffi](https://cffi.readthedocs.io/) / [pycparser](https://github.com/eliben/pycparser) | Native bindings | MIT / BSD 3-Clause |
| [PyInstaller](https://pyinstaller.org/) bootloader | Executable packaging | GPL v2 with bootloader exception |

The server-side daemon (`vanguard_commander/server/vanguard_node_daemon.py`) uses only the Python standard
library and runs in the official `python:3.12-slim` Docker image.

The complete source code of Vanguard Commander, including build scripts, is available in this repository, so
LGPL components can be replaced or relinked by rebuilding the application (`build/`).

License texts of the components are included in the respective Python packages (`*.dist-info/`).
