"""
Intentionally vulnerable fixture — DO NOT merge, DO NOT deploy.
Only exists to exercise gitscribe's deterministic (ruff) and agentic
(local llama.cpp) review paths against known-flaggable patterns.
"""

import os
import pickle
import subprocess
import sqlite3


# --- hardcoded secret (should trip ruff S105/S106/S107 -> category "secrets") ---
API_KEY = "sk-live-51NfakeButShapedRight1234567890abcdef"
DB_PASSWORD = "hunter2-not-a-real-password"


def get_user(username: str) -> sqlite3.Row | None:
    """SQL injection via string-built query (should trip ruff S608 ->
    escalated to 'critical' by gitscribe's severity mapping)."""
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor.execute(query)
    return cursor.fetchone()


def run_diagnostic(hostname: str) -> str:
    """Shell/command injection via shell=True with tainted input (should
    trip ruff S602 -> escalated to 'critical')."""
    result = subprocess.run(
        "ping -c 1 " + hostname,
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def load_cached_session(raw_bytes: bytes):
    """Unsafe deserialization (should trip ruff S301 -> escalated to
    'critical'). pickle.loads on data from an unspecified/untrusted
    source is a classic RCE-via-deserialization pattern."""
    return pickle.loads(raw_bytes)


def read_user_file(filename: str) -> str:
    """Path traversal: caller-supplied filename passed straight to
    open() with no containment check. Whether gitscribe's AST-based
    path-traversal detector fires depends on its exact taint-source
    definition; the AI reviewer should flag this regardless, given the
    unsanitized parameter flowing directly into a filesystem sink."""
    path = os.path.join("/var/app/uploads", filename)
    with open(path) as f:
        return f.read()
