#!/usr/bin/env python3
"""Supplier Price-List CSV Diff & Margin Impact CLI (Lite).

Zero-dependency Python 3.9+ stdlib tool for e-commerce and procurement teams
to compare old and new supplier CSV/TSV price lists by SKU.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SKU_ALIASES = (
    "sku",
    "article",
    "articul",
    "code",
    "item_code",
    "product_code",
    "part_number",
    "id",
    "артикул",
    "код",
)

PRICE_ALIASES = (
    "price",
    "cost",
    "unit_price",
    "wholesale_price",
    "supplier_price",
    "buy_price",
    "цена",
    "закупочная_цена",
)

NAME_ALIASES = (
    "name",
    "title",
    "product",
    "product_name",
    "description",
    "наименование",
    "товар",
    "название",
)

RETAIL_ALIASES = (
    "retail_price",
    "rrp",
    "msrp",
    "sell_price",
    "sale_price",
    "розничная_цена",
    "ррц",
)

_CURRENCY_RE = re.compile(
    r"(?i)(?:usd|eur|rub|rur|uah|kzt|[$€£₽¥₴₸])"
)
_US_THOUSANDS_RE = re.compile(r"^[+-]?[1-9]\d{0,2}(?:,\d{3})+$")
_DOT_THOUSANDS_RE = re.compile(r"^[+-]?[1-9]\d{0,2}(?:\.\d{3})+$")


@dataclass
class PriceRow:
    sku: str
    name: str
    price: float
    retail_price: Optional[float] = None


@dataclass
class ParsedTable:
    rows: Dict[str, PriceRow]
    encoding: str
    delimiter: str
    total_rows: int
    duplicate_skus: List[str]
    skipped_rows: int
    has_retail: bool


@dataclass
class DiffItem:
    sku: str
    name: str
    status: str  # increased | decreased | added | removed | unchanged
    old_price: Optional[float]
    new_price: Optional[float]
    delta_abs: Optional[float]
    delta_pct: Optional[float]
    retail_price: Optional[float]
    old_margin_pct: Optional[float]
    new_margin_pct: Optional[float]
    margin_delta_pp: Optional[float]


@dataclass
class DiffReport:
    meta: Dict[str, Any]
    summary: Dict[str, Any]
    items: List[DiffItem]


def detect_and_decode(raw_bytes: bytes, encoding_override: str = "auto") -> Tuple[str, str]:
    """Decode bytes using BOM / UTF-8 / CP1251 auto-detection."""
    if encoding_override != "auto":
        try:
            return raw_bytes.decode(encoding_override), encoding_override
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Failed to decode file with requested encoding '{encoding_override}': {exc}"
            ) from exc

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return raw_bytes.decode("utf-8-sig"), "utf-8-sig"
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw_bytes.decode("utf-16"), "utf-16"

    try:
        return raw_bytes.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        return raw_bytes.decode("cp1251"), "cp1251"
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1251", errors="replace"), "cp1251"


def detect_delimiter(text: str, delimiter_override: str = "auto") -> str:
    """Auto-detect delimiter among ',', ';', and '\\t'."""
    if delimiter_override != "auto":
        if delimiter_override == "tab":
            return "\t"
        return delimiter_override

    sample_lines = [line for line in text.splitlines() if line.strip()][:15]
    if not sample_lines:
        return ","

    known_headers = set(SKU_ALIASES + PRICE_ALIASES + NAME_ALIASES + RETAIL_ALIASES)
    best_delim = ","
    best_score = -1.0

    for delim in (",", ";", "\t"):
        parsed = list(csv.reader(sample_lines, delimiter=delim))
        if not parsed:
            continue
        header = [col.strip().lower().lstrip("\ufeff") for col in parsed[0]]
        header_cols = len(header)
        if header_cols <= 1:
            continue

        counts = [len(row) for row in parsed]
        consistent_rows = sum(1 for c in counts if c == header_cols)
        consistency = consistent_rows / len(counts)
        header_hits = sum(1 for col in header if col in known_headers)

        score = (consistency * 100.0) + (header_hits * 25.0) + min(header_cols, 12)
        if score > best_score:
            best_score = score
            best_delim = delim

    if best_score > 0:
        return best_delim

    try:
        dialect = csv.Sniffer().sniff("\n".join(sample_lines), delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def parse_price(raw: Any) -> float:
    """Parse supplier price strings such as '$1,299.50', '1 250,50 ₽', or '49.90'."""
    if raw is None:
        raise ValueError("Empty price value")
    text = str(raw).strip()
    if not text:
        raise ValueError("Empty price value")

    cleaned = _CURRENCY_RE.sub("", text)
    cleaned = cleaned.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    cleaned = cleaned.strip()

    if not cleaned:
        raise ValueError(f"Invalid price value: {raw!r}")

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            # e.g. 1.250,50
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # e.g. 1,250.50
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        if _US_THOUSANDS_RE.match(cleaned):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    elif "." in cleaned and cleaned.count(".") > 1 and _DOT_THOUSANDS_RE.match(cleaned):
        cleaned = cleaned.replace(".", "")

    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric price {raw!r}") from exc

    if not math.isfinite(value):
        raise ValueError(f"Non-finite price {raw!r}")
    return value


def _resolve_column(
    headers: Sequence[str],
    explicit: Optional[str],
    aliases: Sequence[str],
    required: bool,
    role: str,
) -> Optional[int]:
    normalized = [h.strip().lower().lstrip("\ufeff") for h in headers]

    if explicit:
        target = explicit.strip().lower()
        for idx, name in enumerate(normalized):
            if name == target:
                return idx
        raise ValueError(f"Column '{explicit}' (for {role}) not found in headers: {list(headers)}")

    for alias in aliases:
        for idx, name in enumerate(normalized):
            if name == alias:
                return idx

    if required:
        raise ValueError(
            f"Could not find required {role} column in headers {list(headers)}. "
            f"Expected one of {list(aliases)} or pass --{role}-col."
        )
    return None


def load_price_csv(
    path: Path,
    *,
    sku_col: Optional[str] = None,
    price_col: Optional[str] = None,
    name_col: Optional[str] = None,
    retail_col: Optional[str] = None,
    encoding_override: str = "auto",
    delimiter_override: str = "auto",
) -> ParsedTable:
    """Load and normalize a supplier CSV/TSV price list."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    raw_bytes = path.read_bytes()
    text, detected_encoding = detect_and_decode(raw_bytes, encoding_override)
    detected_delimiter = detect_delimiter(text, delimiter_override)

    reader = csv.reader(io.StringIO(text), delimiter=detected_delimiter)
    rows_iter = iter(reader)

    headers: Optional[List[str]] = None
    for row in rows_iter:
        if any(cell.strip() for cell in row):
            headers = row
            break

    if not headers:
        raise ValueError(f"Price list file is empty: {path}")

    sku_idx = _resolve_column(headers, sku_col, SKU_ALIASES, required=True, role="sku")
    price_idx = _resolve_column(headers, price_col, PRICE_ALIASES, required=True, role="price")
    name_idx = _resolve_column(headers, name_col, NAME_ALIASES, required=False, role="name")
    retail_idx = _resolve_column(headers, retail_col, RETAIL_ALIASES, required=False, role="retail")

    assert sku_idx is not None and price_idx is not None

    records: Dict[str, PriceRow] = {}
    duplicate_skus: List[str] = []
    total_rows = 0
    skipped_rows = 0
    has_retail = False

    for row in rows_iter:
        if not any(cell.strip() for cell in row):
            continue
        total_rows += 1

        max_req_idx = max(sku_idx, price_idx)
        if len(row) <= max_req_idx:
            skipped_rows += 1
            continue

        sku = row[sku_idx].strip()
        if not sku:
            skipped_rows += 1
            continue

        try:
            price = parse_price(row[price_idx])
        except ValueError:
            skipped_rows += 1
            continue

        name = ""
        if name_idx is not None and name_idx < len(row):
            name = row[name_idx].strip()

        retail_price: Optional[float] = None
        if retail_idx is not None and retail_idx < len(row):
            raw_retail = row[retail_idx].strip()
            if raw_retail:
                try:
                    retail_price = parse_price(raw_retail)
                    has_retail = True
                except ValueError:
                    retail_price = None

        if sku in records:
            duplicate_skus.append(sku)

        records[sku] = PriceRow(
            sku=sku,
            name=name,
            price=price,
            retail_price=retail_price,
        )

    return ParsedTable(
        rows=records,
        encoding=detected_encoding,
        delimiter=detected_delimiter,
        total_rows=total_rows,
        duplicate_skus=duplicate_skus,
        skipped_rows=skipped_rows,
        has_retail=has_retail,
    )


