"""
LLM Service - Unified LLM calling interface with logging and retries

Implements LLMProtocol for dependency injection and testability.
High cohesion: All LLM-related logic in one place.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional, Any, Type, Tuple
from pydantic import BaseModel
import openai
from loguru import logger

from backend.config import settings
from backend.protocols.llm_protocol import LLMProtocol, LLMResponse as LLMResponseProtocol


class LLMResponse(BaseModel):
    """Standard LLM response wrapper."""
    content: str
    parsed: Optional[Dict] = None
    model: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: Optional[str] = None
    latency_ms: int = 0
    success: bool = True
    error: Optional[str] = None
    
    def to_protocol_response(self) -> LLMResponseProtocol:
        """Convert to protocol response type."""
        return LLMResponseProtocol(
            content=self.content,
            parsed=self.parsed,
            model=self.model,
            tokens_used=self.tokens_used,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            finish_reason=self.finish_reason,
            latency_ms=self.latency_ms,
            success=self.success,
            error=self.error,
        )


class LLMBlockedError(Exception):
    """A provider failure that must stop generation before fallback candidates."""

    def __init__(self, kind: str, safe_detail: Optional[str] = None):
        self.kind = kind
        self.safe_detail = safe_detail if safe_detail in {"TIMEOUT", "CONNECT"} else None
        super().__init__(f"LLM_{kind}")


def classify_llm_error(exc: Exception) -> str:
    """Return a safe category; never persist provider response bodies or keys."""
    status = getattr(exc, "status_code", None)
    message = str(exc).lower()
    if status == 401 or "invalid api key" in message:
        return "INVALID_CREDENTIALS"
    if status == 403 and any(word in message for word in ("daily usage limit", "quota", "usage limit", "insufficient")):
        return "DAILY_QUOTA"
    if status == 429 or isinstance(exc, openai.RateLimitError):
        return "RATE_LIMIT"
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, TimeoutError, ConnectionError)):
        return "CONNECTION_ERROR"
    if status == 403:
        return "ACCESS_DENIED"
    return "PROVIDER_ERROR"


def _safe_connection_detail(exc: Exception) -> Optional[str]:
    if isinstance(exc, (openai.APITimeoutError, TimeoutError)):
        return "TIMEOUT"
    if isinstance(exc, (openai.APIConnectionError, ConnectionError)):
        return "CONNECT"
    return None


class LLMService:
    """
    Unified LLM Service implementing LLMProtocol.
    
    Features:
    - Automatic retries with exponential backoff
    - JSON cleaning (markdown removal)
    - Token tracking
    - Structured logging
    - Credential caching with invalidation support
    
    This class implements the LLMProtocol interface, allowing for
    easy mocking in tests and dependency injection.
    """
    
    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None
    ):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url or settings.OPENAI_BASE_URL
        self.model = model or getattr(settings, 'OPENAI_MODEL', 'deepseek-chat')

        self._credentials_lock = asyncio.Lock()
        self._credentials_loaded = False
        self._credentials_revision = -1
        
        self.client = openai.AsyncOpenAI(
            # Delay actual credential availability to _ensure_credentials_loaded;
            # this placeholder is never sent when the credential is absent.
            api_key=self.api_key or "not-configured",
            base_url=self.base_url,
            timeout=getattr(settings, "LLM_TIMEOUT_SECONDS", 60),
            max_retries=0,
        )
        
        logger.info(f"[LLMService] Initialized | model={self.model} base_url={self.base_url}")

    async def _ensure_credentials_loaded(self):
        async with self._credentials_lock:
            try:
                from backend.database import AsyncSessionLocal
                from backend.services.credentials_service import CredentialsService, CredentialKey

                async with AsyncSessionLocal() as db:
                    service = CredentialsService(db)
                    revision = await service.get_revision()
                    if self._credentials_loaded and revision == self._credentials_revision:
                        return
                    api_key = await service.get_credential(CredentialKey.OPENAI_API_KEY) or settings.OPENAI_API_KEY
                    base_url = await service.get_credential(CredentialKey.OPENAI_BASE_URL) or settings.OPENAI_BASE_URL
                    model = await service.get_credential(CredentialKey.OPENAI_MODEL) or getattr(settings, "OPENAI_MODEL", "deepseek-chat")

                changed = (api_key, base_url, model) != (self.api_key, self.base_url, self.model)
                self.api_key, self.base_url, self.model = api_key, base_url, model
                if (changed or not self._credentials_loaded) and self.api_key:
                    old_client = self.client
                    self.client = openai.AsyncOpenAI(
                        api_key=self.api_key,
                        base_url=self.base_url,
                        timeout=getattr(settings, "LLM_TIMEOUT_SECONDS", 60),
                        max_retries=0,
                    )
                    await old_client.close()
                self._credentials_revision = revision
            except Exception as e:
                logger.warning(f"[LLMService] Credential refresh failed ({type(e).__name__})")
            finally:
                self._credentials_loaded = True

    def invalidate_credentials_cache(self):
        self._credentials_loaded = False

    async def test_connection(self) -> Dict[str, Any]:
        """Check provider authentication via the models endpoint; does not run generation."""
        await self._ensure_credentials_loaded()
        if not self.api_key:
            return {"success": False, "error": "LLM API credentials are not configured"}
        try:
            await asyncio.wait_for(self.client.models.list(), timeout=15)
            return {"success": True, "message": "LLM API connection successful"}
        except Exception as exc:
            from backend.services.credentials_service import CredentialsService
            return {"success": False, "error": CredentialsService.sanitize_error(exc, [self.api_key])}
    
    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = True,
        max_tokens: int = 4096
    ) -> LLMResponse:
        """
        Make an LLM call with automatic retries and logging.
        
        Args:
            system_prompt: System message
            user_prompt: User message
            temperature: Sampling temperature
            json_mode: Whether to request JSON output
            max_tokens: Maximum response tokens
            
        Returns:
            LLMResponse with content and metadata
        """
        start_time = time.time()
        call_id = f"{int(start_time * 1000) % 100000}"
        
        logger.debug(f"[LLMService] Call started | id={call_id} json_mode={json_mode}")
        
        try:
            await self._ensure_credentials_loaded()
            
            # Kimi k2.6 and some other o1-like models only support temperature=1
            actual_temp = temperature
            if "kimi-k2.6" in self.model:
                actual_temp = 1.0
            disable_thinking = self._should_disable_thinking()
            if disable_thinking and "kimi-k2.6" in (self.model or "").lower():
                actual_temp = 0.6
                
            request_kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": actual_temp,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"} if json_mode else None,
            }

            if disable_thinking:
                request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

            for attempt in range(3):
                try:
                    response = await self.client.chat.completions.create(**request_kwargs)
                    break
                except Exception as exc:
                    kind = classify_llm_error(exc)
                    if kind in {"RATE_LIMIT", "CONNECTION_ERROR"} and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise LLMBlockedError(
                        kind, _safe_connection_detail(exc) if kind == "CONNECTION_ERROR" else None
                    ) from None
            
            # Defensive: handle empty/malformed responses
            choices = getattr(response, "choices", None)
            if not response or not choices:
                status = getattr(response, "status", None)
                msg = getattr(response, "msg", None)
                extra = f" | status={status} msg={msg}" if status or msg else ""
                raise ValueError(f"Empty response from LLM API{extra}")

            if len(choices) == 0:
                raise ValueError("Empty choices from LLM API")
            
            message = response.choices[0].message
            if not message:
                raise ValueError("No message in LLM response")
                
            content = message.content or ""
            usage = getattr(response, "usage", None)
            tokens_used = getattr(usage, "total_tokens", 0) or 0
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            latency_ms = int((time.time() - start_time) * 1000)
            if json_mode and not content.strip():
                reason = "TRUNCATED_JSON" if finish_reason == "length" else "EMPTY_LLM_CONTENT"
                logger.warning(f"[LLMService] Empty JSON response | id={call_id} reason={reason} finish_reason={finish_reason}")
                return LLMResponse(
                    content="", model=self.model, tokens_used=tokens_used,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    finish_reason=finish_reason, latency_ms=latency_ms,
                    success=False, error=reason,
                )
            
            # Parse JSON if requested
            parsed = None
            parse_error = None
            if json_mode:
                try:
                    cleaned = self._clean_json(content)
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError as e:
                    parse_error = "TRUNCATED_JSON" if finish_reason == "length" else f"INVALID_JSON: {e}"
                    logger.warning(
                        f"[LLMService] JSON parse failed | id={call_id} "
                        f"reason={parse_error} finish_reason={finish_reason} "
                        f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens}"
                    )
                    parsed = self._parse_json_lenient(content)
                    if parsed is not None:
                        parse_error = None
                        logger.info(f"[LLMService] Lenient JSON parse recovered | id={call_id}")
                if finish_reason == "length":
                    parse_error = "TRUNCATED_JSON"
            
            logger.info(
                f"[LLMService] Call complete | id={call_id} parsed={parsed is not None and finish_reason != 'length' if json_mode else 'N/A'} "
                f"finish_reason={finish_reason} prompt_tokens={prompt_tokens} "
                f"completion_tokens={completion_tokens} total_tokens={tokens_used} latency={latency_ms}ms"
            )
            
            return LLMResponse(
                content=content,
                parsed=parsed,
                model=self.model,
                tokens_used=tokens_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason,
                latency_ms=latency_ms,
                success=(not json_mode or (parsed is not None and finish_reason != "length")),
                error=parse_error
            )
            
        except LLMBlockedError as exc:
            logger.warning(
                f"[LLMService] Call blocked | id={call_id} kind={exc.kind} "
                f"detail={exc.safe_detail or 'NONE'}"
            )
            raise
        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            from backend.services.credentials_service import CredentialsService
            safe_error = CredentialsService.sanitize_error(e, [self.api_key, self.base_url])
            logger.error(f"[LLMService] Call failed | id={call_id} error={safe_error}")
            
            return LLMResponse(
                content="",
                model=self.model,
                latency_ms=latency_ms,
                success=False,
                error=safe_error
            )
    
    async def call_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel],
        temperature: float = 0.7
    ) -> tuple[Optional[BaseModel], LLMResponse]:
        """
        Call LLM and validate response against a Pydantic schema.
        
        Args:
            system_prompt: System message
            user_prompt: User message
            schema: Pydantic model class to validate against
            temperature: Sampling temperature
            
        Returns:
            Tuple of (parsed model or None, raw response)
        """
        response = await self.call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_mode=True
        )
        
        if not response.success or not response.parsed:
            return None, response
        
        try:
            validated = schema.model_validate(response.parsed)
            return validated, response
        except Exception as e:
            logger.warning(f"[LLMService] Schema validation failed | error={e}")
            return None, response
    
    def _clean_json(self, content: str) -> str:
        """Remove markdown code blocks from JSON response."""
        content = content.strip()
        
        # Remove leading markdown
        if content.startswith('```json'):
            content = content[7:]
        elif content.startswith('```'):
            content = content[3:]
        
        # Remove trailing markdown
        if content.endswith('```'):
            content = content[:-3]
        
        return content.strip()

    def _parse_json_lenient(self, content: str) -> Optional[Dict]:
        """Recover common provider JSON wrappers without hiding truncation errors."""
        cleaned = self._clean_json(content)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        try:
            parsed = json.loads(cleaned[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _should_disable_thinking(self) -> bool:
        """Disable reasoning mode for JSON workflow calls on supported providers."""
        if not getattr(settings, "LLM_DISABLE_THINKING", True):
            return False

        model = (self.model or "").lower()
        base_url = (self.base_url or "").lower()
        is_kimi = "moonshot" in base_url and ("kimi-k2.5" in model or "kimi-k2.6" in model)
        is_deepseek_v4 = "deepseek" in base_url and model in {"deepseek-v4-flash", "deepseek-v4-pro"}
        return is_kimi or is_deepseek_v4


# Singleton instance for reuse
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get or create singleton LLM service."""
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
