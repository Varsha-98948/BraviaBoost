"""Tests for Sony BRAVIA KD-75X9400C specific safety protections and DryRun mode.

Verifies:
1. Rejection of TV hardware, HDMI, tuner, and MediaTek middleware packages.
2. Rejection of Chromecast, screen-mirroring, and DLNA packages.
3. Conditional protection of com.sony.dtv.tvlaunchercustomizer based on active Home launcher.
4. Genuine -DryRun mode: no commands executed on TV, no recovery record written.
5. Handling of single-package debloat lists.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import recovery  # noqa: E402

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

HARDWARE_PROTECTED = [
    "com.sony.dtv.b2b.hotelmode",
    "com.sony.dtv.tvx",
    "com.sony.dtv.tvinput.external",
    "com.sony.dtv.tvinput.tuner",
    "com.mediatek.agent",
    "mtktvapi.agent",
    "com.mediatek.dmagent",
]

CASTING_PROTECTED = [
    "com.google.android.apps.mediashell",
    "com.google.android.sss",
    "com.google.android.sss.authbridge",
    "screnmirroring.com",
    "com.sony.dtv.networkapp.wifidirect",
    "com.sonyericsson.dlna",
    "com.sonyericsson.dlna.dtcpplayer",
    "com.sony.huey.dlna.renderersettings",
    "com.sony.huey.dlna.module",
    "com.vewd.core.integration.dia",
]

FAKE_TV = r'''
import json
import os
import sys

STATE_PATH = os.environ.get("FAKE_TV_STATE")

def load():
    if STATE_PATH and os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"installed": [], "disabled": [], "home": "com.google.android.tvlauncher/.MainActivity", "claims": ["com.google.android.tvlauncher"], "commands": []}

state = load()

def save():
    if STATE_PATH:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)

def answer(command):
    if command == "pm list packages" or command == "pm list packages --user 0":
        return "".join(f"package:{name}\n" for name in state.get("installed", []))
    if command.startswith("pm list packages -d"):
        return "".join(f"package:{name}\n" for name in state.get("disabled", []))
    if command.startswith("pm list packages -e"):
        enabled = [p for p in state.get("installed", []) if p not in state.get("disabled", [])]
        return "".join(f"package:{name}\n" for name in enabled)
    if command.startswith("cmd package query-activities") and "category.HOME" in command:
        return "".join(f"  {c if '/' in c else c + '/.MainActivity'}\n" for c in state.get("claims", []))
    if command.startswith("cmd package resolve-activity") and "category.HOME" in command:
        return (state.get("home") or "") + "\n"
    if command.startswith("cmd package query-activities") and "channel" in command:
        return "com.sony.dtv.tvx/.MainActivity\n"
    if command.startswith("cmd package query-activities") and "SETTINGS" in command:
        return "com.android.tv.settings/com.sony.dtv.settings.MainSettings\n"
    if command.startswith("dumpsys wifi"):
        return "cname=android.uid.system:1000\n"
    if command.startswith("getprop ro.adb.secure"):
        return "1\n"
    if command.startswith("settings get global "):
        return "1.0\n"
    return ""

class Device:
    def shell(self, command, read_timeout_s=None):
        return answer(command)

    def close(self):
        pass

def connect():
    return Device()

def main():
    args = sys.argv[1:]
    if "--batch" in args:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                state.setdefault("commands", []).append(line)
                sys.stdout.write(f"$ {line}\n")
                sys.stdout.write(answer(line))
        save()
        return 0
    cmd = " ".join(args)
    if cmd.startswith("dumpsys wifi"):
        sys.stdout.write("cname=android.uid.system:1000\n")
        return 0
    if cmd.startswith("getprop ro.adb.secure"):
        sys.stdout.write("1\n")
        return 0
    if cmd.startswith("cmd package resolve-activity") and "category.HOME" in cmd:
        sys.stdout.write((state.get("home") or "") + "\n")
        return 0
    if cmd.startswith("cmd package query-activities") and "category.HOME" in cmd:
        for c in state.get("claims", []):
            sys.stdout.write(f"  {c if '/' in c else c + '/.MainActivity'}\n")
        return 0
    if "vnd.android.cursor.item/channel" in cmd:
        sys.stdout.write("com.sony.dtv.tvx/.MainActivity\n==\ncom.android.tv.settings/com.sony.dtv.settings.MainSettings\n")
        return 0
    sys.stdout.write(answer(cmd))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


class RecoveryValidationTest(unittest.TestCase):
    def test_all_hardware_protected_packages_are_identified(self):
        detected = recovery.hardware_protected_packages(HARDWARE_PROTECTED)
        self.assertEqual(sorted(HARDWARE_PROTECTED), sorted(detected))

    def test_hardware_protected_packages_refuse_unrelated_packages(self):
        unrelated = ["com.google.android.music", "com.amazon.aiv.us", "com.example.app"]
        self.assertEqual([], recovery.hardware_protected_packages(unrelated))

    def test_all_casting_protected_packages_are_identified(self):
        detected = recovery.casting_packages(CASTING_PROTECTED)
        self.assertEqual(sorted(CASTING_PROTECTED), sorted(detected))

    def test_casting_packages_refuse_unrelated_packages(self):
        unrelated = ["com.google.android.music", "com.amazon.aiv.us"]
        self.assertEqual([], recovery.casting_packages(unrelated))

    def test_launcher_customizer_conditional_protection(self):
        customizer = "com.sony.dtv.tvlaunchercustomizer"
        stock_home = "com.google.android.tvlauncher/.MainActivity"
        new_home = "com.gathin.tvhome/.MainActivity"

        self.assertTrue(recovery.is_launcher_customizer_protected(customizer, stock_home))
        self.assertFalse(recovery.is_launcher_customizer_protected(customizer, new_home))
        self.assertFalse(recovery.is_launcher_customizer_protected("com.example.app", stock_home))

    def test_validate_debloat_list_catches_all_categories(self):
        names = ["android", "com.sony.dtv.tvx", "com.google.android.apps.mediashell", "com.sony.dtv.tvlaunchercustomizer", "com.example.safe"]
        violations = recovery.validate_debloat_list(names, active_home="com.google.android.tvlauncher/.MainActivity")
        self.assertIn("core", violations)
        self.assertIn("hardware", violations)
        self.assertIn("casting", violations)
        self.assertIn("customizer", violations)
        self.assertNotIn("com.example.safe", violations.get("hardware", []))


class DisablePs1SafetyTest(unittest.TestCase):
    def setUp(self):
        if not POWERSHELL:
            raise unittest.SkipTest("PowerShell is required for disable.ps1 tests")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repo"
        self.tools = self.repo / "tools"
        self.tools.mkdir(parents=True)

        for name in ("disable.ps1", "recovery.py"):
            shutil.copy2(ROOT / "tools" / name, self.tools / name)

        (self.tools / "tv.py").write_text(FAKE_TV, encoding="utf-8")

        self.state_path = self.base / "fake_tv_state.json"
        self.init_fake_tv()

    def tearDown(self):
        self.temp_dir.cleanup()

    def init_fake_tv(self, home="com.google.android.tvlauncher/.MainActivity", claims=None):
        claims = claims or ["com.google.android.tvlauncher"]
        state = {
            "installed": ["com.qterics.da.product", "com.sony.dtv.tvlaunchercustomizer", "com.google.android.tvlauncher"],
            "disabled": [],
            "home": home,
            "claims": claims,
            "commands": [],
        }
        with open(self.state_path, "w") as f:
            json.dump(state, f)

    def run_disable_ps1(self, list_path, extra_args=None):
        env = os.environ.copy()
        env["TV_PYTHON"] = sys.executable
        env["TV_HOST"] = "192.168.1.34"
        env["FAKE_TV_STATE"] = str(self.state_path)
        env["HOME"] = str(self.base / "fake_home")
        env["USERPROFILE"] = env["HOME"]
        os.makedirs(env["HOME"], exist_ok=True)

        cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(self.tools / "disable.ps1"), "-TvHost", "192.168.1.34", str(list_path)]
        if extra_args:
            cmd.extend(extra_args)

        return subprocess.run(cmd, cwd=self.repo, env=env, capture_output=True, text=True, timeout=30)

    def test_rejection_of_hardware_protected_packages(self):
        for pkg in HARDWARE_PROTECTED:
            list_file = self.base / f"debloat_{pkg}.txt"
            list_file.write_text(f"{pkg}\n", encoding="utf-8")
            res = self.run_disable_ps1(list_file)
            self.assertNotEqual(0, res.returncode, f"Expected {pkg} to be rejected")
            output = res.stdout + res.stderr
            self.assertIn("refusing", output)
            self.assertIn("protected TV hardware, HDMI, or platform middleware packages", output)
            self.assertIn(pkg, output)

    def test_rejection_of_casting_packages(self):
        for pkg in CASTING_PROTECTED:
            list_file = self.base / f"debloat_cast_{pkg}.txt"
            list_file.write_text(f"{pkg}\n", encoding="utf-8")
            res = self.run_disable_ps1(list_file)
            self.assertNotEqual(0, res.returncode, f"Expected {pkg} to be rejected")
            output = res.stdout + res.stderr
            self.assertIn("refusing", output)
            self.assertIn("protected casting, screen-mirroring or DLNA packages", output)
            self.assertIn(pkg, output)

    def test_conditional_customizer_protection_when_stock_launcher_is_home(self):
        # Stock launcher is active Home
        self.init_fake_tv(home="com.google.android.tvlauncher/.MainActivity")
        list_file = self.base / "debloat_customizer.txt"
        list_file.write_text("com.qterics.da.product\ncom.sony.dtv.tvlaunchercustomizer\n", encoding="utf-8")

        res = self.run_disable_ps1(list_file, extra_args=["-DryRun"])
        self.assertEqual(0, res.returncode, res.stderr)
        output = res.stdout + res.stderr
        self.assertIn("Skipping com.sony.dtv.tvlaunchercustomizer", output)
        self.assertIn("stock Google TV Launcher", output)
        # Verify customizer is not in the packages that would be disabled
        self.assertIn("- com.qterics.da.product", res.stdout)
        self.assertNotIn("- com.sony.dtv.tvlaunchercustomizer", res.stdout)

    def test_conditional_customizer_allowed_when_custom_launcher_is_home(self):
        # Custom CleanYourTV launcher is active Home
        self.init_fake_tv(home="com.gathin.tvhome/.MainActivity", claims=["com.gathin.tvhome"])
        list_file = self.base / "debloat_customizer_allowed.txt"
        list_file.write_text("com.qterics.da.product\ncom.sony.dtv.tvlaunchercustomizer\n", encoding="utf-8")

        res = self.run_disable_ps1(list_file, extra_args=["-DryRun"])
        self.assertEqual(0, res.returncode, res.stderr)
        output = res.stdout + res.stderr
        self.assertNotIn("Skipping com.sony.dtv.tvlaunchercustomizer", output)
        self.assertIn("- com.sony.dtv.tvlaunchercustomizer", res.stdout)
        self.assertIn("pm disable-user --user 0 com.sony.dtv.tvlaunchercustomizer", res.stdout)

    def test_dry_run_executes_no_commands_and_creates_no_snapshot(self):
        list_file = self.base / "debloat_dryrun.txt"
        list_file.write_text("com.qterics.da.product\n", encoding="utf-8")

        res = self.run_disable_ps1(list_file, extra_args=["-DryRun"])
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("=== DRY-RUN PREVIEW MODE ===", res.stdout)
        self.assertIn("No changes were made to the TV and no recovery record was created.", res.stdout)
        self.assertIn("- com.qterics.da.product", res.stdout)
        self.assertIn("pm disable-user --user 0 com.qterics.da.product", res.stdout)

        # Verify no recovery record was created in fake ~/.cleanyourtv/
        rec_dir = self.base / "fake_home" / ".cleanyourtv"
        records = list(rec_dir.glob("*.json")) if rec_dir.exists() else []
        self.assertEqual([], records, "DryRun must NOT create recovery record")

        # Verify no package modification commands reached the fake TV
        with open(self.state_path) as f:
            state = json.load(f)
        mod_cmds = [c for c in state.get("commands", []) if c.startswith("pm disable") or c.startswith("settings put") or "set-home" in c]
        self.assertEqual([], mod_cmds, "DryRun must NOT send package modification commands to TV")

    def test_single_package_list_handling(self):
        list_file = self.base / "single_package.txt"
        list_file.write_text("# Single package debloat test\ncom.qterics.da.product\n", encoding="utf-8")

        res = self.run_disable_ps1(list_file, extra_args=["-DryRun"])
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("Validated package count: 1", res.stdout)
        self.assertIn("- com.qterics.da.product", res.stdout)
        self.assertIn("pm disable-user --user 0 com.qterics.da.product", res.stdout)
        self.assertIn("pm list packages -d", res.stdout)


if __name__ == "__main__":
    unittest.main()
