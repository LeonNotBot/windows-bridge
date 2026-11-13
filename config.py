import hashlib
import json
import re
from pathlib import Path

from connectors.python_script import ScriptTarget


def load_targets(path):
    config_path = Path(path).resolve()
    value = json.loads(config_path.read_text(encoding="utf-8"))
    targets = {}
    for item in value["targets"]:
        if set(item) - {
            "id",
            "script",
            "sha256",
            "benchmarks",
            "result_mode",
            "python",
            "timeout_seconds",
            "max_result_bytes",
            "report_file",
            "argument_template",
            "allowed_arguments",
            "allowed_switches",
            "working_dir",
            "argument_mode",
            "review_script", "review_sha256", "review_argument_template",
        }:
            raise ValueError("Unknown target configuration field")
        key = item["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or key in targets:
            raise ValueError("Duplicate target")
        script = (config_path.parent / item["script"]).resolve(strict=True)
        if (
            script.is_symlink()
            or hashlib.sha256(script.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ValueError("Script hash mismatch")
        options = {k: v for k, v in item.items() if k not in {"id", "script", "sha256", "review_sha256"}}
        if options.get("review_script"):
            review_script = (config_path.parent / options["review_script"]).resolve(strict=True)
            if hashlib.sha256(review_script.read_bytes()).hexdigest() != item.get("review_sha256"):
                raise ValueError("Review script hash mismatch")
            options["review_script"] = review_script
        for field in (
            "argument_template",
            "allowed_arguments",
            "allowed_switches",
            "benchmarks",
            "review_argument_template",
        ):
            if field in options:
                options[field] = tuple(options[field])
        if options.get("working_dir"):
            options["working_dir"] = config_path.parent / options["working_dir"]
        targets[key] = ScriptTarget(script=script, **options)
    return targets


def mock_targets():
    script = Path(__file__).parent / "connectors" / "mock_benchmark.py"
    targets = {
        key: ScriptTarget(
            script,
            (
                "mock-kb",
                "mock-meeting",
                "mock-failure",
                "mock-crash",
                "mock-invalid",
                "mock-timeout",
            ),
        )
        for key in ("mock-knowledge-base", "mock-fw-agent")
    }
    targets["mock-fw-coding-report"] = ScriptTarget(
        Path(__file__).parent / "connectors" / "mock_fw_report.py",
        (
            "smoke",
            "slow",
            "crash",
            "missing-report",
            "invalid-progress",
            "no-progress",
            "html",
            "manual",
        ),
        result_mode="report",
        timeout_seconds=3600,
        allowed_arguments=("--cases", "--note"),
        allowed_switches=("--include-log",),
    )
    targets["mock-visual-review"] = ScriptTarget(
        Path(__file__).parent / "connectors" / "mock_visual_benchmark.py",
        ("smoke", "two-samples"), result_mode="report",
        review_script=Path(__file__).parent / "connectors" / "mock_visual_benchmark.py",
        review_argument_template=("--eval", "{input_file}"),
    )
    return targets
