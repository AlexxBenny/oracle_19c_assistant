import * as vscode from "vscode";

// ── Types matching the backend AssistResponse ────────────────────────

interface StaticFinding {
  rule_id: string;
  severity: string;
  finding_type: string;
  description: string;
  evidence: string;
}

interface LlmFinding {
  severity: string;
  finding_type: string;
  description: string;
  evidence: string;
  evidence_source: string;
  knowledge_id: string;
}

interface AssistResponse {
  task: string;
  summary: string;
  static_findings: StaticFinding[];
  llm_findings: LlmFinding[];
  proposed_code: string;
  explanation: string;
  warnings: string[];
  validation_status: string;
  schema_source: string;
}

// ── Captured selection context ──────────────────────────────────────
// Stored at analysis time so the Apply Fix action works even if the
// user moves the cursor or switches files before clicking the button.

interface AnalysisOrigin {
  documentUri: vscode.Uri;
  selectionRange: vscode.Range;
  originalCode: string;
  documentVersion: number;
  wasFullFile: boolean;
}

let lastOrigin: AnalysisOrigin | undefined;

// ── Surrounding code collection ─────────────────────────────────────

const SURROUNDING_LINES = 60; // lines above/below selection to include

/**
 * Get the enclosing scope around the selection.
 *
 * Strategy: walk upward from the selection start to find the nearest
 * CREATE OR REPLACE / CREATE / DECLARE / BEGIN boundary.  Walk downward
 * to find the matching END.  If no scope boundary is found, fall back
 * to a fixed window of lines around the selection.
 */
function getSurroundingCode(
  doc: vscode.TextDocument,
  selection: vscode.Selection
): string {
  if (selection.isEmpty) {
    // Entire file selected — no surrounding context needed
    return "";
  }

  const totalLines = doc.lineCount;
  const selStart = selection.start.line;
  const selEnd = selection.end.line;

  // Try to find enclosing scope boundary above selection
  const scopePatterns =
    /^\s*(CREATE\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b|DECLARE\b|BEGIN\b)/i;

  let scopeStart = selStart;
  for (let i = selStart - 1; i >= Math.max(0, selStart - SURROUNDING_LINES); i--) {
    if (scopePatterns.test(doc.lineAt(i).text)) {
      scopeStart = i;
      break;
    }
  }

  // Walk downward to find END or use fixed window
  const endPattern = /^\s*END\s*[;]/i;
  let scopeEnd = selEnd;
  for (let i = selEnd + 1; i < Math.min(totalLines, selEnd + SURROUNDING_LINES); i++) {
    if (endPattern.test(doc.lineAt(i).text)) {
      scopeEnd = i;
      break;
    }
  }

  // Fall back to fixed window if scope detection didn't expand
  if (scopeStart === selStart && scopeEnd === selEnd) {
    scopeStart = Math.max(0, selStart - SURROUNDING_LINES);
    scopeEnd = Math.min(totalLines - 1, selEnd + SURROUNDING_LINES);
  }

  // Don't include the selected lines themselves — they're sent as `code`
  const above = scopeStart < selStart
    ? doc.getText(new vscode.Range(scopeStart, 0, selStart, 0))
    : "";
  const below = scopeEnd > selEnd
    ? doc.getText(new vscode.Range(selEnd + 1, 0, scopeEnd + 1, 0))
    : "";

  const surrounding = (above + below).trim();
  return surrounding.substring(0, 8000); // Budget: ~8k chars
}

// ── Extension lifecycle ─────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "oracle19c.analyzeCode",
      () => runAnalysis(context)
    ),
    vscode.commands.registerCommand(
      "oracle19c.refreshCatalogue",
      () => refreshCatalogue()
    )
  );
}

export function deactivate() {}

// ── Main flow ───────────────────────────────────────────────────────

