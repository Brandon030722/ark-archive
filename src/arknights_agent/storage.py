import hashlib
import os
import re
import tempfile
from pathlib import Path

from arknights_agent.errors import Conflict, InvalidData


class SnapshotStore:
    """Content-addressed raw bytes. No user-provided paths or overwrites."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, content_hash: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise InvalidData("invalid snapshot SHA-256")
        path = self.root / content_hash[:2] / content_hash
        if not path.resolve().is_relative_to(self.root):
            raise InvalidData("snapshot path escapes storage root")
        return path

    def put(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        path = self.path_for(content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.read_verified(content_hash)
            return content_hash
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                self.read_verified(content_hash)
        finally:
            os.unlink(temporary)
        return content_hash

    def read_verified(self, content_hash: str) -> bytes:
        path = self.path_for(content_hash)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise Conflict("snapshot object is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise Conflict("snapshot object failed SHA-256 verification")
        return content
