#!/usr/bin/env node
/**
 * Engram Session-Capture Hook for Cursor IDE
 *
 * Fires on `stop` (agent turn ends) or `sessionEnd` to surface memory capture
 * suggestions based on what happened during the session.
 *
 * What it does:
 *   1. Extracts the last user message and agent summary from CURSOR_AGENT_CONTEXT.
 *   2. Calls `engram suggest-capture` to get heuristic-based capture candidates.
 *   3. Prints any capture suggestions back to Cursor as a non-blocking notice.
 *
 * Installation:
 *   Copy (or symlink) this file to `~/.cursor/hooks/session-capture.js`
 *   Then add to your `~/.cursor/hooks.json`:
 *
 *     "stop": [
 *       {
 *         "command": "node ~/.cursor/hooks/session-capture.js",
 *         "timeout": 10,
 *         "failClosed": false
 *       }
 *     ]
 *
 * Configuration (environment variables):
 *   ENGRAM_DIR      — Path to the engram project root (auto-detected by default)
 *   ENGRAM_DB_PATH  — Override the database path passed to engram CLI
 *   ENGRAM_CAPTURE_MIN_SIGNALS — Minimum signal score to show suggestions (default: 1)
 */

const { execSync } = require('child_process');
const path = require('path');

// ── Config ────────────────────────────────────────────────────────────────────

const ENGRAM_DIR = process.env.ENGRAM_DIR ||
  path.resolve(__dirname, '..');  // cursor-hooks/ is inside the engram repo

const DB_PATH_ARG = process.env.ENGRAM_DB_PATH
  ? `ENGRAM_DB_PATH="${process.env.ENGRAM_DB_PATH}" `
  : '';

const MIN_SIGNALS = parseInt(process.env.ENGRAM_CAPTURE_MIN_SIGNALS || '1', 10);

// ── Helpers ───────────────────────────────────────────────────────────────────

function runEngram(args) {
  return execSync(`${DB_PATH_ARG}python3 -m src.cli ${args}`, {
    cwd: ENGRAM_DIR,
    encoding: 'utf-8',
    timeout: 8000,
    env: { ...process.env },
  });
}

function extractLastUserMessage(messages) {
  if (!Array.isArray(messages)) return '';
  const userMsgs = messages.filter(m => m.role === 'user');
  if (!userMsgs.length) return '';
  const last = userMsgs[userMsgs.length - 1];
  const content = typeof last.content === 'string'
    ? last.content
    : Array.isArray(last.content)
      ? last.content.filter(c => c.type === 'text').map(c => c.text).join(' ')
      : '';
  return content.trim().slice(0, 500);
}

function extractLastAssistantMessage(messages) {
  if (!Array.isArray(messages)) return '';
  const assistMsgs = messages.filter(m => m.role === 'assistant');
  if (!assistMsgs.length) return '';
  const last = assistMsgs[assistMsgs.length - 1];
  const content = typeof last.content === 'string'
    ? last.content
    : Array.isArray(last.content)
      ? last.content.filter(c => c.type === 'text').map(c => c.text || '').join(' ')
      : '';
  return content.trim().slice(0, 800);
}

function safeArg(str) {
  return str.replace(/"/g, '\\"').replace(/\n/g, ' ').replace(/`/g, "'");
}

// ── Main ──────────────────────────────────────────────────────────────────────

function run() {
  const contextRaw = process.env.CURSOR_AGENT_CONTEXT;
  if (!contextRaw) return;

  let context;
  try {
    context = JSON.parse(contextRaw);
  } catch (_) {
    return;
  }

  const messages = context.messages || [];
  const taskDesc = extractLastUserMessage(messages);
  const outcome = extractLastAssistantMessage(messages);

  if (!taskDesc) return;

  try {
    // Call suggest-capture with task description and outcome
    const taskArg = safeArg(taskDesc);
    const outcomeArg = safeArg(outcome);

    const raw = runEngram(`suggest-capture --task "${taskArg}" --outcome "${outcomeArg}" --json`);
    if (!raw || !raw.trim()) return;

    let suggestion;
    try {
      suggestion = JSON.parse(raw.trim());
    } catch (_) {
      // CLI may output non-JSON if no signals detected — that's fine
      return;
    }

    // Only surface if we have meaningful signal
    const signalCount = (suggestion.signals || []).length;
    if (signalCount < MIN_SIGNALS) return;

    const type = suggestion.suggested_type || 'memory entry';
    const title = suggestion.draft_title || taskDesc.slice(0, 60);
    const signals = (suggestion.signals || []).slice(0, 3).join(', ');

    console.log(
      `\n[ENGRAM CAPTURE SUGGESTION]\n` +
      `  Type:    ${type}\n` +
      `  Title:   ${title}\n` +
      `  Signals: ${signals}\n` +
      `\n  To save this, run:\n` +
      `    engram add ${type} --title "${safeArg(title)}"\n` +
      `  or use the MCP tool: memory_suggest_capture`
    );

  } catch (_) {
    // Fail silently — never interrupt the IDE
  }
}

run();
