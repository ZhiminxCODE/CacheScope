import errno
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ownership_fixture import change_to_mapped_owner, mapped_alternate_id


class OwnershipFixtureTests(unittest.TestCase):
    def test_alternate_id_respects_mapping_ranges(self):
        self.assertEqual(mapped_alternate_id("0 0 4294967295\n", 0), 12345)
        self.assertEqual(mapped_alternate_id("0 1000 2\n", 0), 1)
        self.assertEqual(mapped_alternate_id("0 1000 1\n500 2000 1\n", 0), 500)
        self.assertIsNone(mapped_alternate_id("0 1000 1\n", 0))
        self.assertIsNone(mapped_alternate_id("", 0))

    def test_single_uid_namespace_skips_before_chown(self):
        with patch("ownership_fixture.os.geteuid", return_value=0), \
             patch("ownership_fixture.os.getegid", return_value=0), \
             patch("ownership_fixture.Path.read_text", return_value="0 1000 1\n"), \
             patch("ownership_fixture.os.chown") as chown:
            with self.assertRaisesRegex(unittest.SkipTest, "no alternate mapped UID"):
                change_to_mapped_owner(Path("fixture"))
            chown.assert_not_called()

    def test_existing_gid_is_retained_when_only_uid_has_an_alternate(self):
        with patch("ownership_fixture.os.geteuid", return_value=0), \
             patch("ownership_fixture.os.getegid", return_value=0), \
             patch("ownership_fixture.Path.read_text", side_effect=["0 1000 2\n", "0 1000 1\n"]), \
             patch("ownership_fixture.Path.stat", return_value=SimpleNamespace(st_uid=1)), \
             patch("ownership_fixture.os.chown") as chown:
            self.assertEqual(change_to_mapped_owner(Path("fixture")), (1, -1))
            chown.assert_called_once_with(Path("fixture"), 1, -1)

    def test_unavailable_chown_permission_skips_with_a_reason(self):
        for code in (errno.EPERM, errno.EINVAL):
            with self.subTest(code=code), \
                 patch("ownership_fixture.os.geteuid", return_value=0), \
                 patch("ownership_fixture.os.getegid", return_value=0), \
                 patch("ownership_fixture.Path.read_text", return_value="0 0 65536\n"), \
                 patch("ownership_fixture.os.chown", side_effect=OSError(code, "fixture restriction")):
                with self.assertRaisesRegex(unittest.SkipTest, "cannot create differently owned fixture"):
                    change_to_mapped_owner(Path("fixture"))

    def test_unexpected_fixture_errors_are_not_hidden(self):
        with patch("ownership_fixture.os.geteuid", return_value=0), \
             patch("ownership_fixture.os.getegid", return_value=0), \
             patch("ownership_fixture.Path.read_text", return_value="0 0 65536\n"), \
             patch("ownership_fixture.os.chown", side_effect=FileNotFoundError(errno.ENOENT, "missing fixture")):
            with self.assertRaises(FileNotFoundError):
                change_to_mapped_owner(Path("fixture"))
