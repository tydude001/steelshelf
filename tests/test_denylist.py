"""The publish gate at `pytest` time: no tracked file may match the push denylist.

The list is kept outside this repo, because a list in the public tree would publish
it; it is copied to `.git/hooks/denylist.txt`, which git never pushes. So this skips
wherever that file is absent (CI, a stranger's clone) and runs where the gate is
installed. The pre-push hook is the same scan over commit messages too; this catches
a hit before it is committed at all.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def _denylist() -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--git-path", "hooks/denylist.txt"],
                             cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    path = REPO / out.strip()
    return path if path.is_file() else None


DENYLIST = _denylist()


def _patterns() -> list[re.Pattern[bytes]]:
    lines = DENYLIST.read_text().splitlines()
    return [re.compile(ln.strip().encode(), re.IGNORECASE) for ln in lines
            if ln.strip() and not ln.lstrip().startswith("#")]


@pytest.mark.skipif(DENYLIST is None, reason="no .git/hooks/denylist.txt on this machine")
def test_no_tracked_file_matches_the_denylist():
    files = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                           check=True).stdout.split(b"\0")
    pats = _patterns()
    hits = []
    for name in filter(None, files):
        path = REPO / name.decode()
        if not path.is_file():
            continue
        data = path.read_bytes()
        hits += [f"{name.decode()}: {p.pattern.decode()}" for p in pats if p.search(data)]
    assert not hits, "denylisted strings in tracked files:\n" + "\n".join(hits)
