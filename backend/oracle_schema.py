"""Oracle Schema Catalogue Service.

Connects to Oracle via python-oracledb (thin mode) and builds an
in-memory metadata catalogue of all tables, views, columns, and
synonyms for the configured schema owner.

The catalogue exists independently of any SQL file.
Selected code is matched against it.
"""

import os
import logging
import time
from typing import Optional

logger = logging.getLogger("oracle_assistant.schema")


class SchemaCatalogue:
    """In-memory catalogue of Oracle database objects.

    Built by querying ALL_TABLES, ALL_VIEWS, ALL_TAB_COLUMNS,
    and ALL_SYNONYMS for a given owner. Refreshes on a configurable TTL.

    Usage:
        catalogue = SchemaCatalogue()
        if catalogue.is_available:
            catalogue.build()
            tables = catalogue.get_known_tables()
            # {"EMPLOYEE": {"EMP_ID", "EMP_NAME", ...}, ...}
    """

    def __init__(self):
        self._tables: dict[str, list[str]] = {}   # NAME → [columns ordered by column_id]
        self._synonyms: dict[str, str] = {}        # SYNONYM_NAME → real TABLE_NAME
        self._pool = None
        self._owner: str = ""
        self._last_build: float = 0.0
        self._ttl: int = 600  # 10 minutes
        self._available: bool = False
        self._connect()

    def _connect(self) -> None:
        """Create a connection pool from environment variables."""
        try:
            import oracledb
        except ImportError:
            logger.warning("oracledb not installed — pip install oracledb")
            return

        user = os.getenv("ORACLE_USER", "")
        password = os.getenv("ORACLE_PASSWORD", "")
        dsn = os.getenv("ORACLE_DSN", "")

        if not all([user, password, dsn]):
            logger.info("ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN not set")
            return

        try:
            self._pool = oracledb.create_pool(
                user=user, password=password, dsn=dsn,
                min=1, max=3, increment=1,
            )
            self._owner = user.upper()
            self._available = True
            logger.info("Connected to Oracle: %s as %s", dsn, user)
        except Exception as exc:
            logger.warning("Oracle connection failed: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def owner(self) -> str:
        """The schema owner the catalogue was built for."""
        return self._owner

    # ── Catalogue construction ──────────────────────────────────

    def build(self, owner: str = "") -> None:
        """Query the Data Dictionary and populate the catalogue.

        Queries:
            ALL_TABLES      — table names
            ALL_VIEWS       — view names
            ALL_TAB_COLUMNS — columns for all tables and views
            ALL_SYNONYMS    — synonym → real object mapping
        """
        if not self._available:
            return

        owner = owner.upper() if owner else self._owner
        tables: dict[str, list[str]] = {}
        synonyms: dict[str, str] = {}

        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    # A. Tables and views
                    cur.execute("""
                        SELECT table_name FROM all_tables WHERE owner = :o
                        UNION
                        SELECT view_name FROM all_views WHERE owner = :o
                    """, {"o": owner})
                    for (name,) in cur:
                        tables[name] = []

                    # B. Columns (ordered by position)
                    cur.execute("""
                        SELECT table_name, column_name
                        FROM all_tab_columns
                        WHERE owner = :o
                        ORDER BY table_name, column_id
                    """, {"o": owner})
                    for tbl, col in cur:
                        if tbl in tables:
                            tables[tbl].append(col)

                    # C. Synonyms (private + public)
                    cur.execute("""
                        SELECT synonym_name, table_name
                        FROM all_synonyms
                        WHERE owner = :o OR owner = 'PUBLIC'
                    """, {"o": owner})
                    for syn, real in cur:
                        synonyms[syn] = real
                        if real in tables:
                            tables[syn] = list(tables[real])

            self._tables = tables
            self._synonyms = synonyms
            self._last_build = time.time()
            logger.info(
                "Catalogue built: %d objects, %d synonyms for %s",
                len(tables), len(synonyms), owner,
            )
        except Exception as exc:
            logger.warning("Catalogue build failed: %s", exc)

    def _refresh_if_stale(self) -> None:
        """Rebuild the catalogue if TTL has expired."""
        if self._available and time.time() - self._last_build > self._ttl:
            self.build()

    # ── Query methods ───────────────────────────────────────────

    def get_known_tables(self) -> dict[str, set[str]]:
        """All objects as {NAME: {columns}} for the static analyzer."""
        self._refresh_if_stale()
        return {name: set(cols) for name, cols in self._tables.items() if cols}

    def format_for_tables(self, names: list[str]) -> str:
        """Format schema of specific tables for the LLM prompt.

        Returns compact format:
            EMPLOYEE(EMP_ID, EMP_NAME, EMP_DEPT, SALARY)
            DEPARTMENT(DEPT_ID, DEPT_NAME)
        """
        self._refresh_if_stale()
        lines = []
        for name in sorted(set(n.upper() for n in names)):
            cols = self._tables.get(name)
            if cols is None:
                # Try synonym resolution
                real = self._synonyms.get(name)
                if real:
                    cols = self._tables.get(real)
            if cols:
                lines.append(f"{name}({', '.join(cols)})")
        return "\n".join(lines)

    def resolve_qualified_ref(self, schema: str, table_name: str) -> dict:
        """Resolve a schema-qualified reference (e.g., HR.EMPLOYEE).

        Runs live queries to distinguish four situations:
          - 'found':        the object exists and is accessible
          - 'wrong_schema': object not found under given schema,
                            but same table name exists under other owners
          - 'not_found':    object not found anywhere in accessible metadata

        Returns:
            dict with keys: referenced, status, owner_exists,
            object_exists, columns, alternatives
        """
        schema = schema.upper()
        table_name = table_name.upper()
        result: dict = {
            "referenced": f"{schema}.{table_name}",
            "status": "not_found",
            "owner_exists": False,
            "object_exists": False,
            "columns": None,
            "alternatives": [],
        }

        if not self._available:
            return result

        try:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    # 1. Does the schema/user exist?
                    cur.execute(
                        "SELECT username FROM all_users WHERE username = :u",
                        {"u": schema},
                    )
                    if cur.fetchone():
                        result["owner_exists"] = True

                    # 2. Does the specific object exist and is accessible?
                    cur.execute("""
                        SELECT owner, object_name, object_type
                        FROM all_objects
                        WHERE owner = :o AND object_name = :t
                    """, {"o": schema, "t": table_name})
                    if cur.fetchone():
                        result["object_exists"] = True
                        result["status"] = "found"
                        # Get its columns
                        cur.execute("""
                            SELECT column_name
                            FROM all_tab_columns
                            WHERE owner = :o AND table_name = :t
                            ORDER BY column_id
                        """, {"o": schema, "t": table_name})
                        result["columns"] = [r[0] for r in cur]
                        return result

                    # 3. Object not found — find same table name under other owners
                    cur.execute("""
                        SELECT DISTINCT owner
                        FROM all_tab_columns
                        WHERE table_name = :t
                        ORDER BY owner
                    """, {"t": table_name})
                    alternatives = [r[0] for r in cur]
                    if alternatives:
                        result["alternatives"] = [
                            {"owner": alt, "table_name": table_name}
                            for alt in alternatives
                        ]
                        result["status"] = "wrong_schema"
                    # else status stays "not_found"
        except Exception as exc:
            logger.warning("resolve_qualified_ref failed: %s", exc)

        return result

    def close(self) -> None:
        if self._pool:
            self._pool.close(force=True)
            self._pool = None
            self._available = False


# ── Singleton ───────────────────────────────────────────────────

_catalogue: Optional[SchemaCatalogue] = None


def get_catalogue() -> SchemaCatalogue:
    global _catalogue
    if _catalogue is None:
        _catalogue = SchemaCatalogue()
    return _catalogue


def is_schema_available() -> bool:
    return get_catalogue().is_available
