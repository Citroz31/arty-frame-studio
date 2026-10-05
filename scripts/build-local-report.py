#!/usr/bin/env python3
"""Build the French local diagnostic PDF from its Markdown source.

Document-only dependency: reportlab. Application dependencies are unchanged.
Use --font-dir if DejaVu fonts are not installed in a conventional directory.
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PROJECT = Path(__file__).resolve().parents[1]
ACCENT = colors.HexColor("#155E75")
INK = colors.HexColor("#172B3A")
MUTED = colors.HexColor("#536878")


def register_fonts(font_dir: Path | None) -> None:
    candidates = [font_dir] if font_dir else []
    candidates += [
        PROJECT / "docs" / "fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/local/share/fonts/dejavu"),
    ]
    directory = next((p for p in candidates if p and (p / "DejaVuSans.ttf").is_file()), None)
    if directory is None:
        raise SystemExit("Polices DejaVu absentes : préciser --font-dir.")
    for name, filename in (
        ("DejaVu", "DejaVuSans.ttf"),
        ("DejaVu-Bold", "DejaVuSans-Bold.ttf"),
        ("DejaVu-Oblique", "DejaVuSans-Oblique.ttf"),
        ("DejaVu-BoldOblique", "DejaVuSans-BoldOblique.ttf"),
        ("DejaVuMono", "DejaVuSansMono.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(directory / filename)))
    pdfmetrics.registerFontFamily(
        "DejaVu",
        normal="DejaVu",
        bold="DejaVu-Bold",
        italic="DejaVu-Oblique",
        boldItalic="DejaVu-BoldOblique",
    )


def inline(markdown: str) -> str:
    result = html.escape(markdown, quote=False)
    result = re.sub(r"`([^`]+)`", r'<font name="DejaVuMono">\1</font>', result)
    result = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", result)

    def link(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith("https://"):
            return f'<link href="{target}" color="#155E75">{label}</link>'
        return label

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, result)


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    common = dict(fontName="DejaVu", textColor=INK, alignment=TA_LEFT)
    return {
        "body": ParagraphStyle(
            "Body", parent=base, fontSize=9, leading=13.2, spaceAfter=7.5, **common
        ),
        "title": ParagraphStyle(
            "Title",
            parent=base,
            fontSize=22,
            leading=27,
            spaceAfter=13,
            fontName="DejaVu-Bold",
            textColor=INK,
        ),
        "h2": ParagraphStyle(
            "Section",
            parent=base,
            fontSize=15,
            leading=20,
            spaceBefore=5,
            spaceAfter=11,
            keepWithNext=True,
            fontName="DejaVu-Bold",
            textColor=ACCENT,
        ),
        "h3": ParagraphStyle(
            "Subsection",
            parent=base,
            fontSize=10.6,
            leading=14.5,
            spaceBefore=9,
            spaceAfter=6,
            keepWithNext=True,
            fontName="DejaVu-Bold",
            textColor=INK,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base,
            fontSize=9,
            leading=13.2,
            leftIndent=11,
            firstLineIndent=-9,
            spaceAfter=5.5,
            **common,
        ),
        "cell": ParagraphStyle("Cell", parent=base, fontSize=8.05, leading=11.5, **common),
        "cellhead": ParagraphStyle(
            "CellHead",
            parent=base,
            fontSize=8.2,
            leading=11.5,
            fontName="DejaVu-Bold",
            textColor=colors.white,
        ),
        "code": ParagraphStyle(
            "Code",
            parent=base,
            fontName="DejaVuMono",
            fontSize=7.5,
            leading=11.5,
            textColor=INK,
            spaceAfter=0,
            splitLongWords=True,
        ),
    }


def make_table(lines: list[str], width: float, style: dict[str, ParagraphStyle]) -> Table:
    rows = [line.strip().strip("|").split("|") for line in lines]
    rows = [rows[0], *rows[2:]]
    count = len(rows[0])
    proportions = [0.29, 0.34, 0.37] if count == 3 else [0.31, 0.69]
    if count != len(proportions):
        proportions = [1 / count] * count
    data = [
        [Paragraph(inline(cell.strip()), style["cellhead" if i == 0 else "cell"]) for cell in row]
        for i, row in enumerate(rows)
    ]
    table = Table(data, colWidths=[width * p for p in proportions], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F0F5F7"), colors.white]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, ACCENT),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#D7E1E6")),
            ]
        )
    )
    table.spaceAfter = 10
    return table


def parse(source: str, width: float, style: dict[str, ParagraphStyle]) -> list:
    lines = source.splitlines()
    story = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
        elif line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            text = "<br/>".join(html.escape(row, quote=False) or " " for row in code)
            block = Table([[Paragraph(text, style["code"])]], colWidths=[width])
            block.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EDF3F5")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CFDFE6")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 9),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story += [KeepTogether([block]), Spacer(1, 9)]
        elif line.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            story.append(make_table(table_lines, width, style))
            continue
        elif line.startswith("# "):
            story += [
                Paragraph(inline(line[2:]), style["title"]),
                HRFlowable(width="100%", thickness=2, color=ACCENT, spaceAfter=13),
            ]
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), style["h2"]))
        elif line.startswith("### "):
            story.append(Paragraph(inline(line[4:]), style["h3"]))
        elif line.startswith("- "):
            story.append(Paragraph("• " + inline(line[2:]), style["bullet"]))
        elif re.match(r"\d+\. ", line):
            story.append(Paragraph(inline(line), style["bullet"]))
        else:
            paragraph = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                paragraph.append(lines[index].strip())
                index += 1
            story.append(Paragraph(inline(" ".join(paragraph)), style["body"]))
            continue
        index += 1
    return story


def page_footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(colors.HexColor("#CBD8DE"))
    canvas.line(doc.leftMargin, 15 * mm, width - doc.rightMargin, 15 * mm)
    canvas.setFont("DejaVu", 7.2)
    canvas.setFillColor(MUTED)
    canvas.drawString(
        doc.leftMargin, 10.5 * mm, "ARTY FRAME STUDIO · DIAGNOSTIC LOCAL · 05.10.2026"
    )
    canvas.drawRightString(width - doc.rightMargin, 10.5 * mm, str(doc.page))
    if doc.page > 1:
        canvas.setFont("DejaVu", 7)
        canvas.drawString(
            doc.leftMargin,
            height - 11 * mm,
            "Revue technique · Windows natif / UART / Oscilloscope",
        )
    canvas.restoreState()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-dir", type=Path)
    parser.add_argument("--source", type=Path, default=PROJECT / "docs/rapport-diagnostic-local.md")
    parser.add_argument(
        "--output", type=Path, default=PROJECT / "docs/rapport-diagnostic-local.pdf"
    )
    args = parser.parse_args()
    register_fonts(args.font_dir)
    doc = SimpleDocTemplate(
        str(args.output),
        pagesize=A4,
        leftMargin=19 * mm,
        rightMargin=19 * mm,
        topMargin=18 * mm,
        bottomMargin=21 * mm,
        title="Diagnostic local — Arty Frame Studio",
        author="Arty Frame Studio",
        subject="Revue locale Windows, UART et DSOX1202A",
    )
    doc.build(
        parse(args.source.read_text(encoding="utf-8"), doc.width, styles()),
        onFirstPage=page_footer,
        onLaterPages=page_footer,
    )
    print(f"PDF généré : {args.output}")


if __name__ == "__main__":
    main()
