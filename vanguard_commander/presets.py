# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Vorlagen für häufig gebrauchte Server-Befehle.

In den Einstellungen unter „Eigene Befehle → Aus Vorlagen“ auswählbar. Übernommene Vorlagen
werden zu ganz normalen eigenen Buttons und lassen sich danach frei ändern.

Felder: group (Abschnitt in der Seitenleiste), name (Button), cmd (bash), root (als root bzw. mit sudo),
confirm (vor dem Ausführen nachfragen – für Befehle, die etwas verändern).
"""

from __future__ import annotations

DEFAULT_GROUP = "Allgemein"


def _p(group: str, name: str, cmd: str, root: bool = False, confirm: bool = False) -> dict:
    return {"group": group, "name": name, "cmd": cmd, "root": root, "confirm": confirm}


PRESETS: list[dict] = [
    # --- System ---------------------------------------------------------------------------
    _p("System", "Systemübersicht", "hostnamectl 2>/dev/null; echo; uptime"),
    _p("System", "Festplatten (df -h)", "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay"),
    _p("System", "Speicher (free -h)", "free -h"),
    _p("System", "Top-Prozesse CPU", "ps aux --sort=-%cpu | head -n 15"),
    _p("System", "Top-Prozesse RAM", "ps aux --sort=-%mem | head -n 15"),
    _p("System", "Größte Ordner in /opt", "du -xh --max-depth=1 /opt 2>/dev/null | sort -rh | head -n 20", root=True),
    _p("System", "Fehlgeschlagene Dienste", "systemctl --failed --no-pager"),
    _p("System", "Fehler im Systemlog (heute)", "journalctl -p err --since today --no-pager | tail -n 60", root=True),
    _p("System", "Neustart nötig?",
       "if [ -f /var/run/reboot-required ]; then cat /var/run/reboot-required; "
       "cat /var/run/reboot-required.pkgs 2>/dev/null; else echo 'Kein Neustart nötig.'; fi"),
    _p("System", "Server neu starten", "systemctl reboot", root=True, confirm=True),
    # --- Wartung --------------------------------------------------------------------------
    _p("Wartung", "Verfügbare Updates", "apt list --upgradable 2>/dev/null"),
    _p("Wartung", "Paket-Cache aufräumen", "apt-get clean && apt-get autoremove -y", root=True, confirm=True),
    _p("Wartung", "Journal auf 14 Tage kürzen", "journalctl --vacuum-time=14d", root=True, confirm=True),
    # --- Docker ---------------------------------------------------------------------------
    _p("Docker", "Docker-Speicherbelegung", "docker system df", root=True),
    _p("Docker", "Ressourcen je Container",
       "docker stats --no-stream --format 'table {{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.NetIO}}'", root=True),
    _p("Docker", "Compose-Projekte", "docker compose ls", root=True),
    _p("Docker", "Images",
       "docker images --format 'table {{.Repository}}\\t{{.Tag}}\\t{{.Size}}\\t{{.CreatedSince}}'", root=True),
    _p("Docker", "Ungenutzte Images löschen", "docker image prune -af", root=True, confirm=True),
    # --- Netzwerk -------------------------------------------------------------------------
    _p("Netzwerk", "IP-Adressen", "ip -br address"),
    _p("Netzwerk", "Öffentliche IP", "curl -s --max-time 5 https://ifconfig.me; echo"),
    _p("Netzwerk", "Offene Ports (ss -tulpn)", "ss -tulpn", root=True),
    _p("Netzwerk", "Aktive Verbindungen", "ss -tn state established | head -n 40"),
    _p("Netzwerk", "DNS-Test", "getent hosts example.org || echo 'DNS-Auflösung fehlgeschlagen'"),
    # --- Sicherheit -----------------------------------------------------------------------
    _p("Sicherheit", "Letzte Anmeldungen", "last -n 20 -a", root=True),
    _p("Sicherheit", "Fehlgeschlagene SSH-Logins (heute)",
       "journalctl -u ssh -u sshd --since today --no-pager | grep -iE 'failed|invalid' | tail -n 30 "
       "|| echo 'Keine Fehlversuche heute.'", root=True),
    _p("Sicherheit", "Fail2ban-Status", "fail2ban-client status", root=True),
    _p("Sicherheit", "Firewall-Status", "ufw status verbose", root=True),
    _p("Sicherheit", "Angemeldete Benutzer", "who -a"),
]

# Die bisherigen Standard-Buttons bleiben wie gehabt erhalten.
DEFAULT_COMMANDS: list[dict] = [
    _p(DEFAULT_GROUP, "Festplatten (df -h)", "df -h"),
    _p(DEFAULT_GROUP, "Speicher (free -m)", "free -m"),
    _p(DEFAULT_GROUP, "Offene Ports (ss -tulpn)", "ss -tulpn", root=True),
]


def normalize_command(c: dict) -> dict | None:
    if not isinstance(c, dict):
        return None
    name, cmd = str(c.get("name", "")).strip(), str(c.get("cmd", "")).strip()
    if not name or not cmd:
        return None
    return {
        "group": str(c.get("group") or DEFAULT_GROUP).strip() or DEFAULT_GROUP,
        "name": name, "cmd": cmd,
        "root": bool(c.get("root", False)), "confirm": bool(c.get("confirm", False)),
        "profile": str(c.get("profile") or "").strip(),   # leer = für alle Profile
    }


def groups_in_order(commands: list[dict], profile: str | None = None) -> list[tuple[str, list[dict]]]:
    """Gruppiert Befehle, Reihenfolge der Gruppen = erstes Vorkommen.
    Mit profile: nur Befehle für alle Profile und für genau dieses Profil."""
    order: dict[str, list[dict]] = {}
    for c in commands:
        if profile is not None and c.get("profile") not in ("", None, profile):
            continue
        order.setdefault(c.get("group") or DEFAULT_GROUP, []).append(c)
    return list(order.items())
