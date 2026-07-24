#!/usr/bin/env python3
"""Tests für sin_cache v1 – 6-Schichten-Cache."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import sin_cache
from sin_cache import SinCache, canonical_query, repository_identity, evidence_is_current, sha256_json


class TestCanonicalQuery(unittest.TestCase):
    def test_stopwords_removed(self):
        result = canonical_query("Wo wird refreshToken validiert?")
        self.assertIn("refreshtoken", result)
        self.assertNotIn("wo", result)
        self.assertNotIn("wird", result)

    def test_deterministic_sort(self):
        a = canonical_query("Which function validates the token?")
        b = canonical_query("The function which validates token")
        self.assertEqual(a, b)

    def test_empty(self):
        self.assertEqual(canonical_query(""), "")


class TestRepositoryIdentity(unittest.TestCase):
    def test_same_repo_same_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmpdir, capture_output=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=tmpdir, capture_output=True)

            id1 = repository_identity(tmpdir)
            id2 = repository_identity(tmpdir)
            self.assertEqual(id1, id2)
            self.assertEqual(len(id1), 24)

    def test_different_repos_different_identity(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            subprocess.run(["git", "init"], cwd=d1, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d1, capture_output=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=d1, capture_output=True)

            subprocess.run(["git", "init"], cwd=d2, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d2, capture_output=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=d2, capture_output=True)

            id1 = repository_identity(d1)
            id2 = repository_identity(d2)
            self.assertNotEqual(id1, id2)


class TestEvidenceValidation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "auth.ts").write_text("export function validate() {}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_valid_evidence(self):
        evidence = [{
            "path": "src/auth.ts",
            "content_sha256": sin_cache.file_content_hash(self.repo / "src" / "auth.ts"),
        }]
        self.assertTrue(evidence_is_current(self.repo, evidence))

    def test_invalid_evidence(self):
        evidence = [{
            "path": "src/auth.ts",
            "content_sha256": "wrong_hash",
        }]
        self.assertFalse(evidence_is_current(self.repo, evidence))

    def test_missing_file(self):
        evidence = [{
            "path": "src/nonexistent.ts",
            "content_sha256": "abc",
        }]
        self.assertFalse(evidence_is_current(self.repo, evidence))

    def test_malformed_or_escaping_evidence_is_invalid(self):
        self.assertFalse(evidence_is_current(self.repo, [{}]))
        self.assertFalse(evidence_is_current(self.repo, ["not-an-object"]))
        self.assertFalse(evidence_is_current(
            self.repo,
            [{"path": "../outside", "content_sha256": "abc"}],
        ))


class TestCacheL1Exact(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cache = SinCache(db_path=self.tmpdir / "test.db")

    def tearDown(self):
        self.cache.close()
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_miss_then_hit(self):
        result = self.cache.get("code_symbol", "graphify", "where is token", "repo1")
        self.assertIsNone(result)

        self.cache.put(
            "code_symbol", "graphify", "where is token", "repo1",
            "Token is in src/auth.ts",
        )

        result = self.cache.get("code_symbol", "graphify", "where is token", "repo1")
        self.assertIsNotNone(result)
        self.assertEqual(result["content"], "Token is in src/auth.ts")

    def test_put_update_preserves_hit_count(self):
        key = self.cache.put(
            "code_symbol", "graphify", "where is token", "repo1",
            "first answer",
        )
        self.assertIsNotNone(
            self.cache.get(
                "code_symbol", "graphify", "where is token", "repo1"
            )
        )
        self.cache.put(
            "code_symbol", "graphify", "where is token", "repo1",
            "updated answer",
        )

        hit_count = self.cache.conn.execute(
            "SELECT hit_count FROM cache_entries WHERE cache_key = ?",
            (key,),
        ).fetchone()[0]
        self.assertEqual(hit_count, 1)

    def test_invalidation_by_path(self):
        self.cache.put(
            "code_symbol", "graphify", "query", "repo1",
            "answer",
            evidence=[{"path": "src/auth.ts", "content_sha256": "abc"}],
        )

        count = self.cache.invalidate_by_path("src/auth.ts")
        self.assertEqual(count, 1)

        result = self.cache.get("code_symbol", "graphify", "query", "repo1")
        self.assertIsNone(result)

    def test_stale_while_revalidate(self):
        self.cache.put(
            "code_symbol", "graphify", "query", "repo1",
            "old answer",
        )

        result = self.cache.get("code_symbol", "graphify", "query", "repo1")
        self.assertIsNotNone(result)

    def test_shared_blob_survives_single_entry_invalidation(self):
        first = self.cache.put(
            "code_symbol", "graphify", "query one", "repo1",
            "shared answer",
        )
        second = self.cache.put(
            "code_symbol", "graphify", "query two", "repo1",
            "shared answer",
        )
        self.assertNotEqual(first, second)
        self.assertEqual(
            self.cache.conn.execute(
                "SELECT COUNT(*) FROM cache_blobs"
            ).fetchone()[0],
            1,
        )

        self.cache.invalidate_by_key(first)
        self.assertIsNotNone(
            self.cache.get(
                "code_symbol", "graphify", "query two", "repo1"
            )
        )
        self.assertEqual(
            self.cache.conn.execute(
                "SELECT COUNT(*) FROM cache_blobs"
            ).fetchone()[0],
            1,
        )

        self.cache.invalidate_by_key(second)
        self.assertEqual(
            self.cache.conn.execute(
                "SELECT COUNT(*) FROM cache_blobs"
            ).fetchone()[0],
            0,
        )

    def test_close_is_idempotent(self):
        for _ in range(2):
            self.cache.close()


class TestCacheL2Semantic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cache = SinCache(db_path=self.tmpdir / "test.db")

    def tearDown(self):
        self.cache.close()
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_semantic_hit(self):
        self.cache.put(
            "code_symbol", "graphify", "refresh token validation code", "repo1",
            "In src/auth.ts Zeile 71",
        )

        result = self.cache.get_semantic(
            "code_symbol", "graphify",
            "refresh token validation function",
            "repo1",
            threshold=0.3,
            require_evidence=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["content"], "In src/auth.ts Zeile 71")

    def test_no_semantic_hit(self):
        self.cache.put(
            "code_symbol", "graphify", "database schema design", "repo1",
            "Schema is in schema.sql",
        )

        result = self.cache.get_semantic(
            "code_symbol", "graphify",
            "how to deploy to kubernetes",
            "repo1",
            threshold=0.7,
            require_evidence=False,
        )
        self.assertIsNone(result)

    def test_semantic_cache_is_scoped_to_policy_and_provider_version(self):
        self.cache.put(
            "code_symbol",
            "graphify",
            "refresh token validation code",
            "repo1",
            "In src/auth.ts",
            policy_hash="policy-v1",
            provider_version="graphify-v8",
        )

        self.assertIsNotNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "refresh token validation function",
            "repo1",
            threshold=0.3,
            policy_hash="policy-v1",
            provider_version="graphify-v8",
            require_evidence=False,
        ))
        self.assertIsNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "refresh token validation function",
            "repo1",
            threshold=0.3,
            policy_hash="policy-v2",
            provider_version="graphify-v8",
            require_evidence=False,
        ))
        self.assertIsNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "refresh token validation function",
            "repo1",
            threshold=0.3,
            policy_hash="policy-v1",
            provider_version="graphify-v9",
            require_evidence=False,
        ))

    def test_semantic_cache_requires_evidence_by_default(self):
        self.cache.put(
            "code_symbol", "graphify", "refresh token validation", "repo1",
            "answer without evidence",
        )
        self.assertIsNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "refresh token validation",
            "repo1",
            threshold=1.0,
        ))
        self.assertIsNotNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "refresh token validation",
            "repo1",
            threshold=1.0,
            require_evidence=False,
        ))

    def test_semantic_cache_invalidates_changed_evidence(self):
        repository = self.tmpdir / "repository"
        repository.mkdir()
        evidence_file = repository / "evidence.txt"
        evidence_file.write_text("first", encoding="utf-8")

        self.cache.put_evidence(
            "code_symbol",
            "graphify",
            "evidence lookup",
            "repo-evidence",
            "answer",
            [{"path": "evidence.txt"}],
            repository_path=repository,
            policy_hash="policy",
            provider_version="provider",
        )
        self.assertIsNotNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "evidence lookup",
            "repo-evidence",
            threshold=1.0,
            repository_path=repository,
            policy_hash="policy",
            provider_version="provider",
        ))

        evidence_file.write_text("changed", encoding="utf-8")
        self.assertIsNone(self.cache.get_semantic(
            "code_symbol",
            "graphify",
            "evidence lookup",
            "repo-evidence",
            threshold=1.0,
            repository_path=repository,
            policy_hash="policy",
            provider_version="provider",
        ))
        self.assertEqual(
            self.cache.conn.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0],
            0,
        )

    def test_invalid_semantic_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cache.get_semantic(
                "code_symbol", "graphify", "query", "repo1",
                threshold=1.1,
            )

    def test_put_evidence_rejects_missing_or_escaping_paths(self):
        repository = self.tmpdir / "repository"
        repository.mkdir()
        with self.assertRaises(ValueError):
            self.cache.put_evidence(
                "code_symbol", "graphify", "query", "repo1", "answer",
                [{"path": "missing.txt"}],
                repository_path=repository,
            )
        with self.assertRaises(ValueError):
            self.cache.put_evidence(
                "code_symbol", "graphify", "query", "repo1", "answer",
                [{"path": "../outside.txt"}],
                repository_path=repository,
            )


class TestCacheL4WorkerArtifacts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cache = SinCache(db_path=self.tmpdir / "test.db")

    def tearDown(self):
        self.cache.close()
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_explorer_cache(self):
        self.cache.cache_explorer_result(
            "repo1", "task_hash_abc",
            claims=["auth module found"],
            evidence=[{"path": "src/auth.ts"}],
            candidate_paths=["src/auth.ts", "src/auth/"],
        )

        result = self.cache.get(
            "explorer_result", "worker",
            "task_hash_abc", "repo1",
        )
        self.assertIsNotNone(result)
        data = json.loads(result["content"])
        self.assertIn("auth module found", data["claims"])

    def test_review_cache(self):
        self.cache.cache_review_result(
            "repo1", "task_hash", "acc_hash", "diff_hash", "test_hash",
            "kilo-code", "accept",
            criteria=[{"id": "AC01", "status": "proven"}],
        )

        result = self.cache.get(
            "review_result", "kilo-code",
            sha256_json({
                "task_hash": "task_hash",
                "acceptance_hash": "acc_hash",
                "diff_hash": "diff_hash",
                "test_hash": "test_hash",
                "reviewer": "kilo-code",
                "schema": sin_cache.REVIEW_SCHEMA_VERSION,
            }),
            "repo1",
        )
        self.assertIsNotNone(result)


class TestCacheStats(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cache = SinCache(db_path=self.tmpdir / "test.db")

    def tearDown(self):
        self.cache.close()
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_stats_tracking(self):
        self.cache.put("code_symbol", "g", "q1", "r1", "a1")
        self.cache.get("code_symbol", "g", "q1", "r1")
        self.cache.get("code_symbol", "g", "q2", "r1")

        stats = self.cache.stats()
        self.assertEqual(stats["exact_hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["total"], 2)


if __name__ == "__main__":
    unittest.main()
