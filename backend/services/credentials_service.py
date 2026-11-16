"""
Credentials Service - Secure storage and retrieval of sensitive configuration

Features:
1. AES encryption for sensitive data (passwords, API keys)
2. Database persistence with SystemConfig table
3. Caching for performance
4. Fallback to environment variables when not configured
"""

import os
import base64
import hashlib
import json
from typing import Optional, Dict, Any
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from loguru import logger


# =============================================================================
# ENCRYPTION UTILITIES
# =============================================================================

def _get_encryption_key() -> bytes:
    """
    Get or generate encryption key from environment.
    
    Uses CREDENTIALS_SECRET env var, or generates from a combination
    of system-specific values for basic protection.
    """
    secret = os.getenv("CREDENTIALS_SECRET")
    
    if not secret:
        # Fallback: use a combination of env values
        # In production, CREDENTIALS_SECRET should always be set
        secret = os.getenv("SECRET_KEY", "aiac-default-secret-change-me")
        logger.warning(
            "CREDENTIALS_SECRET not set. Using fallback. "
            "Set CREDENTIALS_SECRET for production security."
        )
    
    # Derive a proper key using PBKDF2
    salt = b"aiac-2.0-salt"  # Fixed salt (could be made configurable)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
    return key


def encrypt_value(plain_text: str) -> str:
    """Encrypt a string value using Fernet (AES-128-CBC)."""
    if not plain_text:
        return ""
    
    key = _get_encryption_key()
    f = Fernet(key)
    encrypted = f.encrypt(plain_text.encode())
    return base64.urlsafe_b64encode(encrypted).decode()


def decrypt_value(encrypted_text: str) -> str:
    """Decrypt a Fernet-encrypted string."""
    if not encrypted_text:
        return ""
    
    try:
        key = _get_encryption_key()
        f = Fernet(key)
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_text.encode())
        decrypted = f.decrypt(encrypted_bytes)
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Failed to decrypt value: {e}")
        return ""


def mask_value(value: str, show_chars: int = 4) -> str:
    """Mask sensitive value for display (show only last N chars)."""
    if not value or len(value) <= show_chars:
        return "****"
    return "*" * (len(value) - show_chars) + value[-show_chars:]


# =============================================================================
# CREDENTIAL KEYS
# =============================================================================

class CredentialKey:
    """Standard credential key names."""
    BRAIN_EMAIL = "brain_email"
    BRAIN_PASSWORD = "brain_password"
    OPENAI_API_KEY = "openai_api_key"
    OPENAI_BASE_URL = "openai_base_url"
    OPENAI_MODEL = "openai_model"


# =============================================================================
# CREDENTIALS SERVICE
# =============================================================================