async function runAnalysis(context: vscode.ExtensionContext) {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("Open a SQL/PL-SQL file first.");
    return;
  }

  const doc = editor.document;
  const selection = editor.selection;
  const wasFullFile = selection.isEmpty;
  const code = wasFullFile ? doc.getText() : doc.getText(selection);

  if (!code.trim()) {
    vscode.window.showWarningMessage("No code to analyze.");
    return;
  }

  // ── Capture the original selection NOW ────────────────────────
  // The user may move the cursor or switch files before clicking
  // "Apply Fix", so we snapshot everything we need.
  const selectionRange = wasFullFile
    ? new vscode.Range(
        new vscode.Position(0, 0),
        doc.lineAt(doc.lineCount - 1).range.end
      )
    : new vscode.Range(selection.start, selection.end);

  lastOrigin = {
    documentUri: doc.uri,
    selectionRange,
    originalCode: code,
    documentVersion: doc.version,
    wasFullFile,
  };

  const instruction = await vscode.window.showInputBox({
    prompt: "What should the assistant do?",
    placeHolder:
      "e.g. Review for 19c compatibility, explain this procedure, fix ORA-00904...",
    ignoreFocusOut: true,
  });
  if (!instruction) {
    return;
  }

  // Collect surrounding code for LLM context
  const surroundingCode = getSurroundingCode(doc, selection);

  const config = vscode.workspace.getConfiguration("oracle19c");
  const backendUrl = config.get<string>("backendUrl", "http://127.0.0.1:8000");
  const oracleVersion = config.get<string>("oracleVersion", "19c");
  const oracleOwner = config.get<string>("oracleOwner", "");

  const panel = createResultPanel(context);
  panel.webview.html = getLoadingHtml();

  try {
    const response = await callBackend(backendUrl, {
      instruction,
      code,
      oracle_version: oracleVersion,
      surrounding_code: surroundingCode,
      oracle_owner: oracleOwner,
    });
    panel.webview.html = getResultHtml(response);
  } catch (err: any) {
    panel.webview.html = getErrorHtml(err.message || String(err));
  }
}

// ── Apply Fix handler ───────────────────────────────────────────────

async function applyFix(proposedCode: string): Promise<void> {
  if (!lastOrigin) {
    vscode.window.showWarningMessage("No analysis origin found. Re-run the analysis.");
    return;
  }

  const { documentUri, selectionRange, originalCode, documentVersion } = lastOrigin;

  // Open the document (it may no longer be the active editor)
  const doc = await vscode.workspace.openTextDocument(documentUri);

  // Safety check: has the document changed since we analyzed it?
  if (doc.version !== documentVersion) {
    // The file was edited after analysis — find the original code by content match
    const currentText = doc.getText();
    const idx = currentText.indexOf(originalCode);
    if (idx === -1) {
      vscode.window.showWarningMessage(
        "The file has changed and the original code can no longer be found. " +
        "Re-run the analysis on the current code."
      );
      return;
    }
    // Recalculate the range from the content match
    const startPos = doc.positionAt(idx);
    const endPos = doc.positionAt(idx + originalCode.length);
    const edit = new vscode.WorkspaceEdit();
    edit.replace(documentUri, new vscode.Range(startPos, endPos), proposedCode);
    await vscode.workspace.applyEdit(edit);
  } else {
    // Document unchanged — use the exact saved range
    const edit = new vscode.WorkspaceEdit();
    edit.replace(documentUri, selectionRange, proposedCode);
    await vscode.workspace.applyEdit(edit);
  }

  // Show the document with the applied fix
  const editor = await vscode.window.showTextDocument(doc, vscode.ViewColumn.One);

  // Highlight the replaced region briefly
  const newEnd = doc.positionAt(
    doc.offsetAt(selectionRange.start) + proposedCode.length
  );
  editor.selection = new vscode.Selection(selectionRange.start, newEnd);
  editor.revealRange(new vscode.Range(selectionRange.start, newEnd));

  vscode.window.showInformationMessage("Fix applied.");
}

// ── Refresh schema catalogue ────────────────────────────────────────

async function refreshCatalogue(): Promise<void> {
  const config = vscode.workspace.getConfiguration("oracle19c");
  const backendUrl = config.get<string>("backendUrl", "http://127.0.0.1:8000");

  try {
    const resp = await fetch(`${backendUrl}/refresh-catalogue`, {
      method: "POST",
    });
    if (!resp.ok) {
      const text = await resp.text();
      vscode.window.showErrorMessage(`Catalogue refresh failed: ${text}`);
      return;
    }
    const data = (await resp.json()) as { message: string; objects: number };
    vscode.window.showInformationMessage(data.message);
  } catch (err: any) {
    vscode.window.showErrorMessage(
      `Could not reach backend: ${err.message || String(err)}`
    );
  }
}

// ── Backend call ────────────────────────────────────────────────────

