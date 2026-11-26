# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Tests für Statusauswertung, Compose-Befehle und ANSI-Filter (ohne Qt)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vanguard_commander import serverinfo as si  # noqa: E402

SAMPLE = """cpu  4705 356 584 3699176 23060 0 277 0 0 0
MemTotal:        8146348 kB
MemAvailable:    6123456 kB
/dev/vda1        81000000 36450000 40000000      48% /
0.42 0.37 0.30 2/412 12345
4
"""


class StatusTest(unittest.TestCase):
    def test_parse(self):
        info = si.parse_status(SAMPLE)
        self.assertEqual(info["mem_total_kb"], 8146348)
        self.assertEqual(info["mem_avail_kb"], 6123456)
        self.assertEqual(info["disk_pct"], 48)
        self.assertAlmostEqual(info["load1"], 0.42)
        self.assertEqual(info["ncpu"], 4)
        self.assertEqual(info["cpu_idle"], 3699176 + 23060)

    def test_cpu_percent(self):
        a = {"cpu_total": 1000, "cpu_idle": 900}
        b = {"cpu_total": 1200, "cpu_idle": 1000}
        self.assertAlmostEqual(si.cpu_percent(a, b), 50.0)
        self.assertIsNone(si.cpu_percent(None, b))

    @unittest.skipUnless(os.path.exists("/proc/stat"), "nur unter Linux")
    def test_real_command(self):
        out = subprocess.run(["bash", "-c", si.STATUS_CMD], capture_output=True, text=True).stdout
        info = si.parse_status(out)
        for key in ("cpu_total", "mem_total_kb", "mem_avail_kb", "load1", "ncpu"):
            self.assertIn(key, info, out)


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "braucht bash")
class ComposeTest(unittest.TestCase):
    def test_parse_ls(self):
        out = '[{"Name":"opt","Status":"running(5)","ConfigFiles":"/opt/docker-compose.yml"},' \
              '{"Name":"x","Status":"exited(1)","ConfigFiles":"/srv/x/a.yml,/srv/x/b.yml"}]'
        p = si.parse_compose_ls(out)
        self.assertEqual(p[0]["files"], ["/opt/docker-compose.yml"])
        self.assertEqual(p[1]["files"], ["/srv/x/a.yml", "/srv/x/b.yml"])
        self.assertEqual(si.parse_compose_ls("[]"), [])
        self.assertEqual(si.parse_compose_ls("unknown command"), [])

    def test_command_syntax(self):
        for action in si.COMPOSE_ACTIONS:
            cmd = si.compose_command(["/opt/mein projekt/docker-compose.yml"], action)
            r = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cd '/opt/mein projekt'", cmd)

    def test_scan_finds_files_and_skips_junk(self):
        root = Path(tempfile.mkdtemp())
        (root / "projekt" / "node_modules" / "x").mkdir(parents=True)
        (root / "web").mkdir()
        (root / "projekt" / "docker-compose.yml").write_text("x")
        (root / "web" / "compose.yaml").write_text("x")
        (root / "projekt" / "node_modules" / "x" / "docker-compose.yml").write_text("x")
        d = Path(tempfile.mkdtemp())
        fake = d / "docker"
        fake.write_text("#!/bin/sh\n"
                        f"if [ \"$1 $2\" = \"compose ls\" ]; then echo '[{{\"Name\":\"projekt\",\"Status\":\"running(2)\","
                        f"\"ConfigFiles\":\"{root}/projekt/docker-compose.yml\"}}]'; exit 0; fi\n"
                        f"if [ \"$1\" = \"ps\" ]; then echo '{root}/web/compose.yaml'; echo '/weg/alt.yml'; fi\n")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
        out = subprocess.run(["bash", "-c", si.compose_scan_command([str(root)])], capture_output=True, text=True,
                             env=env).stdout
        projects = si.parse_compose_scan(out, ["/srv/hand/docker-compose.yml"])
        files = [p["files"][0] for p in projects]
        self.assertEqual(files, [f"{root}/projekt/docker-compose.yml", f"{root}/web/compose.yaml",
                                 "/srv/hand/docker-compose.yml"])
        self.assertEqual(projects[0]["status"], "running(2)")
        self.assertNotIn("/weg/alt.yml", files)

    def test_project_label(self):
        self.assertEqual(si.compose_project_of("a=b,com.docker.compose.project=opt,x=y"), "opt")
        self.assertEqual(si.compose_project_of(""), "")

    def test_command_runs_with_fake_docker(self):
        d = Path(tempfile.mkdtemp())
        fake = d / "docker"
        fake.write_text('#!/bin/sh\nif [ "$1 $2" = "compose version" ]; then exit 0; fi\necho "ARGS: $*"; pwd\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
        cmd = si.compose_command(["/tmp/docker-compose.yml"], "pull_up")
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env).stdout
        self.assertIn("ARGS: compose -f /tmp/docker-compose.yml pull", out)
        self.assertIn("ARGS: compose -f /tmp/docker-compose.yml up -d", out)
        self.assertIn("/tmp", out)


class PresetTest(unittest.TestCase):
    def test_profile_filter(self):
        from vanguard_commander.presets import groups_in_order, normalize_command
        cmds = [normalize_command(c) for c in ({"name": "a", "cmd": "x"},
                                               {"name": "b", "cmd": "y", "profile": "Webserver", "group": "G"},
                                               {"name": "c", "cmd": "z", "profile": "Homelab"})]
        names = lambda prof: [c["name"] for _g, lst in groups_in_order(cmds, prof) for c in lst]  # noqa: E731
        self.assertEqual(names("Webserver"), ["a", "b"])
        self.assertEqual(names("Homelab"), ["a", "c"])
        self.assertEqual(names(None), ["a", "c", "b"])


class AnsiTest(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(si.strip_ansi("\x1b[32mgrün\x1b[0m text\r\n"), "grün text\r\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
