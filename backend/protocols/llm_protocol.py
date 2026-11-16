"""
LLM Protocol - Abstract interface for Large Language Model services

This protocol defines the contract for LLM interactions,
allowing for easy testing with mock implementations and
flexible switching between different LLM providers.
"""

from typing import Protocol, Dict, Optional, Any, Type, Tuple, runtime_checkable
from dataclasses import dataclass
from pydantic import BaseModel


@dataclass
class LLMResponse:
    """
    Standard response wrapper for LLM calls.
    
    Attributes:
        content: Raw text content from LLM
        parsed: Parsed JSON if json_mode was used
        model: Model identifier used for the call
        tokens_used: Total tokens consumed
        latency_ms: Response latency in milliseconds
        success: Whether the call succeeded
        error: Error message if call failed
    """
    content: str
    parsed: Optional[Dict[str, Any]] = None
    model: str = ""
    tokens_used: int = 0
    latency_ms: int = 0
    success: bool = True
    error: Optional[str] = None
    
    @classmethod
    def from_error(cls, error: str, model: str = "", latency_ms: int = 0) -> "LLMResponse":
        """Create a failed response from an error message."""
        return cls(
            content="",
            model=model,
            latency_ms=latency_ms,
            success=False,
            error=error,
        )
    
    def get_parsed_or_default(self, default: Dict[str, Any] = None) -> Dict[str, Any]:
        """Get parsed content or return default if parsing failed."""
        return self.parsed if self.parsed is not None else (default or {})


@dataclass
class LLMCallOptions:
    """Options for LLM calls."""
    temperature: float = 0.7
    max_tokens: int = 4096
    json_mode: bool = True
    timeout_seconds: float = 60.0
    retry_attempts: int = 3


@runtime_checkable
class LLMProtocol(Protocol):
    """
    Protocol for LLM service interactions.
    
    Implementations must provide methods for:
    - Basic text completion with optional JSON mode
    - Schema-validated responses using Pydantic models
    - Credential management
    """
    
    @property
    def model(self) -> str:
        """Get the current model identifier."""
        ...
    
    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = True,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """
        Make an LLM call with automatic retries and logging.
        
        Args:
            system_prompt: System/instruction message
            user_prompt: User/input message
            temperature: Sampling temperature (0.0 to 2.0)
            json_mode: Whether to request JSON output format
            max_tokens: Maximum tokens in response
            
        Returns:
            LLMResponse with content and metadata
        """
        ...
    
    async def call_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[BaseModel],
        temperature: float = 0.7,
    ) -> Tuple[Optional[BaseModel], LLMResponse]:
        """
        Call LLM and validate response against a Pydantic schema.
        
        Args:
            system_prompt: System/instruction message
            user_prompt: User/input message
            schema: Pydantic model class to validate against
            temperature: Sampling temperature
            
        Returns:
            Tuple of (validated model or None, raw response)
        """
        ...
    
    def invalidate_credentials_cache(self) -> None:
        """
        Invalidate cached credentials.
        
        Call this after credentials are updated in the database
        to force reloading on next call.
        """
        ...


class LLMServiceProtocol(LLMProtocol, Protocol):
    """
    Extended protocol for LLM service with additional management methods.
    
    This extends LLMProtocol with methods for service lifecycle management.
    """
    
    async def health_check(self) -> bool:
        """
        Check if the LLM service is healthy and responding.
        
        Returns:
            True if service is healthy, False otherwise
        """
        ...
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get usage statistics for the service.
        
        Returns:
            Dict with stats like total_calls, total_tokens, avg_latency
        """
        ...
