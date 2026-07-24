#!/usr/bin/env node
"use strict";

/**
 * PreToolUse guard for expensive, broad context operations.
 *
 * Reads a Claude-style hook payload from stdin.
 * Exit 0: allow.
 * Broad operations emit a non-blocking optimization nudge.
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

  const command = String(input.command || "").trim();
  const filePath = String(input.file_path || "").trim();
  const grepPattern = String(input.pattern || "").trim();
  const grepPath = String(input.path || "").trim();
  const displayValue = command || filePath || grepPattern;

  if (!displayValue) {
    process.exit(0);
  }

  // Broker calls must never block themselves.
  if (toolName === "Bash" && /\bsin-context\b/.test(command)) {
    process.exit(0);
  }

  const rootLikePath = value =>
    value === "." || value === ".." || value === "/" || value.endsWith("/");
  const containsGlob = value => /[*?\[\]]/.test(value);

  let broadOperation = false;

  if (toolName === "Bash") {
    const normalized = command.replace(/\s+/g, " ").trim();
    const broadPatterns = [
      /^(?:cat|sed|awk)\s+.*[*?\[]/i,
      /^find\s+\.\s*(?:-type\s+f\s*)?$/i,
      /^(?:rg|ripgrep)\s+(?:--files|\.)\s*$/i,
      /^grep\s+.*(?:-R|-r|--recursive)(?:\s|$)/i,
      /^tree(?:\s+-a)?\s*$/i,
      /^git\s+log(?:\s+--all)?\s*$/i,
      /^(?:python(?:3)?\s+-m\s+)?pytest(?:\s+(?:-q|--quiet))*\s*$/i,
      /^npm\s+test\s*$/i,
      /^cargo\s+test\s*$/i
    ];
    broadOperation = broadPatterns.some(pattern => pattern.test(normalized));
  } else if (toolName === "Read") {
    // A single named file is targeted even without offset/limit. Only directory
    // or wildcard reads are broad enough to redirect through the broker.
    broadOperation = rootLikePath(filePath) || containsGlob(filePath);
  } else if (toolName === "Grep") {
    // Grep's pattern is data, not a shell command. Scope using path/glob fields.
    broadOperation =
      (!grepPath || rootLikePath(grepPath)) &&
      !String(input.glob || "").trim() &&
      !Number.isInteger(input.head_limit);
  }

  if (!broadOperation) {
    process.exit(0);
  }

  const reason = [
    "Broad context operation detected by SIN context budget.",
    "Use the smallest targeted query first:",
    `sin-context ${JSON.stringify(displayValue)}`,
    "Read raw files only when the broker result is insufficient."
  ].join("\n");

  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: reason
      }
    })
  );
});