def _calc_margin_pct(retail_price: Optional[float], cost_price: Optional[float]) -> Optional[float]:
    if retail_price is None or cost_price is None or retail_price <= 0:
        return None
    return ((retail_price - cost_price) / retail_price) * 100.0


def _delimiter_label(delim: str) -> str:
    if delim == "\t":
        return "\\t"
    return delim


def compare_tables(
    old_table: ParsedTable,
    new_table: ParsedTable,
    *,
    min_pct: float = 0.0,
    include_unchanged: bool = False,
    sort_by: str = "pct",
    markup_pct: Optional[float] = None,
    old_label: str = "old_prices.csv",
    new_label: str = "new_prices.csv",
) -> DiffReport:
    """Compare two parsed price tables and compute deltas and margin impact."""
    if min_pct < 0:
        raise ValueError("--min-pct must be >= 0")

    old_rows = old_table.rows
    new_rows = new_table.rows

    all_skus = set(old_rows.keys()) | set(new_rows.keys())

    counts = {
        "added": 0,
        "removed": 0,
        "increased": 0,
        "decreased": 0,
        "unchanged": 0,
        "filtered_changes": 0,
    }

    matched_old_total = 0.0
    matched_new_total = 0.0
    matched_retail_total = 0.0
    matched_retail_old_cost = 0.0
    matched_retail_new_cost = 0.0
    matched_with_retail_count = 0

    delta_pcts: List[float] = []
    old_margins: List[float] = []
    new_margins: List[float] = []
    margin_deltas: List[float] = []

    items: List[DiffItem] = []

    for sku in sorted(all_skus):
        old_row = old_rows.get(sku)
        new_row = new_rows.get(sku)

        if old_row is None and new_row is not None:
            counts["added"] += 1
            retail = new_row.retail_price
            if retail is None and markup_pct is not None:
                retail = new_row.price * (1.0 + markup_pct / 100.0)
            new_m = _calc_margin_pct(retail, new_row.price)
            items.append(
                DiffItem(
                    sku=sku,
                    name=new_row.name,
                    status="added",
                    old_price=None,
                    new_price=round(new_row.price, 4),
                    delta_abs=None,
                    delta_pct=None,
                    retail_price=round(retail, 4) if retail is not None else None,
                    old_margin_pct=None,
                    new_margin_pct=round(new_m, 2) if new_m is not None else None,
                    margin_delta_pp=None,
                )
            )
            continue

        if new_row is None and old_row is not None:
            counts["removed"] += 1
            retail = old_row.retail_price
            if retail is None and markup_pct is not None:
                retail = old_row.price * (1.0 + markup_pct / 100.0)
            old_m = _calc_margin_pct(retail, old_row.price)
            items.append(
                DiffItem(
                    sku=sku,
                    name=old_row.name,
                    status="removed",
                    old_price=round(old_row.price, 4),
                    new_price=None,
                    delta_abs=None,
                    delta_pct=None,
                    retail_price=round(retail, 4) if retail is not None else None,
                    old_margin_pct=round(old_m, 2) if old_m is not None else None,
                    new_margin_pct=None,
                    margin_delta_pp=None,
                )
            )
            continue

        assert old_row is not None and new_row is not None
        name = new_row.name or old_row.name
        old_p = old_row.price
        new_p = new_row.price
        delta_abs = new_p - old_p

        matched_old_total += old_p
        matched_new_total += new_p

        if abs(delta_abs) < 1e-9:
            status = "unchanged"
            delta_pct: Optional[float] = 0.0
            counts["unchanged"] += 1
        elif delta_abs > 0:
            status = "increased"
            delta_pct = ((delta_abs / old_p) * 100.0) if old_p != 0 else None
            counts["increased"] += 1
        else:
            status = "decreased"
            delta_pct = ((delta_abs / old_p) * 100.0) if old_p != 0 else None
            counts["decreased"] += 1

        if delta_pct is not None and status in ("increased", "decreased"):
            delta_pcts.append(delta_pct)
        elif status == "unchanged":
            delta_pcts.append(0.0)

        retail = new_row.retail_price if new_row.retail_price is not None else old_row.retail_price
        if retail is None and markup_pct is not None:
            retail = old_p * (1.0 + markup_pct / 100.0)

        old_m = _calc_margin_pct(retail, old_p)
        new_m = _calc_margin_pct(retail, new_p)
        m_delta: Optional[float] = None
        if old_m is not None and new_m is not None:
            m_delta = new_m - old_m
            old_margins.append(old_m)
            new_margins.append(new_m)
            margin_deltas.append(m_delta)
            assert retail is not None
            matched_retail_total += retail
            matched_retail_old_cost += old_p
            matched_retail_new_cost += new_p
            matched_with_retail_count += 1

        if status == "unchanged" and not include_unchanged:
            continue

        if status in ("increased", "decreased") and min_pct > 0:
            if delta_pct is not None and abs(delta_pct) < min_pct:
                counts["filtered_changes"] += 1
                continue

        items.append(
            DiffItem(
                sku=sku,
                name=name,
                status=status,
                old_price=round(old_p, 4),
                new_price=round(new_p, 4),
                delta_abs=round(delta_abs, 4),
                delta_pct=round(delta_pct, 2) if delta_pct is not None else None,
                retail_price=round(retail, 4) if retail is not None else None,
                old_margin_pct=round(old_m, 2) if old_m is not None else None,
                new_margin_pct=round(new_m, 2) if new_m is not None else None,
                margin_delta_pp=round(m_delta, 2) if m_delta is not None else None,
            )
        )

    items = _sort_items(items, sort_by)

    matched_delta_abs = matched_new_total - matched_old_total
    matched_delta_pct = (
        (matched_delta_abs / matched_old_total) * 100.0 if matched_old_total != 0 else None
    )

    weighted_old_m: Optional[float] = None
    weighted_new_m: Optional[float] = None
    weighted_m_delta: Optional[float] = None
    if matched_retail_total > 0:
        weighted_old_m = ((matched_retail_total - matched_retail_old_cost) / matched_retail_total) * 100.0
        weighted_new_m = ((matched_retail_total - matched_retail_new_cost) / matched_retail_total) * 100.0
        weighted_m_delta = weighted_new_m - weighted_old_m

    meta = {
        "old_file": old_label,
        "new_file": new_label,
        "old_encoding": old_table.encoding,
        "new_encoding": new_table.encoding,
        "old_delimiter": _delimiter_label(old_table.delimiter),
        "new_delimiter": _delimiter_label(new_table.delimiter),
        "old_skus": len(old_rows),
        "new_skus": len(new_rows),
        "min_pct": min_pct,
        "include_unchanged": include_unchanged,
        "markup_pct": markup_pct,
        "sort_by": sort_by,
        "warnings": {
            "old_duplicate_skus": old_table.duplicate_skus,
            "new_duplicate_skus": new_table.duplicate_skus,
            "old_skipped_rows": old_table.skipped_rows,
            "new_skipped_rows": new_table.skipped_rows,
        },
    }

    summary = {
        "total_unique_skus": len(all_skus),
        "matched_skus": counts["increased"] + counts["decreased"] + counts["unchanged"],
        "added": counts["added"],
        "removed": counts["removed"],
        "increased": counts["increased"],
        "decreased": counts["decreased"],
        "unchanged": counts["unchanged"],
        "filtered_changes": counts["filtered_changes"],
        "reported_items": len(items),
        "matched_old_basket_total": round(matched_old_total, 2),
        "matched_new_basket_total": round(matched_new_total, 2),
        "matched_basket_delta_abs": round(matched_delta_abs, 2),
        "matched_basket_delta_pct": round(matched_delta_pct, 2) if matched_delta_pct is not None else None,
        "avg_delta_pct": round(sum(delta_pcts) / len(delta_pcts), 2) if delta_pcts else 0.0,
        "margin_impact": {
            "skus_with_retail": matched_with_retail_count,
            "avg_old_margin_pct": round(sum(old_margins) / len(old_margins), 2) if old_margins else None,
            "avg_new_margin_pct": round(sum(new_margins) / len(new_margins), 2) if new_margins else None,
            "avg_margin_delta_pp": round(sum(margin_deltas) / len(margin_deltas), 2) if margin_deltas else None,
            "weighted_old_margin_pct": round(weighted_old_m, 2) if weighted_old_m is not None else None,
            "weighted_new_margin_pct": round(weighted_new_m, 2) if weighted_new_m is not None else None,
            "weighted_margin_delta_pp": round(weighted_m_delta, 2) if weighted_m_delta is not None else None,
        },
    }

    return DiffReport(meta=meta, summary=summary, items=items)


