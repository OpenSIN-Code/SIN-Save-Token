#!/usr/bin/env python3

import unittest
from pathlib import Path
from types import ModuleType
import sys

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "bin" / "sin-memory-write"

MODULE = ModuleType("sin_memory_write")
MODULE.__file__ = str(MODULE_PATH)
sys.modules["sin_memory_write"] = MODULE
exec(compile(MODULE_PATH.read_text(encoding="utf-8"), str(MODULE_PATH), "exec"), MODULE.__dict__)


class MemoryWriteTests(unittest.TestCase):
    def test_identical_text_is_duplicate(self):
        left = "We decided that Cognee owns durable domain memory and Simone owns project code facts."
        right = left
        self.assertEqual(MODULE.similarity(left, right), 1.0)

    def test_similar_decisions_are_detected(self):
        left = "Cognee is the canonical owner of durable domain decisions across all agents."
        right = "Cognee is the canonical owner of durable domain decisions for the full agent fleet."
        self.assertGreater(MODULE.similarity(left, right), 0.4)

    def test_unrelated_memories_are_not_duplicates(self):
        left = "Cognee stores durable architecture decisions."
        right = "Graphify determines callers and symbol dependencies."
        self.assertLess(MODULE.similarity(left, right), 0.2)

    def test_rejects_speculation(self):
        policy = {"write_policy": {"minimum_length": 10, "maximum_length": 500, "allowed_types": ["decision"], "reject_patterns": ["^maybe\\b"]}}
        problem = MODULE.validate("Maybe this database is faster.", "decision", policy)
        self.assertIsNotNone(problem)

    def test_accepts_atomic_decision(self):
        policy = {"write_policy": {"minimum_length": 10, "maximum_length": 500, "allowed_types": ["decision"], "reject_patterns": ["^maybe\\b"]}}
        problem = MODULE.validate("Cognee is the canonical durable domain-memory owner.", "decision", policy)
        self.assertIsNone(problem)


if __name__ == "__main__":
    unittest.main()
