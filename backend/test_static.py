"""Smoke tests for knowledge-driven static analysis."""
from static_analysis import run_static_analysis, extract_table_refs

# ── Identifier matcher tests ─────────────────────────────────────────

# Test 1: Should NOT flag DBMS_STREAMS inside a comment
code_comment = """\
-- TODO: Replace DBMS_STREAMS in old documentation
SELECT emp_name FROM employee;
"""
result = run_static_analysis(code_comment)
comment_findings = [f for f in result["findings"] if "STREAMS" in f.get("evidence", "")]
assert not comment_findings, f"False positive: flagged DBMS_STREAMS inside comment: {comment_findings}"
print("PASS: comment-only DBMS_STREAMS not flagged")

# Test 2: Should NOT flag DBMS_STREAMS inside a string literal
code_string = """\
SELECT 'Use DBMS_STREAMS for replication' AS hint FROM dual;
"""
result = run_static_analysis(code_string)
string_findings = [f for f in result["findings"] if "STREAMS" in f.get("evidence", "")]
assert not string_findings, f"False positive: flagged DBMS_STREAMS inside string: {string_findings}"
print("PASS: string-literal DBMS_STREAMS not flagged")

# Test 3: SHOULD flag DBMS_STREAMS in actual code
code_real = """\
BEGIN
    DBMS_STREAMS.SET_TAG(tag => HEXTORAW('1'));
END;
"""
result = run_static_analysis(code_real)
real_findings = [f for f in result["findings"] if "DBMS_STREAMS" in f.get("evidence", "")]
assert real_findings, "False negative: DBMS_STREAMS in real code was not flagged"
assert real_findings[0]["rule_id"] == "ORA19-DESUP-001"
print("PASS: real DBMS_STREAMS flagged with correct rule_id")

# Test 4: DBMS_XMLQUERY detection (new rule from knowledge.json)
code_xml = "SELECT DBMS_XMLQUERY.GETXML('SELECT * FROM emp') FROM dual;"
result = run_static_analysis(code_xml)
xml_findings = [f for f in result["findings"] if "DBMS_XMLQUERY" in f.get("evidence", "")]
assert xml_findings, "False negative: DBMS_XMLQUERY not flagged"
assert xml_findings[0]["rule_id"] == "ORA19-DEPR-002"
print("PASS: DBMS_XMLQUERY flagged with correct rule_id")

# ── AST matcher tests ────────────────────────────────────────────────

# Test 5: TO_DATE with ONE arg — should be flagged
code_todate_bad = "SELECT TO_DATE('01-02-2026') FROM DUAL"
result = run_static_analysis(code_todate_bad)
todate_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-001"]
assert todate_findings, "False negative: TO_DATE without format mask not flagged"
print("PASS: TO_DATE without format mask flagged")

# Test 6: TO_DATE with TWO args — should NOT be flagged
code_todate_good = "SELECT TO_DATE('01-02-2026', 'DD-MM-YYYY') FROM DUAL"
result = run_static_analysis(code_todate_good)
todate_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-001"]
assert not todate_findings, f"False positive: TO_DATE with format mask flagged: {todate_findings}"
print("PASS: TO_DATE with format mask not flagged")

# Test 7: SELECT * — should be flagged
code_star = "SELECT * FROM EMPLOYEES"
result = run_static_analysis(code_star)
star_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-003"]
assert star_findings, "False negative: SELECT * not flagged"
print("PASS: SELECT * flagged")

# Test 8: SELECT col1, col2 — should NOT be flagged
code_cols = "SELECT col1, col2 FROM EMPLOYEES"
result = run_static_analysis(code_cols)
star_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-003"]
assert not star_findings, f"False positive: explicit columns flagged: {star_findings}"
print("PASS: explicit column list not flagged")

# Test 9: COUNT(*) — should NOT be flagged as SELECT *
code_count = "SELECT COUNT(*) FROM EMPLOYEES"
result = run_static_analysis(code_count)
star_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-003"]
assert not star_findings, f"False positive: COUNT(*) flagged as SELECT *: {star_findings}"
print("PASS: COUNT(*) not flagged as SELECT *")

