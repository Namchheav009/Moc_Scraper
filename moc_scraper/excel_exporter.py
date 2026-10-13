"""Export public MOC business records without changing their contact details."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.styles import Font


COLUMNS = ["No.", "Company Name", "Phone", "Email", "Registration Date"]
COLUMN_WIDTHS = (8, 45, 22, 35, 20)
MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


def _text(value: Any) -> str:
    """Retain source text, including phone prefixes and leading zeroes."""
    if value is None or value is pd.NA or value is pd.NaT:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


def _parse_date(value: Any) -> date | None:
    if value is pd.NaT or value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(_text(value).translate(_KHMER_DIGITS).split())
    if not text:
        return None

    # Numeric source dates are day-first; ISO dates are unambiguous.
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?", text)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", text)
        if match:
            day, month, year = map(int, match.groups())
        else:
            # Use English month names explicitly so output does not depend on
            # the Windows locale of the machine running the scraper.
            match = re.fullmatch(r"(\d{1,2})[\s\-/]+([A-Za-z]+)[\s,\-/]+(\d{2}|\d{4})", text)
            if match:
                day_text, month_text, year_text = match.groups()
            else:
                match = re.fullmatch(r"([A-Za-z]+)[\s\-/]+(\d{1,2})[\s,\-/]+(\d{2}|\d{4})", text)
                if not match:
                    return None
                month_text, day_text, year_text = match.groups()
            month_names = {name.lower(): i for i, name in enumerate(MONTHS, 1)}
            month = month_names.get(month_text[:3].lower(), 0)
            day, year = int(day_text), int(year_text)
        if year < 100:
            year += 2000 if year <= 68 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_date(value: Any) -> str:
    """Return DD-MMM-YY, retaining unrecognized source text instead of losing it."""
    parsed = _parse_date(value)
    if parsed is None:
        return _text(value)
    return f"{parsed.day:02d}-{MONTHS[parsed.month - 1]}-{parsed.year % 100:02d}"


def export_results(
    records: Iterable[Mapping[str, Any]], output_dir: str | Path = ".",
    *, basename: str = "MOC_Businesses",
) -> tuple[Path, Path]:
    """Write MOC_Businesses.xlsx and a UTF-8 CSV backup; return their paths.

    Input keys are the exact names in COLUMNS. Missing fields remain blank,
    except missing record numbers, which receive their one-based position.
    Excel strings are explicitly stored as text, even when beginning with '='.
    CSV keeps original contact text; import its columns as text in spreadsheet
    applications to retain phone prefixes/zeroes and avoid formula evaluation.
    """
    if not basename or basename in {".", ".."} or any(character in basename for character in '/\\:'):
        raise ValueError("basename must be a filename without directory components.")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    excel_path = destination / f"{basename}.xlsx"
    csv_path = destination / f"{basename}.csv"

    rows: list[dict[str, Any]] = []
    parsed_dates: list[date | None] = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise TypeError("Each business record must be a mapping of column names to values.")
        row = {column: _text(record.get(column)) for column in COLUMNS}
        supplied_number = record.get("No.")
        row["No."] = supplied_number if row["No."] else index
        row["Registration Date"] = normalize_date(record.get("Registration Date"))
        rows.append(row)
        parsed_dates.append(_parse_date(record.get("Registration Date")))

    frame = pd.DataFrame(rows, columns=COLUMNS)
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Businesses")
        sheet = writer.sheets["Businesses"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:E{max(len(rows) + 1, 1)}"
        for cell, width in zip(sheet[1], COLUMN_WIDTHS):
            cell.font = Font(bold=True)
            sheet.column_dimensions[cell.column_letter].width = width
        for row_index, row in enumerate(rows, 2):
            for column_index, column in enumerate(COLUMNS, 1):
                cell = sheet.cell(row_index, column_index)
                value = row[column]
                if column == "Registration Date":
                    parsed = parsed_dates[row_index - 2]
                    if parsed is not None:
                        cell.value = parsed
                        cell.number_format = "DD-MMM-YY"
                        continue
                if isinstance(value, str):
                    cell.value = value
                    # Assign after value: openpyxl otherwise treats '=...' as
                    # a formula, even when its display format is text.
                    cell.data_type = "s"
                if column in {"Phone", "Email"}:
                    cell.number_format = "@"

    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return excel_path, csv_path
