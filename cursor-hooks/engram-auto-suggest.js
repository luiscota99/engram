#!/usr/bin/env node
/**
 * Engram Auto-Suggest Hook for Cursor IDE
 * 
 * Place this file in `~/.cursor/hooks/engram-auto-suggest.js`
 * It runs before the agent starts generating a response to inject
 * relevant skills or past mistakes from Engram directly into the prompt context.
 */

const { execSync } = require('child_process');

function run() {
  const contextRaw = process.env.CURSOR_AGENT_CONTEXT;
  if (!contextRaw) return;

  try {
    const context = JSON.parse(contextRaw);
    const userPrompt = context.messages.filter(m => m.role === 'user').pop()?.content;
    
    if (!userPrompt) return;

    // Call Engram's semantic suggest via CLI
    // Suggesting skills based on the user's latest prompt
    const skillResult = execSync(`python3 -m src.cli suggest "${userPrompt.replace(/"/g, '\\"')}" -t skill -n 1`, {
      cwd: '/Users/luismiguel/Desktop/AI/engram',
      encoding: 'utf-8'
    });

    if (skillResult && !skillResult.includes('No matching skills found')) {
      console.log(`\n\n[ENGRAM AUTO-SUGGEST: RELEVANT SKILL]\n${skillResult}`);
    }

  } catch (err) {
    // Fail silently so we don't break the IDE
  }
}

run();
