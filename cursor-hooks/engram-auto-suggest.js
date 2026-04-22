#!/usr/bin/env node
/**
 * Engram Auto-Suggest Hook for Cursor IDE
 *
 * Place this file in `~/.cursor/hooks/engram-auto-suggest.js`
 * It runs before the agent starts generating a response to:
 *   1. Inject relevant skills or past mistakes from Engram into the prompt context.
 *   2. Suggest exporting high-usage Engram skills to Cursor (≥ EXPORT_THRESHOLD uses).
 *
 * Configuration (environment variables):
 *   ENGRAM_DIR        - Path to the engram project (default: auto-detected from this file)
 *   ENGRAM_EXPORT_THRESHOLD - Min usage_count to suggest exporting a skill (default: 2)
 *   ENGRAM_SKILLS_DIR - Where to export Cursor skills (default: ~/.cursor/skills)
 */

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

// ── Config ──────────────────────────────────────────────────────────

const ENGRAM_DIR = process.env.ENGRAM_DIR ||
  path.resolve(__dirname, '..');  // hooks/ is inside the engram repo

const EXPORT_THRESHOLD = parseInt(process.env.ENGRAM_EXPORT_THRESHOLD || '2', 10);
const CURSOR_SKILLS_DIR = process.env.ENGRAM_SKILLS_DIR ||
  path.join(process.env.HOME || '~', '.cursor', 'skills');

// ── Helpers ──────────────────────────────────────────────────────────

function runEngram(args) {
  return execSync(`python3 -m src.cli ${args}`, {
    cwd: ENGRAM_DIR,
    encoding: 'utf-8',
    timeout: 8000,
  });
}

function skillAlreadyExported(slug) {
  const skillFile = path.join(CURSOR_SKILLS_DIR, slug, 'SKILL.md');
  return fs.existsSync(skillFile);
}

function slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 64);
}

// ── Main ─────────────────────────────────────────────────────────────

function run() {
  const contextRaw = process.env.CURSOR_AGENT_CONTEXT;
  if (!contextRaw) return;

  try {
    const context = JSON.parse(contextRaw);
    const userPrompt = context.messages?.filter(m => m.role === 'user').pop()?.content;

    if (!userPrompt) return;

    const safePrompt = userPrompt.replace(/"/g, '\\"').slice(0, 300);

    // 1. Suggest the most relevant skill for the current task
    try {
      const skillResult = runEngram(`suggest "${safePrompt}" -t skill -n 1`);
      if (skillResult && !skillResult.includes('No matching') && skillResult.trim()) {
        console.log(`\n\n[ENGRAM AUTO-SUGGEST: RELEVANT SKILL]\n${skillResult.trim()}`);
      }
    } catch (_) { /* non-fatal */ }

    // 2. Check for high-usage skills that haven't been exported to Cursor yet
    try {
      // Get recent skills with usage_count info via stats-style JSON output
      // We use the search command with a broad query and parse the skill IDs, then
      // check for export candidates via the export --dry-run output.
      const exportCheck = runEngram(
        `export-skills --min-usage ${EXPORT_THRESHOLD} --dry-run`
      );

      if (exportCheck && exportCheck.includes('would be exported')) {
        // Extract skill names from dry-run output lines like:  Pokemon Proxy Pipeline  (usage: 3)
        const lines = exportCheck.split('\n').filter(l => l.trim().startsWith('-') || /^\s{2}\S/.test(l));
        const candidates = lines
          .map(l => l.replace(/^\s+/, '').replace(/\(usage:.*$/, '').trim())
          .filter(Boolean);

        if (candidates.length > 0) {
          const names = candidates.slice(0, 3).join(', ');
          console.log(
            `\n\n[ENGRAM SKILL EXPORT SUGGESTION]\n` +
            `${candidates.length} proven skill(s) could be exported to Cursor for permanent discovery:\n` +
            `  ${names}${candidates.length > 3 ? ` (+${candidates.length - 3} more)` : ''}\n` +
            `Run: engram export-skills --min-usage ${EXPORT_THRESHOLD}\n` +
            `  or use the MCP tool: memory_export_skill`
          );
        }
      }
    } catch (_) { /* non-fatal — Engram export command may not be installed yet */ }

  } catch (err) {
    // Fail silently so we never break the IDE
  }
}

run();
