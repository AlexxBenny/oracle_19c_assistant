"""Oracle-aware static analysis using ANTLR4 and SQLGlot.

Detection rules are loaded from knowledge.json at startup.
Each issue with a `static_detection` field becomes a static rule.

Four matcher types:
  - identifier:  word-boundary match (DBMS_STREAMS, DBMS_JOB.SUBMIT)
  - literal:     whitespace-normalized text match (WHEN OTHERS THEN NULL)
  - regex:       explicit regex pattern
  - ast:         structural check on ANTLR parse-tree facts

Parsing pipeline:
  ANTLR4 parses the full PL/SQL or SQL input, producing structural facts
  (function calls, SELECT stars) and outermost DML fragments.  AST checks
  evaluate the facts.  SQLGlot parses extracted DML for schema validation.
"""

from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from plsql_parser import parse_and_extract, ParseFacts

logger = logging.getLogger("oracle_assistant.static")

# ── Knowledge-driven rule loading ────────────────────────────────────

_KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge.json"


def _severity_from(category: str) -> str:
    """Map knowledge.json category to finding severity."""
    if "desupported" in category:
        return "high"
    if "deprecated" in category:
        return "medium"
    return "low"


def _finding_type_from(category: str) -> str:
    """Map knowledge.json category to StaticFinding.finding_type."""
    if "desupported" in category:
        return "desupported_feature"
    if "deprecated" in category:
        return "deprecated_feature"
    return "anti_pattern"


