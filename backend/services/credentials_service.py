"""Secure credential storage and shared credential revision tracking."""

import base64
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class CredentialConfigurationError(RuntimeError):
    """Raised when credential encryption is not configured safely."""


def _get_encryption_key() -> bytes:
    secret = _credentials_secret()
    if not secret or not secret.strip():
        raise CredentialConfigurationError(
            "CREDENTIALS_SECRET must be configured before saving credentials"
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"aiac-2.0-salt",
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode()))


def _credentials_secret() -> str:
    secret = os.getenv("CREDENTIALS_SECRET", "")
    if secret.strip():
        return secret
    try:
        from backend.config import settings
        return settings.CREDENTIALS_SECRET or ""
    except Exception:
        return ""


def encrypt_value(plain_text: str) -> str:
    if not plain_text:
        return ""
    return base64.urlsafe_b64encode(Fernet(_get_encryption_key()).encrypt(plain_text.encode())).decode()


def decrypt_value(encrypted_text: str) -> str:
    if not encrypted_text:
        return ""
    try:
        key = _get_encryption_key()
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_text.encode())
        return Fernet(key).decrypt(encrypted_bytes).decode()
    except CredentialConfigurationError:
        logger.error("Cannot decrypt stored credential: CREDENTIALS_SECRET is missing")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        logger.error("Cannot decrypt stored credential: secret mismatch or invalid ciphertext")
    return ""


def mask_value(value: str, show_chars: int = 4) -> str:
    if not value or len(value) <= show_chars:
        return "****"
    return "*" * (len(value) - show_chars) + value[-show_chars:]


class CredentialKey:
    BRAIN_EMAIL = "brain_email"
    BRAIN_PASSWORD = "brain_password"
    OPENAI_API_KEY = "openai_api_key"
    OPENAI_BASE_URL = "openai_base_url"
    OPENAI_MODEL = "openai_model"


ENV_BY_KEY = {
    CredentialKey.BRAIN_EMAIL: "BRAIN_EMAIL",
    CredentialKey.BRAIN_PASSWORD: "BRAIN_PASSWORD",
    CredentialKey.OPENAI_API_KEY: "OPENAI_API_KEY",
    CredentialKey.OPENAI_BASE_URL: "OPENAI_BASE_URL",
    CredentialKey.OPENAI_MODEL: "OPENAI_MODEL",
}
REVISION_KEY = "credentials:revision"
TEST_STATUS_PREFIX = "credentials:test:"


