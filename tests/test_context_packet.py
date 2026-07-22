#!/usr/bin/env python3

import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "bin" / "sin-context-packet.py"

MODULE = ModuleType("sin_context_packet")
MODULE.__file__ = str(MODULE_PATH)
sys.modules["sin_context_packet"] = MODULE
exec(compile(MODULE_PATH.read_text(encoding="utf-8"), str(MODULE_PATH), "exec"), MODULE.__dict__)


class ContextPacketTests(unittest.TestCase):
    def test_extract_files(self):
        text = "See src/main.py and lib/utils.py for details"
        files = MODULE.extract_files(text)
        self.assertIn("src/main.py", files)
        self.assertIn("lib/utils.py", files)

    def test_uncertainty_detection(self):
        text = "This result is not sure about the answer"
        uncertainty = MODULE.detect_uncertainty(text)
        self.assertIn("uncertainty", uncertainty.lower())

    def test_packet_to_json(self):
        packet = MODULE.ContextPacket(
            answer="Test answer",
            files=["src/main.py"],
            approx_tokens=100,
            provider="graphify",
            route="code_symbol",
        )
        data = json.loads(packet.to_json())
        self.assertEqual(data["answer"], "Test answer")
        self.assertEqual(data["files"], ["src/main.py"])
        self.assertEqual(data["provider"], "graphify")
        self.assertNotIn("evidence", data)
        self.assertNotIn("novelty_score", data)

    def test_build_packet(self):
        text = "The function createCommit is in src/main.py"
        packet = MODULE.build_packet(text, "graphify", "code_symbol", 50)
        self.assertIn("createCommit", packet.answer)
        self.assertIn("src/main.py", packet.files)
        self.assertEqual(packet.provider, "graphify")

    def test_empty_packet_has_no_empty_fields(self):
        packet = MODULE.ContextPacket(answer="simple answer")
        data = json.loads(packet.to_json())
        self.assertNotIn("files", data)
        self.assertNotIn("uncertainty", data)
        self.assertNotIn("next_read", data)


if __name__ == "__main__":
    unittest.main()
