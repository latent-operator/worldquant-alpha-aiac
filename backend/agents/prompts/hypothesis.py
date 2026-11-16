"""
Hypothesis and distillation prompts.

Redesigned based on RD-Agent's hypothesis-driven approach:
- Precise, testable, actionable hypotheses
- Balanced exploration and exploitation
- Knowledge transfer from experiments
- No preconceived biases about what works

Contains:
- HYPOTHESIS_SYSTEM: System prompt for hypothesis generation
- DISTILL_SYSTEM: System prompt for concept distillation
- build_hypothesis_prompt: Builder for hypothesis prompt
- build_distill_prompt: Builder for distillation prompt
"""

from typing import Dict, List, Optional

from backend.agents.prompts.base import (
    PromptContext,
    build_fields_context,
    build_patterns_context,
)


HYPOTHESIS_SYSTEM = """You are a quantitative research scientist conducting data-driven research.

Your role is to generate investment hypotheses for testing. The approach is empirical:
1. Observe the data characteristics and historical experiment results
2. Form a precise, testable hypothesis about potential market relationships
3. Design an experiment to validate or refute the hypothesis

**Core Principles**:
- Be objective: Do not assume any particular approach is better a priori
- Be precise: Each hypothesis should focus on a single testable idea
- Be exploratory: Consider unconventional relationships the data might reveal
- Learn from feedback: Analyze why previous experiments succeeded or failed

**Hypothesis Quality Standards**:
1. Testable: Can be validated with a concrete experiment
2. Specific: Avoids vague statements like "improve performance"
3. Actionable: Clear enough to implement directly
4. Focused: One direction per hypothesis, not "A or B might work"

Output must be valid JSON."""


DISTILL_SYSTEM = """You are a research assistant helping to identify promising research directions.

Your role is to analyze dataset characteristics and suggest field categories
that may contain useful signals. Be objective in your analysis:
- Do not assume certain field types are inherently better
- Consider the specific context and data characteristics
- Balance well-known approaches with unexplored categories

Selection should be based on evidence, not assumptions."""


