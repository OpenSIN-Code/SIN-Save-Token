#!/usr/bin/env node
// orca-delegation-guard — PreToolUse hook (WebFetch | WebSearch | Bash)
//
// Purpose: NUDGE (never block) the main agent to delegate expensive exploration
// — web lookups and BROAD, repo-wide code searches — to the `mimo-code`
// subagent via a new Orca terminal in the CURRENT worktree, per the canonical
// `orca-sin-team` skill in wow-my-zsh. Never create a worker worktree.
//
// Mechanism (PreToolUse, verified against current Claude Code docs):
//   To surface a non-blocking note to the agent, emit on stdout:
//     { "hookSpecificOutput": {
//         "hookEventName": "PreToolUse",
//         "permissionDecision": "allow",           // ALLOW — must not block work
//         "permissionDecisionReason": "<nudge text surfaced to the agent>"
//     } }
//   To stay silent: exit 0 with no stdout.
//
// This hook NEVER emits `deny`. A missed nudge is harmless; blocking real work
// is not. Any error / unexpected shape → exit 0 (silent allow).
//
// Trigger conditions (ALL must hold):
//   1. Tool is WebFetch, WebSearch, OR Bash where the command is a BROAD
//      code-exploration search (recursive/repo-wide grep|rg|find). A grep with
//      a single explicit file arg is NARROW → no nudge.
//   2. Not already inside a subagent (best-effort — see caveat below).
//   3. Throttle: at most one nudge per ~600s (mtime of ~/.orca-nudge-stamp).
//
// Subagent-detection caveat: Claude Code does not pass a reliable, documented
// "am I a subagent" signal into the hook stdin payload, and the env var
// CLAUDE_CODE_SUBAGENT_MODEL is present in BOTH main and subagent processes on
// this machine (it advertises which model subagents WOULD use — it is not a
// "you are a subagent" flag). We therefore treat CLAUDE_CODE_CHILD_SESSION=1
// as the subagent signal when present (observed to be set for spawned child
// sessions) and otherwise fall through. Because the nudge is a lightweight,
// throttled, non-blocking `allow`, a false positive in either direction is
// harmless — worst case is one extra harmless note per 10 minutes.

const fs = require('fs');
const os = require('os');
const path = require('path');

const STAMP = path.join(os.homedir(), '.claude', '.orca-nudge-stamp');
const THROTTLE_MS = 600 * 1000; // ~10 minutes

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);
    const toolName = data.tool_name;

    // --- Condition 1: is this an expensive-exploration tool call? ---
    let isExpensive = false;
    if (toolName === 'WebFetch' || toolName === 'WebSearch') {
      isExpensive = true;
    } else if (toolName === 'Bash') {
      const cmd = (data.tool_input && data.tool_input.command) || '';
      if (typeof cmd === 'string' && cmd.trim() !== '') {
        isExpensive = isBroadSearch(cmd.trim());
      }
    }
    if (!isExpensive) process.exit(0);

    // --- Condition 2: not already inside a subagent ---
    // Best-effort: child sessions set CLAUDE_CODE_CHILD_SESSION=1. If we are a
    // child/subagent, stay silent (the delegation already happened).
    if (process.env.CLAUDE_CODE_CHILD_SESSION === '1') process.exit(0);

    // --- Condition 3: throttle to at most once per ~10 min ---
    try {
      const st = fs.statSync(STAMP);
      if (Date.now() - st.mtimeMs < THROTTLE_MS) process.exit(0);
    } catch {
      // no stamp yet → not throttled
    }

    // Refresh the throttle stamp (touch). Best-effort — if this fails we still
    // nudge this once; we just may nudge again sooner than intended.
    try {
      fs.mkdirSync(path.dirname(STAMP), { recursive: true });
      fs.writeFileSync(STAMP, String(Date.now()));
    } catch {
      /* ignore */
    }

    const reason =
      'Delegations-Doktrin: teure Exploration an mimo-code via ' +
      '`orca terminal create --worktree path:<repo> --command mimo-code` im selben Worktree. ' +
      'Parent-Terminal-Handle für direkte Callbacks mitgeben; kein `orca worktree create`, kein Sleep-Polling. ' +
      'Skill: wow-my-zsh/orca-sin-team. (Gedrosselter Hinweis.)';

    const output = {
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'allow',
        permissionDecisionReason: reason,
      },
    };
    process.stdout.write(JSON.stringify(output));
    process.exit(0);
  } catch {
    // Never break a tool call.
    process.exit(0);
  }
});

