"""Tests for the Mosquitto password-file backend (addon/app/mosquitto.py).

Run from the repo root, no root or Docker needed:

    python3 -m unittest discover -s tests

Integration check against a real Mosquitto (manual, needs Docker): start the broker from
docker-compose.standalone.yml, add a login through mosquitto.add_mosquitto_login() from a
container that shares its PID namespace and the auth volume (as the app does), then connect
with mosquitto_pub: the new login works without a broker restart, connected clients stay
connected, and a removed login is refused after the next reload.
"""

import base64
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon", "app"))

try:
    import aiohttp  # noqa: F401
except ImportError:
    # The password-file backend does not use aiohttp, only the Supervisor one does.
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

import mosquitto  # noqa: E402


def setUpModule():
    logging.disable(logging.CRITICAL)  # the error paths log on purpose


def tearDownModule():
    logging.disable(logging.NOTSET)

# Entries made by `mosquitto_passwd -b` from the eclipse-mosquitto 2.0.23 and 2.1.4 images.
MOSQUITTO_2_0_ENTRY = (
    "$7$101$ZGve71XdTqhn/Y/Y$fZtViwqb4m9x9lfhXGC439SHBLEIrdVeDqkpX5FKQ4MWYgzZz79C8+J4Gcmnex9U6KHk/OTPjBNk86Tqi4895w=="
)
MOSQUITTO_2_1_ENTRY = (
    "$7$20000$OnNDMXmLn6ITjVCuxZj/dPHphfrwGDChPDjcq9EQsK2fZyW66G7pPBllXdEmr3opz3w9cfoRgnBj6o4lEZpM7A==$"
    "vQCgu/xYPFs4XlRZAvbillrd8x7vgh3rkHTsuwmCB89cTaEKTpcSHhiRzWHHltYJsJoFIRT9OGCDFcRsiXtA6g=="
)
MOSQUITTO_2_1_ODD_PASSWORD_ENTRY = (
    "$7$20000$MNUkPoFMutDeYiDapxPfSLebeSSXfGqF8WW+ey4vh4jtdUgvaVIXkW17bKrSgHc0tXJDUA3L0dHrPoYU+M4OoA==$"
    "lpAhqV2idLONXbXrPC3JAUqVpwTHEuvHSn+vtaLYstuT5odQvS5qnx+tJJ2VUcgRI0DJX4aX+vqkE/c8e8VMXQ=="
)


class HashTests(unittest.TestCase):
    def test_format(self):
        entry = mosquitto._hash_password("secret1")
        self.assertRegex(entry, r"^\$7\$\d+\$[A-Za-z0-9+/]+={0,2}\$[A-Za-z0-9+/]+={0,2}$")
        _, _, iterations, salt, digest = entry.split("$")
        self.assertEqual(int(iterations), mosquitto._HASH_ITERATIONS)
        self.assertEqual(len(base64.b64decode(salt)), mosquitto._HASH_SALT_BYTES)
        self.assertEqual(len(base64.b64decode(digest)), 64)

    def test_salt_is_random(self):
        self.assertNotEqual(mosquitto._hash_password("secret1"), mosquitto._hash_password("secret1"))

    def test_round_trip(self):
        entry = mosquitto._hash_password("p:ss w0rd é")
        self.assertTrue(mosquitto._verify_password("p:ss w0rd é", entry))
        self.assertFalse(mosquitto._verify_password("p:ss w0rd", entry))

    def test_verifies_entries_made_by_mosquitto_passwd(self):
        self.assertTrue(mosquitto._verify_password("secret1", MOSQUITTO_2_0_ENTRY))
        self.assertTrue(mosquitto._verify_password("secret1", MOSQUITTO_2_1_ENTRY))
        self.assertTrue(mosquitto._verify_password("p:ss w0rd", MOSQUITTO_2_1_ODD_PASSWORD_ENTRY))
        self.assertFalse(mosquitto._verify_password("wrong", MOSQUITTO_2_1_ENTRY))

    def test_unknown_formats_do_not_verify(self):
        for stored in ("", "plain", "$6$abc$def", "$7$abc$AAAA$AAAA", "$7$1$!!!$AAAA", "$7$1$AAAA", "$7$0$AAAA$AAAA"):
            self.assertFalse(mosquitto._verify_password("secret1", stored), stored)


class PasswdFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "passwd")
        env = mock.patch.dict(os.environ, {"MOSQUITTO_PASSWD_FILE": self.path})
        env.start()
        self.addCleanup(env.stop)
        reload_ = mock.patch.object(mosquitto, "_reload_mosquitto", return_value=True)
        self.reload = reload_.start()
        self.addCleanup(reload_.stop)

    def lines(self):
        with open(self.path) as f:
            return f.read().splitlines()

    def entry(self, username):
        return next(l.partition(":")[2] for l in self.lines() if l.startswith(username + ":"))

    async def test_backend_selected_by_env(self):
        self.assertTrue(mosquitto.passwd_backend_active())
        with mock.patch.dict(os.environ, {"MOSQUITTO_PASSWD_FILE": ""}):
            self.assertFalse(mosquitto.passwd_backend_active())

    async def test_add_creates_missing_file(self):
        self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "pw1"), "added")
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(mosquitto._verify_password("pw1", self.entry("dev1")))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.reload.assert_called_once()

    async def test_add_exists_conflict_and_update(self):
        await mosquitto.add_mosquitto_login("dev1", "pw1")
        self.reload.reset_mock()
        before = self.lines()

        self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "pw1"), "exists")
        self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "other"), "conflict")
        self.assertEqual(self.lines(), before)
        self.reload.assert_not_called()

        self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "other", allow_update=True), "added")
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(mosquitto._verify_password("other", self.entry("dev1")))
        self.reload.assert_called_once()

    async def test_existing_logins_and_comments_are_kept(self):
        with open(self.path, "w") as f:
            f.write(f"# managed by hand\nalice:{MOSQUITTO_2_1_ENTRY}\n\n")
        self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "pw1"), "added")
        self.assertEqual(self.lines()[:2], ["# managed by hand", f"alice:{MOSQUITTO_2_1_ENTRY}"])
        # A login made elsewhere (mosquitto_passwd) is recognised as the same one...
        self.assertEqual(await mosquitto.add_mosquitto_login("alice", "secret1"), "exists")
        # ...and is protected from being overwritten unless the caller says it is ours.
        self.assertEqual(await mosquitto.add_mosquitto_login("alice", "nope"), "conflict")
        self.assertEqual(await mosquitto.remove_mosquitto_login("dev1"), "removed")
        self.assertEqual(self.lines(), ["# managed by hand", f"alice:{MOSQUITTO_2_1_ENTRY}", ""])

    async def test_remove(self):
        await mosquitto.add_mosquitto_login("dev1", "pw1")
        await mosquitto.add_mosquitto_login("dev2", "pw2")
        self.reload.reset_mock()
        self.assertEqual(await mosquitto.remove_mosquitto_login("dev1"), "removed")
        self.assertEqual([l.partition(":")[0] for l in self.lines()], ["dev2"])
        self.reload.assert_called_once()
        self.reload.reset_mock()
        self.assertEqual(await mosquitto.remove_mosquitto_login("dev1"), "not_found")
        self.reload.assert_not_called()

    async def test_remove_with_missing_file(self):
        self.assertEqual(await mosquitto.remove_mosquitto_login("dev1"), "not_found")
        self.assertFalse(os.path.exists(self.path))

    async def test_invalid_usernames(self):
        for name in ("", "a:b", "a\nb"):
            self.assertEqual(await mosquitto.add_mosquitto_login(name, "pw"), "error", repr(name))
        self.assertFalse(os.path.exists(self.path))

    async def test_mode_is_preserved_and_no_temp_files_left(self):
        await mosquitto.add_mosquitto_login("dev1", "pw1")
        os.chmod(self.path, 0o640)
        await mosquitto.add_mosquitto_login("dev2", "pw2")
        await mosquitto.remove_mosquitto_login("dev1")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual(os.listdir(self._dir.name), ["passwd"])

    async def test_ownership_is_best_effort(self):
        with mock.patch.object(mosquitto.os, "chown", side_effect=PermissionError):
            self.assertEqual(await mosquitto.add_mosquitto_login("dev1", "pw1"), "added")
        self.assertTrue(os.path.exists(self.path))

    async def test_failed_write_keeps_the_old_file(self):
        await mosquitto.add_mosquitto_login("dev1", "pw1")
        before = self.lines()
        with mock.patch.object(mosquitto.os, "replace", side_effect=OSError("boom")):
            self.assertEqual(await mosquitto.add_mosquitto_login("dev2", "pw2"), "error")
            self.assertEqual(await mosquitto.remove_mosquitto_login("dev1"), "error")
        self.assertEqual(self.lines(), before)
        self.assertEqual(os.listdir(self._dir.name), ["passwd"])

    async def test_ensure_app_login(self):
        self.assertEqual(await mosquitto.ensure_app_login("petlibro", "pw1"), "added")
        self.assertEqual(await mosquitto.ensure_app_login("petlibro", "pw1"), "exists")
        # The environment wins over what is stored.
        self.assertEqual(await mosquitto.ensure_app_login("petlibro", "pw2"), "added")
        self.assertTrue(mosquitto._verify_password("pw2", self.entry("petlibro")))
        self.assertEqual(await mosquitto.ensure_app_login("petlibro", ""), "error")


@unittest.skipUnless(os.path.isdir("/proc/self"), "needs Linux /proc")
class ReloadTests(unittest.TestCase):
    def test_sighup_goes_to_the_process_named_mosquitto(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The process name (/proc/<pid>/comm) comes from the executed file name.
            fake = os.path.join(tmp, "mosquitto")
            os.symlink(sys.executable, fake)
            proc = subprocess.Popen([fake, "-c", "import time; time.sleep(30)"])
            self.addCleanup(proc.kill)
            time.sleep(0.5)
            self.assertTrue(mosquitto._reload_mosquitto())
            # Python has no SIGHUP handler, so the default action ends it.
            self.assertEqual(proc.wait(timeout=5), -signal.SIGHUP)

    def test_missing_process_is_not_an_error(self):
        with mock.patch.object(mosquitto.os, "listdir", return_value=["not-a-pid"]):
            self.assertFalse(mosquitto._reload_mosquitto())


if __name__ == "__main__":
    unittest.main()
