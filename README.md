# csv-price-diff-cli-lite

**Supplier Price-List CSV Diff & Margin Impact CLI (Lite)** — 100% Python 3.9+ standard-library utility for e-commerce teams and procurement managers.

Утилита на чистом Python 3.9+ (без `pip install` и сторонних зависимостей) для e-commerce и закупок: сравнивает старый и новый прайс-лист поставщика в CSV/TSV по `sku`, автоматически определяет кодировку (`UTF-8` / `UTF-8-SIG` / `CP1251`) и разделитель (`,` / `;` / `\t`), находит добавленные, удалённые, подорожавшие и подешевевшие позиции, считает абсолютную и процентную дельту, оценивает влияние на маржу, фильтрует мелкие колебания через `--min-pct` и выгружает отчёт в **Markdown, JSON или CSV**.

---

## Features / Возможности

- **Zero dependencies** — 100% Python 3.9+ `stdlib` (`csv`, `json`, `argparse`, `dataclasses`, `pathlib`).
- **Encoding Auto-Detection** — seamlessly handles `UTF-8`, `UTF-8-SIG` (Excel BOM), `UTF-16`, and Windows Cyrillic `CP1251`.
- **Delimiter Auto-Detection** — automatically detects comma (`,`), semicolon (`;`), and tab (`\t`) separators, including quoted decimal commas.
- **Dirty Price Normalization** — parses `$1,299.50`, `1 250,50 ₽`, `€ 49,90`, `1.250,75 EUR`, and standard floats.
- **Flexible Column Matching** — recognizes English and Russian aliases (`sku`/`article`/`артикул`, `price`/`cost`/`цена`, `name`/`наименование`, `retail_price`/`rrp`/`ррц`) or accepts explicit overrides (`--sku-col`, `--price-col`, `--name-col`, `--retail-col`).
- **Full Diff Classification** — separates `increased`, `decreased`, `added`, `removed`, and `unchanged` SKUs with `delta_abs` and `delta_pct`.
- **Margin Impact Analysis** — computes old vs. new gross margin `%` and percentage-point shift (`margin_delta_pp`) from an optional `retail_price` / `rrp` column or via `--markup <pct>` simulation.
- **Threshold Filtering (`--min-pct`)** — filters out noisy micro-changes (e.g., `< 5%`) while preserving added/removed SKUs and tracking filtered counts.
- **Export Formats** — `--format markdown` (default), `--format json`, or `--format csv`, plus `--output <file>`.

---

## CSV Input Schema

Minimal required columns (case-insensitive, aliases supported):

```csv
sku,price
```

Recommended schema with product names and retail prices for margin impact:

```csv
sku,name,price,retail_price
EL-1001,USB-C GaN Charger 65W,18.50,34.90
```

See [`old_prices.csv`](old_prices.csv) and [`new_prices.csv`](new_prices.csv) for ready-to-run sample price lists.

---

## Quick Start / Быстрый старт

Compare the bundled sample price lists (Markdown output):

```bash
python price_diff.py old_prices.csv new_prices.csv
```

Filter out changes below `5%` (`--min-pct 5`):

```bash
python price_diff.py old_prices.csv new_prices.csv --min-pct 5
```

Export structured JSON or CSV to a file:

```bash
python price_diff.py old_prices.csv new_prices.csv --min-pct 5 --format json --output diff.json
python price_diff.py old_prices.csv new_prices.csv --format csv --output diff.csv
```

Simulate a fixed `45%` initial retail markup when your supplier CSV has no `retail_price` column:

```bash
python price_diff.py old_prices.csv new_prices.csv --markup 45
```

Override column names explicitly (useful for custom supplier exports):

```bash
python price_diff.py supplier_jan.csv supplier_feb.csv --sku-col "Vendor Code" --price-col "Net Cost" --name-col "Title"
```

Run unit tests:

```bash
py -B -m unittest -v
```

---

