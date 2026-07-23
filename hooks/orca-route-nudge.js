#!/usr/bin/env node
// orca-route-nudge — UserPromptSubmit hook (NEVER blocks).
//
// If the user prompt looks like a heavy/default task the Lead should delegate,
// emit a nudge via additionalContext. Throttle: at most once per 90s.
// Do NOT run orca synchronously. Do NOT use permissionDecision:deny.
//
//   { "hookSpecificOutput": {
//       "hookEventName": "UserPromptSubmit",
//       "additionalContext": "<nudge: delegate via orca terminal create in current worktree>"
//   } }
//
// Fail-open exit 0 on any error.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const STAMP = path.join(os.homedir(), '.claude', '.orca-route-nudge-stamp');
const THROTTLE_MS = 90 * 1000; // 90 seconds

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);
    if (data.hook_event_name !== 'UserPromptSubmit') process.exit(0);

    const prompt = (data.prompt || data.user_prompt || '').trim();
    if (!prompt) process.exit(0);

    // Resolve orca-route CLI
    let routeBin = null;
    try {
      routeBin = execFileSync('/bin/sh', ['-c', 'command -v orca-route'], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 500,
      }).trim();
    } catch {
      // not on PATH; try sibling
      const root = findRoot();
      const sibling = path.join(root, 'bin', 'orca-route');
      if (fs.existsSync(sibling)) routeBin = sibling;
    }
    if (!routeBin) process.exit(0);

    // Run classifier
    let result = '';
    try {
      result = execFileSync(routeBin, ['--explain'], {
        input: prompt,
        encoding: 'utf8',
        timeout: 2000,
      }).trim();
    } catch {
      process.exit(0);
    }

    // Parse agent from output
    const agentMatch = result.match(/agent=(\S+)/);
    if (!agentMatch) process.exit(0);
    const agent = agentMatch[1];

    // Only nudge for non-trivial tasks (codex or opencode with heavy signals)
    if (agent === 'mimo-code') process.exit(0); // trivial — no delegation needed

    // Throttle check
    try {
      const st = fs.statSync(STAMP);
      if (Date.now() - st.mtimeMs < THROTTLE_MS) process.exit(0);
    } catch {
      // no stamp yet
    }
    try {
      fs.mkdirSync(path.dirname(STAMP), { recursive: true });
      fs.writeFileSync(STAMP, String(Date.now()));
    } catch {
      /* best-effort */
    }

    const modelHint = agent === 'codex' ? ' (gpt-5.6-sol)' : '';
    const nudge =
      `nudge: use the orca-sin-team skill and create \`${agent}${modelHint}\` as a new Orca terminal ` +
      `in the current worktree via \`orca terminal create --worktree path:<repo> --command ${agent}\`. ` +
      `Pass the parent terminal handle for direct callbacks; never create a worker worktree or sleep-poll. ` +
      `(Classification: ${result.split('\n')[0] || result}. Throttled: ≤1/90s.)`;

    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'UserPromptSubmit',
          additionalContext: nudge,
        },
      })
    );
    process.exit(0);
  } catch {
    process.exit(0);
  }
});

function findRoot() {
  let current = process.cwd();
  while (true) {
    if (fs.existsSync(path.join(current, 'BRIEF.md'))) return current;
    const parent = path.dirname(current);
    if (parent === current) return process.cwd();
    current = parent;
  }
}
