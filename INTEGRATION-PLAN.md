# SIN Architecture Integration Plan

## Guiding Principle

> Maximale Lösungsqualität, Intelligenz und Verlässlichkeit pro Sol-Token.
> Nicht "so wenig Tokens wie möglich".

## Architecture Overview

```
                    Codex GPT-5.6 Sol
                    (Planung, Entscheidung, Verifikation)
                           │
                    Simone MCP
                    (Task-/Workflowzustand)
                           │
          ┌────────────────┼────────────────┐
          │                │                │
      sin-orca       sin-context     sin-review-context
      (Worker-       (Retrieval-     (Diff-/Review-
       Ausführung)     Broker)         Intelligence)
          │                │                │
    Capability-       Graphify        code-review-graph
      Loader       (Knowledge/      (Diff/Flows/Test-
          │        Architecture)     Gaps/Risk)
     ┌────┴────┐        │                │
   explore  implement   │                │
   research  verify     │                │
   review               │                │
          │                │                │
          └────────────────┼────────────────┘
                           │
                 L1/L2/L3 Memory Layer
                           │
                    actual Git-Diff
                    Tests / Typecheck / Lint
```

## Responsibility Matrix

| Component | Responsibility | Stores |
|---|---|---|
| Simone MCP | Task definitions, steps, acceptance criteria, research plans, checkpoint/review status, evidence references, architecture decisions, activity/event log | task.json, events.jsonl, decisions.json |
| sin-orca | Worker dispatch, capability loading, intervention, verification, blind review orchestration | task state, command history, activity |
| sin-context | Unified retrieval broker across all sources | cache, query routing |
| Graphify | General code understanding, symbol search, paths, architecture, rationale, ADRs | knowledge graph |
| code-review-graph | Diff analysis, changed function mapping, affected flows, test gaps, risk signals | review artifacts |
| DeepTutor principles | Research decomposition, citation management, memory layers | L1/L2/L3 memory |
| Codex GPT-5.6 Sol | Planning, evaluation, final decision | decisions only |

## File Layout

```
lib/
├── sin_cache.py              # 6-layer cache (existing)
├── sin_memory.py             # L1/L2/L3 memory layer
├── sin_research.py           # Research pipeline (DeepTutor principles)
├── sin_capability.py         # Capability loader for agent loop
├── sin_citation.py           # Citation/evidence manager
└── sin_review_context.py     # CRG adapter (review sensor)

bin/
├── sin-orca                  # Orchestrator (existing + capabilities)
├── sin-cache                 # Cache CLI (existing)
├── sin-memory                # Memory CLI
├── sin-review-context        # Review context CLI
└── sin-research              # Research pipeline CLI

config/
├── orca-orchestrator.json    # Orchestrator config (existing)
├── capabilities.json         # Capability definitions
├── research-pipeline.json    # Research config
└── memory-policy.json        # Memory L1/L2/L3 policy
```

## JSON Schemas

### 1. Capability Definition (capabilities.json)

```json
{
  "schema_version": 1,
  "capabilities": {
    "explore": {
      "description": "Code exploration and understanding",
      "tools": ["graphify_query", "graphify_path", "graphify_explain"],
      "max_steps": null,
      "allowed_artifacts": ["checkpoint.json", "report.json"],
      "prompt_template": "explore-prompt.md"
    },
    "research": {
      "description": "Structured research with evidence",
      "tools": ["web_search", "sin_http_get", "graphify_query"],
      "max_steps": null,
      "allowed_artifacts": ["checkpoint.json", "report.json", "citation.json"],
      "prompt_template": "research-prompt.md",
      "allows_dynamic_subquestions": true
    },
    "implement": {
      "description": "Code implementation",
      "tools": ["edit", "write", "bash", "graphify_query"],
      "max_steps": null,
      "allowed_artifacts": ["checkpoint.json", "report.json"],
      "prompt_template": "implement-prompt.md",
      "requires_approval": true
    },
    "verify": {
      "description": "Run tests and verification",
      "tools": ["bash", "graphify_query"],
      "max_steps": null,
      "allowed_artifacts": ["checkpoint.json", "report.json"],
      "prompt_template": "verify-prompt.md"
    },
    "review": {
      "description": "Code review with CRG integration",
      "tools": ["sin_review_context", "graphify_query"],
      "max_steps": null,
      "allowed_artifacts": ["review.json"],
      "prompt_template": "review-prompt.md"
    }
  }
}
```

### 2. Research Decomposition (sin-research output)

```json
{
  "schema_version": 1,
  "main_question": "How does the auth token refresh flow work?",
  "subquestions": [
    {
      "id": "sq-01",
      "question": "Where are refresh tokens stored?",
      "status": "answered",
      "evidence": [
        {
          "source": "src/auth/token.ts",
          "content_sha256": "a82e...",
          "lines": "71-104",
          "claim": "Refresh tokens are stored in httpOnly cookies"
        }
      ],
      "synthesis": "Refresh tokens use httpOnly cookies with 7-day expiry"
    },
    {
      "id": "sq-02",
      "question": "What happens when a refresh token expires?",
      "status": "pending",
      "evidence": [],
      "synthesis": null
    }
  ],
  "citations": {
    "manager": "inline",
    "entries": []
  },
  "contradictions": [],
  "open_questions": ["sq-02"]
}
```

### 3. L1/L2/L3 Memory Entry

