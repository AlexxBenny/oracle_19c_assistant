from typing import Literal
from pydantic import BaseModel, Field


class AssistRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    code: str = Field(min_length=1, max_length=20000)
    oracle_version: str = Field(default="19c", max_length=40)
    surrounding_code: str = Field(
        default="",
        max_length=20000,
        description="Enclosing scope and nearby code from the same file."
    )
    oracle_owner: str = Field(
        default="",
        max_length=128,
        description="Oracle schema owner for Data Dictionary lookup. "
                    "Defaults to ORACLE_USER env var on the backend."
    )


# ── Static analysis output (deterministic, pre-LLM) ─────────────────

class StaticFinding(BaseModel):
    rule_id: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    finding_type: Literal[
        "syntax_error", "desupported_feature", "deprecated_feature",
        "schema_mismatch", "anti_pattern", "parse_warning"
    ]
    description: str
    evidence: str
    source: str = "static_analysis"


# ── LLM-generated output ────────────────────────────────────────────

class Finding(BaseModel):
    severity: Literal["info", "low", "medium", "high", "critical"]
    finding_type: Literal[
        "confirmed_issue", "potential_risk", "best_practice",
        "insufficient_evidence"
    ]
    description: str
    evidence: str
    evidence_source: Literal[
        "provided_code", "official_documentation",
        "community_report", "inference", "none"
    ]
    knowledge_id: str = Field(
        default="",
        description="Issue ID from the knowledge base that supports this finding. "
                    "Empty string if not backed by a knowledge base entry."
    )


class AssistantResult(BaseModel):
    """Schema the LLM must produce."""
    task: str
    summary: str
    findings: list[Finding]
    proposed_code: str
    explanation: str
    warnings: list[str]
    validation_status: Literal["not_run"] = "not_run"


# ── Combined API response ───────────────────────────────────────────

class AssistResponse(BaseModel):
    """What the /assist endpoint actually returns to the extension."""
    task: str
    summary: str
    static_findings: list[StaticFinding] = Field(
        default_factory=list,
        description="Deterministic findings from static analysis (SQLGlot, pattern checks)."
    )
    llm_findings: list[Finding] = Field(
        default_factory=list,
        description="LLM-generated findings backed by retrieved knowledge."
    )
    proposed_code: str = ""
    explanation: str = ""
    warnings: list[str] = Field(default_factory=list)
    validation_status: str = "not_run"
    schema_source: str = Field(
        default="none",
        description="Where schema came from: 'live_oracle_db', 'file_extraction', or 'none'."
    )