/**
 * Return true if `cmd` is a BROAD code-exploration search:
 *   - grep/rg/find whose first program token is one of grep|egrep|fgrep|rg|find
 *   - AND it is recursive / repo-wide rather than scoped to one explicit file.
 *
 * Broad:
 *   grep -r pattern .        grep -R pattern src/      rg pattern
 *   rg pattern src/          find . -name '*.js'       find src -type f
 * Narrow (NOT broad → no nudge):
 *   grep pattern file.txt    rg pattern path/to/one.rs
 */
function isBroadSearch(cmd) {
  // Only inspect the FIRST simple command; a pipeline's head is what matters
  // for classifying the exploration. We tokenize loosely on whitespace.
  const tokens = cmd.split(/\s+/);

  // Skip leading VAR=VALUE env assignments.
  let i = 0;
  while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) i++;
  if (i >= tokens.length) return false;

  const prog = path.basename(tokens[i]);
  i++;

  const isGrep = prog === 'grep' || prog === 'egrep' || prog === 'fgrep';
  const isRg = prog === 'rg';
  const isFind = prog === 'find';
  if (!isGrep && !isRg && !isFind) return false;

  // Split remaining tokens into flags and non-flag (positional) args.
  const rest = tokens.slice(i);
  const flags = rest.filter((t) => t.startsWith('-'));
  const positionals = rest.filter((t) => !t.startsWith('-'));

  if (isFind) {
    // `find` is inherently a recursive tree walk. Treat `find <dir> ...` or a
    // bare `find` / `find .` as broad. The only non-broad case is essentially
    // meaningless for exploration, so any find invocation counts as broad.
    return true;
  }

  if (isGrep) {
    // Recursive flag anywhere → broad regardless of args.
    const recursive = flags.some((f) => /^-[A-Za-z]*[Rr]/.test(f) || f === '--recursive');
    if (recursive) return true;
    // Non-recursive grep: broad only if it is NOT scoped to explicit files.
    // Heuristic: grep needs a PATTERN + at least one FILE to be narrow.
    // positionals[0] is the pattern; positionals[1..] are files.
    // 0 file args → reading stdin/dir-wide context → treat as broad.
    // >=1 explicit file arg that isn't a directory-tree marker → narrow.
    const fileArgs = positionals.slice(1);
    if (fileArgs.length === 0) return false; // e.g. `grep foo` (needs a target) — not a repo scan
    // If the only "file" is `.` or a directory-ish path with no recursion, it
    // won't actually scan a tree (grep errors on a dir without -r), so narrow.
    return false;
  }

  if (isRg) {
    // ripgrep is recursive BY DEFAULT. It is broad UNLESS the user scoped it to
    // exactly one explicit file path (positionals: [pattern, singleFile]).
    // positionals[0] = pattern; positionals[1..] = paths.
    const pathArgs = positionals.slice(1);
    if (pathArgs.length === 0) return true; // rg pattern → whole repo
    if (pathArgs.length === 1) {
      // One path arg. If it's clearly a single file (has an extension and is
      // not a bare dir like `.` or `src`), treat as narrow; otherwise broad.
      const p = pathArgs[0];
      const looksLikeDir = p === '.' || p.endsWith('/') || !path.basename(p).includes('.');
      return looksLikeDir;
    }
    return true; // multiple paths → broad
  }

  return false;
}
