#!/usr/bin/env node
"use strict";

/**
 * PreToolUse guard for expensive, broad context operations.
 *
 * Reads a Claude-style hook payload from stdin.
 * Exit 0: allow.
 * JSON output with permissionDecision=deny: block once and redirect.
 */

let raw = "";

process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => {
  raw += chunk;
});

process.stdin.on("end", () => {
  let payload;

  try {
    payload = JSON.parse(raw || "{}");
  } catch {
    process.exit(0);
  }

  const toolName = String(payload.tool_name || "");
  const input = payload.tool_input || {};

  if (!["Bash", "Grep", "Read"].includes(toolName)) {
    process.exit(0);
  }

  const command = String(
    input.command ||
    input.pattern ||
    input.file_path ||
    ""
  ).trim();

  if (!command) {
    process.exit(0);
  }

  // Broker calls must never block themselves.
  if (/\bsin-context\b/.test(command)) {
    process.exit(0);
  }

  const broadPatterns = [
    /\bcat\s+.+(?:\*|\.|\/)$/i,
    /\bfind\s+\.\s+(?:-type\s+f)?\s*$/i,
    /\brg\s+(?:--files|\.)\s*$/i,
    /\bgrep\s+-R\b/i,
    /\btree(?:\s+-a)?\s*$/i,
    /\bgit\s+log\b(?!.*-(?:n|max-count))/i,
    /\b(?:pytest|npm\s+test|cargo\s+test)\b(?!.*(?:quiet|fail|short))/i
  ];

  const broadRead =
    toolName === "Read" &&
    !Number.isInteger(input.offset) &&
    !Number.isInteger(input.limit);

  const broadCommand = broadPatterns.some(pattern => pattern.test(command));

  if (!broadRead && !broadCommand) {
    process.exit(0);
  }

  const reason = [
    "Broad context operation blocked by SIN context budget.",
    "Use the smallest targeted query first:",
    `sin-context ${JSON.stringify(command)}`,
    "Read raw files only when the broker result is insufficient."
  ].join("\n");

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: reason
      }
    })
  );
});
