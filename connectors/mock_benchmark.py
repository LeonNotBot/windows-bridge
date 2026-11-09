"""A standalone mock with the proposed Python benchmark CLI contract."""

import argparse
import json
import os
from pathlib import Path
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.benchmark == "mock-timeout":
        time.sleep(60)
    if args.benchmark == "mock-crash":
        raise SystemExit(3)
    if args.benchmark == "mock-invalid":
        text = "invalid JSON"
    else:
        failed = args.benchmark == "mock-failure"
        error = (
            {
                "code": "KB_NOT_READY",
                "message": "Mock knowledge base is not ready",
                "retryable": False,
            }
            if failed
            else None
        )
        body = {
            "schema_version": "1.0",
            "benchmark": {"id": args.benchmark, "name": "Mock benchmark", "version": "v1"},
            "run_id": str(uuid.uuid4()),
            "status": "failed" if failed else "succeeded",
            "summary": {"total": 1, "succeeded": 0 if failed else 1, "failed": 1 if failed else 0},
            "results": [
                {
                    "sample_id": "sample-1",
                    "question": "Mock question",
                    "status": "failed" if failed else "succeeded",
                    "output": None if failed else {"answer": "Mock answer"},
                    "error": error,
                }
            ],
            "metrics": [],
            "error": error,
        }
        if not failed:
            meeting = args.benchmark == "mock-meeting"
            body["benchmark"]["name"] = "Mock 会议摘要" if meeting else "Mock 知识库"
            first = body["results"][0]
            first["question"] = "总结本次会议并提取待办" if meeting else "产品支持什么系统？"
            if meeting:
                first["output"] = {
                    "summary": "下周开展验收。",
                    "action_items": [{"content": "整理清单", "assignee": "张三"}],
                }
            first["metrics"] = [
                {"key": "correctness", "name": "正确性", "value": 1, "unit": "score"}
            ]
            body["results"].append(
                {
                    "sample_id": "sample-2",
                    "question": first["question"],
                    "status": "succeeded",
                    "output": (
                        {"summary": "摘要遗漏部分待办。"}
                        if meeting
                        else {"answer": "Mock incomplete answer"}
                    ),
                    "metrics": [
                        {"key": "correctness", "name": "正确性", "value": 0, "unit": "score"}
                    ],
                    "error": None,
                }
            )
            body["summary"] = {"total": 2, "succeeded": 2, "failed": 0}
            body["metrics"] = [
                {
                    "key": "accuracy",
                    "name": "回答正确率",
                    "value": 0.5,
                    "unit": "ratio",
                    "valid_count": 2,
                },
                {
                    "key": "new_metric_example",
                    "name": "新增指标示例",
                    "value": 12,
                    "unit": "count",
                    "description": "未预注册的指标也可展示",
                },
            ]
        text = json.dumps(body, ensure_ascii=False)
    if args.output:
        temporary = args.output.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(args.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
