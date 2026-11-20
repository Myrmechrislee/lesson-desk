from io import BytesIO
from html import escape
from pathlib import Path
import re

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .core import day_label, money, stamp, zone

INK = colors.HexColor("#1b2d28")
GREEN = colors.HexColor("#235d4b")
GRAY = colors.HexColor("#576961")
LINE = colors.HexColor("#dce4dc")
WASH = colors.HexColor("#edf3ed")
CJK = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff]+")

FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("InvoiceSans", str(FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("InvoiceSansBold", str(FONT_DIR / "VeraBd.ttf")))
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def para(value, style):
    safe = escape(str(value)).replace("\n", "<br/>")
    safe = CJK.sub(lambda match: '<font name="STSong-Light">' + match.group() + '</font>', safe)
    return Paragraph(safe, style)


def invoice_pdf(invoice):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=46, rightMargin=46, topMargin=44, bottomMargin=47,
        title=f"Invoice {invoice['number']}", author=invoice["supplier"]["name"],
    )
    body = ParagraphStyle("body", fontName="InvoiceSans", fontSize=10, leading=15, textColor=INK)
    small = ParagraphStyle("small", parent=body, fontSize=9, leading=13, textColor=GRAY)
    heading = ParagraphStyle("heading", parent=body, fontName="InvoiceSansBold", fontSize=36, leading=43)
    label = ParagraphStyle("label", parent=small, fontName="InvoiceSansBold", textColor=GREEN)
    bold = ParagraphStyle("bold", parent=body, fontName="InvoiceSansBold")
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)
    total_style = ParagraphStyle("total", parent=right, fontName="InvoiceSansBold", fontSize=15, leading=21)
    supplier = invoice["supplier"]
    recipient = invoice["recipient"]
    width = doc.width
    issued = stamp(invoice["issued_at"], "date")
    due = day_label(invoice["due_on"])
    abn = " ".join((supplier["abn"][:2], supplier["abn"][2:5], supplier["abn"][5:8], supplier["abn"][8:]))
    meta = Table([
        [para(invoice["number"], label), para("ISSUED", label), para("DUE", label)],
        [para("AUD", small), para(issued, body), para(due, body)],
    ], colWidths=[width * .44, width * .28, width * .28])
    meta.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WASH),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
    ]))
    from_values = [para("FROM", label), para(supplier["name"], bold), para(f"ABN {abn}", body)]
    from_values.extend(para(supplier[key], small) for key in ("email", "phone", "address") if supplier.get(key))
    to_values = [para("BILL TO", label), para(recipient["name"], bold), para(recipient["email"], small)]
    contacts = Table([[from_values, to_values]], colWidths=[width * .55, width * .45])
    contacts.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))

    rows = [[para("STUDENT", label), para("LESSON TIME", label), para("AUD", right)]]
    for line in invoice["lines"]:
        if not line.get("lesson_id") and line.get("description"):
            rows.append([para(line["description"], body), para("Adjustment", small), para(money(line["amount_cents"]), right)])
            continue
        start = line["start_at"].astimezone(zone())
        end = line["end_at"].astimezone(zone())
        end_text = stamp(line["end_at"], "time") if start.date() == end.date() else stamp(line["end_at"], "full")
        rows.append([
            [para(line["student"], body), para(line["topic"], small)] if line.get("topic") else para(line["student"], body),
            para(f"{start:%d %b %Y}  |  {stamp(line['start_at'], 'time')}–{end_text}", body),
            para(money(line["amount_cents"]), right),
        ])
    table = Table(rows, colWidths=[width * .34, width * .46, width * .20], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), WASH),
        ("LINEBELOW", (0, 0), (-1, -1), .5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    total = Table(
        [[para("Total due (AUD)", bold), para(money(invoice["total_cents"]), total_style)]],
        colWidths=[width * .72, width * .28],
    )
    total.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, 0), (-1, 0), 1, GREEN),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story = [
        para("Invoice", heading), Spacer(1, 12), meta, Spacer(1, 25), contacts,
        Spacer(1, 26), para(f"Lesson times · {zone().key}", small), Spacer(1, 6), table,
        Spacer(1, 15), total,
    ]
    if supplier.get("payment"):
        story.extend([Spacer(1, 18), para("PAYMENT", label), para(supplier["payment"], body)])
    if invoice.get("note"):
        story.extend([Spacer(1, 18), para("NOTE", label), para(invoice["note"], body)])

    def footer(canvas, page):
        canvas.setStrokeColor(LINE)
        canvas.line(page.leftMargin, 34, A4[0] - page.rightMargin, 34)
        canvas.setFont("InvoiceSans", 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(page.leftMargin, 21, invoice["number"])
        canvas.drawRightString(A4[0] - page.rightMargin, 21, str(page.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
