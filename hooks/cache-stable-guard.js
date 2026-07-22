#!/usr/bin/env node
// cache-stable-guard — PreToolUse hook (Write | Edit | MultiEdit) + fail-open nudge
//
// Purpose: WARN (never block) when a tool call would mutate a cache-buster file
// that sits in the prompt-cache prefix (CLAUDE.md, AGENTS.md, settings.json,
// MCP servers.json). Changing these mid-session invalidates the warm prefix for
// all active sessions — next turn is cold (full input price).
//
// Mechanism (Claude Code PreToolUse):
//   stdout: { hookSpecificOutput: {
//     hookEventName: "PreToolUse",
//     permissionDecision: "allow",
//     permissionDecisionReason: "<warning>"
//   } }
//   silent allow: exit 0 with no stdout
//
// Never emits deny. Any error / unexpected shape → exit 0.
// Also usable as standalone CLI via bin/cache-stable-guard (Python).

const path = require('path');

// Basename / suffix matchers for cache-prefix busters.
const BUSTER_BASENAMES = new Set([
  'claude.md',
  'agents.md',
  'settings.json',
  'servers.json',
]);

// Path fragments that mark MCP registry files even under nested dirs.
const BUSTER_FRAGMENTS = [
  '/shared/mcp/servers.json',
  '/mcp/servers.json',
  '/.claude/settings.json',
  '\\shared\\mcp\\servers.json',
  '\\mcp\\servers.json',
  '\\.claude\\settings.json',
];

const WARN =
  'Cache-Prefix: diese Datei ist Teil des stabilen Prompt-Cache-Prefix. ' +
  'Änderung invalidiert den warmen Cache für alle laufenden Sessions ' +
  '(nächster Turn = kalt, voller Input-Preis). Sammle solche Änderungen ' +
  'an Session-Grenzen. (Non-blocking Nudge — Arbeit wird nicht blockiert.)';

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => (input += c));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    run(input);
  } catch {
    process.exit(0);
  }
});

function run(raw) {
  if (!raw || !raw.trim()) process.exit(0);

  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  const toolName = data.tool_name || data.toolName || '';
  // Write / Edit / MultiEdit (Claude) and common aliases
  const mutators = new Set([
    'Write',
    'Edit',
    'MultiEdit',
    'NotebookEdit',
    'str_replace',
    'create_file',
    'search_replace',
  ]);
  if (!mutators.has(toolName) && !/^Write|Edit/i.test(toolName)) {
    // Bash: only flag obvious redirects/writes to buster files
    if (toolName === 'Bash' || toolName === 'bash') {
      const cmd = (data.tool_input && (data.tool_input.command || data.tool_input.cmd)) || '';
      if (typeof cmd === 'string' && cmdTouchesBuster(cmd)) {
        emitWarn();
      }
      process.exit(0);
    }
    process.exit(0);
  }

  const paths = extractPaths(data.tool_input || data.input || {});
  if (paths.some(isCacheBusterPath)) {
    emitWarn();
  }
  process.exit(0);
}

function extractPaths(inp) {
  const out = [];
  if (!inp || typeof inp !== 'object') return out;
  for (const key of [
    'file_path',
    'filePath',
    'path',
    'notebook_path',
    'notebookPath',
    'target_file',
  ]) {
    const v = inp[key];
    if (typeof v === 'string' && v) out.push(v);
  }
  // MultiEdit: edits[].path or file_path at top
  if (Array.isArray(inp.edits)) {
    for (const e of inp.edits) {
      if (e && typeof e.path === 'string') out.push(e.path);
      if (e && typeof e.file_path === 'string') out.push(e.file_path);
    }
  }
  return out;
}

function isCacheBusterPath(p) {
  if (!p || typeof p !== 'string') return false;
  const norm = p.replace(/\\/g, '/');
  const base = path.basename(norm).toLowerCase();
  if (BUSTER_BASENAMES.has(base)) {
    // settings.json only if it looks like agent/claude settings, not random app config
    if (base === 'settings.json') {
      return (
        /\/\.claude\//i.test(norm) ||
        /\/\.codex\//i.test(norm) ||
        /\/\.config\/opencode\//i.test(norm) ||
        /(^|\/)settings\.json$/i.test(norm) && /claude|codex|opencode|sin/i.test(norm)
      );
    }
    // servers.json: only MCP registry-ish paths
    if (base === 'servers.json') {
      return /mcp/i.test(norm) || /servers\.json$/i.test(norm);
    }
    return true; // CLAUDE.md / AGENTS.md anywhere
  }
  const lower = norm.toLowerCase();
  for (const frag of BUSTER_FRAGMENTS) {
    if (lower.endsWith(frag.replace(/\\/g, '/').toLowerCase()) || lower.includes(frag.replace(/\\/g, '/').toLowerCase())) {
      return true;
    }
  }
  // basename CLAUDE.md case variants already covered; also AGENTS.MD etc.
  if (/^claude\.md$/i.test(base) || /^agents\.md$/i.test(base)) return true;
  return false;
}

function cmdTouchesBuster(cmd) {
  // Cheap heuristic: command line contains a buster filename near a write redirect
  const lower = cmd.toLowerCase();
  const names = ['claude.md', 'agents.md', 'servers.json'];
  const hasName = names.some((n) => lower.includes(n)) ||
    (lower.includes('settings.json') && /(claude|codex|opencode|\.claude)/.test(lower));
  if (!hasName) return false;
  // write-ish operators
  return /(>{1,2}|tee\b|sed\s+-i|perl\s+-i|mv\b|cp\b|rm\b|truncate\b)/.test(lower);
}

function emitWarn() {
  try {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'allow',
          permissionDecisionReason: WARN,
        },
      })
    );
  } catch {
    /* ignore */
  }
}
