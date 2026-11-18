"""Deterministic review-loop replay and adapters for public workflow resources."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int
    concern: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or value.get("severity") not in ("blocker", "high", "medium"):
            raise ValueError("finding needs a supported severity")
        if not isinstance(value.get("line"), int) or isinstance(value["line"], bool) or value["line"] < 1:
            raise ValueError("finding line must be positive")
        if any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("path", "concern")):
            raise ValueError("finding needs a path and concern")
        return cls(value["severity"], value["path"], value["line"], value["concern"])


def bound(value, low, high, name):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer within [{low}, {high}]")


def run(fixture, max_actor_calls=2, max_review_calls=16, concurrency=3):
    bound(max_actor_calls, 0, 2, "max_actor_calls")
    bound(max_review_calls, 1, 20, "max_review_calls")
    bound(concurrency, 1, 3, "concurrency")
    if not isinstance(fixture, dict):
        raise ValueError("fixture must be an object")
    for name in ("runId", "workItem", "intent"):
        if not isinstance(fixture.get(name), str) or not fixture[name].strip():
            raise ValueError(f"{name} must be nonempty")
    rounds = fixture.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise ValueError("fixture needs review rounds")
    history, actor_calls, review_calls = [], 0, 0
    findings = []
    status, reason = "blocked", "fixture exhausted before independent verification"
    for number, stage in enumerate(rounds):
        if not isinstance(stage, dict) or type(stage.get("validation")) is not bool or not isinstance(stage.get("snapshot"), str) or not stage["snapshot"]:
            raise ValueError("round needs a snapshot and boolean validation")
        if number and stage["snapshot"] == rounds[number-1]["snapshot"]:
            reason = "recorded repair made no snapshot progress"
            break
        reviews = stage.get("reviews")
        if not isinstance(reviews, list) or not reviews or any(not isinstance(x, list) for x in reviews):
            raise ValueError("round needs reviewer finding arrays")
        history.append({"phase": "validated", "round": number, "passed": stage["validation"]})
        if review_calls + len(reviews) > max_review_calls:
            reason = "review call budget exhausted"
            break
        with ThreadPoolExecutor(max_workers=concurrency) as workers:
            parsed = list(workers.map(lambda values: [Finding.parse(v) for v in values], reviews))
        review_calls += len(parsed)
        # Preserve independent reviewer outputs; deduplicate only identical findings.
        findings = list(dict.fromkeys(f for group in parsed for f in group))
        history.append({"phase": "reviewed", "round": number, "reviewers": len(parsed), "findings": [asdict(f) for f in findings]})
        if stage["validation"] and not findings:
            status, reason = "passed", "validation and independent fixture reviews passed"
            break
        if actor_calls >= max_actor_calls:
            reason = "repair call budget exhausted"
            break
        if number + 1 >= len(rounds):
            break
        actor_calls += 1
        history.append({"phase": "actor_completed", "round": number, "note": "next recorded snapshot; no files or agent process changed"})
    snapshot = hashlib.sha256(rounds[min(number, len(rounds)-1)]["snapshot"].encode()).hexdigest()
    return {"workItem": fixture["workItem"], "runId": fixture["runId"], "status": status,
            "reason": reason, "actorCalls": actor_calls, "agentCalls": actor_calls + review_calls,
            "reviewCalls": review_calls, "snapshot_digest": snapshot,
            "findings": [asdict(f) for f in findings], "history": history,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "mode": "synthetic fixture replay; no model or code edits", "quality_evidence": False}


def native_commands(input_file):
    return {"requires": "installed Swamp and upstream @adam/garfield extension; native run also needs Codex and edits workspaceDir",
            "validate": ["swamp", "workflow", "validate", "@adam/garfield", "--json"],
            "run": ["swamp", "workflow", "run", "@adam/garfield", "--input-file", str(input_file), "--skip-reports", "--timeout", "2h"],
            "history": ["swamp", "workflow", "history", "get", "@adam/garfield", "--json"],
            "resources": ["swamp", "data", "list", "workflow-garfield", "--type", "resource", "--json"],
            "executed": False}


def summarize_native_result(value):
    """Read an unwrapped result resource; this is not a full upstream schema validator."""
    if not isinstance(value, dict) or value.get("status") not in ("passed", "blocked"):
        raise ValueError("expected an unwrapped native result resource")
    for field in ("workItem", "runId", "reason", "completedAt"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"native result missing {field}")
    for field in ("actorCalls", "agentCalls"):
        bound(value.get(field), 0, 1_000_000, field)
    if not isinstance(value.get("snapshot"), dict) or not isinstance(value.get("usage"), dict):
        raise ValueError("native result needs snapshot and usage objects")
    for field in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"):
        bound(value["usage"].get(field), 0, 10**15, field)
    snapshot = value["snapshot"]
    if not isinstance(snapshot.get("hash"), str) or not snapshot["hash"]:
        raise ValueError("native snapshot needs a hash")
    if any(not isinstance(snapshot.get(k), str) for k in ("status", "diff")) or type(snapshot.get("diffTruncated")) is not bool:
        raise ValueError("invalid native snapshot fields")
    if not isinstance(snapshot.get("changedPaths"), list) or any(not isinstance(p, str) for p in snapshot["changedPaths"]):
        raise ValueError("native changedPaths must be strings")
    for instant in (value["completedAt"], snapshot.get("capturedAt", "")):
        if not isinstance(instant, str) or not instant:
            raise ValueError("native timestamps must be nonempty strings")
        if datetime.fromisoformat(instant.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("native timestamps need timezone offsets")
    return {k: value[k] for k in ("workItem", "runId", "status", "reason", "actorCalls", "agentCalls", "completedAt", "usage")}


def read_json(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("resource exceeds 2 MiB")
    return json.loads(raw)


def save_resources(directory, fixture, result):
    """Persist independent replay resources without pretending they are native ones."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    for number, event in enumerate(result["history"]):
        resource = {"resource_kind": "independent replay checkpoint", "workItem": result["workItem"], **event}
        (directory / f"checkpoint-{number:03}.json").write_text(json.dumps(resource, indent=2) + "\n", encoding="utf-8")
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("demo", "native-commands", "inspect-resource"), nargs="?", default="demo")
    parser.add_argument("--fixture", type=Path, default=HERE / "fixtures/review.json")
    parser.add_argument("--input-file", type=Path, default=HERE / "examples/native-input.json")
    parser.add_argument("--resource", type=Path)
    parser.add_argument("--max-actor-calls", type=int, default=2)
    parser.add_argument("--max-review-calls", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "native-commands":
            result = native_commands(args.input_file)
        elif args.mode == "inspect-resource":
            if not args.resource:
                raise ValueError("--resource is required")
            result = summarize_native_result(read_json(args.resource))
        else:
            fixture = read_json(args.fixture)
            result = run(fixture, args.max_actor_calls, args.max_review_calls, args.concurrency)
            if args.output_dir:
                save_resources(args.output_dir, fixture, result)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
