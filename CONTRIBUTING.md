# Contributing

Thanks for your interest! Bug reports, ideas and pull requests are welcome.
*Beiträge auf Deutsch sind genauso willkommen.*

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_commander.py
python -m unittest discover -s tests -v
```

## Guidelines

* Keep the app **cross-platform** (Windows, macOS, Linux). No platform-only APIs without a fallback.
* Network work never runs in the GUI thread – use `TaskPool.run(...)` (`ui/workers.py`).
* Server commands: quote paths with `shlex.quote`; never build shell strings from unvalidated input.
* Anything that changes files on a server must respect `protocol.write_problem()` / `delete_problem()`.
* Logic without Qt (protocol, parsing, commands) belongs in testable modules (`serverinfo.py`, `protocol.py`, …)
  and gets a unit test in `tests/`.
* The daemon (`server/vanguard_node_daemon.py`) uses the **standard library only**. If you change its
  behaviour, bump its `VERSION` – clients then redeploy it automatically.
* UI texts are currently German. English translations (Qt Linguist) are a welcome contribution.

## License

By contributing you agree that your contribution is licensed under the **GPL-3.0-or-later**.
Add yourself to `AUTHORS` if you like.
