"""Build the API PDF from its maintained Markdown source (requires reportlab).

Use a Python documentation environment with reportlab installed; this is not an
API runtime dependency. --check checks route coverage without importing reportlab.
"""

import argparse
import ast
import re
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/api-reference.md"
TARGET = ROOT / "output/pdf/vocard-api-documentation.pdf"


def check_routes(source):
    for filename, prefix in [("api.py", ""), ("student_api.py", "/ai"), ("chat.py", "")]:
        module = ast.parse((ROOT / "vocard" / filename).read_text())
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Attribute)
                    or decorator.func.attr not in {"get", "post", "put", "patch", "delete"}
                    or not decorator.args
                    or not isinstance(decorator.args[0], ast.Constant)
                ):
                    continue
                route = f"{decorator.func.attr.upper()} {prefix}{decorator.args[0].value}"
                if route not in source:
                    raise ValueError(f"API documentation is missing {route}")


def build(source, font):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    pdfmetrics.registerFont(TTFont("VocardText", str(font)))
    pdfmetrics.registerFontFamily(
        "VocardText",
        normal="VocardText",
        bold="VocardText",
        italic="VocardText",
        boldItalic="VocardText",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "VocardBody",
        fontName="VocardText",
        fontSize=9.4,
        leading=13.2,
        textColor=colors.HexColor("#293348"),
        spaceAfter=8,
        splitLongWords=True,
        alignment=TA_LEFT,
    )
    heading = ParagraphStyle(
        "VocardHeading",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=29,
        spaceAfter=20,
        textColor=colors.HexColor("#152743"),
    )
    subheading = ParagraphStyle(
        "VocardSubheading", parent=heading, fontSize=12.5, leading=17, spaceBefore=10, spaceAfter=7
    )
    cell = ParagraphStyle("VocardCell", parent=body, fontSize=8.7, leading=11.5, spaceAfter=0)
    code = ParagraphStyle(
        "VocardCode",
        parent=body,
        fontSize=8.3,
        leading=11.5,
        backColor=colors.HexColor("#F1F4F9"),
        borderPadding=10,
        spaceBefore=5,
        spaceAfter=13,
    )

    def inline(text):
        value = escape(text)
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
        return re.sub(r"`([^`]+)`", r'<font color="#405E89">\1</font>', value)

    story = []
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line == "<!-- page -->":
            story.append(PageBreak())
        elif line.startswith("# "):
            story.append(Paragraph(inline(line[2:]), heading))
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), subheading))
        elif line.startswith("```"):
            content = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                content.append(lines[i])
                i += 1
            story.append(
                Preformatted(
                    "\n".join(content), code, maxLineLength=82, splitChars=" ", newLineChars="  "
                )
            )
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                values = [v.strip() for v in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", v) for v in values):
                    rows.append([Paragraph(inline(v), cell) for v in values])
                i += 1
            i -= 1
            width = A4[0] - 96
            table = Table(rows, colWidths=[width * 0.43, width * 0.57], repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E4ECF7")),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#F6F8FB")],
                        ),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 5.5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#B9CBE4")),
                    ]
                )
            )
            story.extend([table, Spacer(1, 13)])
        elif line.startswith("- "):
            story.append(Paragraph("• " + inline(line[2:]), body))
        else:
            content = [line]
            while (
                i + 1 < len(lines)
                and lines[i + 1].strip()
                and not lines[i + 1].startswith(("#", "- ", "|", "```", "<!--"))
            ):
                i += 1
                content.append(lines[i].strip())
            story.append(Paragraph(inline(" ".join(content)), body))
        i += 1

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D5DFED"))
        canvas.line(48, A4[1] - 43, A4[0] - 48, A4[1] - 43)
        canvas.setFillColor(colors.HexColor("#63738B"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(48, A4[1] - 33, "VOCARD / Developer API reference / 17 Oct 2026")
        canvas.drawString(48, 30, "Stateless grammar Q&A + vocabulary tutoring")
        canvas.drawRightString(A4[0] - 48, 30, str(doc.page))
        canvas.restoreState()

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(TARGET),
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=66,
        bottomMargin=52,
        title="Vocard API documentation",
        author="Vocard",
        pageCompression=1,
    )
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    print(TARGET)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check HTTP route documentation only")
    parser.add_argument("--font", type=Path, help="Korean-capable TrueType font")
    args = parser.parse_args()
    source = SOURCE.read_text()
    check_routes(source)
    if args.check:
        print("All current HTTP routes are documented")
        return
    candidates = [
        args.font,
        Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    ]
    font = next((p for p in candidates if p is not None and p.is_file()), None)
    if font is None:
        parser.error("Pass --font with a Korean-capable TrueType font")
    build(source, font)


if __name__ == "__main__":
    main()
