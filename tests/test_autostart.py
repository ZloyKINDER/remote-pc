from __future__ import annotations

import unittest
from pathlib import Path

from remotepc import admin_task, autostart


class AutostartTests(unittest.TestCase):
    def test_launcher_command(self):
        command = autostart.startup_command()
        self.assertIn("pythonw.exe", command.lower())
        self.assertIn("remote-pc-gui.pyw", command)
        self.assertTrue(command.startswith('"'))

    def test_vbs_quote(self):
        quoted = autostart._vbs_quote('"C:\\A B\\pythonw.exe" "C:\\A B\\x.pyw"')
        self.assertTrue(quoted.startswith('"""'))
        self.assertTrue(quoted.endswith('"""'))

    def test_config_path_added_when_custom(self):
        command = autostart.startup_command(Path("C:/other/remote-pc.json"))
        self.assertIn("--config", command)
        self.assertIn("remote-pc.json", command)

    def test_admin_task_command(self):
        command = admin_task.task_command()
        self.assertIn("pythonw.exe", command.lower())
        self.assertIn("remote-pc-gui.pyw", command)

    def test_admin_task_xml_is_highest_and_logon(self):
        xml = admin_task.task_xml()
        self.assertIn("HighestAvailable", xml)
        self.assertIn("InteractiveToken", xml)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("remote-pc-gui.pyw", xml)
        custom = admin_task.task_xml(Path("C:/other/remote-pc.json"))
        self.assertEqual(custom.count("--config"), 1)


if __name__ == "__main__":
    unittest.main()
