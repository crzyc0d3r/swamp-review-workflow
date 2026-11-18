from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import app


class ReviewTests(unittest.TestCase):
    def setUp(self): self.fixture = app.read_json(app.HERE/"fixtures/review.json")

    def test_bounded_repair_and_independent_rereview(self):
        result = app.run(self.fixture)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["actorCalls"], 1)
        self.assertEqual(result["reviewCalls"], 6)
        initial = next(h for h in result["history"] if h["phase"] == "reviewed")
        self.assertEqual(len(initial["findings"]), 1)
        self.assertFalse(result["quality_evidence"])

    def test_no_repair_budget_blocks(self):
        result = app.run(self.fixture, max_actor_calls=0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["actorCalls"], 0)

    def test_review_budget_is_enforced_before_calls(self):
        result = app.run(self.fixture, max_review_calls=2)
        self.assertEqual(result["reviewCalls"], 0)
        self.assertEqual(result["status"], "blocked")

    def test_failed_validation_cannot_pass_clean_reviews(self):
        value = deepcopy(self.fixture); value["rounds"] = [{"snapshot":"bad", "validation":False,"reviews":[[]]}]
        self.assertEqual(app.run(value)["status"], "blocked")

    def test_missing_final_review_blocks(self):
        value = deepcopy(self.fixture); value["rounds"] = value["rounds"][:1]
        self.assertEqual(app.run(value)["status"], "blocked")

    def test_invalid_bounds_and_findings(self):
        for kwargs in ({"max_actor_calls":3},{"max_review_calls":0},{"concurrency":True}):
            with self.assertRaises(ValueError): app.run(self.fixture, **kwargs)
        for value in ({}, {"severity":"high","line":True,"path":"x","concern":"x"}):
            with self.assertRaises(ValueError): app.Finding.parse(value)
        with self.assertRaises(ValueError): app.run([])

    def test_repair_without_snapshot_progress_blocks(self):
        value = deepcopy(self.fixture)
        value["rounds"][1]["snapshot"] = value["rounds"][0]["snapshot"]
        self.assertEqual(app.run(value)["status"], "blocked")

    def test_persisted_independent_checkpoints_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/"run"; result = app.run(self.fixture)
            app.save_resources(out, self.fixture, result)
            self.assertEqual(json.loads((out/"result.json").read_text())["status"], "passed")
            self.assertEqual(len(list(out.glob("checkpoint-*.json"))), len(result["history"]))
            with self.assertRaises(FileExistsError): app.save_resources(out, self.fixture, result)

    def test_native_commands_are_not_executed(self):
        result = app.native_commands("input file.json")
        self.assertFalse(result["executed"])
        self.assertIn("input file.json", result["run"])
        self.assertEqual(result["validate"], ["swamp","workflow","validate","@adam/garfield","--json"])

    def test_native_resource_typed_subset(self):
        resource = {"workItem":"x","runId":"r","status":"passed","reason":"validated","actorCalls":1,"agentCalls":4,
                    "snapshot":{"hash":"fixture-hash","status":"clean","diff":"","diffTruncated":False,"changedPaths":[],"capturedAt":"2026-11-18T00:00:00Z"},
                    "usage":dict.fromkeys(["inputTokens","cachedInputTokens","outputTokens","reasoningOutputTokens","totalTokens"],0),"completedAt":"2026-11-18T00:00:00Z"}
        self.assertEqual(app.summarize_native_result(resource)["agentCalls"], 4)
        resource["snapshot"]["capturedAt"] = None
        with self.assertRaises(ValueError): app.summarize_native_result(resource)
        resource["snapshot"]["capturedAt"] = "2026-11-18T00:00:00Z"
        resource["agentCalls"] = True
        with self.assertRaises(ValueError): app.summarize_native_result(resource)

    def test_cli_outputs_demo(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output): self.assertEqual(app.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "passed")


if __name__ == "__main__": unittest.main()