def _load_static_rules() -> list[dict]:
    """Load issues from knowledge.json that have static_detection configured.

    Each returned rule dict has:
        id, rule, matcher, patterns, ast_check, severity, finding_type
    """
    try:
        data = json.loads(_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Could not load knowledge.json: %s", exc)
        return []

    rules = []
    for issue in data.get("issues", []):
        detection = issue.get("static_detection")
        if not detection:
            continue
        category = issue.get("category", "")
        rules.append({
            "id": issue["id"],
            "rule": issue.get("rule", ""),
            "matcher": detection["matcher"],
            "patterns": detection.get("patterns", []),
            "ast_check": detection.get("ast_check", ""),
            "severity": _severity_from(category),
            "finding_type": _finding_type_from(category),
        })
    return rules


_STATIC_RULES = _load_static_rules()


# ── Matchers ─────────────────────────────────────────────────────────

def _match_identifier(code: str, patterns: list[str]) -> str | None:
    """Match SQL/PL-SQL identifiers using word boundaries.

    Correct for tokens like DBMS_STREAMS, DBMS_JOB.SUBMIT, ORDImage.
    Dots in patterns like DBMS_JOB.SUBMIT are matched literally.
    """
    regex = re.compile(
        "|".join(rf"\b{re.escape(p)}\b" for p in patterns),
        re.IGNORECASE,
    )
    m = regex.search(code)
    return m.group(0) if m else None


def _match_literal(code: str, patterns: list[str]) -> str | None:
    """Match literal text after collapsing whitespace.

    Handles formatting variations like:
        WHEN OTHERS THEN NULL
        WHEN OTHERS  THEN  NULL
        WHEN OTHERS\\n    THEN NULL
    """
    normalized = re.sub(r"\s+", " ", code.upper())
    for pattern in patterns:
        target = re.sub(r"\s+", " ", pattern.strip().upper())
        if target in normalized:
            return pattern
    return None


def _match_regex(code: str, patterns: list[str]) -> str | None:
    """Match an explicitly defined regex pattern."""
    for pattern in patterns:
        m = re.search(pattern, code, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


# ── AST-based checks ────────────────────────────────────────────────
#
# Each check function takes a ParseFacts and returns evidence (str)
# if the condition is found, or None.  The _check_ast_rules loop
# selects which check to run based on knowledge.json "ast_check".

def _ast_select_star(facts: ParseFacts) -> str | None:
    """Return evidence if any bare SELECT * was observed."""
    for ss in facts.select_stars:
        return ss.text
    return None


def _ast_to_date_no_format(facts: ParseFacts) -> str | None:
    """Return evidence if any TO_DATE call has exactly 1 argument."""
    for fc in facts.function_calls:
        if fc.name == 'TO_DATE' and fc.arg_count == 1:
            return fc.text
    return None


_AST_CHECKS: dict[str, callable] = {
    "select_star": _ast_select_star,
    "to_date_no_format": _ast_to_date_no_format,
}


# ── Comment / string stripping ───────────────────────────────────────

def _strip_comments_and_strings(code: str) -> str:
    """Remove SQL comments and string literals so regex checks
    don't match text inside comments or quoted strings.

    Handles:
      - Single-line comments: -- ...
      - Multi-line comments: /* ... */
      - String literals: '...' (including escaped '')
    """
    result = []
    i = 0
    while i < len(code):
        # Single-line comment
        if code[i:i+2] == "--":
            end = code.find("\n", i)
            i = end + 1 if end != -1 else len(code)
        # Multi-line comment
        elif code[i:i+2] == "/*":
            end = code.find("*/", i + 2)
            i = end + 2 if end != -1 else len(code)
        # String literal
        elif code[i] == "'":
            i += 1
            while i < len(code):
                if code[i] == "'" and i + 1 < len(code) and code[i+1] == "'":
                    i += 2  # escaped quote
                elif code[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
        else:
            result.append(code[i])
            i += 1
    return "".join(result)


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class StaticFinding:
    """A single deterministic finding from static analysis."""
    rule_id: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    finding_type: Literal[
        "syntax_error", "desupported_feature", "deprecated_feature",
        "schema_mismatch", "anti_pattern", "parse_warning"
    ]
    description: str
    evidence: str
    source: str = "static_analysis"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResult:
    """Aggregate result from all static analysis passes."""
    parse_success: bool = True
    parse_errors: list[str] = field(default_factory=list)
    findings: list[StaticFinding] = field(default_factory=list)
    statements_parsed: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


class StaticAnalyzer:
    """Runs Oracle-aware deterministic checks on SQL/PL-SQL code."""

    def __init__(self, known_tables: dict[str, set[str]] | None = None,
                 catalogue_owner: str = ""):
        self.known_tables: dict[str, set[str]] = known_tables or {}
        self.catalogue_owner: str = catalogue_owner.upper()

    def analyze(self, code: str) -> AnalysisResult:
        """Run all analysis passes and return aggregated results."""
        result = AnalysisResult()

        # Pass 1: Text-based rules (identifier, literal, regex)
        stripped = _strip_comments_and_strings(code)
        self._check_text_rules(stripped, result)

        # Pass 2: ANTLR parse → structural facts
        facts = parse_and_extract(code)

        if facts.parse_errors and facts.is_plsql:
            result.findings.append(StaticFinding(
                rule_id="PARSE-001",
                severity="info",
                finding_type="parse_warning",
                description=(
                    "PL/SQL parse completed with errors. "
                    "Analysis may be incomplete."
                ),
                evidence=facts.parse_errors[0]["message"],
            ))

        # Pass 3: AST-based rules from knowledge.json (against ANTLR facts)
        self._check_ast_rules(facts, result)

        # Pass 4: Schema-aware checks (SQLGlot on extracted DML)
        statements = self._parse_extracted_sql(facts, code, result)
        if statements and self.known_tables:
            for stmt in statements:
                self._validate_schema(stmt, result)

        return result

    def _parse_extracted_sql(
        self,
        facts: ParseFacts,
        original_code: str,
        result: AnalysisResult,
    ) -> list:
        """Parse extracted SQL with SQLGlot for schema validation.

        If ANTLR found PL/SQL, parse each extracted DML fragment.
        Otherwise, parse the original code directly with SQLGlot.
        """
        statements = []

        if facts.sql_statements:
            # Parse each extracted DML fragment
            for extracted in facts.sql_statements:
                try:
                    parsed = sqlglot.parse(extracted.text, dialect="oracle")
                    statements.extend(s for s in parsed if s is not None)
                except (ParseError, TokenError):
                    logger.debug(
                        "SQLGlot could not parse extracted %s at line %d",
                        extracted.rule, extracted.start_line,
                    )
        elif not facts.is_plsql:
            # Not PL/SQL and no DML extracted — try direct SQLGlot parse
            try:
                parsed = sqlglot.parse(original_code, dialect="oracle")
                statements = [s for s in parsed if s is not None]
            except (ParseError, TokenError):
                result.parse_success = False

        result.statements_parsed = len(statements)
        return statements

    def _check_text_rules(self, stripped_code: str, result: AnalysisResult) -> None:
        """Run identifier, literal, and regex matchers from knowledge.json."""
        for rule in _STATIC_RULES:
            matcher = rule["matcher"]
            if matcher == "ast":
                continue  # handled in _check_ast_rules

            evidence = None
            if matcher == "identifier":
                evidence = _match_identifier(stripped_code, rule["patterns"])
            elif matcher == "literal":
                evidence = _match_literal(stripped_code, rule["patterns"])
            elif matcher == "regex":
                evidence = _match_regex(stripped_code, rule["patterns"])

            if evidence:
                result.findings.append(StaticFinding(
                    rule_id=rule["id"],
                    severity=rule["severity"],
                    finding_type=rule["finding_type"],
                    description=rule["rule"],
                    evidence=evidence,
                ))

    def _check_ast_rules(self, facts: ParseFacts, result: AnalysisResult) -> None:
        """Run AST-based checks from knowledge.json against ANTLR facts."""
        for rule in _STATIC_RULES:
            if rule["matcher"] != "ast":
                continue
            check_fn = _AST_CHECKS.get(rule["ast_check"])
            if not check_fn:
                continue
            evidence = check_fn(facts)
            if evidence:
                result.findings.append(StaticFinding(
                    rule_id=rule["id"],
                    severity=rule["severity"],
                    finding_type=rule["finding_type"],
                    description=rule["rule"],
                    evidence=evidence,
                ))

    def _validate_schema(self, stmt, result: AnalysisResult) -> None:
        """Resolve table aliases from the AST, then validate columns
        against their owning table. Only flag when we can be specific."""

        # Step 1: Build alias → table name mapping from FROM/JOIN clauses
        alias_map: dict[str, str] = {}  # alias (upper) → table name (upper)
        table_names: set[str] = set()

        for table_node in stmt.find_all(exp.Table):
            table_name = table_node.name.upper() if table_node.name else None
            if not table_name:
                continue

            # Skip tables with a foreign schema qualifier —
            # these are resolved separately via live queries
            schema_qualifier = table_node.db.upper() if table_node.db else ""
            if schema_qualifier and schema_qualifier != self.catalogue_owner:
                continue

            table_names.add(table_name)
            alias = table_node.alias
            if alias:
                alias_map[alias.upper()] = table_name
            else:
                # Unaliased table: the table name itself acts as qualifier
                alias_map[table_name] = table_name

        # Step 2: Check referenced tables exist in schema
        for table_name in table_names:
            if table_name not in self.known_tables:
                result.findings.append(StaticFinding(
                    rule_id="SCHEMA-001",
                    severity="medium",
                    finding_type="schema_mismatch",
                    description=(
                        f"Table '{table_name}' not found in supplied schema. "
                        f"Known tables: {', '.join(sorted(self.known_tables))}."
                    ),
                    evidence=table_name,
                ))

        # Step 3: Check columns — only qualified columns (e.g. e.SALARY)
        # Unqualified columns are ambiguous; leave those for the LLM.
        for col_node in stmt.find_all(exp.Column):
            col_name = col_node.name.upper() if col_node.name else None
            if not col_name:
                continue

            # Only check columns with an explicit qualifier (e.g., e.SALARY)
            qualifier = col_node.table.upper() if col_node.table else None
            if not qualifier:
                continue

            # Resolve qualifier → table name
            resolved_table = alias_map.get(qualifier)
            if resolved_table is None:
                # Qualifier doesn't match any known alias or table
                result.findings.append(StaticFinding(
                    rule_id="SCHEMA-002",
                    severity="medium",
                    finding_type="schema_mismatch",
                    description=(
                        f"Qualifier '{qualifier}' does not match any table or "
                        f"alias in this statement."
                    ),
                    evidence=f"{qualifier}.{col_name}",
                ))
                continue

            # We know the table — check if the column exists
            known_cols = self.known_tables.get(resolved_table)
            if known_cols is not None and col_name not in known_cols:
                result.findings.append(StaticFinding(
                    rule_id="SCHEMA-003",
                    severity="medium",
                    finding_type="schema_mismatch",
                    description=(
                        f"Column '{col_name}' not found in table "
                        f"'{resolved_table}'. "
                        f"Known columns: {', '.join(sorted(known_cols))}."
                    ),
                    evidence=f"{qualifier}.{col_name}",
                ))


def run_static_analysis(
    code: str,
    known_tables: dict[str, set[str]] | None = None,
    catalogue_owner: str = "",
) -> dict:
    """Entry point: run all static checks and return a serializable dict."""
    analyzer = StaticAnalyzer(
        known_tables=known_tables,
        catalogue_owner=catalogue_owner,
    )
    return analyzer.analyze(code).to_dict()


def extract_table_refs(code: str) -> list[tuple[str, str]]:
    """Extract table references from SQL as (schema_qualifier, table_name) tuples.

    schema_qualifier is an empty string for unqualified references.
    For example, 'SELECT * FROM HR.EMPLOYEE e JOIN DEPARTMENT d'
    returns [('', 'DEPARTMENT'), ('HR', 'EMPLOYEE')].

    Uses ANTLR to extract DML from PL/SQL, then SQLGlot for table refs.
    Returns empty list if the code cannot be parsed.
    """
    refs: set[tuple[str, str]] = set()
    facts = parse_and_extract(code)

    sql_sources: list[str] = []
    if facts.sql_statements:
        sql_sources = [s.text for s in facts.sql_statements]
    elif not facts.is_plsql:
        sql_sources = [code]

    for sql in sql_sources:
        try:
            for stmt in sqlglot.parse(sql, dialect="oracle"):
                if stmt is None:
                    continue
                for table in stmt.find_all(exp.Table):
                    if table.name:
                        schema = table.db.upper() if table.db else ""
                        refs.add((schema, table.name.upper()))
        except (ParseError, TokenError):
            pass
    return sorted(refs)