async function callBackend(
  baseUrl: string,
  body: {
    instruction: string;
    code: string;
    oracle_version: string;
    surrounding_code: string;
    oracle_owner: string;
  }
): Promise<AssistResponse> {
  const url = `${baseUrl}/assist`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Backend returned ${resp.status}: ${text}`);
  }

  return (await resp.json()) as AssistResponse;
}

// ── Webview panel ───────────────────────────────────────────────────

let currentPanel: vscode.WebviewPanel | undefined;

function createResultPanel(
  context: vscode.ExtensionContext
): vscode.WebviewPanel {
  if (currentPanel) {
    currentPanel.reveal(vscode.ViewColumn.Beside);
    return currentPanel;
  }

  currentPanel = vscode.window.createWebviewPanel(
    "oracle19cResults",
    "Oracle 19c Analysis",
    vscode.ViewColumn.Beside,
    { enableScripts: true }  // Required for Apply Fix button
  );

  // Listen for messages from the webview (Apply Fix button)
  currentPanel.webview.onDidReceiveMessage(async (msg) => {
    if (msg.command === "applyFix" && msg.code) {
      await applyFix(msg.code);
    }
  });

  currentPanel.onDidDispose(() => {
    currentPanel = undefined;
  });

  return currentPanel;
}

// ── HTML helpers ────────────────────────────────────────────────────

function esc(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function severityLabel(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function findingTypeLabel(t: string): string {
  const labels: Record<string, string> = {
    syntax_error: "Syntax Error",
    desupported_feature: "Desupported Feature",
    deprecated_feature: "Deprecated Feature",
    schema_mismatch: "Schema Mismatch",
    anti_pattern: "Anti-pattern",
    parse_warning: "Parser Limitation",
    confirmed_issue: "Confirmed Issue",
    potential_risk: "Potential Risk",
    best_practice: "Best Practice",
    insufficient_evidence: "Needs Investigation",
  };
  return labels[t] || t;
}

// ── Render findings as a unified list ───────────────────────────────

interface UnifiedFinding {
  severity: string;
  type_label: string;
  description: string;
  evidence: string;
  rule_id: string;
}

function buildUnifiedFindings(r: AssistResponse): UnifiedFinding[] {
  const findings: UnifiedFinding[] = [];

  for (const f of r.static_findings) {
    findings.push({
      severity: f.severity,
      type_label: findingTypeLabel(f.finding_type),
      description: f.description,
      evidence: f.evidence,
      rule_id: f.rule_id,
    });
  }

  for (const f of r.llm_findings) {
    findings.push({
      severity: f.severity,
      type_label: findingTypeLabel(f.finding_type),
      description: f.description,
      evidence: f.evidence,
      rule_id: f.knowledge_id || "",
    });
  }

  return findings;
}

function renderFindings(findings: UnifiedFinding[]): string {
  if (!findings.length) {
    return `<div class="no-issues">No issues found.</div>`;
  }

  const countText =
    findings.length === 1 ? "1 issue" : `${findings.length} issues`;

  const rows = findings
    .map(
      (f) => `
    <div class="finding">
      <div class="finding-header">
        <strong>${esc(severityLabel(f.severity))}</strong>
        <span class="finding-type">${esc(f.type_label)}</span>
        ${f.rule_id ? `<code class="rule-id">${esc(f.rule_id)}</code>` : ""}
      </div>
      <div class="finding-desc">${esc(f.description)}</div>
      ${f.evidence ? `<code class="finding-evidence">${esc(f.evidence)}</code>` : ""}
    </div>`
    )
    .join("");

  return `
    <div class="section-header">${countText}</div>
    ${rows}`;
}

// ── Page templates ──────────────────────────────────────────────────

const STYLES = `
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Helvetica Neue', sans-serif;
    padding: 20px 24px;
    color: #d4d4d4;
    background: #1e1e1e;
    line-height: 1.6;
    font-size: 13px;
  }

  h1 {
    font-size: 15px;
    font-weight: 600;
    color: #e0e0e0;
    margin: 0 0 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid #333;
    letter-spacing: 0.3px;
  }

  .summary {
    color: #b0b0b0;
    margin-bottom: 20px;
    font-size: 13px;
  }

  .section-header {
    font-size: 12px;
    font-weight: 600;
    color: #999;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin: 20px 0 10px;
    padding-bottom: 6px;
    border-bottom: 1px solid #2a2a2a;
  }

  .finding {
    padding: 10px 12px;
    margin: 8px 0;
    border-left: 2px solid #555;
    background: #252526;
  }

  .finding-header {
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .finding-header strong {
    color: #e0e0e0;
    font-size: 12px;
  }

  .finding-type {
    color: #888;
    font-size: 11px;
    font-style: italic;
  }

  .rule-id {
    color: #666;
    font-size: 10px;
    margin-left: auto;
    font-family: 'SF Mono', Consolas, 'Courier New', monospace;
  }

  .finding-desc {
    color: #ccc;
    font-size: 13px;
  }

  .finding-evidence {
    display: block;
    margin-top: 6px;
    color: #999;
    font-size: 12px;
    font-family: 'SF Mono', Consolas, 'Courier New', monospace;
  }

  .no-issues {
    color: #888;
    font-style: italic;
    padding: 12px 0;
  }

  pre {
    background: #1a1a1a;
    border: 1px solid #333;
    padding: 12px;
    overflow-x: auto;
    font-size: 12px;
    color: #d4d4d4;
    font-family: 'SF Mono', Consolas, 'Courier New', monospace;
    line-height: 1.5;
  }

  .explanation {
    color: #b0b0b0;
    font-size: 13px;
    margin-top: 4px;
  }

  .warning-item {
    color: #b0b0b0;
    font-size: 12px;
    padding: 4px 0;
    font-style: italic;
  }

  .center-message {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 80vh;
    color: #888;
    font-size: 14px;
  }

  .spinner {
    width: 20px;
    height: 20px;
    border: 2px solid #333;
    border-top-color: #888;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-right: 10px;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  .apply-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-top: 10px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: #fff;
    background: #0e639c;
    border: none;
    border-radius: 3px;
    cursor: pointer;
    font-family: inherit;
    letter-spacing: 0.3px;
  }

  .apply-btn:hover {
    background: #1177bb;
  }

  .apply-btn:active {
    background: #0d5689;
  }

  .apply-btn.applied {
    background: #2d6b3f;
    cursor: default;
  }
`;

function schemaSourceBadge(source: string): string {
  if (source === "live_oracle_db") {
    return `<span style="display:inline-block;font-size:11px;padding:2px 8px;border:1px solid #4a4;
            color:#8c8;border-radius:3px;margin-left:8px;letter-spacing:0.3px;">
            Schema: Live Oracle DB</span>`;
  }
  return `<span style="display:inline-block;font-size:11px;padding:2px 8px;border:1px solid #444;
          color:#888;border-radius:3px;margin-left:8px;letter-spacing:0.3px;">
          Schema: Not available</span>`;
}

function getResultHtml(r: AssistResponse): string {
  const findings = buildUnifiedFindings(r);

  const warningsHtml =
    r.warnings.length > 0
      ? `<div class="section-header">Warnings</div>
         ${r.warnings.map((w) => `<div class="warning-item">${esc(w)}</div>`).join("")}`
      : "";

  // Proposed code section with Apply Fix button
  const hasProposedCode = r.proposed_code && r.proposed_code.trim();
  const proposedHtml = hasProposedCode
    ? `<div class="section-header">Proposed Code</div>
       <pre>${esc(r.proposed_code)}</pre>
       <button class="apply-btn" id="applyBtn" onclick="handleApply()">
         &#9654; Apply Fix
       </button>`
    : "";

  const explanationHtml =
    r.explanation && r.explanation.trim()
      ? `<div class="section-header">Explanation</div>
         <div class="explanation">${esc(r.explanation)}</div>`
      : "";

  // Embed the proposed code as a data attribute for the JS to read
  const proposedCodeData = hasProposedCode
    ? `<script>
        const vscode = acquireVsCodeApi();
        const proposedCode = ${JSON.stringify(r.proposed_code)};

        function handleApply() {
          const btn = document.getElementById('applyBtn');
          btn.disabled = true;
          btn.classList.add('applied');
          btn.innerHTML = '&#10003; Applied';
          vscode.postMessage({ command: 'applyFix', code: proposedCode });
        }
      </script>`
    : "";

  return `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><style>${STYLES}</style></head>
<body>
  <h1>Oracle 19c Analysis ${schemaSourceBadge(r.schema_source)}</h1>
  <div class="summary">${esc(r.summary)}</div>
  ${renderFindings(findings)}
  ${warningsHtml}
  ${proposedHtml}
  ${explanationHtml}
  ${proposedCodeData}
</body>
</html>`;
}

function getLoadingHtml(): string {
  return `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><style>${STYLES}</style></head>
<body>
  <div class="center-message">
    <div class="spinner"></div>
    Analyzing...
  </div>
</body>
</html>`;
}

function getErrorHtml(message: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><style>${STYLES}</style></head>
<body>
  <h1>Analysis Failed</h1>
  <div class="summary">Could not reach the backend. Make sure it is running:</div>
  <pre>cd backend &amp;&amp; uvicorn main:app --reload</pre>
  <div class="section-header">Error</div>
  <pre>${esc(message)}</pre>
</body>
</html>`;
}
