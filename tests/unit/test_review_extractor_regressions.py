from datetime import datetime, time, timedelta

import openpyxl
import pytest
from selectolax.parser import HTMLParser

from adapters.extractors.base import ExtractorError, canonical_bytes
from adapters.extractors.html import HtmlExtractor
from adapters.extractors.spreadsheet import SpreadsheetExtractor
from apps.evidence.models import validate_evidence_locator


@pytest.mark.parametrize(
    "markup",
    [
        "<h1>Title</h1><p>First fact</p><p>Second fact</p>",
        "<section><p>First fact</p></section><section><p>Second fact</p></section>",
        '<p id="same">First fact</p><p id="same">Second fact</p>',
        '<p id="123">First fact</p><p id="has:colon">Second fact</p>',
    ],
)
def test_html_evidence_selectors_resolve_only_their_original_node(tmp_path, markup):
    source = tmp_path / "notice.html"
    source.write_text(markup, encoding="utf-8")
    output = HtmlExtractor().extract(source)
    original = HTMLParser(markup)
    assert output.records
    for record in output.records:
        matches = original.css(record.locator["css_selector"])
        assert len(matches) == 1
        assert matches[0].text(separator=" ", strip=True) == record.text
        validate_evidence_locator(record.locator_type, record.locator)


def test_xlsx_native_date_time_and_duration_cells_are_canonical_json(tmp_path):
    source = tmp_path / "notice.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([datetime(2026, 10, 3), time(9, 30), timedelta(days=2, hours=3), True, 42])
    sheet["C1"].number_format = "[h]:mm:ss"
    workbook.save(source)
    workbook.close()
    output = SpreadsheetExtractor().extract(source)
    encoded = canonical_bytes(output.as_dict())
    assert b"2026-10-03T00:00:00" in encoded
    assert [cell["value"] for cell in output.records[0].structured_data["cells"]] == [
        "2026-10-03T00:00:00",
        "09:30:00",
        "2 days, 3:00:00",
        True,
        42,
    ]


@pytest.mark.parametrize("suffix", [".csv", ".tsv", ".xlsx"])
def test_spreadsheet_output_locators_satisfy_the_persistence_contract(tmp_path, suffix):
    source = tmp_path / ("notice" + suffix)
    if suffix == ".xlsx":
        workbook = openpyxl.Workbook()
        workbook.active.append(["Official fact", 42])
        workbook.save(source)
        workbook.close()
    else:
        source.write_text("Official fact" + ("\t" if suffix == ".tsv" else ",") + "42\n")
    output = SpreadsheetExtractor().extract(source)
    assert len(output.records) == 1
    for record in output.records:
        validate_evidence_locator(record.locator_type, record.locator)
        assert record.locator["cell_range"] == "A1:B1"


@pytest.mark.parametrize("separator,suffix", [(",", ".csv"), ("\t", ".tsv")])
def test_delimited_cell_limit_counts_all_ragged_rows(tmp_path, separator, suffix):
    source = tmp_path / ("ragged" + suffix)
    source.write_text(separator.join(["value"] * 10) + "\nextra\n")
    with pytest.raises(ExtractorError, match="cells"):
        SpreadsheetExtractor({"max_cells": 10}).extract(source)
    source.write_text("one\n" + separator.join(["value"] * 6) + "\n")
    assert len(SpreadsheetExtractor({"max_cells": 10}).extract(source).records) == 2


def test_blank_csv_rows_do_not_allocate_evidence_and_keep_physical_locators(tmp_path):
    source = tmp_path / "blank-rows.csv"
    source.write_text("\n\nfirst\n\nsecond\n")
    output = SpreadsheetExtractor({"max_cells": 2}).extract(source)
    assert [record.text for record in output.records] == ["first", "second"]
    assert [record.locator["cell_range"] for record in output.records] == ["A3:A3", "A5:A5"]
