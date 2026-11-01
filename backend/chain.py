"""LangChain chain for the Oracle assistant LLM call.

The chain receives static analysis results and retrieved knowledge
as context. It produces only LLM-generated findings — the caller
merges these with the deterministic static findings.
"""

import os
from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from schemas import AssistantResult

SYSTEM = """\
You are an Oracle SQL/PL-SQL legacy-code assistant.

You will receive:
  1. The developer's code and instruction.
  2. Static analysis results (deterministic checks already run).
  3. Retrieved knowledge entries (may or may not be relevant).

Deduplication (critical):
- DO NOT repeat, restate, or summarize static analysis findings in
  your findings, summary, explanation, or warnings. They are already
  shown to the developer. Your job is to add value BEYOND static analysis.
- If static analysis already covers the only issue, return an empty
  findings list and a brief summary acknowledging it.

Findings rules:
- Only report issues you can support with a specific code fragment.
- Set knowledge_id to the matching issue ID from retrieved knowledge.
  If no knowledge entry applies, leave knowledge_id empty.
- Never invent bugs, schema objects, APIs, or citations.
- Do not prescribe specific migration replacements unless you have
  concrete evidence the replacement fits this use case. State the
  fact (desupported/deprecated) and recommend the developer assess
  the appropriate migration path.
- Community reports are unverified leads, not confirmations.

Output rules:
- If no code change is justified, set proposed_code to an empty string.
  Do not return the original code as proposed_code.
- validation_status must always be "not_run".
- Keep summary and explanation concise — developers read this in an IDE.
- warnings should contain only information NOT already in findings.
"""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM),
    ("human", """\
Oracle version: {oracle_version}

Instruction: {instruction}

Selected code:
<code>
{code}
</code>

Surrounding code from the same file (for context only):
<surrounding>
{surrounding_code}
</surrounding>

Schema context ({schema_label}):
{schema_context}

Static analysis already found these issues (DO NOT repeat):
{static_findings}

Retrieved knowledge (assess critically — use only if relevant):
{retrieved_knowledge}

Return the required structured response.""")
])


@lru_cache(maxsize=1)
def _build_chain():
    """Build and cache the LangChain chain (singleton)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    model = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        google_api_key=key,
        temperature=0.1,
        max_retries=2,
    )
    return PROMPT | model.with_structured_output(
        AssistantResult, method="json_schema"
    )


def run_assistant(
    instruction: str,
    code: str,
    oracle_version: str,
    schema_context: str,
    surrounding_code: str,
    static_findings: list[dict],
    retrieved_knowledge: list[dict],
    schema_source: str = "none",
) -> dict:
    """Invoke the LLM chain and return the result as a dict."""
    # Tell the LLM how much to trust the schema
    if schema_source == "live_oracle_db":
        schema_label = "verified — queried from Oracle Data Dictionary"
    else:
        schema_label = "not provided"

    result = _build_chain().invoke({
        "instruction": instruction,
        "code": code,
        "oracle_version": oracle_version,
        "surrounding_code": surrounding_code or "(none)",
        "schema_context": schema_context or "(not provided)",
        "schema_label": schema_label,
        "static_findings": static_findings or "(none)",
        "retrieved_knowledge": retrieved_knowledge or "(none)",
    })
    payload = result.model_dump() if hasattr(result, "model_dump") else result
    payload["validation_status"] = "not_run"
    return payload