## CLI Options

| Flag | Default | Description |
| :--- | :--- | :--- |
| `old_csv` | *(required)* | Baseline / previous supplier CSV or TSV file |
| `new_csv` | *(required)* | Updated supplier CSV or TSV file |
| `--min-pct` | `0.0` | Minimum absolute `%` change for increased/decreased items |
| `--include-unchanged` | `false` | Include unchanged SKUs in the item breakdown |
| `--markup` | `None` | Assumed retail markup `%` over old price if `retail_price` is absent |
| `--sort` | `pct` | Sort within groups by `pct`, `abs`, or `sku` |
| `-f`, `--format` | `markdown` | Output format: `markdown` (`md`), `json`, or `csv` |
| `-o`, `--output` | `stdout` | Write UTF-8 report to a file |
| `--sku-col` | `auto` | Explicit SKU column header |
| `--price-col` | `auto` | Explicit price column header |
| `--name-col` | `auto` | Explicit product name column header |
| `--retail-col` | `auto` | Explicit retail / RRP column header |
| `--encoding` | `auto` | Force `utf-8`, `utf-8-sig`, or `cp1251` |
| `--delimiter` | `auto` | Force `,`, `;`, or `tab` |

---

## Example Output (Markdown)

Running `python price_diff.py old_prices.csv new_prices.csv --min-pct 5`:

```markdown
# Supplier Price-List Diff & Margin Impact Report

- **Old Price List:** `old_prices.csv` (`utf-8`, delimiter `,`, 12 SKUs)
- **New Price List:** `new_prices.csv` (`utf-8`, delimiter `,`, 12 SKUs)
- **Threshold (`--min-pct`):** `5.00%`

## Summary

| Metric | Value |
| :--- | ---: |
| Total Unique SKUs | 14 |
| Added SKUs | 2 |
| Removed SKUs | 2 |
| Price Increased SKUs | 5 |
| Price Decreased SKUs | 3 |
| Unchanged SKUs | 2 |
| Filtered Changes (< 5.00%) | 2 |
| Matched Basket Cost (Old -> New) | 137.40 -> 138.85 |
| Matched Basket Delta | +1.45 (+1.06%) |
| Average Matched SKU Delta % | +3.98% |
| Weighted Gross Margin (Old -> New) | 51.74% -> 51.23% |
| Weighted Margin Impact | -0.51 pp |

## Price Increases (4)

| SKU | Name | Old Price | New Price | Delta | Delta % | Retail | Margin Old -> New | Margin Impact |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `EL-1003` | NVMe Enclosure USB 3.2 | 14.00 | 16.80 | +2.80 | +20.00% | 29.90 | 53.18% -> 43.81% | -9.36 pp |
| `EL-1006` | Smart Wi-Fi Plug Mini | 5.80 | 6.90 | +1.10 | +18.97% | 12.90 | 55.04% -> 46.51% | -8.53 pp |
| `EL-1001` | USB-C GaN Charger 65W | 18.50 | 21.00 | +2.50 | +13.51% | 34.90 | 46.99% -> 39.83% | -7.16 pp |
| `EL-1012` | MagSafe Car Mount Vent | 11.00 | 12.10 | +1.10 | +10.00% | 24.90 | 55.82% -> 51.41% | -4.42 pp |
```

---

## Need a Full Local Web Studio for CSV/TSV/JSON Lead Cleaning, E.164 Phone Normalization, Fuzzy Dedup & Price Diffing?

This Lite CLI focuses on fast command-line SKU price diffing. If you need an **all-in-one local browser Web Studio** for interactive CSV/TSV/JSON lead cleaning, E.164 international phone normalization, fuzzy deduplication, batch validation, and visual supplier price-list diffing without sending client data to the cloud:

- **Pro Full Local Web Studio on FunPay:** https://funpay.com/lots/offer?id=82601753

---

## License

MIT License — see [LICENSE](LICENSE).
