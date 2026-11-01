"""ANTLR4-based Oracle PL/SQL parser.

Parses complete PL/SQL units (procedures, functions, packages, triggers,
anonymous blocks) and standalone SQL.  Extracts:

  1. Structural facts (function calls, SELECT-star occurrences)
     for knowledge-driven AST checks.
  2. Outermost embedded SQL statements (for SQLGlot schema validation).

Grammar source: antlr/grammars-v4  sql/plsql  (Apache 2.0)
Pinned:  ANTLR 4.13.2
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The ANTLR4 base classes use bare imports (e.g. 'from PlSqlLexer import ...')
# which require the generated directory to be on sys.path.
_GENERATED_DIR = str(Path(__file__).resolve().parent / "plsql_grammar" / "generated")
if _GENERATED_DIR not in sys.path:
    sys.path.insert(0, _GENERATED_DIR)

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from plsql_grammar.generated.PlSqlLexer import PlSqlLexer
from plsql_grammar.generated.PlSqlParser import PlSqlParser
from plsql_grammar.generated.PlSqlParserVisitor import PlSqlParserVisitor

logger = logging.getLogger("oracle_assistant.plsql_parser")


# ── Structural facts ─────────────────────────────────────────────────

@dataclass
class FunctionCallFact:
    """A function call observed anywhere in the parse tree."""
    name: str        # upper-cased, e.g. "TO_DATE"
    arg_count: int   # number of actual arguments
    line: int        # 1-based line in source
    text: str        # original source for evidence


@dataclass
class SelectStarFact:
    """A SELECT * (bare wildcard) observed in a query block."""
    line: int
    text: str


@dataclass
class ExtractedSQL:
    """An outermost SQL DML statement extracted from PL/SQL."""
    text: str
    start_line: int
    start_col: int
    stop_line: int
    stop_col: int
    rule: str        # "select_statement", "insert_statement", etc.


@dataclass
class ParseFacts:
    """All structural information extracted from a single parse."""
    function_calls: list[FunctionCallFact] = field(default_factory=list)
    select_stars: list[SelectStarFact] = field(default_factory=list)
    sql_statements: list[ExtractedSQL] = field(default_factory=list)
    parse_errors: list[dict] = field(default_factory=list)
    is_plsql: bool = False


# ── Error listener ───────────────────────────────────────────────────

class _CollectingErrorListener(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: list[dict] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append({
            "line": line,
            "column": column,
            "message": msg,
        })


# ── Fact-extracting visitor ──────────────────────────────────────────

class _FactExtractorVisitor(PlSqlParserVisitor):
    """Walk the ANTLR parse tree and collect structural facts.

    This visitor knows nothing about rule IDs, severities, or
    knowledge.json.  It records what it observes; the static analysis
    engine evaluates those facts against configured rules.
    """

    def __init__(self, token_stream: CommonTokenStream):
        self._stream = token_stream
        self.function_calls: list[FunctionCallFact] = []
        self.select_stars: list[SelectStarFact] = []
        self.sql_statements: list[ExtractedSQL] = []
        self._inside_dml = False

    # ── Outermost DML extraction (prevents nested duplicates) ────

    def _extract_dml(self, ctx, rule: str):
        """Record a DML statement, but only if we are not already
        inside an outer DML node (avoids INSERT + nested SELECT dupes).
        Children are always visited so structural facts are collected."""
        if self._inside_dml:
            # Nested DML: don't extract, but still visit for facts
            return self.visitChildren(ctx)
        self._inside_dml = True
        if ctx.start is not None and ctx.stop is not None:
            self.sql_statements.append(ExtractedSQL(
                text=self._stream.getText(ctx.start, ctx.stop),
                start_line=ctx.start.line,
                start_col=ctx.start.column,
                stop_line=ctx.stop.line,
                stop_col=ctx.stop.column,
                rule=rule,
            ))
        self.visitChildren(ctx)          # recurse for function calls, etc.
        self._inside_dml = False

    def visitSelect_statement(self, ctx):
        self._extract_dml(ctx, "select_statement")
        return None

    def visitInsert_statement(self, ctx):
        self._extract_dml(ctx, "insert_statement")
        return None

    def visitUpdate_statement(self, ctx):
        self._extract_dml(ctx, "update_statement")
        return None

    def visitDelete_statement(self, ctx):
        self._extract_dml(ctx, "delete_statement")
        return None

    def visitMerge_statement(self, ctx):
        self._extract_dml(ctx, "merge_statement")
        return None

    # ── Function call facts ──────────────────────────────────────

    def visitString_function(self, ctx):
        """Extract function-call metadata from grammar's string_function rule.

        The TO_DATE branch of string_function looks like:

            TO_DATE '(' (table_element | standard_function | expression)
                (DEFAULT concatenation ON CONVERSION ERROR)?
                (',' quoted_string (',' quoted_string)?)?
            ')'

        Direct-child comma tokens separate actual arguments.
        Commas inside nested expressions (e.g. DECODE(x, 1, ...))
        are children of their own sub-contexts, not of this node.
        """
        first = ctx.getChild(0)
        if first is not None and hasattr(first, 'symbol'):
            name = first.getText().upper()
            if name == 'TO_DATE':
                commas = sum(
                    1 for i in range(ctx.getChildCount())
                    if hasattr(ctx.getChild(i), 'symbol')
                    and ctx.getChild(i).getText() == ','
                )
                self.function_calls.append(FunctionCallFact(
                    name=name,
                    arg_count=commas + 1,
                    line=ctx.start.line,
                    text=self._stream.getText(ctx.start, ctx.stop),
                ))
        return self.visitChildren(ctx)

    # ── SELECT * facts ───────────────────────────────────────────

    def visitSelected_list(self, ctx):
        """Detect bare SELECT * via the grammar's selected_list rule.

        Grammar:
            selected_list
                : '*'
                | select_list_elements (',' select_list_elements)*
                ;

        A bare '*' is a single terminal child.  COUNT(*) is parsed
        under select_list_elements as a function, so it never matches.
        """
        if ctx.getChildCount() == 1:
            child = ctx.getChild(0)
            if hasattr(child, 'symbol') and child.getText() == '*':
                self.select_stars.append(SelectStarFact(
                    line=ctx.start.line,
                    text="SELECT *",
                ))
        return self.visitChildren(ctx)


# ── PL/SQL detection ─────────────────────────────────────────────────

class _PlsqlDetector(PlSqlParserVisitor):
    """Quick check: does the parse tree contain any PL/SQL unit?"""

    def __init__(self):
        self.found = False

    def visitPlsql_unit(self, ctx):
        self.found = True
        return None  # stop recursing

    def visitAnonymous_block(self, ctx):
        self.found = True
        return None


# ── Public API ───────────────────────────────────────────────────────

def parse_and_extract(code: str) -> ParseFacts:
    """Parse SQL/PL-SQL code with ANTLR4 and extract structural facts.

    Returns a ParseFacts containing:
      - function_calls:  every function call with name/arg_count
      - select_stars:    every bare SELECT *
      - sql_statements:  outermost DML statements (text for SQLGlot)
      - parse_errors:    any syntax errors from the parser
      - is_plsql:        True if PL/SQL constructs were found
    """
    # Lex
    input_stream = InputStream(code)
    lexer = PlSqlLexer(input_stream)
    lexer.removeErrorListeners()
    lex_errors = _CollectingErrorListener()
    lexer.addErrorListener(lex_errors)

    # Parse
    token_stream = CommonTokenStream(lexer)
    parser = PlSqlParser(token_stream)
    parser.removeErrorListeners()
    parse_errors = _CollectingErrorListener()
    parser.addErrorListener(parse_errors)

    tree = parser.sql_script()
    all_errors = lex_errors.errors + parse_errors.errors

    # Detect PL/SQL
    detector = _PlsqlDetector()
    detector.visit(tree)

    # Extract facts
    visitor = _FactExtractorVisitor(token_stream)
    visitor.visit(tree)

    return ParseFacts(
        function_calls=visitor.function_calls,
        select_stars=visitor.select_stars,
        sql_statements=visitor.sql_statements,
        parse_errors=all_errors,
        is_plsql=detector.found,
    )