class CredentialsService:
    """Database-first credentials with environment fallback and revision state."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def encryption_configured() -> bool:
        return bool(_credentials_secret().strip())

    async def get_worker_encryption_status(self) -> Dict[str, Any]:
        """Check that each live Celery worker has the matching encryption key."""
        backend_configured = self.encryption_configured()
        if not backend_configured:
            return {
                "backend_configured": False,
                "worker_configured": False,
                "worker_count": 0,
                "ready": False,
            }
        try:
            from backend.celery_app import celery_app
            from backend.config import settings
            from redis.asyncio import Redis

            replies = await asyncio.to_thread(celery_app.control.ping, timeout=1)
            live_workers = [next(iter(reply)) for reply in (replies or []) if reply]
            if not live_workers:
                return {
                    "backend_configured": True,
                    "worker_configured": False,
                    "worker_count": 0,
                    "ready": False,
                }

            expected = hashlib.sha256(_credentials_secret().encode()).hexdigest()
            redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                configured_workers = 0
                for hostname in live_workers:
                    raw = await redis_client.get(f"aiac:credential-runtime:{hostname}")
                    try:
                        worker_status = json.loads(raw or "{}")
                    except (TypeError, ValueError):
                        worker_status = {}
                    if worker_status.get("configured") and worker_status.get("fingerprint") == expected:
                        configured_workers += 1
            finally:
                await redis_client.aclose()

            return {
                "backend_configured": True,
                "worker_configured": configured_workers == len(live_workers),
                "worker_count": len(live_workers),
                "ready": configured_workers == len(live_workers),
            }
        except Exception as exc:
            logger.warning(f"Could not verify worker credential readiness ({type(exc).__name__})")
            return {
                "backend_configured": True,
                "worker_configured": False,
                "worker_count": 0,
                "ready": False,
            }

    async def require_worker_encryption(self) -> None:
        status = await self.get_worker_encryption_status()
        if not status["backend_configured"]:
            raise CredentialConfigurationError(
                "CREDENTIALS_SECRET must be configured in the backend before saving credentials"
            )
        if not status["ready"]:
            raise CredentialConfigurationError(
                "Every online Celery worker must be running with the same CREDENTIALS_SECRET before saving credentials"
            )

    @staticmethod
    def _environment_value(env_name: str) -> str:
        value = os.getenv(env_name, "")
        if value.strip():
            return value.strip()
        try:
            from backend.config import settings
            return str(getattr(settings, env_name, "") or "").strip()
        except Exception:
            return ""

    async def _db_value(self, key: str) -> Optional[str]:
        from backend.models import SystemConfig

        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key == f"credential:{key}")
        )
        config = result.scalar_one_or_none()
        if config and config.config_value:
            value = decrypt_value(config.config_value)
            return value or None
        return None

    async def get_credential(self, key: str, fallback_env: Optional[str] = None) -> Optional[str]:
        value = await self._db_value(key)
        if value:
            return value
        env_name = fallback_env or ENV_BY_KEY.get(key)
        env_value = self._environment_value(env_name) if env_name else ""
        return env_value or None

    async def get_revision(self) -> int:
        from backend.models import SystemConfig

        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key == REVISION_KEY)
        )
        row = result.scalar_one_or_none()
        try:
            return int(row.config_value) if row and row.config_value else 0
        except (TypeError, ValueError):
            return 0

    async def _bump_revision(self) -> int:
        from backend.models import SystemConfig

        current = await self.get_revision()
        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key == REVISION_KEY)
        )
        row = result.scalar_one_or_none()
        if row:
            row.config_value = str(current + 1)
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            self.db.add(SystemConfig(
                config_key=REVISION_KEY,
                config_value="1",
                config_type="internal",
                description="Shared credential configuration revision",
            ))
        return current + 1

    async def set_credential(self, key: str, value: str, description: str = "") -> bool:
        return await self.set_credentials({key: (value, description)})

    async def set_credentials(self, values: Dict[str, tuple[str, str]]) -> bool:
        from backend.models import SystemConfig

        # Encrypt every value before touching the DB so a missing key cannot
        # partially update a credential bundle.
        encrypted_values = {key: (encrypt_value(value), description) for key, (value, description) in values.items()}
        for key, (encrypted, description) in encrypted_values.items():
            config_key = f"credential:{key}"
            result = await self.db.execute(
                select(SystemConfig).where(SystemConfig.config_key == config_key)
            )
            row = result.scalar_one_or_none()
            if row:
                row.config_value = encrypted
                row.config_type = "encrypted"
                row.description = description
                row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                self.db.add(SystemConfig(
                    config_key=config_key,
                    config_value=encrypted,
                    config_type="encrypted",
                    description=description,
                ))
        await self._bump_revision()
        await self.db.commit()
        logger.info("Credential bundle updated; shared credential revision advanced")
        return True

    async def delete_credential(self, key: str) -> bool:
        from backend.models import SystemConfig

        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key == f"credential:{key}")
        )
        row = result.scalar_one_or_none()
        if not row:
            return False
        await self.db.delete(row)
        await self._bump_revision()
        await self.db.commit()
        return True

    async def get_all_credentials_masked(self) -> Dict[str, Dict[str, Any]]:
        from backend.models import SystemConfig

        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key.like("credential:%"))
        )
        db_rows = {
            row.config_key.removeprefix("credential:"): row
            for row in result.scalars().all()
        }
        credentials = {}
        for key, env_name in ENV_BY_KEY.items():
            row = db_rows.get(key)
            db_value = decrypt_value(row.config_value) if row and row.config_value else ""
            env_value = self._environment_value(env_name)
            value = db_value or env_value
            source = "db" if db_value else ("env" if env_value else None)
            if key in (CredentialKey.BRAIN_EMAIL, CredentialKey.OPENAI_BASE_URL):
                masked = value[:3] + "***" + value[-10:] if len(value) > 13 else (mask_value(value) if value else "(未配置)")
            elif key == CredentialKey.OPENAI_MODEL:
                masked = value if value else "(未配置)"
            else:
                masked = mask_value(value) if value else "(未配置)"
            credentials[key] = {
                "masked": masked,
                "is_set": bool(value),
                "stored": bool(row and row.config_value),
                "available": bool(value),
                "error": "encryption_unavailable" if row and row.config_value and not db_value else None,
                "source": source,
                "updated_at": row.updated_at.isoformat() if source == "db" and row.updated_at else None,
            }
        return credentials

    @staticmethod
    def sanitize_error(error: Any, secrets=()) -> str:
        text = str(error or "Connection failed")
        for secret in secrets:
            if secret:
                text = text.replace(str(secret), "[redacted]")
        text = re.sub(r"(?i)(bearer\s+)[a-z0-9._-]+", r"\1[redacted]", text)
        text = re.sub(r"(?i)(api[_ -]?key\s*[:=]?\s*)[^\s,;]+", r"\1[redacted]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
        return text[:500]

    async def record_connection_test(self, provider: str, success: bool, error: str = "") -> Dict[str, Any]:
        from backend.models import SystemConfig

        tested_at = datetime.now(timezone.utc).isoformat()
        secrets = [self._environment_value(name) for name in ENV_BY_KEY.values()]
        secrets.extend([await self._db_value(key) for key in ENV_BY_KEY])
        status = {
            "success": bool(success),
            "tested_at": tested_at,
            "error": None if success else self.sanitize_error(error, secrets),
        }
        key = f"{TEST_STATUS_PREFIX}{provider}"
        result = await self.db.execute(select(SystemConfig).where(SystemConfig.config_key == key))
        row = result.scalar_one_or_none()
        if row:
            row.config_value = json.dumps(status)
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            self.db.add(SystemConfig(config_key=key, config_value=json.dumps(status), config_type="status"))
        await self.db.commit()
        return status

    async def get_connection_test_status(self) -> Dict[str, Any]:
        from backend.models import SystemConfig

        result = await self.db.execute(
            select(SystemConfig).where(SystemConfig.config_key.like(f"{TEST_STATUS_PREFIX}%"))
        )
        statuses = {}
        for row in result.scalars().all():
            try:
                statuses[row.config_key.removeprefix(TEST_STATUS_PREFIX)] = json.loads(row.config_value or "{}")
            except (TypeError, ValueError):
                continue
        return statuses

    async def test_brain_credentials(self) -> Dict[str, Any]:
        email = await self.get_credential(CredentialKey.BRAIN_EMAIL)
        password = await self.get_credential(CredentialKey.BRAIN_PASSWORD)
        if not email or not password:
            result = {"success": False, "error": "Brain credentials are not configured"}
        else:
            try:
                from backend.adapters.brain_adapter import BrainAdapter
                adapter = BrainAdapter(email=email, password=password)
                adapter.client = await adapter.get_client()
                success = await adapter.authenticate()
                result = {"success": success, "message": "Authentication successful" if success else "Authentication failed"}
            except Exception as exc:
                result = {"success": False, "error": self.sanitize_error(exc, [email, password])}
        await self.record_connection_test("brain", result["success"], result.get("error", ""))
        return result

    @classmethod
    def invalidate_cache(cls):
        """Compatibility shim; credential reads are now revision-backed and uncached."""
        return None


def get_credentials_service(db: AsyncSession) -> CredentialsService:
    return CredentialsService(db)
