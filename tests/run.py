"""Run the test suite against a throwaway copy of the app.

    python tests/run.py

Starts M clipper on a spare port with an empty, temporary data folder, runs the tests against it, then stops it and
deletes the folder. Your real data and your running server are never touched. No AI key is needed: the tests cover accounts, permissions, limits and the queue, not clip quality."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("MCLIPPER_TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="mclipper-test-"))
    env = {**os.environ, "MCLIPPER_DATA": str(data), "MCLIPPER_PORT": str(PORT), "MCLIPPER_PUBLIC_URL": BASE,
           "MCLIPPER_HOST": "127.0.0.1", "MCLIPPER_AUTO_RELOAD": "0", "DATABASE_URL": "",
           "MCLIPPER_ACCOUNTS": "1", "MCLIPPER_FETCH_MODEL": "0", "MCLIPPER_UPDATE_CHECK": "0",                   # these tests are about people signing in; tests/test_local.py covers the default
           # nothing outside this machine is contacted: no AI, no email, no Google sign-in
           "MCLIPPER_SMTP_HOST": "", "MCLIPPER_VERIFY_EMAIL": "1",   # the tests cover the confirm-your-email rule
           "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "", "GOOGLE_CLIENT_FILE": "", "PYTHONUNBUFFERED": "1",
           "MCLIPPER_AI_KEY": "", "MCLIPPER_AI_KEY_FILE": "", "MCLIPPER_AI_MODELS": "",
           "MCLIPPER_UPLOAD_PART_MB": "5", "MCLIPPER_MAX_UPLOAD_MB": "64"}
    log = data / "server.log"
    failed = []
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen([sys.executable, "-m", "studio.server"], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=2)
                break
            except Exception:  # noqa: BLE001
                if server.poll() is not None:
                    print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
                    return 1
                time.sleep(1)
        else:
            print("The test server did not start.")
            return 1
        runs = [("rename compatibility", ["-m", "unittest", "tests.test_branding"]),
                ("permissions", ["tests/test_permissions.py", BASE, str(ROOT), sys.executable]),
                ("editor drafts and revision history", ["tests/test_editor.py"]),
                ("MCP source and export gates", ["-m", "unittest", "tests.test_mcp_bridge"]),
                ("montage and reaction framing", ["-m", "unittest", "tests.test_editing"]),
                ("abuse and limits", ["tests/test_abuse.py", BASE, str(ROOT)]),
                ("queue under load", ["tests/test_load_queue.py", str(ROOT)]),
                ("deleting and bulk actions", ["tests/test_delete.py"]),
                ("big uploads in pieces", ["tests/test_upload_parts.py", BASE]),
                ("AI services", ["tests/test_ai.py"]),
                ("no sign-in, and the AI key from the dashboard", ["tests/test_local.py"]),
                ("Hindi and Urdu captions in Roman letters", ["tests/test_roman.py"]),
                ("connecting YouTube, Instagram and TikTok", ["tests/test_connect.py"]),
                ("the new-version notice", ["tests/test_updates.py"]),
                ("no limits by default", ["tests/test_limits.py"]),
                ("captions never overlap, stuck clips, sentence endings", ["tests/test_stuck.py"]),
                ("fresh installs: ffmpeg, the speech model, AI errors", ["tests/test_tools.py"]),
                ("the storage folder: full drive, open, move", ["tests/test_storage.py"])]
        for name, args in runs:
            print(f"\n=== {name} ===", flush=True)
            r = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            lines = (r.stdout + r.stderr).splitlines()
            passed = sum(1 for x in lines if x.startswith("PASS"))
            if not passed and r.returncode == 0:
                count = re.search(r"^Ran (\d+) tests? in ", r.stdout + r.stderr, re.M)
                passed = int(count.group(1)) if count else 0
            for x in lines:
                if not x.startswith("PASS") and x.strip():
                    print("  " + x)
            print(f"  {passed} checks passed")
            if r.returncode != 0 or any(x.startswith("FAIL") for x in lines):
                failed.append(name)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data, ignore_errors=True)
    print("\nFAILED: " + ", ".join(failed) if failed else "\nAll tests passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
