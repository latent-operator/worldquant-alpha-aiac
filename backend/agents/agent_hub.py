from typing import List, Dict, Optional
import logging
from pydantic import BaseModel, Field
from backend.config import settings

logger = logging.getLogger("agent_hub")

# --- Schemas ---
class AlphaExpression(BaseModel):
    alpha_expression: str 
    economic_rationale: str
    data_fields_used: List[str]
    operators_used: List[str]

class AlphaExpressions(BaseModel):
    alphas: List[AlphaExpression]

# --- Prompts ---
SYSTEM_PROMPT = """You are an expert quantitative researcher with deep knowledge of WorldQuant Brain syntax. 
Your goal is to generate high-quality, diverse alpha expressions based on given datasets and hypotheses.
You must strictly follow the syntax rules and output JSON only."""

class AgentHub:
    def __init__(self):
        self.model = settings.OPENAI_MODEL

    async def generate_alphas(self, 
                              hypothesis: str, 
                              dataset_context: str, 
                              operator_context: str,
                              n: int = 5) -> List[Dict]:
        
        user_prompt = f"""
        Hypothesis: "{hypothesis}"
        
        Target: Generate {n} diverse alpha expressions.
        
        Dataset Info:
        {dataset_context}
        
        Operators:
        {operator_context}
        
        Requirements:
        1. Valid WorldQuant syntax.
        2. Use specified fields/operators.
        3. Explain logic clearly.
        4. Output JSON format matching schema:
        {{
            "alphas": [
                {{
                    "alpha_expression": "...", 
                    "economic_rationale": "...",
                    "data_fields_used": ["..."],
                    "operators_used": ["..."]
                }}
            ]
        }}
        """
        
        try:
            from backend.agents.services.llm_service import get_llm_service

            response = await get_llm_service().call(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.7,
                json_mode=True,
            )
            if not response.success or not response.parsed:
                logger.warning("LLM generation failed")
                return []
            return response.parsed.get("alphas", [])
        except Exception as e:
            from backend.services.credentials_service import CredentialsService
            logger.error(f"LLM Generation failed: {CredentialsService.sanitize_error(e)}")
            return []

agent_hub = AgentHub()
