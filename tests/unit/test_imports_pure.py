"""Import extraction rules (POC_05 §3.2–3.3): price normalization, hallucination check, spreadsheet parsing."""

from __future__ import annotations

import asyncio
import io
from uuid import uuid4

import pytest
from openpyxl import Workbook

from reflexo.knowledge.imports import (
    ExtractedItem,
    ExtractedVariant,
    ImportFailed,
    extract_spreadsheet,
    normalize_price,
    same_variant,
    validate,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize(
    ("raw", "paise", "issue"),
    [
        ("₹1,499", 149900, None),
        ("Rs. 1499/-", 149900, None),
        ("1499 rs", 149900, None),
        ("1.5k", 150000, None),
        ("₹1,10,000", 11000000, None),
        ("९५०", 95000, None),
        (999, 99900, None),
        ("500-700", None, "price_range"),
        ("", None, "missing_price"),
        ("on request", None, "missing_price"),
        ("0", None, "price_out_of_range"),
        ("₹20,00,000", None, "price_out_of_range"),
    ],
)
def test_normalize_price(raw: str | int, paise: int | None, issue: str | None) -> None:
    assert normalize_price(raw) == (paise, issue)


def test_validate_flags_hallucinated_missing_and_duplicate_prices() -> None:
    items = [
        ExtractedItem(
            name="Truffle", variants=[ExtractedVariant(name="1 kg", price_text="₹950")], source_text="Truffle 1kg 950"
        ),
        ExtractedItem(
            name="Red Velvet",
            variants=[ExtractedVariant(name="1 kg", price_text="₹1,100")],
            source_text="Red Velvet 1kg",
        ),
        ExtractedItem(name="Custom Photo Cake", price_on_request=True, source_text="Custom Photo Cake"),
        ExtractedItem(name="truffle", variants=[ExtractedVariant(price_text="950")], source_text="truffle 950"),
    ]
    checked = {i.name: issues for i, issues in validate(items, visual=True)}
    assert checked == {
        "Truffle": [],
        "Red Velvet": ["price_not_in_source"],  # 1100 is not in its own source text
        "Custom Photo Cake": ["missing_price"],
        "truffle": ["possible_duplicate"],
    }
    assert items[0].variants[0].price_paise == 95000


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_spreadsheet_columns_detected_and_sizes_grouped() -> None:
    content = _xlsx(
        [
            ["Meera's Bakes — price list Oct", None, None],
            ["Item", "Size", "Price (₹)"],
            ["Chocolate Truffle", "500 g", 550],
            ["Chocolate Truffle", "1 kg", 999],
            ["Lemon Tart", None, "380"],
        ]
    )
    items = asyncio.run(extract_spreadsheet(uuid4(), content, XLSX, "prices.xlsx"))
    assert [(i.name, [(v.name, v.price_text) for v in i.variants]) for i in items] == [
        ("Chocolate Truffle", [("500 g", "550"), ("1 kg", "999")]),
        ("Lemon Tart", [("default", "380")]),
    ]
    assert "Chocolate Truffle | 1 kg | 999" in items[0].source_text


def test_unreadable_files_give_owner_facing_errors() -> None:
    with pytest.raises(ImportFailed, match=r".xlsx or CSV"):
        asyncio.run(extract_spreadsheet(uuid4(), b"x", "application/vnd.ms-excel", "menu.xls"))
    with pytest.raises(ImportFailed, match="password"):
        asyncio.run(extract_spreadsheet(uuid4(), b"not a zip", XLSX, "menu.xlsx"))


def test_variant_names_match_sizes_and_packs() -> None:
    assert same_variant("1kg", "1 kg") and same_variant("500 g", "500g")
    assert same_variant("Box of 6", "box of 6") and same_variant("Jar of 12", "JAR OF 12")
    assert not same_variant("1 kg", "500 g") and not same_variant("Box of 6", "Box of 12")
    assert not same_variant("default", "default")
