#!/usr/bin/env node
// agent-grep-nudge — PreToolUse hook (Grep)
//
// Purpose: NUDGE (never block) the agent toward `agent-grep` when it runs a
// BROAD search with Claude Code's native Grep tool. `agent-grep` returns the
// same hits but tags each with its enclosing symbol and self-truncates with a
// visible "… +N more" marker — so the agent rarely needs a follow-up file read
// (the real token cost of searching). See bin/agent-grep in this repo.
//
// Why a nudge on the Grep TOOL, not a Bash rewrite: rtk-auto-rewrite already
// rewrites Bash `grep`/`rg` to `rtk grep`. A second hook rewriting the same
// commands would be two hooks fighting over one string — fragile. The native
// Grep tool is a separate surface rtk never touches, so nudging here is
// conflict-free and can never corrupt a command.
//
// Mechanism (PreToolUse, verified against current Claude Code docs):
//   Non-blocking note surfaced to the agent:
//     { "hookSpecificOutput": {
//         "hookEventName": "PreToolUse",
//         "permissionDecision": "allow",            // ALLOW — must not block work
//         "permissionDecisionReason": "<nudge text>"
//     } }
//   Silent: exit 0 with no stdout.
//
// This hook NEVER emits `deny`. A missed nudge is harmless; blocking a search is
// not. Any error / unexpected shape → exit 0 (silent allow).
//
// Trigger conditions (ALL must hold):
//   1. Tool is Grep.
//   2. The search is BROAD: no single explicit file `path`, i.e. it scans a
//      directory tree (path is absent, ".", or a directory). A Grep already
//      scoped to one file is narrow → no nudge (agent-grep wouldn't help much).
//   3. `agent-grep` is resolvable on PATH (else the nudge is useless).
//   4. Throttle: at most one nudge per ~600s (mtime of a stamp file).

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const STAMP = path.join(os.homedir(), '.claude', '.agent-grep-nudge-stamp');
const THROTTLE_MS = 600 * 1000; // ~10 minutes

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);

    // --- Condition 1: native Grep tool only ---
    if (data.tool_name !== 'Grep') process.exit(0);

    const ti = data.tool_input || {};

    // --- Condition 2: is it broad (tree scan) rather than one explicit file? ---
    if (!isBroadGrep(ti)) process.exit(0);

    // --- Condition 3: agent-grep must be on PATH, else the nudge is pointless.
    try {
      execFileSync('/bin/sh', ['-c', 'command -v agent-grep'], { stdio: 'ignore' });
    } catch {
      process.exit(0);
    }

    // --- Condition 4: throttle to at most once per ~10 min ---
    try {
      const st = fs.statSync(STAMP);
      if (Date.now() - st.mtimeMs < THROTTLE_MS) process.exit(0);
    } catch {
      // no stamp yet → not throttled
    }
    try {
      fs.mkdirSync(path.dirname(STAMP), { recursive: true });
      fs.writeFileSync(STAMP, String(Date.now()));
    } catch {
      /* best-effort */
    }

    const reason =
      'Tipp: für breite Code-Suchen `agent-grep <pattern> <pfad>` (Bash) statt ' +
      'des rohen Grep-Tools — es taggt jeden Treffer mit seiner umschließenden ' +
      'Funktion und kürzt sichtbar (… +N more), spart also den Datei-Nachlesen-' +
      'Schritt. (Hinweis erscheint gedrosselt.)';

    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'allow',
          permissionDecisionReason: reason,
        },
      })
    );
    process.exit(0);
  } catch {
    process.exit(0);
  }
});

/**
 * Broad = the Grep scans a tree, not a single explicit file.
 *
 * Claude Code's Grep tool_input carries `pattern` and optional `path` (+ glob,
 * output_mode, etc). We classify by `path`:
 *   - absent / "" / "."           → whole-tree scan            → BROAD
 *   - ends with "/" or has no ext → directory                 → BROAD
 *   - a concrete file (has a base name with an extension)      → NARROW
 * A `glob` filter (e.g. "*.ts") still scans a tree, so glob presence keeps it
 * broad regardless of path shape.
 */
function isBroadGrep(ti) {
  if (ti.glob) return true;
  const p = ti.path;
  if (p === undefined || p === null || p === '' || p === '.') return true;
  if (typeof p !== 'string') return true;
  if (p.endsWith('/')) return true;
  const base = path.basename(p);
  // No dot in the basename → almost certainly a directory (src, lib, hooks).
  return !base.includes('.');
}
