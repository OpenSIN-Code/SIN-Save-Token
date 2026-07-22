#!/usr/bin/env node
// SessionStart hook: start a bounded cache warm without delaying the session.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

if (process.argv.includes('--help')) {
  process.stdout.write('Usage: cache-warm-session.js < SessionStart-hook-json\n');
  process.exit(0);
}

const root = findRoot();
const siblingWarm = path.resolve(root, '..', 'cache-opt-grok', 'bin', 'cache-warm');

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);
    if (data.hook_event_name !== 'SessionStart') process.exit(0);

    const command = resolveCommand('cache-warm', siblingWarm);
    if (!command) return emitStatus('skipped');

    const probe = spawnSync(command, ['--dry-run', '--timeout', '6'], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 1200,
    });
    if (probe.error || probe.status !== 0) return emitStatus('skipped');

    const provider = parseProvider(probe.stdout) || providerFromEnv();
    if (!hasCredential(provider)) return emitStatus('skipped');
    spawn('/bin/sh', ['-c', warmCommand(command)], {
      cwd: root,
      detached: true,
      stdio: 'ignore',
      env: process.env,
    });
    emitStatus(provider || 'skipped');
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

function resolveCommand(name, fallback) {
  try {
    const result = spawnSync('/bin/sh', ['-c', `command -v ${name}`], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 500,
    });
    const found = result.status === 0 && result.stdout.trim();
    if (found) return found;
  } catch {
    /* try the documented sibling fallback */
  }
  return fs.existsSync(fallback) ? fallback : null;
}

function parseProvider(output) {
  const match = String(output || '').match(/provider=([^\s]+)/);
  return match ? match[1] : '';
}

function providerFromEnv() {
  const configured = (process.env.ORCA_CACHE_PROVIDER || '').trim().toLowerCase();
  if (configured && configured !== 'auto') return configured;
  if (process.env.ANTHROPIC_API_KEY || process.env.ORCA_CACHE_API_KEY) return 'anthropic';
  if (process.env.OPENAI_API_KEY) return 'openai';
  return '';
}

function hasCredential(provider) {
  if (provider === 'openai') {
    return Boolean(
      process.env.OPENAI_API_KEY ||
      process.env.ORCA_CACHE_API_KEY ||
      process.env.ANTHROPIC_API_KEY
    );
  }
  return Boolean(
    process.env.ANTHROPIC_API_KEY ||
    process.env.ORCA_CACHE_API_KEY ||
    process.env.OPENAI_API_KEY
  );
}

function warmCommand(command) {
  const quoted = `'${String(command).replace(/'/g, `'"'"'`)}'`;
  return `${quoted} --timeout 6 >/dev/null 2>&1 & pid=$!; ` +
    `(sleep 8; kill "$pid" 2>/dev/null) &`;
}

function emitStatus(status) {
  try {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'SessionStart',
          additionalContext: `cache prewarmed: ${status}`,
        },
      })
    );
  } catch {
    /* fail-open */
  }
  process.exit(0);
}