def _sort_items(items: List[DiffItem], sort_by: str) -> List[DiffItem]:
    status_rank = {
        "increased": 0,
        "decreased": 1,
        "added": 2,
        "removed": 3,
        "unchanged": 4,
    }

    if sort_by == "sku":
        return sorted(items, key=lambda x: (status_rank[x.status], x.sku))

    if sort_by == "abs":
        def abs_key(x: DiffItem) -> Tuple[int, float, str]:
            val = abs(x.delta_abs) if x.delta_abs is not None else -1.0
            return (status_rank[x.status], -val, x.sku)
        return sorted(items, key=abs_key)

    # default: "pct"
    def pct_key(x: DiffItem) -> Tuple[int, float, str]:
        if x.status == "increased":
            val = -(x.delta_pct if x.delta_pct is not None else float("inf"))
        elif x.status == "decreased":
            val = x.delta_pct if x.delta_pct is not None else 0.0
        else:
            val = 0.0
        return (status_rank[x.status], val, x.sku)

    return sorted(items, key=pct_key)


def _fmt_num(val: Optional[float], signed: bool = False) -> str:
    if val is None:
        return "-"
    if signed:
        return f"{val:+.2f}"
    return f"{val:.2f}"


def _fmt_pct(val: Optional[float], signed: bool = False) -> str:
    if val is None:
        return "-"
    if signed:
        return f"{val:+.2f}%"
    return f"{val:.2f}%"


