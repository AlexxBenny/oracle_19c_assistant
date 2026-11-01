"""Oracle Legacy Code Assistant — FastAPI application.

Pipeline:
  0. Schema catalogue (built from Oracle Data Dictionary, cached)
  1. Static analysis (deterministic, SQLGlot + pattern checks)
  2. Hybrid retrieval (Chroma vectors + BM25, fused with RRF)
  3. LLM analysis (Gemini, receives static + retrieved context)
  4. Response assembly (deduplicate, filter, suppress noise)
"""

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # Must run before any module reads os.getenv

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from chain import run_assistant
from oracle_schema import get_catalogue, is_schema_available
from retrieval import get_retrieval_status, hybrid_retrieve
from schemas import AssistRequest, AssistResponse, StaticFinding
from static_analysis import extract_table_refs, run_static_analysis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oracle_assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalogue = get_catalogue()
    if catalogue.is_available:
        catalogue.build()
        logger.info("Schema catalogue: READY")
    else:
        logger.info("Schema catalogue: UNAVAILABLE (no Oracle credentials)")
    yield


app = FastAPI(
    title="Oracle Legacy Code Assistant",
    version="0.6.0",
    description="Oracle SQL/PL-SQL assistant: static analysis + RAG + Gemini.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    try:
        return {
            "status": "ok",
            "schema_catalogue": is_schema_available(),
            **get_retrieval_status(),
        }
    except Exception as exc:
        logger.exception("Health check failed")
        raise HTTPException(
            503, detail=f"Retrieval unavailable: {type(exc).__name__}"
        ) from exc


@app.post("/refresh-catalogue")
def refresh_catalogue():
    """Force-rebuild the schema catalogue from Oracle Data Dictionary."""
    catalogue = get_catalogue()
    if not catalogue.is_available:
        raise HTTPException(
            503, detail="Oracle connection not configured. Check ORACLE_USER/PASSWORD/DSN."
        )
    catalogue.build()
    known = catalogue.get_known_tables()
    return {
        "status": "ok",
        "objects": len(known),
        "message": f"Catalogue refreshed: {len(known)} objects loaded.",
    }


@app.post("/assist", response_model=AssistResponse)
def assist(request: AssistRequest):
    try:
        catalogue = get_catalogue()
        cat_owner = catalogue.owner if catalogue.is_available else ""

        # ── Step 1: Get schema from catalogue ────────────────────
        known_tables = catalogue.get_known_tables() if catalogue.is_available else {}
        schema_source = "live_oracle_db" if known_tables else "none"

        # ── Step 2: Deterministic static analysis ────────────────
        #   Passes catalogue_owner so the analyzer skips tables
        #   with foreign schema qualifiers (e.g., HR.EMPLOYEE).
        static_result = run_static_analysis(
            code=request.code,
            known_tables=known_tables,
            catalogue_owner=cat_owner,
        )
        static_findings = [
            StaticFinding(**f) for f in static_result.get("findings", [])
        ]

        # ── Step 3: Resolve table references ─────────────────────
        #   Separate own-schema refs from foreign-schema refs.
        table_refs = extract_table_refs(request.code)
        own_tables = [
            name for schema, name in table_refs
            if not schema or schema == cat_owner
        ]
        foreign_refs = [
            (schema, name) for schema, name in table_refs
            if schema and schema != cat_owner
        ]

        # Build LLM schema context from own-schema tables only
        schema_context = catalogue.format_for_tables(own_tables) if own_tables else ""

        # Resolve foreign-schema references via live queries
        for schema, name in foreign_refs:
            resolution = catalogue.resolve_qualified_ref(schema, name)

            if resolution["status"] == "found" and resolution["columns"]:
                # Object is accessible — add to schema context for the LLM
                cols = ", ".join(resolution["columns"])
                schema_context += f"\n{schema}.{name}({cols})"

            elif resolution["status"] == "wrong_schema":
                alts = ", ".join(
                    f"{a['owner']}.{a['table_name']}"
                    for a in resolution["alternatives"]
                )
                owner_note = (
                    f"The schema '{schema}' exists"
                    if resolution["owner_exists"]
                    else f"The schema '{schema}' was not found in accessible metadata"
                )
                static_findings.append(StaticFinding(
                    rule_id="SCHEMA-RESOLVE",
                    severity="medium",
                    finding_type="schema_mismatch",
                    description=(
                        f"{schema}.{name} was not found in accessible metadata. "
                        f"{owner_note}. "
                        f"A table with the same name exists under: {alts}. "
                        f"This is a different database object. "
                        f"Verify whether the intended table is under "
                        f"one of those schemas, or whether {schema} "
                        f"should be accessible to the connected account."
                    ),
                    evidence=f"{schema}.{name}",
                    source="schema_resolution",
                ))

            elif resolution["status"] == "not_found":
                owner_note = (
                    f"The schema '{schema}' exists but {name} was not found in it"
                    if resolution["owner_exists"]
                    else f"The schema '{schema}' was not found in accessible metadata"
                )
                static_findings.append(StaticFinding(
                    rule_id="SCHEMA-RESOLVE",
                    severity="medium",
                    finding_type="schema_mismatch",
                    description=(
                        f"{schema}.{name} was not found in accessible metadata. "
                        f"{owner_note}. "
                        f"This does not confirm the object doesn't exist globally "
                        f"— it may be inaccessible to the connected account."
                    ),
                    evidence=f"{schema}.{name}",
                    source="schema_resolution",
                ))

        # ── Step 4: Hybrid retrieval (internal context) ──────────
        retrieved = hybrid_retrieve(
            query=f"{request.instruction}\n{request.code}",
            oracle_version=request.oracle_version,
            top_k=3,
        )

        # ── Step 5: LLM analysis ────────────────────────────────
        llm_result = run_assistant(
            instruction=request.instruction,
            code=request.code,
            oracle_version=request.oracle_version,
            schema_context=schema_context,
            surrounding_code=request.surrounding_code,
            static_findings=[f.model_dump() for f in static_findings],
            retrieved_knowledge=retrieved,
            schema_source=schema_source,
        )

        # ── Step 6: Deduplicate + filter ─────────────────────────
        retrieved_ids = {str(x["id"]) for x in retrieved}
        static_rule_ids = {f.rule_id for f in static_findings}

        llm_findings = []
        for f in llm_result.get("findings", []):
            kid = str(f.get("knowledge_id", ""))
            if kid and kid not in retrieved_ids:
                continue
            if kid and kid in static_rule_ids:
                continue
            llm_findings.append(f)

        # ── Step 7: Suppress proposed_code when unchanged ────────
        proposed = llm_result.get("proposed_code", "")
        if proposed.strip() == request.code.strip():
            proposed = ""

        # ── Step 8: Assemble response ────────────────────────────
        return AssistResponse(
            task=llm_result.get("task", request.instruction),
            summary=llm_result.get("summary", ""),
            static_findings=static_findings,
            llm_findings=llm_findings,
            proposed_code=proposed,
            explanation=llm_result.get("explanation", ""),
            warnings=llm_result.get("warnings", []),
            validation_status="not_run",
            schema_source=schema_source,
        )

    except Exception as exc:
        logger.exception("Assistant pipeline failed")
        raise HTTPException(
            502,
            detail=f"Assistant pipeline failed: {type(exc).__name__}. "
                   f"Check backend terminal.",
        ) from exc
