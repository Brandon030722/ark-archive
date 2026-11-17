"""Owner-local, write-only credential management. Never part of model tools."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from .account import AccountError, read_balance


class CredentialError(Exception):
    pass


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CredentialError("credential_storage_unavailable")


def _read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise CredentialError("credential_storage_unavailable")
        return stream.read(16385)


def _atomic_private(path, content):
    _private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CredentialStore:
    def __init__(self, path: Path, key_path: Path):
        self.path, self.key_path = path, key_path

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            payload = Fernet(_read_private(self.key_path)).decrypt(_read_private(self.path))
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {"token", "updated_at"}:
                raise ValueError()
            if data["token"] is not None:
                validate_token(data["token"])
            datetime.fromisoformat(data["updated_at"])
            return data
        except (OSError, ValueError, TypeError, InvalidToken, CredentialError):
            raise CredentialError("credential_storage_unavailable") from None

    def write(self, token):
        try:
            _private_dir(self.key_path.parent)
            if not self.key_path.exists():
                if self.path.exists() or self.key_path.is_symlink():
                    raise CredentialError("credential_storage_unavailable")
                # Exclusive creation avoids silently replacing another deployment's master key.
                try:
                    fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(Fernet.generate_key())
                        stream.flush()
                        os.fsync(stream.fileno())
            data = {"token": token, "updated_at": datetime.now(UTC).isoformat()}
            encrypted = Fernet(_read_private(self.key_path)).encrypt(json.dumps(data).encode())
            _atomic_private(self.path, encrypted)
            return data
        except (OSError, ValueError, TypeError):
            raise CredentialError("credential_storage_unavailable") from None


def validate_token(value):
    if (not isinstance(value, str) or not 16 <= len(value) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise CredentialError("credential_invalid_token")


class CredentialManager:
    def __init__(self, settings):
        self.lock = threading.RLock()
        self.store = (CredentialStore(settings.credential_store_path, settings.credential_master_key_path)
                      if settings.credential_store_path and settings.credential_master_key_path else None)
        self.key = settings.deepseek_api_key
        self.source = "environment" if self.key else "missing"
        self.updated_at = None
        self.error = None
        self.nonce = secrets.token_urlsafe(32)
        if self.store:
            try:
                data = self.store.read()
            except CredentialError:
                self.key = None
                self.source = "unavailable"
                self.error = "credential_storage_unavailable"
            else:
                if data is not None:
                    self._install(data)

    def _install(self, data):
        self.key = SecretStr(data["token"]) if data["token"] else None
        self.source = "encrypted" if self.key else "disabled"
        self.updated_at = data["updated_at"]
        self.error = None

    def apply(self, settings):
        from .stage26_current_model import MODEL_ALIAS

        with self.lock:
            return settings.model_copy(update={"deepseek_api_key": self.key,
                                                "deepseek_model": settings.deepseek_model or
                                                (MODEL_ALIAS if self.store else None)})

    def public(self, can_manage=False):
        with self.lock:
            return {"configured": bool(self.key), "source": self.source,
                    "setup_required": bool(self.store and not self.key and not self.error),
                    "can_manage": bool(self.store and can_manage), "updated_at": self.updated_at,
                    "error": self.error, "nonce": self.nonce if self.store and can_manage else None}

    def replace(self, value, *, checker=None):
        if not self.store:
            raise CredentialError("credential_management_unavailable")
        with self.lock:
            if value is not None:
                validate_token(value)
                try:
                    (checker or read_balance)(SecretStr(value))
                except AccountError:
                    raise CredentialError("credential_check_failed") from None
            data = self.store.write(value)
            self._install(data)


def effective_settings(app, default):
    configured = getattr(app.state, "workbench_runtime", {}).get(
        "settings", getattr(app.state, "runtime_settings", default))
    manager = getattr(app.state, "credential_manager", None)
    configured = manager.apply(configured) if manager else configured
    providers = getattr(app.state, "provider_settings", None)
    return providers.apply(configured) if providers else configured


def owner_local(request: Request):
    try:
        host = urlsplit(str(request.url)).hostname
        return bool(request.client and ip_address(request.client.host).is_loopback
                    and host in {"localhost", "127.0.0.1", "::1"}
                    and not any(name in request.headers for name in
                                ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")))
    except ValueError:
        return False


def install_credential_routes(app, settings):
    manager = CredentialManager(settings)
    app.state.credential_manager = manager
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    tour_marker = (manager.store.path.with_name("workbench-tour.done") if manager.store
                   else Path(".local/workbench-preferences/tour.done"))
    tour_nonce = secrets.token_urlsafe(32)

    def require_owner(request, header, nonce):
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if (not owner_local(request) or request.headers.get("origin") != origin
                or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
                or not secrets.compare_digest(request.headers.get(header, ""), nonce)):
            raise HTTPException(403, "credential_owner_required")

    @app.get("/v1/account/credential", include_in_schema=False)
    def status(request: Request):
        owner = owner_local(request)
        public = manager.public(owner)
        try:
            public["tour_pending"] = owner and not tour_marker.is_file()
        except OSError:
            public["tour_pending"] = False
        if public["tour_pending"]:
            public["tour_nonce"] = tour_nonce
        providers = getattr(app.state, "provider_settings", None)
        if providers and not providers.error:
            selected = providers.connections.get(providers.active)
            if selected:
                public.update(configured=True, setup_required=False,
                              source=public["source"] if providers.legacy_adopted else "encrypted",
                              updated_at=selected.updated_at)
            elif providers.active is None:
                public.update(configured=False, setup_required=True, source="disabled")
        return JSONResponse(public, headers=headers)

    @app.post("/v1/workbench/tour/dismiss", include_in_schema=False)
    def dismiss_tour(request: Request):
        require_owner(request, "x-tour-nonce", tour_nonce)
        try:
            _atomic_private(tour_marker, b"1\n")
        except (OSError, CredentialError):
            raise HTTPException(503, "tour_storage_unavailable") from None
        return JSONResponse({"tour_pending": False}, headers=headers)

    @app.post("/v1/account/credential", include_in_schema=False)
    async def change(request: Request):
        require_owner(request, "x-credential-nonce", manager.nonce)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "credential_invalid_request")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, "credential_invalid_request")
        try:
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {"action", "token", "acknowledged"}
                    or data["acknowledged"] is not True or data["action"] not in {"save", "remove"}
                    or data["action"] == "remove" and data["token"] is not None):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, "credential_invalid_request") from None
        from starlette.concurrency import run_in_threadpool

        try:
            if data["action"] == "save":
                validate_token(data["token"])
            await run_in_threadpool(manager.replace, data["token"] if data["action"] == "save" else None)
        except CredentialError as error:
            return JSONResponse({"detail": str(error)}, status_code=422, headers=headers)
        callback = getattr(app.state, "credential_changed", None)
        if callback:
            await run_in_threadpool(callback)
        return JSONResponse(manager.public(True), headers=headers)
