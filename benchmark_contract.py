"""Benchmark response v1 contract. Synced into windows-bridge/benchmark_contract.py."""

import math


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def error_object(value):
    return (
        isinstance(value, dict)
        and text(value.get("code"))
        and text(value.get("message"))
        and ("retryable" not in value or isinstance(value["retryable"], bool))
    )


def metrics(values, sample_count=None):
    require(isinstance(values, list), "metrics must be an array")
    seen = set()
    for metric in values:
        require(isinstance(metric, dict), "Invalid metric")
        require(
            all(text(metric.get(k)) for k in ("key", "name", "unit")),
            "Metric key, name and unit required",
        )
        require(metric["key"] not in seen, "Duplicate metric key")
        seen.add(metric["key"])
        value = metric.get("value")
        require(
            type(value) in (int, float) and math.isfinite(value),
            "Metric value must be finite numeric",
        )
        if metric["unit"] == "ratio":
            require(0 <= value <= 1, "Ratio must be between zero and one")
        for key in ("description", "reason", "aggregation"):
            require(
                metric.get(key) is None or isinstance(metric[key], str),
                "Invalid metric description",
            )
        if "valid_count" in metric:
            count = metric["valid_count"]
            require(
                type(count) is int
                and count >= 0
                and (sample_count is None or count <= sample_count),
                "Invalid valid_count",
            )


def validate_benchmark(body, benchmark_id=None):
    require(
        isinstance(body, dict) and body.get("schema_version") == "1.0", "Unsupported result schema"
    )
    benchmark = body.get("benchmark")
    require(
        isinstance(benchmark, dict)
        and all(text(benchmark.get(k)) for k in ("id", "name", "version")),
        "Benchmark identity required",
    )
    require(benchmark_id is None or benchmark["id"] == benchmark_id, "Result benchmark mismatch")
    require(text(body.get("run_id")), "run_id required")
    require(body.get("status") in ("succeeded", "failed"), "Invalid run status")
    samples = body.get("results")
    require(isinstance(samples, list), "results must be an array")
    seen, succeeded = set(), 0
    for sample in samples:
        require(
            isinstance(sample, dict)
            and text(sample.get("sample_id"))
            and text(sample.get("question")),
            "Sample identity and question required",
        )
        require(sample["sample_id"] not in seen, "Duplicate sample ID")
        seen.add(sample["sample_id"])
        require(sample.get("status") in ("succeeded", "failed"), "Invalid sample status")
        if sample["status"] == "succeeded":
            succeeded += 1
            require(
                isinstance(sample.get("output"), dict) and bool(sample["output"]),
                "Successful sample output required",
            )
            require(sample.get("error") is None, "Successful sample cannot have error")
        else:
            require(error_object(sample.get("error")), "Failed sample error required")
            require(
                sample.get("output") is None or isinstance(sample["output"], dict),
                "Invalid failed sample output",
            )
        require("output" in sample and "error" in sample, "Sample output/error fields required")
        for field in ("meeting", "knowledge_base"):
            if field in sample:
                require(
                    isinstance(sample[field], dict)
                    and all(text(sample[field].get(k)) for k in ("id", "version")),
                    "Invalid material reference",
                )
        if "expected_answer" in sample:
            require(isinstance(sample["expected_answer"], str), "expected_answer must be text")
        output = sample.get("output") or {}
        for field in ("answer", "summary"):
            if field in output:
                require(isinstance(output[field], str), "Text output must be a string")
        if "citations" in output:
            require(isinstance(output["citations"], list), "citations must be an array")
            for citation in output["citations"]:
                require(
                    isinstance(citation, dict) and text(citation.get("source_id")),
                    "Citation source_id required",
                )
                for field in ("title", "url", "excerpt"):
                    require(
                        citation.get(field) is None or isinstance(citation[field], str),
                        "Invalid citation field",
                    )
        if "decisions" in output:
            require(
                isinstance(output["decisions"], list)
                and all(isinstance(v, str) for v in output["decisions"]),
                "Invalid decisions",
            )
        if "action_items" in output:
            require(isinstance(output["action_items"], list), "action_items must be an array")
            for item in output["action_items"]:
                require(
                    isinstance(item, dict) and text(item.get("content")),
                    "Action item content required",
                )
                require(
                    item.get("assignee") is None or isinstance(item["assignee"], str),
                    "Invalid assignee",
                )
        metrics(sample.get("metrics", []))
    summary = body.get("summary")
    require(
        isinstance(summary, dict)
        and all(
            type(summary.get(k)) is int and summary[k] >= 0
            for k in ("total", "succeeded", "failed")
        ),
        "Invalid summary",
    )
    require(
        summary["total"] == len(samples)
        and summary["succeeded"] == succeeded
        and summary["failed"] == len(samples) - succeeded,
        "Summary does not match samples",
    )
    require(
        body["status"] != "succeeded" or summary["failed"] == 0,
        "Successful run contains execution failures",
    )
    require(body.get("error") is None or error_object(body["error"]), "Invalid run error")
    require(
        body["status"] != "succeeded" or body.get("error") is None,
        "Successful run cannot have error",
    )
    metrics(body.get("metrics"), len(samples))
    return body
