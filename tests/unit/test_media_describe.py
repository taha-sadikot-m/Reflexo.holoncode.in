"""Deterministic spreadsheet descriptions for the owner's file library (POC_04 §8.2)."""

from __future__ import annotations

import io

from openpyxl import Workbook

from reflexo.agent.media_describe import summarize_spreadsheet

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(rows: list[list[object]], sheets: int = 1) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"
    for r in rows:
        ws.append(r)
    for i in range(1, sheets):
        wb.create_sheet(f"S{i}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_summary_has_header_count_and_first_rows() -> None:
    content = _xlsx(
        [["Item", "Price"], ["Chocolate Truffle 1kg", 999], [None, None], ["Brownies", 450], ["Lemon Tart", 380]]
    )
    assert summarize_spreadsheet(content, XLSX, "prices_oct.xlsx") == (
        'Excel sheet "Prices": 3 rows: Item, Price; Chocolate Truffle 1kg 999; Brownies 450; Lemon Tart 380'
    )


def test_more_sheets_csv_old_xls_and_empty() -> None:
    assert 'sheet "Prices (+2 more sheets)"' in summarize_spreadsheet(_xlsx([["A"], ["1"]], sheets=3), XLSX)
    csv_text = b"Item,Price\nRed Velvet 1kg,1100\n"
    assert summarize_spreadsheet(csv_text, "text/csv") == "CSV: 1 rows: Item, Price; Red Velvet 1kg 1100"
    assert "old .xls" in summarize_spreadsheet(b"\xd0\xcf", "application/vnd.ms-excel", "menu.xls")
    assert summarize_spreadsheet(b"not a zip", XLSX, "x.xlsx") == "Excel file (could not be read)"
    assert summarize_spreadsheet(_xlsx([]), XLSX) == 'Excel sheet "Prices": empty'
