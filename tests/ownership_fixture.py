"""Ownership fixtures that respect Linux user-namespace ID mappings."""

import errno
import os
from pathlib import Path
import unittest


def mapped_alternate_id(mapping, current, preferred=12345):
    ranges = []
    for line in mapping.splitlines():
        if line.strip():
            inside, _outside, count = map(int, line.split())
            if inside < 0 or count <= 0:
                raise ValueError("invalid ID mapping")
            ranges.append((inside, count))
    candidates = [preferred]
    for start, count in ranges:
        candidates.append(start)
        if count > 1:
            candidates.append(start + 1)
    for candidate in candidates:
        if candidate != current and any(start <= candidate < start + count for start, count in ranges):
            return candidate
    return None


def change_to_mapped_owner(path):
    current_uid, current_gid = os.geteuid(), os.getegid()
    if current_uid != 0:
        raise unittest.SkipTest("requires root with permission to create a differently owned fixture")
    try:
        uid_map = Path("/proc/self/uid_map").read_text()
        gid_map = Path("/proc/self/gid_map").read_text()
    except OSError as error:
        raise unittest.SkipTest(f"cannot read namespace UID/GID mappings: {error}") from error
    uid = mapped_alternate_id(uid_map, current_uid)
    if uid is None:
        raise unittest.SkipTest(f"no alternate mapped UID; namespace maps only the current UID {current_uid}")
    gid = mapped_alternate_id(gid_map, current_gid)
    if gid is None:
        # chown's -1 sentinel leaves the existing group unchanged, even if unmapped.
        # Only the owner UID needs to differ for the inspector's ownership check.
        gid = -1
    try:
        os.chown(path, uid, gid)
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise unittest.SkipTest(f"cannot create differently owned fixture with mapped UID {uid} (GID request {gid}): {error}") from error
        raise
    if Path(path).stat().st_uid == current_uid:
        raise unittest.SkipTest("filesystem did not expose a different owner after chown")
    return uid, gid