class CredentialsService:
    """
    Service for managing encrypted credentials in database.
    
    Usage:
        service = CredentialsService(db)
        await service.set_credential("brain_password", "secret123")
        password = await service.get_credential("brain_password")
    """
    
    # In-memory cache for decrypted credentials
    _cache: Dict[str, str] = {}
    _cache_time: Optional[datetime] = None
    _cache_ttl_seconds: int = 300  # 5 minutes
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def get_credential(
        self, 
        key: str, 
        fallback_env: Optional[str] = None
    ) -> Optional[str]:
        """
        Get a credential value, with optional env fallback.
        
        Priority:
        1. Cached value (if valid)
        2. Database value (decrypted)
        3. Environment variable fallback
        """
        # Check cache first
        if self._is_cache_valid() and key in self._cache:
            return self._cache[key]
        
        # Query database
        from backend.models import SystemConfig
        
        query = select(SystemConfig).where(
            SystemConfig.config_key == f"credential:{key}"
        )
        result = await self.db.execute(query)
        config = result.scalar_one_or_none()
        
        if config and config.config_value:
            # Decrypt and cache
            decrypted = decrypt_value(config.config_value)
            if decrypted:
                self._cache[key] = decrypted
                self._cache_time = datetime.utcnow()
                return decrypted
        
        # Fallback to environment variable
        if fallback_env:
            env_value = os.getenv(fallback_env)
            if env_value:
                return env_value
        
        return None
    
    async def set_credential(self, key: str, value: str, description: str = "") -> bool:
        """
        Set a credential value (encrypted).
        
        Returns True on success.
        """
        from backend.models import SystemConfig
        
        encrypted = encrypt_value(value)
        config_key = f"credential:{key}"
        
        # Check if exists
        query = select(SystemConfig).where(SystemConfig.config_key == config_key)
        result = await self.db.execute(query)
        existing = result.scalar_one_or_none()
        
        if existing:
            # Update
            existing.config_value = encrypted
            existing.config_type = "encrypted"
            existing.description = description
            existing.updated_at = datetime.utcnow()
        else:
            # Insert
            new_config = SystemConfig(
                config_key=config_key,
                config_value=encrypted,
                config_type="encrypted",
                description=description
            )
            self.db.add(new_config)
        
        await self.db.commit()
        
        # Invalidate cache
        if key in self._cache:
            del self._cache[key]
        
        logger.info(f"Credential '{key}' updated successfully")
        return True
    
    async def delete_credential(self, key: str) -> bool:
        """Delete a credential."""
        from backend.models import SystemConfig
        
        config_key = f"credential:{key}"
        query = select(SystemConfig).where(SystemConfig.config_key == config_key)
        result = await self.db.execute(query)
        existing = result.scalar_one_or_none()
        
        if existing:
            await self.db.delete(existing)
            await self.db.commit()
            
            # Invalidate cache
            if key in self._cache:
                del self._cache[key]
            
            return True
        
        return False
    
    async def get_all_credentials_masked(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all credentials with masked values (for display).
        
        Returns dict like:
        {
            "brain_email": {"value": "user@example.com", "masked": "u***@example.com", "is_set": True},
            "brain_password": {"value": None, "masked": "****1234", "is_set": True},
            ...
        }
        """
        from backend.models import SystemConfig
        
        query = select(SystemConfig).where(
            SystemConfig.config_key.like("credential:%")
        )
        result = await self.db.execute(query)
        configs = result.scalars().all()
        
        credentials = {}
        for config in configs:
            key = config.config_key.replace("credential:", "")
            decrypted = decrypt_value(config.config_value) if config.config_value else ""
            
            # For email/URL, show partial; for passwords/keys, fully mask
            if key in [CredentialKey.BRAIN_EMAIL, CredentialKey.OPENAI_BASE_URL]:
                masked = decrypted[:3] + "***" + decrypted[-10:] if len(decrypted) > 13 else mask_value(decrypted)
            elif key == CredentialKey.OPENAI_MODEL:
                masked = decrypted  # Model name doesn't need masking
            else:
                masked = mask_value(decrypted)
            
            credentials[key] = {
                "masked": masked,
                "is_set": bool(decrypted),
                "source": "db",
                "updated_at": config.updated_at.isoformat() if config.updated_at else None
            }
        
        # Add defaults for known keys that aren't set
        for known_key in [
            CredentialKey.BRAIN_EMAIL,
            CredentialKey.BRAIN_PASSWORD,
            CredentialKey.OPENAI_API_KEY,
            CredentialKey.OPENAI_BASE_URL,
            CredentialKey.OPENAI_MODEL,
        ]:
            if known_key not in credentials:
                # Check env fallback
                env_map = {
                    CredentialKey.BRAIN_EMAIL: "BRAIN_EMAIL",
                    CredentialKey.BRAIN_PASSWORD: "BRAIN_PASSWORD",
                    CredentialKey.OPENAI_API_KEY: "OPENAI_API_KEY",
                    CredentialKey.OPENAI_BASE_URL: "OPENAI_BASE_URL",
                    CredentialKey.OPENAI_MODEL: "OPENAI_MODEL",
                }
                env_value = os.getenv(env_map.get(known_key, ""))
                credentials[known_key] = {
                    "masked": mask_value(env_value) if env_value else "(未配置)",
                    "is_set": bool(env_value),
                    "source": "env" if env_value else None,
                    "updated_at": None
                }
        
        return credentials
    
    async def test_brain_credentials(self) -> Dict[str, Any]:
        """Test Brain credentials by attempting login."""
        email = await self.get_credential(
            CredentialKey.BRAIN_EMAIL, 
            fallback_env="BRAIN_EMAIL"
        )
        password = await self.get_credential(
            CredentialKey.BRAIN_PASSWORD,
            fallback_env="BRAIN_PASSWORD"
        )
        
        if not email or not password:
            return {
                "success": False,
                "error": "Brain credentials not configured"
            }
        
        try:
            from backend.adapters.brain_adapter import BrainAdapter
            # Create adapter with explicit credentials
            adapter = BrainAdapter(email=email, password=password)
            # Get client and try to authenticate
            adapter.client = await adapter.get_client()
            result = await adapter.authenticate()
            return {
                "success": result,
                "message": "Authentication successful" if result else "Authentication failed"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid."""
        if not self._cache_time:
            return False
        age = (datetime.utcnow() - self._cache_time).total_seconds()
        return age < self._cache_ttl_seconds
    
    @classmethod
    def invalidate_cache(cls):
        """Invalidate the credential cache."""
        cls._cache.clear()
        cls._cache_time = None


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

def get_credentials_service(db: AsyncSession) -> CredentialsService:
    """Factory function to create CredentialsService."""
    return CredentialsService(db)
