"""Plain local credentials store (JSON file, no encryption).

Stored at `.auth/creds.json` with file permission 0600. The user chose plain
storage during setup; if you'd prefer encryption, swap this module out.
"""

import json
import os
from pathlib import Path


class CredentialsStore:
    def __init__(self, auth_dir: Path):
        self.auth_dir = Path(auth_dir)
        self.path = self.auth_dir / "creds.json"

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, username: str, password: str) -> None:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"username": username, "password": password}))
        os.chmod(self.path, 0o600)

    def load(self) -> tuple[str, str]:
        if not self.exists():
            raise FileNotFoundError("No saved credentials.")
        d = json.loads(self.path.read_text())
        return d["username"], d["password"]

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
