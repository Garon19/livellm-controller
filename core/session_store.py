"""Encrypted local persistence for reusable browser sessions."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken


class SessionStoreError(RuntimeError):
    """Base error for encrypted session persistence."""


class SessionConflictError(SessionStoreError):
    """Raised when a registration would overwrite different configuration."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a session registration does not exist."""


class SessionStore:
    """Fernet-encrypted, per-record session store with atomic writes."""

    RECORD_VERSION = 1

    def __init__(
        self,
        directory: Optional[str] = None,
        key: Optional[str] = None,
        key_file: Optional[str] = None,
    ) -> None:
        configured_dir = directory or os.environ.get("SESSION_STORE_DIR")
        if configured_dir:
            default_dir = Path(configured_dir).expanduser()
        elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            default_dir = Path(os.environ["LOCALAPPDATA"]) / "livellm-controller" / "sessions"
        else:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            default_dir = data_home / "livellm-controller" / "sessions"
        self.directory = default_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self._restrict(self.directory, 0o700)
        self._lock = threading.RLock()

        configured_key = key or os.environ.get("SESSION_STORE_KEY")
        configured_key_file = key_file or os.environ.get("SESSION_STORE_KEY_FILE")
        if configured_key:
            try:
                raw_key = configured_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise SessionStoreError("SESSION_STORE_KEY is not a valid Fernet key") from exc
        else:
            path = Path(configured_key_file).expanduser() if configured_key_file else self.directory / "session_store.key"
            raw_key = self._load_or_create_key(path)

        try:
            self._fernet = Fernet(raw_key)
        except (ValueError, TypeError) as exc:
            raise SessionStoreError("SESSION_STORE_KEY is not a valid Fernet key") from exc

    @staticmethod
    def _restrict(path: Path, mode: int) -> None:
        try:
            os.chmod(str(path), mode)
        except OSError:
            # Windows and some mounted filesystems only support permissions best-effort.
            pass

    def _load_or_create_key(self, path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict(path.parent, 0o700)
        try:
            raw_key = path.read_bytes().strip()
        except FileNotFoundError:
            candidate = Fernet.generate_key()
            fd = None
            try:
                # Create the final path exclusively. If another process wins the
                # race, read its key instead of replacing it with a different one.
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    fd = None
                    stream.write(candidate)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._fsync_directory(path.parent)
                raw_key = candidate
            except FileExistsError:
                raw_key = b""
                try:
                    for _ in range(100):
                        raw_key = path.read_bytes().strip()
                        if raw_key:
                            break
                        time.sleep(0.01)
                except OSError as exc:
                    raise SessionStoreError(
                        "Unable to read the concurrently created session-store key"
                    ) from exc
                if not raw_key:
                    raise SessionStoreError("The session-store key file is empty")
            except OSError as exc:
                raise SessionStoreError("Unable to create the session-store key file") from exc
            finally:
                if fd is not None:
                    os.close(fd)
        except OSError as exc:
            raise SessionStoreError("Unable to read the session-store key file") from exc
        self._restrict(path, 0o600)
        return raw_key

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Best-effort persistence of directory-entry changes on POSIX."""
        if os.name == "nt":
            return
        descriptor = None
        try:
            descriptor = os.open(str(directory), os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _record_path(self, session_id: str) -> Path:
        # session_id is API-validated; this assertion also protects internal callers.
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in session_id):
            raise SessionStoreError("Invalid session identifier")
        if session_id in {".", ".."}:
            raise SessionStoreError("Invalid session identifier")
        return self.directory / (session_id + ".session.enc")

    def _atomic_write(self, path: Path, payload: bytes, mode: int = 0o600) -> None:
        temporary = path.with_name(".%s.%s.tmp" % (path.name, os.urandom(8).hex()))
        fd = None
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._restrict(temporary, mode)
            os.replace(str(temporary), str(path))
            self._restrict(path, mode)
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise SessionStoreError("Unable to write encrypted session state") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _read_unlocked(self, session_id: str) -> Dict[str, Any]:
        path = self._record_path(session_id)
        try:
            encrypted = path.read_bytes()
        except FileNotFoundError as exc:
            raise SessionNotFoundError("Session is not registered") from exc
        except OSError as exc:
            raise SessionStoreError("Unable to read encrypted session state") from exc
        try:
            data = json.loads(self._fernet.decrypt(encrypted).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise SessionStoreError("Unable to decrypt session state") from exc
        if not isinstance(data, dict) or data.get("version") != self.RECORD_VERSION:
            raise SessionStoreError("Unsupported session record format")
        return data

    def _write_unlocked(self, record: Dict[str, Any]) -> None:
        serialized = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(serialized)
        self._atomic_write(self._record_path(record["session_id"]), encrypted)

    @staticmethod
    def _matches_configuration(
        record: Dict[str, Any], browser_id: Optional[str], proxy: Dict[str, Any]
    ) -> bool:
        browser_matches = browser_id is None or record.get("browser_id") == browser_id
        return browser_matches and record["proxy"] == proxy

    def registration_matches(
        self, session_id: str, proxy: Dict[str, Any], browser_id: Optional[str] = None
    ) -> bool:
        with self._lock:
            return self._matches_configuration(
                self._read_unlocked(session_id), browser_id, proxy
            )

    def exists(self, session_id: str) -> bool:
        try:
            return self._record_path(session_id).is_file()
        except SessionStoreError:
            return False

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._read_unlocked(session_id)

    def list(self) -> List[Dict[str, Any]]:
        records = []
        with self._lock:
            for path in sorted(self.directory.glob("*.session.enc")):
                session_id = path.name[: -len(".session.enc")]
                records.append(self._read_unlocked(session_id))
        return records

    def register(
        self,
        session_id: str,
        proxy: Dict[str, Any],
        browser_id: Optional[str] = None,
        replace: bool = False,
    ) -> str:
        """Create/update a definition; return created, unchanged, or replaced."""
        with self._lock:
            try:
                current = self._read_unlocked(session_id)
            except SessionNotFoundError:
                current = None

            if current is not None:
                if self._matches_configuration(current, browser_id, proxy):
                    return "unchanged"
                if not replace:
                    raise SessionConflictError(
                        "Session is already registered with different configuration"
                    )

            now = self._timestamp()
            record = {
                "version": self.RECORD_VERSION,
                "session_id": session_id,
                "browser_id": browser_id,
                "proxy": proxy,
                "storage_state": None,
                "created_at": current.get("created_at", now) if current else now,
                "updated_at": now,
            }
            self._write_unlocked(record)
            return "replaced" if current is not None else "created"

    def bind_browser(self, session_id: str, browser_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._read_unlocked(session_id)
            current = record.get("browser_id")
            if current and current != browser_id:
                raise SessionConflictError("Session is registered to another browser")
            if current != browser_id:
                record["browser_id"] = browser_id
                record["updated_at"] = self._timestamp()
                self._write_unlocked(record)
            return record

    def save_storage_state(self, session_id: str, storage_state: Dict[str, Any]) -> None:
        with self._lock:
            record = self._read_unlocked(session_id)
            record["storage_state"] = storage_state
            record["updated_at"] = self._timestamp()
            self._write_unlocked(record)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            path = self._record_path(session_id)
            try:
                path.unlink()
                self._fsync_directory(path.parent)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise SessionStoreError("Unable to delete encrypted session state") from exc


session_store = SessionStore()
