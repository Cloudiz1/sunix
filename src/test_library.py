"""Tests for the Library dedup."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from . import library


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sunix-lib-test-"))
    print(f"using tmp dir: {tmp}")
    try:
        db_path = tmp / "test.db"

        lib = library.Library(db_path)

        # Empty library.
        assert not lib.has("aaa"), "empty library should not have anything"
        print("  OK: empty library returns False")

        # Add an ID.
        lib.add("aaa")
        assert lib.has("aaa"), "library should have 'aaa' after add"
        assert not lib.has("bbb"), "library should not have 'bbb'"
        print("  OK: add + has round-trips")

        # Idempotent add.
        lib.add("aaa")
        assert lib.has("aaa")
        print("  OK: add is idempotent")

        lib.close()

        # Re-open: DB persists.
        lib2 = library.Library(db_path)
        assert lib2.has("aaa"), "library should persist across instances"
        assert not lib2.has("ccc")
        lib2.close()
        print("  OK: persists across instances")

        # Different DB file: empty.
        lib3 = library.Library(tmp / "other.db")
        assert not lib3.has("aaa"), "different DB should be empty"
        lib3.close()
        print("  OK: separate DB is independent")

        print("=== ALL TESTS PASSED ===")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())