# ── Literal matcher tests ────────────────────────────────────────────

# Test 10: WHEN OTHERS THEN NULL — should be flagged
code_when = """\
BEGIN
    NULL;
EXCEPTION
    WHEN OTHERS THEN
        NULL;
END;
"""
result = run_static_analysis(code_when)
when_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-PLSQL-001"]
assert when_findings, "False negative: WHEN OTHERS THEN NULL not flagged"
print("PASS: WHEN OTHERS THEN NULL flagged")

# Test 11: WHEN OTHERS with RAISE — should NOT be flagged
code_when_raise = """\
BEGIN
    NULL;
EXCEPTION
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE(SQLERRM);
        RAISE;
END;
"""
result = run_static_analysis(code_when_raise)
when_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-PLSQL-001"]
assert not when_findings, f"False positive: WHEN OTHERS with RAISE flagged: {when_findings}"
print("PASS: WHEN OTHERS with RAISE not flagged")

# ── Schema validation tests ──────────────────────────────────────────

# Test 12: Schema validation — qualified column on correct table
known_tables = {
    "EMPLOYEES": {"EMPLOYEE_ID", "FIRST_NAME", "DEPARTMENT_ID"},
    "DEPARTMENTS": {"DEPARTMENT_ID", "DEPARTMENT_NAME"},
}
code_valid = "SELECT e.EMPLOYEE_ID, d.DEPARTMENT_NAME FROM EMPLOYEES e JOIN DEPARTMENTS d ON e.DEPARTMENT_ID = d.DEPARTMENT_ID"
result = run_static_analysis(code_valid, known_tables=known_tables, catalogue_owner="ORACLE_DEV")
schema_findings = [f for f in result["findings"] if f["finding_type"] == "schema_mismatch"]
assert not schema_findings, f"False positive on valid query: {schema_findings}"
print("PASS: valid qualified columns not flagged")

# Test 13: Schema validation — wrong column
code_bad_col = "SELECT e.SALARY FROM EMPLOYEES e"
result = run_static_analysis(code_bad_col, known_tables=known_tables, catalogue_owner="ORACLE_DEV")
schema_findings = [f for f in result["findings"] if f["rule_id"] == "SCHEMA-003"]
assert schema_findings, "False negative: SALARY not in EMPLOYEES should be flagged"
print("PASS: e.SALARY correctly flagged as not in EMPLOYEES")

# Test 14: Foreign-schema table should NOT be validated
code_foreign = "SELECT emp_name FROM HR.EMPLOYEE"
result = run_static_analysis(code_foreign, known_tables=known_tables, catalogue_owner="ORACLE_DEV")
schema_findings = [f for f in result["findings"] if f["finding_type"] == "schema_mismatch"]
assert not schema_findings, f"False positive: HR.EMPLOYEE should be skipped: {schema_findings}"
print("PASS: foreign-schema HR.EMPLOYEE not validated against ORACLE_DEV catalogue")

# ── extract_table_refs tests ─────────────────────────────────────────

# Test 15: Unqualified tables
refs = extract_table_refs("SELECT e.NAME FROM EMPLOYEES e JOIN DEPARTMENTS d ON e.DEPT_ID = d.DEPT_ID")
assert ("", "EMPLOYEES") in refs
assert ("", "DEPARTMENTS") in refs
print("PASS: extract_table_refs finds unqualified tables")

# Test 16: Schema-qualified table
refs = extract_table_refs("SELECT emp_name FROM HR.EMPLOYEE")
assert ("HR", "EMPLOYEE") in refs
print("PASS: extract_table_refs preserves schema qualifier")

# ── PL/SQL parsing tests (ANTLR4 integration) ───────────────────────

# Test 17: TO_DATE in PL/SQL assignment — previously missed entirely
code_plsql_todate = """\
BEGIN
    v_date := TO_DATE('2026-11-01');
END;
/
"""
result = run_static_analysis(code_plsql_todate)
todate_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-001"]
assert todate_findings, "False negative: TO_DATE in PL/SQL assignment not flagged"
print("PASS: TO_DATE in PL/SQL assignment flagged")

