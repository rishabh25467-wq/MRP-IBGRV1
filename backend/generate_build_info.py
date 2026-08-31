"""Regenerates backend/build_info.json from the current git commit - run
this and commit the result BEFORE every publish/deploy (real incident,
Sep 2026: a deployed production build has no `.git` history, so reading
git directly at runtime always failed there - see _get_build_version's
own docstring in server.py). Usage: python3 generate_build_info.py
"""
import json
import subprocess
from pathlib import Path

if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    commit = subprocess.check_output(
        ["git", "log", "-1", "--format=%h|%cd", "--date=format:%d %b %Y"],
        cwd=repo_root,
    ).decode().strip()
    short_hash, commit_date = commit.split("|", 1)
    out_path = Path(__file__).parent / "build_info.json"
    out_path.write_text(json.dumps({"commit": short_hash, "commit_date": commit_date}))
    print(f"Wrote {out_path}: {short_hash} · {commit_date}")
