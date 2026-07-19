#!/usr/bin/env node
// rtk-auto-rewrite — PreToolUse hook (Bash)
// Transparently rewrites shell commands to run through `rtk` (Rust Token Killer),
// a token-optimizing CLI proxy. `rtk <cmd>` compresses output; if rtk has no
// dedicated filter it passes the command through unchanged, so prepending `rtk`
// is always output-safe.
//
// Mechanism (verified against current Claude Code docs, v2.0.10+):
//   PreToolUse hooks rewrite a tool's arguments by emitting, on stdout:
//     { "hookSpecificOutput": {
//         "hookEventName": "PreToolUse",
//         "permissionDecision": "allow",     // REQUIRED for updatedInput to apply
//         "updatedInput": { ...full tool_input... }  // must be the COMPLETE object
//     } }
//   Source: https://code.claude.com/docs/en/hooks  (Hooks reference)
//
// Safety model (this runs on EVERY Bash call — a corrupted command is far worse
// than a missed rewrite, so we are deliberately conservative):
//   * Only the Bash tool is touched.
//   * Only rewrite when the FIRST token is on a curated allowlist.
//   * Only rewrite SIMPLE single commands. If the command contains ANY shell
//     control/metacharacter (&& || | ; & ` $( ) < > ( ) newline), we pass it
//     through UNCHANGED — we do not try to parse compound commands.
//   * Idempotent: never prefix a command that already starts with `rtk`.
//   * If `rtk` is not on PATH, pass through unchanged.
//   * Any parse error / unexpected shape → exit 0 (allow unchanged). Never block.

const { execFileSync } = require('child_process');
const path = require('path');
const { tokenize } = require(path.join(__dirname, 'lib', 'git-cmd.js'));

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);

    // Only the Bash tool carries a shell command string.
    if (data.tool_name !== 'Bash') process.exit(0);

    const toolInput = data.tool_input || {};
    const command = toolInput.command;
    if (typeof command !== 'string' || command.trim() === '') process.exit(0);

    // Curated allowlist of first-tokens that benefit from rtk filtering.
    const ALLOW = new Set([
      'git', 'cargo', 'npm', 'npx', 'pnpm', 'yarn',
      'tsc', 'jest', 'vitest', 'playwright', 'pytest',
      'go', 'rake', 'rspec', 'prettier', 'next',
      'ls', 'grep', 'find', 'docker', 'kubectl',
      'gh', 'prisma', 'eslint', 'biome',
    ]);

    const trimmed = command.trim();

    // Idempotency: already an rtk invocation → leave it alone.
    // (Word-boundary check so we don't match e.g. an "rtkfoo" binary.)
    if (/^rtk(\s|$)/.test(trimmed)) process.exit(0);

    // Conservative bail-out: any shell control/metacharacter means we do NOT
    // attempt a rewrite. Covers &&, ||, single |, ;, &, subshells, command
    // substitution, redirects, backgrounding, multi-line. A missed rewrite is
    // acceptable; corrupting a compound command is not.
    if (/[&|;`\n<>()]|\$\(/.test(trimmed)) process.exit(0);

    // Resolve the real program via git-cmd.tokenize: skip leading ENV=val
    // assignments and accept full paths (/usr/bin/git → git).
    const tokens = tokenize(trimmed);
    let i = 0;
    while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) i++;
    if (i >= tokens.length) process.exit(0);
    const progToken = tokens[i];
    const prog = path.basename(progToken);
    if (!ALLOW.has(prog)) process.exit(0);

    // Guard: rtk must be resolvable on PATH, else pass through unchanged.
    try {
      execFileSync('/bin/sh', ['-c', 'command -v rtk'], { stdio: 'ignore' });
    } catch {
      process.exit(0);
    }

    // Insert `rtk` after any leading env assignments so
    // `FOO=1 cargo build` → `FOO=1 rtk cargo build` (valid shell).
    const rewritten = [...tokens.slice(0, i), 'rtk', ...tokens.slice(i)].join(' ');

    const output = {
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'allow',
        permissionDecisionReason: `rtk-auto-rewrite: inserted rtk before \`${prog}\` for token savings`,
        updatedInput: {
          ...toolInput,
          command: rewritten,
        },
      },
    };

    process.stdout.write(JSON.stringify(output));
    process.exit(0);
  } catch {
    // Silent fail — never block or corrupt a tool call.
    process.exit(0);
  }
});
