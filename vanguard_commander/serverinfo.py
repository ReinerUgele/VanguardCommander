# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Reine Hilfsfunktionen ohne Qt: Server-Status auswerten, Docker-Compose-Befehle bauen, ANSI entfernen."""

from __future__ import annotations

import json
import posixpath
import re
import shlex

# ============================================================================ Server-Status
# Nur lesende Befehle, laufen auch ohne root. CPU-Last ergibt sich aus der Differenz zweier Abfragen.
STATUS_CMD = ("head -n1 /proc/stat; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
              "df -P / | tail -n1; cat /proc/loadavg; nproc 2>/dev/null || echo 1")


def parse_status(out: str) -> dict:
    info: dict = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu" and len(parts) >= 5:
            vals = [int(x) for x in parts[1:] if x.isdigit()]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            info["cpu_total"], info["cpu_idle"] = sum(vals[:8]), idle
        elif parts[0] == "MemTotal:":
            info["mem_total_kb"] = int(parts[1])
        elif parts[0] == "MemAvailable:":
            info["mem_avail_kb"] = int(parts[1])
        elif len(parts) >= 6 and parts[-1] == "/" and parts[4].endswith("%"):
            info["disk_size_kb"], info["disk_used_kb"] = int(parts[1]), int(parts[2])
            info["disk_pct"] = int(parts[4].rstrip("%"))
        elif len(parts) >= 5 and "/" in parts[3] and re.fullmatch(r"\d+\.\d+", parts[0]):
            info["load1"] = float(parts[0])
        elif len(parts) == 1 and parts[0].isdigit():
            info["ncpu"] = int(parts[0])
    return info


def cpu_percent(prev: dict | None, cur: dict) -> float | None:
    if not prev or "cpu_total" not in prev or "cpu_total" not in cur:
        return None
    dt = cur["cpu_total"] - prev["cpu_total"]
    di = cur["cpu_idle"] - prev["cpu_idle"]
    if dt <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (dt - di) / dt))


def level(pct: float, warn: float = 75, crit: float = 90) -> str:
    return "crit" if pct >= crit else ("warn" if pct >= warn else "ok")


# ============================================================================ Docker Compose
_DC = 'if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi; '

COMPOSE_ACTIONS = {
    "pull_up": ("Pull & Up", "$DC {f} pull && $DC {f} up -d"),
    "up": ("Up", "$DC {f} up -d"),
    "restart": ("Restart", "$DC {f} restart"),
    "down": ("Down", "$DC {f} down"),
    "ps": ("Status", "$DC {f} ps -a"),
    "logs": ("Logs", "$DC {f} logs --tail 200 --no-color"),
    "logs_f": ("Logs live", "$DC {f} logs -f --tail 100 --no-color"),
}


def parse_compose_ls(out: str) -> list[dict]:
    """Ausgabe von 'docker compose ls --all --format json' -> [{name, status, files}]."""
    out = out.strip()
    if not out.startswith("["):
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    projects = []
    for r in rows:
        files = [f.strip() for f in str(r.get("ConfigFiles", "")).split(",") if f.strip()]
        if files:
            projects.append({"name": str(r.get("Name", "")), "status": str(r.get("Status", "")), "files": files})
    return projects


def compose_command(files: list[str], action: str) -> str:
    if not files or action not in COMPOSE_ACTIONS:
        raise ValueError("Unbekannte Compose-Aktion oder keine Datei")
    workdir = posixpath.dirname(files[0]) or "/"
    flags = " ".join(f"-f {shlex.quote(f)}" for f in files)
    return _DC + f"cd {shlex.quote(workdir)} && " + COMPOSE_ACTIONS[action][1].format(f=flags)


COMPOSE_LS_CMD = "docker compose ls --all --format json 2>/dev/null || echo '[]'"
DEFAULT_COMPOSE_SEARCH = ["/opt", "/srv", "/home", "/root"]
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def compose_scan_command(search_paths: list[str]) -> str:
    """Findet Compose-Dateien über drei Wege: docker compose ls, Labels aller Container, Dateisuche."""
    paths = " ".join(shlex.quote(p) for p in search_paths if p.startswith("/")) or "/opt"
    names = " -o ".join(f"-name {n}" for n in COMPOSE_NAMES)
    return (
        "echo '###LS'; docker compose ls --all --format json 2>/dev/null || echo '[]'; "
        "echo '###LABELS'; docker ps -a --format '{{.Label \"com.docker.compose.project.config_files\"}}' 2>/dev/null "
        "| tr ',' '\\n' | while read -r f; do [ -n \"$f\" ] && [ -f \"$f\" ] && echo \"$f\"; done; "
        f"echo '###FIND'; find {paths} -maxdepth 5 \\( -name node_modules -o -name .git -o -name proc \\) -prune "
        f"-o -type f \\( {names} \\) -print 2>/dev/null; true"
    )


def parse_compose_scan(out: str, manual: list[str] | None = None) -> list[dict]:
    sections: dict[str, list[str]] = {"LS": [], "LABELS": [], "FIND": []}
    current = None
    for line in out.splitlines():
        tag = line.strip()
        if tag.startswith("###") and tag[3:] in sections:
            current = tag[3:]
        elif current:
            sections[current].append(line.rstrip("\r"))
    projects = parse_compose_ls("\n".join(sections["LS"]))
    seen = {f for p in projects for f in p["files"]}
    extra = [(f, "läuft/lief schon") for f in sections["LABELS"]] + \
            [(f, "gefunden") for f in sections["FIND"]] + [(f, "von Hand") for f in (manual or [])]
    for f, status in extra:
        f = f.strip()
        if not f.startswith("/") or f in seen:
            continue
        seen.add(f)
        folder = posixpath.basename(posixpath.dirname(f)) or "/"
        projects.append({"name": folder, "status": status, "files": [f]})
    return projects


def compose_project_of(labels: str) -> str:
    m = re.search(r"(?:^|,)com\.docker\.compose\.project=([^,]+)", labels or "")
    return m.group(1) if m else ""

# ============================================================================ Text
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r(?!\n)")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)