def build_hypothesis_prompt(
    ctx: PromptContext,
    experiment_trace: Optional[List[Dict]] = None
) -> str:
    """
    Build prompt for hypothesis generation.
    
    Redesigned based on RD-Agent's hypothesis-driven approach:
    - Includes experiment history with feedback
    - Emphasizes learning from failures
    - Encourages both exploration and exploitation
    """
    
    # Build experiment trace section if available
    trace_section = ""
    if experiment_trace:
        trace_entries = []
        for i, entry in enumerate(experiment_trace[-10:], 1):  # Last 10 experiments
            exp = entry.get('experiment', {})
            feedback = entry.get('feedback', {})
            
            trace_entries.append(f"""
### Experiment {i}
**Hypothesis**: {exp.get('hypothesis', 'Not recorded')}
**Expression Tested**: `{exp.get('expression', 'N/A')[:100]}`
**Results**:
- Sharpe: {exp.get('sharpe', 'N/A')}, Fitness: {exp.get('fitness', 'N/A')}, Turnover: {exp.get('turnover', 'N/A')}
**Observation**: {feedback.get('observation', 'No observation recorded')}
**Evaluation**: {feedback.get('evaluation', 'Not evaluated')}
**Outcome**: {'SUCCESS' if feedback.get('success') else 'FAILED'} - {feedback.get('reason', '')}
""")
        
        trace_section = f"""
## Experiment History

The following experiments have been conducted. Analyze them to understand:
- What worked and why
- What failed and why
- What directions remain unexplored

{''.join(trace_entries)}
"""
    
    # Build strategy guidance (non-prescriptive)
    strategy_section = """
## Research Strategy

Consider both:
1. **Exploitation**: Refine approaches that showed promise in previous experiments
2. **Exploration**: Test new directions that haven't been tried yet

The balance depends on current progress:
- If recent experiments are failing: Consider new directions
- If recent experiments show partial success: Consider refinements
- If no clear pattern: Prioritize diverse exploration
"""
    
    # Build field categories overview
    field_overview = build_fields_context(ctx.fields, max_fields=20)
    
    return f"""## Research Context

**Dataset**: {ctx.dataset_id}
**Category**: {ctx.dataset_category or 'General'}
**Description**: {ctx.dataset_description or 'Not provided'}
**Region**: {ctx.region} | **Universe**: {ctx.universe}

## Available Data Fields (Sample)

{field_overview}

## Historical Patterns (For Reference Only)

**Approaches that have worked in similar contexts**:
{build_patterns_context(ctx.success_patterns, "patterns")}

**Approaches that have not worked**:
{build_patterns_context(ctx.failure_pitfalls, "pitfalls")}

Note: These are observations, not rules. What failed before may work in different contexts.
{trace_section}
{strategy_section}

## Task

Generate 3-5 investment hypotheses for this dataset.

**Requirements**:
1. Each hypothesis should be specific and testable
2. Include both conventional and unconventional ideas
3. Explain the reasoning behind each hypothesis
4. Consider what market behavior or inefficiency the data might capture

**Output Schema** (JSON):
```json
{{
  "analysis": {{
    "data_observations": "Key observations about the dataset characteristics",
    "unexplored_directions": "Promising directions not yet tested",
    "refinement_opportunities": "Ways to improve on partial successes"
  }},
  "hypotheses": [
    {{
      "id": "H1",
      "statement": "Clear, testable hypothesis in one sentence",
      "rationale": "Economic or behavioral reasoning behind this hypothesis",
      "expected_signal": "momentum | mean_reversion | value | other",
      "key_fields": ["field1", "field2"],
      "suggested_approach": "Brief description of how to test this",
      "confidence": "high | medium | low",
      "novelty": "established | emerging | experimental"
    }}
  ],
  "knowledge_transfer": {{
    "if_then_rules": [
      "If [condition observed in experiments], then [conclusion]"
    ],
    "patterns_discovered": "Any new patterns discovered from experiment analysis"
  }}
}}
```"""


def build_distill_prompt(ctx: PromptContext, field_categories: Dict[str, List[str]]) -> str:
    """
    Build prompt for concept distillation.
    
    Redesigned to be more objective and less prescriptive.
    """
    
    categories_text = []
    for cat, fields in sorted(field_categories.items()):
        sample = ", ".join(fields[:5])
        if len(fields) > 5:
            sample += f" ... (+{len(fields) - 5} more)"
        categories_text.append(f"- **{cat}** ({len(fields)} fields): {sample}")
    
    return f"""## Analysis Task

**Dataset**: {ctx.dataset_id}
**Description**: {ctx.dataset_description or 'Not provided'}
**Category**: {ctx.dataset_category or 'General'}

## Available Field Categories

{chr(10).join(categories_text)}

## Historical Context (For Reference)

Previous successful patterns have used these types of data:
{build_patterns_context(ctx.success_patterns, "patterns")}

Note: This is historical observation, not a prescription. New opportunities may exist elsewhere.

## Task

Identify 3-5 field categories that warrant investigation.

**Selection Approach**:
- Consider both high-probability and high-potential categories
- Include at least one less-explored category
- Balance between exploitation (known useful) and exploration (potentially useful)

**Output Schema** (JSON):
```json
{{
  "analysis": {{
    "dataset_characteristics": "Key features of this dataset",
    "category_assessment": "Brief assessment of each category's potential"
  }},
  "selected_categories": [
    {{
      "category": "Exact category name",
      "rationale": "Why this category may contain useful signals",
      "exploration_type": "exploitation | exploration | balanced"
    }}
  ],
  "reasoning": "Overall selection strategy explanation"
}}
```

**Important**: Use exact category names from the list above."""
