#!/usr/bin/env node
// SessionStart hook: idempotently start the repo-local cache keepalive daemon.

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

if (process.argv.includes('--help')) {
  process.stdout.write('Usage: cache-keepalive-autostart.js < SessionStart-hook-json\n');
  process.exit(0);
}

const root = findRoot();
const stateDir = path.join(root, '.cache-opt');
const pidFile = path.join(stateDir, 'keepalive.pid');
const siblingKeepalive = path.resolve(root, '..', 'cache-opt-grok', 'bin', 'cache-keepalive');

let input = '';
const stdinTimeout = setTimeout(() => process.exit(0), 3000);
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => (input += chunk));
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);
  try {
    const data = JSON.parse(input);
    if (data.hook_event_name !== 'SessionStart') process.exit(0);

    const command = resolveCommand('cache-keepalive', siblingKeepalive);
    if (!command) return emitSkipped();

    const existingPid = readPid();
    if (existingPid && isAlive(existingPid)) return emitRunning();
    try {
      fs.unlinkSync(pidFile);
    } catch {
      /* stale or missing sidecar */
    }

    fs.mkdirSync(stateDir, { recursive: true });
    const result = spawnSync(command, ['start'], {
      cwd: root,
      encoding: 'utf8',
      stdio: 'ignore',
      timeout: 2500,
      env: { ...process.env, CACHE_KEEPALIVE_ROOT: root },
    });
    if (result.error || result.status !== 0) process.exit(0);
    if (!readPid()) process.exit(0);
    emitRunning();
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

function readPid() {
  try {
    const value = fs.readFileSync(pidFile, 'utf8').trim();
    const pid = Number(value);
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function emitRunning() {
  try {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'SessionStart',
          additionalContext: 'cache keepalive: running',
        },
      })
    );
  } catch {
    /* fail-open */
  }
  process.exit(0);
}
