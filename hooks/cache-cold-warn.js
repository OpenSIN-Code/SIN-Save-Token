#!/usr/bin/env node
// cache-cold-warn — Notification / PreToolUse hook (any tool)
//
// Purpose: warn when the Anthropic prompt cache is about to go COLD. Anthropic's
// prompt-cache entries have a ~5-minute TTL: if more than ~5 min pass between
// turns, the next turn re-reads the whole conversation UNCACHED — slower and
// markedly more expensive (cache reads are ~10% the price of fresh input). This
// is jcode idea ④, ported as a dependency-free stamp check.
//
// Mechanism: on each invocation we compare "now" against the mtime of a stamp
// file we touch every turn. If the gap exceeds the TTL threshold, we surface a
// one-line non-blocking note; then we refresh the stamp. Never blocks.
//
//   { "hookSpecificOutput": {
//       "hookEventName": "PreToolUse",
//       "permissionDecision": "allow",
//       "permissionDecisionReason": "<warning>"
//   } }
//
// Any error / unexpected shape → exit 0 (silent allow). A missed warning is
// harmless; blocking a tool call is not.

const fs = require('fs');
const os = require('os');
const path = require('path');

const STAMP = path.join(os.homedir(), '.claude', '.cache-warm-stamp');
const TTL_MS = 5 * 60 * 1000; // Anthropic prompt-cache TTL ≈ 5 minutes

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => (input += c));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  let warn = false;
  let gapMin = 0;
  try {
    const st = fs.statSync(STAMP);
    const gap = Date.now() - st.mtimeMs;
    if (gap > TTL_MS) {
      warn = true;
      gapMin = Math.round(gap / 60000);
    }
  } catch {
    // no stamp yet → first turn, nothing to warn about
  }
  // Refresh the stamp for the next turn (best-effort).
  try {
    fs.mkdirSync(path.dirname(STAMP), { recursive: true });
    fs.writeFileSync(STAMP, String(Date.now()));
  } catch {
    /* ignore */
  }

  if (!warn) process.exit(0);

  const reason =
    `Prompt-Cache vermutlich KALT (~${gapMin} min Pause > 5 min TTL): der ` +
    `nächste Turn liest den Kontext ungecacht neu (teurer/langsamer). Bei ` +
    `großem Kontext lohnt jetzt ggf. /compact, bevor du fortfährst.`;

  try {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'allow',
          permissionDecisionReason: reason,
        },
      })
    );
  } catch {
    /* ignore */
  }
  process.exit(0);
});