def _esc_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: DiffReport) -> str:
    """Render diff report as a readable Markdown document."""
    m = report.meta
    s = report.summary
    mi = s["margin_impact"]

    lines: List[str] = [
        "# Supplier Price-List Diff & Margin Impact Report",
        "",
        f"- **Old Price List:** `{m['old_file']}` (`{m['old_encoding']}`, delimiter `{m['old_delimiter']}`, {m['old_skus']} SKUs)",
        f"- **New Price List:** `{m['new_file']}` (`{m['new_encoding']}`, delimiter `{m['new_delimiter']}`, {m['new_skus']} SKUs)",
        f"- **Threshold (`--min-pct`):** `{m['min_pct']:.2f}%`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Total Unique SKUs | {s['total_unique_skus']} |",
        f"| Added SKUs | {s['added']} |",
        f"| Removed SKUs | {s['removed']} |",
        f"| Price Increased SKUs | {s['increased']} |",
        f"| Price Decreased SKUs | {s['decreased']} |",
        f"| Unchanged SKUs | {s['unchanged']} |",
        f"| Filtered Changes (< {m['min_pct']:.2f}%) | {s['filtered_changes']} |",
        f"| Matched Basket Cost (Old -> New) | {s['matched_old_basket_total']:.2f} -> {s['matched_new_basket_total']:.2f} |",
        f"| Matched Basket Delta | {_fmt_num(s['matched_basket_delta_abs'], signed=True)} ({_fmt_pct(s['matched_basket_delta_pct'], signed=True)}) |",
        f"| Average Matched SKU Delta % | {_fmt_pct(s['avg_delta_pct'], signed=True)} |",
    ]

    if mi["skus_with_retail"] > 0:
        lines.extend(
            [
                f"| Weighted Gross Margin (Old -> New) | {_fmt_pct(mi['weighted_old_margin_pct'])} -> {_fmt_pct(mi['weighted_new_margin_pct'])} |",
                f"| Weighted Margin Impact | {_fmt_num(mi['weighted_margin_delta_pp'], signed=True)} pp |",
            ]
        )

    warnings = m["warnings"]
    if any(warnings.values()):
        lines.extend(
            [
                "",
                "### Data Quality Warnings",
                "",
                f"- Old duplicate SKUs overwritten: {len(warnings['old_duplicate_skus'])}",
                f"- New duplicate SKUs overwritten: {len(warnings['new_duplicate_skus'])}",
                f"- Old malformed/skipped rows: {warnings['old_skipped_rows']}",
                f"- New malformed/skipped rows: {warnings['new_skipped_rows']}",
            ]
        )

    has_margin = any(
        item.old_margin_pct is not None or item.new_margin_pct is not None
        for item in report.items
    )

    def append_section(title: str, group: List[DiffItem]) -> None:
        if not group:
            return
        lines.extend(["", f"## {title} ({len(group)})", ""])
        if has_margin:
            lines.extend(
                [
                    "| SKU | Name | Old Price | New Price | Delta | Delta % | Retail | Margin Old -> New | Margin Impact |",
                    "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for item in group:
                m_transition = f"{_fmt_pct(item.old_margin_pct)} -> {_fmt_pct(item.new_margin_pct)}"
                m_pp = (
                    f"{item.margin_delta_pp:+.2f} pp"
                    if item.margin_delta_pp is not None
                    else "-"
                )
                lines.append(
                    f"| `{_esc_md(item.sku)}` | {_esc_md(item.name) or '-'} | "
                    f"{_fmt_num(item.old_price)} | {_fmt_num(item.new_price)} | "
                    f"{_fmt_num(item.delta_abs, signed=True)} | {_fmt_pct(item.delta_pct, signed=True)} | "
                    f"{_fmt_num(item.retail_price)} | {m_transition} | {m_pp} |"
                )
        else:
            lines.extend(
                [
                    "| SKU | Name | Old Price | New Price | Delta | Delta % |",
                    "| :--- | :--- | ---: | ---: | ---: | ---: |",
                ]
            )
            for item in group:
                lines.append(
                    f"| `{_esc_md(item.sku)}` | {_esc_md(item.name) or '-'} | "
                    f"{_fmt_num(item.old_price)} | {_fmt_num(item.new_price)} | "
                    f"{_fmt_num(item.delta_abs, signed=True)} | {_fmt_pct(item.delta_pct, signed=True)} |"
                )

    append_section("Price Increases", [i for i in report.items if i.status == "increased"])
    append_section("Price Decreases", [i for i in report.items if i.status == "decreased"])
    append_section("Added SKUs", [i for i in report.items if i.status == "added"])
    append_section("Removed SKUs", [i for i in report.items if i.status == "removed"])
    append_section("Unchanged SKUs", [i for i in report.items if i.status == "unchanged"])

    if not report.items:
        lines.extend(["", "_No SKU differences matched the current filter criteria._"])

    return "\n".join(lines) + "\n"


def render_json(report: DiffReport) -> str:
    """Render diff report as formatted JSON."""
    payload = {
        "meta": report.meta,
        "summary": report.summary,
        "items": [asdict(item) for item in report.items],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_csv(report: DiffReport) -> str:
    """Render diff report items as a clean UTF-8 CSV."""
    buf = io.StringIO()
    fieldnames = [
        "sku",
        "name",
        "status",
        "old_price",
        "new_price",
        "delta_abs",
        "delta_pct",
        "retail_price",
        "old_margin_pct",
        "new_margin_pct",
        "margin_delta_pp",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for item in report.items:
        writer.writerow(asdict(item))
    return buf.getvalue()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare old and new supplier CSV/TSV price lists by SKU, "
            "calculate absolute & % deltas and margin impact, and export Markdown/JSON/CSV."
        )
    )
    parser.add_argument("old_csv", type=Path, help="Path to the previous/baseline price list CSV/TSV")
    parser.add_argument("new_csv", type=Path, help="Path to the updated price list CSV/TSV")
    parser.add_argument("--sku-col", default=None, help="Explicit SKU column header name")
    parser.add_argument("--price-col", default=None, help="Explicit wholesale price column header name")
    parser.add_argument("--name-col", default=None, help="Explicit product name column header name")
    parser.add_argument("--retail-col", default=None, help="Explicit retail/RRP column header name")
    parser.add_argument(
        "--markup",
        type=float,
        default=None,
        help="Simulate fixed retail markup %% over old price when retail column is absent",
    )
    parser.add_argument(
        "--min-pct",
        type=float,
        default=0.0,
        help="Minimum absolute %% change threshold for increased/decreased items (default: 0.0)",
    )
    parser.add_argument(
        "--include-unchanged",
        action="store_true",
        help="Include unchanged SKUs in the item breakdown",
    )
    parser.add_argument(
        "--sort",
        choices=("pct", "abs", "sku"),
        default="pct",
        help="Sort order within each status group (default: pct)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("markdown", "md", "json", "csv"),
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Optional file path to save the output (UTF-8)",
    )
    parser.add_argument(
        "--encoding",
        choices=("auto", "utf-8", "utf-8-sig", "cp1251"),
        default="auto",
        help="Force input encoding instead of auto-detection (default: auto)",
    )
    parser.add_argument(
        "--delimiter",
        choices=("auto", ",", ";", "tab"),
        default="auto",
        help="Force input delimiter instead of auto-detection (default: auto)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        old_table = load_price_csv(
            args.old_csv,
            sku_col=args.sku_col,
            price_col=args.price_col,
            name_col=args.name_col,
            retail_col=args.retail_col,
            encoding_override=args.encoding,
            delimiter_override=args.delimiter,
        )
        new_table = load_price_csv(
            args.new_csv,
            sku_col=args.sku_col,
            price_col=args.price_col,
            name_col=args.name_col,
            retail_col=args.retail_col,
            encoding_override=args.encoding,
            delimiter_override=args.delimiter,
        )
        report = compare_tables(
            old_table,
            new_table,
            min_pct=args.min_pct,
            include_unchanged=args.include_unchanged,
            sort_by=args.sort,
            markup_pct=args.markup,
            old_label=args.old_csv.name,
            new_label=args.new_csv.name,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    fmt = "markdown" if args.format == "md" else args.format
    if fmt == "json":
        rendered = render_json(report)
    elif fmt == "csv":
        rendered = render_csv(report)
    else:
        rendered = render_markdown(report)

    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
