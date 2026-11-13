"""Two-stage visual benchmark stand-in. No model calls, media generation or fees."""

import argparse
import json
import time
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZipFile

NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
HEADERS = ["样本ID", "任务类型", "提示词 / 指令", "要求编号", "要求内容", "级别", "评测维度",
           "要求结论", "维度等级", "判定依据 / 工具输出", "问题描述 / 证据位置", "关联验证点", "复核记录", "判定标准"]


def make_workbook(destination, count=1):
    records = []
    for sample in range(1, count + 1):
        for number, (requirement, level, dimension) in enumerate([
            ("画面中包含一只猫", "关键", "主体还原度"),
            ("猫的颜色为白色", "次要", "颜色准确性"),
            ("正常观看时画面清晰", "固定加评", "成像质量"),
        ], 1):
            records.append([f"IMG-{sample:02}", "文生图", "生成一只白猫的图片（Mock）", f"R{number:02}",
                            requirement, level, dimension, "", "", "", "", "", "",
                            "根据生成结果判断；Mock 数据不代表真实模型效果。"])
    sheets = [("使用说明", [["本文件为每次 Mock 运行独立生成的评测记录。"]]),
              ("任务信息", [["生成阶段", "已完成，评分后只运行报告步骤"]]),
              ("②逐行评测记录", [["逐行填写评测结果"], [], [], HEADERS, *records]),
              ("③样本结论", [["由业务 report.py 汇总，此处不计算真实业务得分。"]])]
    with ZipFile(destination, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1)) + '</Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{NAMESPACE}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1)) + '</Relationships>')
        for index, (_, rows) in enumerate(sheets, 1):
            data = []
            for row, values in enumerate(rows, 1):
                data.append(f'<row r="{row}">' + ''.join(f'<c r="{chr(65+column)}{row}" t="inlineStr"><is><t>{escape(value)}</t></is></c>' for column,value in enumerate(values)) + '</row>')
            archive.writestr(f"xl/worksheets/sheet{index}.xml", f'<worksheet xmlns="{NAMESPACE}"><sheetData>' + ''.join(data) + '</sheetData></worksheet>')


def report(workbook):
    namespace = {"s": NAMESPACE}
    with ZipFile(workbook) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
    rows = []
    for row in root.findall("s:sheetData/s:row", namespace):
        if int(row.get("r")) <= 4:
            continue
        values = {c.get("r").rstrip("0123456789"): ''.join(c.find("s:is", namespace).itertext())
                  for c in row.findall("s:c", namespace) if c.find("s:is", namespace) is not None}
        score = values.get("I" if values.get("F") == "固定加评" else "H", "")
        if not score or score == "待复核":
            raise SystemExit("Missing human review")
        rows.append(f'| {values.get("A")} | {values.get("E")} | {score} |')
    (workbook.parent / "report.md").write_text(
        '# Video Creator 人工评测报告（Mock）\n\n'
        '本次仅执行报告生成，未重新生成图片或视频。此报告不代表真实业务评分口径。\n\n'
        '| 样本 | 评测要求 | 人工结果 |\n| --- | --- | --- |\n' + '\n'.join(rows)
        + '\n\n[已评分工作簿](reviewed.xlsx)\n', encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--eval", type=Path)
    parser.add_argument("--benchmark")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--progress-file", type=Path)
    args = parser.parse_args()
    if args.eval:
        report(args.eval)
        return
    if not args.output_dir:
        parser.error("output directory required")
    make_workbook(args.output_dir / "评测记录.xlsx", 2 if args.benchmark == "two-samples" else 1)
    (args.output_dir / "report.md").write_text('# Video Creator 生成阶段（Mock）\n\n生成已完成，请在 Edison 填写评测记录。\n', encoding="utf-8")
    (args.output_dir / "review.required").write_text("Human scoring required", encoding="utf-8")
    if args.progress_file:
        count = 2 if args.benchmark == "two-samples" else 1
        args.progress_file.write_text(json.dumps({"stage": "生成完成，等待人工评分", "completed": count, "total": count}), encoding="utf-8")
        time.sleep(0.25)


if __name__ == "__main__":
    main()
