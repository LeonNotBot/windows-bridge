"""FW Agent Coding stand-in: no real agent, model calls, or mandatory result JSON."""

import argparse
import json
from pathlib import Path
import time
import zipfile


def atomic(path, content):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--progress-file", required=True, type=Path)
    parser.add_argument("--cases", default="case01,case02")
    parser.add_argument("--note", default="Mock 本地验证")
    parser.add_argument("--include-log", action="store_true")
    args, extra_arguments = parser.parse_known_args()
    if args.benchmark == "crash":
        raise SystemExit(7)
    directory = args.output_dir
    # Echo custom CLI parameters so transport can be verified without a real agent.
    atomic(
        directory / "arguments.json",
        json.dumps(
            {
                "extra_arguments": extra_arguments,
                "cases": args.cases,
                "note": args.note,
                "include_log": args.include_log,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    for count in range(3):
        if args.benchmark != "no-progress":
            progress = (
                "not JSON"
                if args.benchmark == "invalid-progress"
                else json.dumps(
                    {
                        "stage": "执行测试",
                        "completed": count,
                        "total": 2,
                        "message": f"已完成 {count}/2",
                    }
                )
            )
            atomic(args.progress_file, progress)
        time.sleep(20 if args.benchmark == "slow" else 0.25)
    if args.benchmark == "missing-report":
        return
    report = (
        "# FW Agent Coding 测试报告\n\n"
        + args.note
        + "\n\n## 本次参数\n\n测试题目：`"
        + args.cases
        + "`\n\n[本次透传参数](arguments.json)\n\n| 指标 | 结果 |\n| --- | --- |\n| 通过数 | 1 |\n| 总题数 | 2 |\n\n[逐题 Excel 数据](case-results.xlsx)\n\n![示意图](assets/sample.png)\n\n业务程序自行定义报告与评分口径。\n"
    )
    atomic(directory / "report.md", report)
    if args.benchmark == "manual":
        atomic(directory / "review.required", "Manual review required")
    if args.benchmark == "html":
        atomic(directory / "report.md", "# HTML 附件示例\n\n[HTML 报告](report.html)")
        atomic(
            directory / "report.html",
            '<h1>HTML 测试报告</h1><p>隔离预览</p><script>window.top.document.body.innerHTML="UNSAFE"</script>',
        )
    # A minimal valid XLSX produced using only the standard library.
    with zipfile.ZipFile(directory / "case-results.xlsx", "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Results" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>sample_id</t></is></c><c r="B1" t="inlineStr"><is><t>result</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>case01</t></is></c><c r="B2"><v>1</v></c></row></sheetData></worksheet>',
        )
    import base64

    (directory / "assets").mkdir()
    (directory / "assets" / "sample.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII="
        )
    )
    if args.include_log:
        atomic(directory / "execution.log", "Mock execution completed\n")


if __name__ == "__main__":
    main()