```json
{
  "schema_version": 1,
  "level": "L2",
  "topic": "auth-refresh-flow",
  "content": "The auth refresh flow uses httpOnly cookies with a 7-day expiry. When expired, the client must re-authenticate. The safeTokenEquals function prevents timing attacks.",
  "evidence_refs": [
    {"source": "events.jsonl", "sequence": 42},
    {"source": "src/auth/token.ts", "content_sha256": "a82e..."}
  ],
  "created_at": "2026-07-22T18:00:00+00:00",
  "confidence": "verified",
  "source_tasks": ["TASK-042", "TASK-038"]
}
```

### 4. Review Context (sin-review-context output)

```json
{
  "schema_version": 1,
  "base_sha": "abc123",
  "head_sha": "def456",
  "changed_files": [
    {
      "path": "src/auth/token.ts",
      "change_type": "modified",
      "lines_added": 12,
      "lines_removed": 3
    }
  ],
  "changed_symbols": [
    {
      "name": "safeTokenEquals",
      "file": "src/auth/token.ts",
      "start_line": 71,
      "end_line": 104,
      "type": "function"
    }
  ],
  "affected_flows": [
    {
      "flow": "auth-refresh",
      "functions": ["validateRefreshToken", "safeTokenEquals"],
      "criticality": "high"
    }
  ],
  "test_gaps": [
    {
      "function": "safeTokenEquals",
      "has_direct_test": false,
      "coverage_type": "indirect",
      "risk": "medium"
    }
  ],
  "risk_signals": [
    {
      "type": "security_keyword",
      "symbol": "safeTokenEquals",
      "score": 0.20
    },
    {
      "type": "flow_participation",
      "symbol": "safeTokenEquals",
      "flow": "auth-refresh",
      "score": 0.25
    }
  ],
  "graphify_paths": [
    {
      "from": "safeTokenEquals",
      "to": "validateRefreshToken",
      "path": ["safeTokenEquals", "validateRefreshToken", "authMiddleware"]
    }
  ],
  "uncertainties": [
    "Dynamic calls in authMiddleware not fully resolved",
    "Framework magic in cookie handling not traced"
  ],
  "recommended_review_order": [
    "safeTokenEquals",
    "validateRefreshToken"
  ],
  "total_risk_score": 0.45
}
```

### 5. Simone Task Contract

```json
{
  "schema_version": 1,
  "task_id": "TASK-042",
  "project": "sin-save-token",
  "objective": "Implement safe token comparison",
  "role": "implementer",
  "allowed_paths": ["src/auth/token.ts", "tests/auth/token.test.ts"],
  "forbidden_paths": ["package.json", "config/"],
  "acceptance_criteria": [
    "Only allowlisted files change",
    "Regression test passes",
    "No timing side-channel"
  ],
  "ordered_steps": [
    {"id": "S01", "instruction": "Implement safeTokenEquals", "approved": true},
    {"id": "S02", "instruction": "Add regression test", "approved": false}
  ],
  "verification_command": "npm test -- tests/auth/token.test.ts",
  "evidence_refs": {
    "graphify": {"artifact_hash": "sha256:...", "relevant_nodes": ["AuthService"]},
    "review": {"artifact_hash": "sha256:...", "risk_score": 0.45},
    "memory": {"topic": "auth-refresh-flow", "confidence": "verified"}
  },
  "events": [
    {"type": "dispatched", "timestamp": "..."},
    {"type": "checkpoint_received", "checkpoint": "plan-ready"},
    {"type": "codex_approved", "step": "S01"},
    {"type": "verification_completed", "ok": true},
    {"type": "review_completed", "verdict": "accept"}
  ],
  "decisions": [
    {
      "id": "DEC-017",
      "decision": "Use constant-time comparison for token validation",
      "rationale": "Prevents timing side-channel attacks",
      "evidence": ["src/auth/token.ts:71-104"],
      "status": "accepted"
    }
  ]
}
```

## Data Flow

### Research Flow

```
Codex defines main question
        ↓
sin-research decomposes into subquestions
        ↓
Worker explores each subquestion
        ↓
Evidence collected with citations
        ↓
Contradictions surfaced
        ↓
Synthesis returned to Codex
        ↓
Memory L2 updated with verified findings
```

### Review Flow

```
sin-orca dispatches review task
        ↓
sin-review-context runs CRG detect_changes
        ↓
sin-review-context runs CRG get_affected_flows
        ↓
sin-review-context runs CRG test_gaps
        ↓
Graphify path/explain for top risk nodes
        ↓
Combined review context returned
        ↓
Blind reviewer receives ONLY:
  - original task
  - bounded diff
  - changed files
  - acceptance criteria
  - review context (without worker reasoning)
        ↓
Verdict returned
        ↓
Memory L2 updated with review decision
```

### Memory Flow

```
Worker produces raw trace (L1)
        ↓
sin-memory compresses to summary (L2)
        ↓
Verified decisions promoted to L3
        ↓
L3 feeds back into future task context
```

## What Goes Where

### In Simone MCP:
- Task definitions and step tracking
- Acceptance criteria management
- Research plan storage
- Checkpoint/review status
- Evidence references (hashes only, not content)
- Architecture decisions with rationale
- Activity/event log

### In sin-orca:
- Capability loading and switching
- Worker dispatch and intervention
- Verification execution
- Blind review orchestration
- Enforcement (repeated failures, stalls)

### In sin-context:
- Query routing to appropriate backend
- Cache management (6-layer)
- Evidence validation

### In Graphify:
- Code understanding (symbols, paths, architecture)
- Knowledge graph maintenance
- Rationale and ADR tracking

### In code-review-graph:
- Git diff analysis
- Changed function mapping
- Affected flow detection
- Test gap identification
- Risk signal calculation

### In Memory Layer:
- L1: Raw events (events.jsonl)
- L2: Compressed summaries (task-summary.json)
- L3: Verified decisions and rules (sin-memory-write)