# Test 18: TO_DATE with format mask in PL/SQL — should NOT be flagged
code_plsql_todate_ok = """\
BEGIN
    v_date := TO_DATE('2026-11-01', 'YYYY-MM-DD');
END;
/
"""
result = run_static_analysis(code_plsql_todate_ok)
todate_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-001"]
assert not todate_findings, f"False positive: TO_DATE with format in PL/SQL: {todate_findings}"
print("PASS: TO_DATE with format in PL/SQL not flagged")

# Test 19: SELECT * inside INSERT in procedure — previously missed
code_proc_select_star = """\
CREATE OR REPLACE PROCEDURE archive_employees AS
BEGIN
    INSERT INTO employee_archive
    SELECT *
    FROM EMPLOYEES;
END;
/
"""
result = run_static_analysis(code_proc_select_star)
star_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-003"]
assert star_findings, "False negative: SELECT * in PL/SQL procedure not flagged"
print("PASS: SELECT * inside PL/SQL procedure flagged")

# Test 20: Schema validation through PL/SQL procedure
known_tables = {
    "EMPLOYEES": {"EMPLOYEE_ID", "FIRST_NAME", "DEPARTMENT_ID"},
}
code_proc_schema = """\
CREATE OR REPLACE PROCEDURE bad_query AS
BEGIN
    SELECT e.SALARY FROM EMPLOYEES e;
END;
/
"""
result = run_static_analysis(code_proc_schema, known_tables=known_tables, catalogue_owner="ORACLE_DEV")
schema_findings = [f for f in result["findings"] if f["rule_id"] == "SCHEMA-003"]
assert schema_findings, "False negative: e.SALARY in PL/SQL not caught by schema validation"
print("PASS: schema validation works through PL/SQL")

# Test 21: extract_table_refs from PL/SQL procedure
code_proc_refs = """\
CREATE OR REPLACE PROCEDURE process_employee AS
BEGIN
    INSERT INTO employee_archive
    SELECT *
    FROM ORACLE_DEV.EMPLOYEE;
END;
/
"""
refs = extract_table_refs(code_proc_refs)
assert ("ORACLE_DEV", "EMPLOYEE") in refs, f"Table refs from PL/SQL: {refs}"
assert ("", "EMPLOYEE_ARCHIVE") in refs, f"Table refs from PL/SQL: {refs}"
print("PASS: extract_table_refs works through PL/SQL")

# Test 22: Full process_employee procedure — the original failing case
code_full = """\
CREATE OR REPLACE PROCEDURE process_employee AS
    v_date DATE;
BEGIN
    v_date := TO_DATE('2026-11-01');

    DBMS_JOB.SUBMIT(
        job       => 100,
        what      => 'BEGIN NULL; END;',
        next_date => SYSDATE,
        interval  => NULL
    );

    INSERT INTO employee_archive
    SELECT *
    FROM ORACLE_DEV.EMPLOYEE;

EXCEPTION
    WHEN OTHERS THEN
        NULL;
END;
/
"""
result = run_static_analysis(code_full)
rule_ids = {f["rule_id"] for f in result["findings"]}
assert "ORA19-SQL-001" in rule_ids, f"TO_DATE not flagged in full proc: {rule_ids}"
assert "ORA19-SQL-003" in rule_ids, f"SELECT * not flagged in full proc: {rule_ids}"
assert "ORA19-DEPR-001" in rule_ids, f"DBMS_JOB not flagged in full proc: {rule_ids}"
assert "ORA19-PLSQL-001" in rule_ids, f"WHEN OTHERS not flagged in full proc: {rule_ids}"
print("PASS: full process_employee — all 4 issues detected")

# Test 23: No duplicate SELECT * from nested INSERT...SELECT
star_findings = [f for f in result["findings"] if f["rule_id"] == "ORA19-SQL-003"]
assert len(star_findings) == 1, f"Duplicate SELECT * findings: {len(star_findings)}"
print("PASS: no duplicate SELECT * from INSERT...SELECT")

print("\nAll 23 tests passed")

