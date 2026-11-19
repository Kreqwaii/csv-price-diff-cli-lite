import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import price_diff


class TestPriceDiff(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _write_bytes(self, filename: str, data: bytes) -> Path:
        p = self.tmp_path / filename
        p.write_bytes(data)
        return p

    def test_detect_encoding_utf8_utf8sig_and_cp1251(self) -> None:
        text = "sku;name;price\nA-1;Кабель USB-C;150,50\n"

        decoded_utf8, enc_utf8 = price_diff.detect_and_decode(text.encode("utf-8"))
        self.assertEqual(enc_utf8, "utf-8")
        self.assertIn("Кабель USB-C", decoded_utf8)

        decoded_bom, enc_bom = price_diff.detect_and_decode(text.encode("utf-8-sig"))
        self.assertEqual(enc_bom, "utf-8-sig")
        self.assertFalse(decoded_bom.startswith("\ufeff"))

        decoded_cp, enc_cp = price_diff.detect_and_decode(text.encode("cp1251"))
        self.assertEqual(enc_cp, "cp1251")
        self.assertIn("Кабель USB-C", decoded_cp)

    def test_detect_delimiter_comma_semicolon_tab(self) -> None:
        csv_comma = 'sku,name,price\nSKU1,"Adapter, Pro",19.99\n'
        csv_semi = 'sku;name;price\nSKU1;"Adapter, Pro";"1 250,50"\n'
        csv_tab = "sku\tname\tprice\nSKU1\tAdapter Pro\t19.99\n"

        self.assertEqual(price_diff.detect_delimiter(csv_comma), ",")
        self.assertEqual(price_diff.detect_delimiter(csv_semi), ";")
        self.assertEqual(price_diff.detect_delimiter(csv_tab), "\t")

    def test_parse_price_formats(self) -> None:
        self.assertAlmostEqual(price_diff.parse_price("$1,299.50"), 1299.50)
        self.assertAlmostEqual(price_diff.parse_price("1 250,50 ₽"), 1250.50)
        self.assertAlmostEqual(price_diff.parse_price("€ 49,90"), 49.90)
        self.assertAlmostEqual(price_diff.parse_price("1.250,75 EUR"), 1250.75)
        self.assertAlmostEqual(price_diff.parse_price("0,125"), 0.125)
        with self.assertRaises(ValueError):
            price_diff.parse_price("N/A")
        with self.assertRaises(ValueError):
            price_diff.parse_price("")

    def test_diff_statuses_and_zero_baseline(self) -> None:
        old_file = self._write_bytes(
            "old.csv",
            (
                "sku,name,price\n"
                "UP-1,Item Up,100.00\n"
                "DN-1,Item Down,100.00\n"
                "EQ-1,Item Same,50.00\n"
                "RM-1,Item Removed,25.00\n"
                "ZR-1,Free Promo to Paid,0.00\n"
            ).encode("utf-8"),
        )
        new_file = self._write_bytes(
            "new.csv",
            (
                "sku,name,price\n"
                "UP-1,Item Up,120.00\n"
                "DN-1,Item Down,80.00\n"
                "EQ-1,Item Same,50.00\n"
                "AD-1,Item Added,40.00\n"
                "ZR-1,Free Promo to Paid,15.00\n"
            ).encode("utf-8"),
        )

        old_t = price_diff.load_price_csv(old_file)
        new_t = price_diff.load_price_csv(new_file)
        report = price_diff.compare_tables(old_t, new_t)

        self.assertEqual(report.summary["increased"], 2)
        self.assertEqual(report.summary["decreased"], 1)
        self.assertEqual(report.summary["unchanged"], 1)
        self.assertEqual(report.summary["added"], 1)
        self.assertEqual(report.summary["removed"], 1)

        by_sku = {item.sku: item for item in report.items}
        self.assertNotIn("EQ-1", by_sku)
        self.assertEqual(by_sku["UP-1"].delta_abs, 20.0)
        self.assertEqual(by_sku["UP-1"].delta_pct, 20.0)
        self.assertEqual(by_sku["DN-1"].delta_abs, -20.0)
        self.assertEqual(by_sku["DN-1"].delta_pct, -20.0)
        self.assertEqual(by_sku["AD-1"].status, "added")
        self.assertEqual(by_sku["RM-1"].status, "removed")
        self.assertEqual(by_sku["ZR-1"].status, "increased")
        self.assertIsNone(by_sku["ZR-1"].delta_pct)

    def test_min_pct_filtering_and_include_unchanged(self) -> None:
        old_file = self._write_bytes(
            "old.csv",
            "sku,price\nA,100\nB,100\nC,100\nD,100\n".encode("utf-8"),
        )
        new_file = self._write_bytes(
            "new.csv",
            "sku,price\nA,102\nB,98.5\nC,110\nD,100\nE,50\n".encode("utf-8"),
        )

        old_t = price_diff.load_price_csv(old_file)
        new_t = price_diff.load_price_csv(new_file)

        filtered = price_diff.compare_tables(old_t, new_t, min_pct=5.0)
        self.assertEqual(filtered.summary["filtered_changes"], 2)
        skus = {item.sku for item in filtered.items}
        self.assertEqual(skus, {"C", "E"})

        with_unchanged = price_diff.compare_tables(
            old_t, new_t, min_pct=5.0, include_unchanged=True
        )
        skus_u = {item.sku for item in with_unchanged.items}
        self.assertEqual(skus_u, {"C", "D", "E"})

    def test_margin_impact_retail_and_markup(self) -> None:
        old_file = self._write_bytes(
            "old.csv",
            "sku,price,retail_price\nM1,50.00,100.00\n".encode("utf-8"),
        )
        new_file = self._write_bytes(
            "new.csv",
            "sku,price,retail_price\nM1,60.00,100.00\n".encode("utf-8"),
        )
        report = price_diff.compare_tables(
            price_diff.load_price_csv(old_file),
            price_diff.load_price_csv(new_file),
        )
        item = report.items[0]
        self.assertEqual(item.old_margin_pct, 50.0)
        self.assertEqual(item.new_margin_pct, 40.0)
        self.assertEqual(item.margin_delta_pp, -10.0)

        # Simulated markup when retail_price column is absent
        old_no_ret = self._write_bytes("old_nr.csv", "sku,price\nM2,100.00\n".encode("utf-8"))
        new_no_ret = self._write_bytes("new_nr.csv", "sku,price\nM2,125.00\n".encode("utf-8"))
        # 100% markup => retail = 200.00; old margin = 50%, new margin = 37.5%
        report_markup = price_diff.compare_tables(
            price_diff.load_price_csv(old_no_ret),
            price_diff.load_price_csv(new_no_ret),
            markup_pct=100.0,
        )
        m2 = report_markup.items[0]
        self.assertEqual(m2.retail_price, 200.0)
        self.assertEqual(m2.old_margin_pct, 50.0)
        self.assertEqual(m2.new_margin_pct, 37.5)
        self.assertEqual(m2.margin_delta_pp, -12.5)

    def test_cp1251_semicolon_and_russian_aliases_with_duplicates(self) -> None:
        old_cp = self._write_bytes(
            "old_ru.csv",
            (
                "Артикул;Наименование;Цена\n"
                "RU-01;Блок питания 65Вт;1 200,00\n"
                "RU-01;Блок питания 65Вт (обновл.);1 250,00\n"
            ).encode("cp1251"),
        )
        new_tsv = self._write_bytes(
            "new_ru.tsv",
            (
                "SKU\tName\tPrice\n"
                "RU-01\tБлок питания 65Вт\t1500.00\n"
            ).encode("utf-8"),
        )
        old_t = price_diff.load_price_csv(old_cp)
        new_t = price_diff.load_price_csv(new_tsv)
        self.assertEqual(old_t.encoding, "cp1251")
        self.assertEqual(old_t.delimiter, ";")
        self.assertEqual(new_t.delimiter, "\t")
        self.assertEqual(old_t.duplicate_skus, ["RU-01"])

        report = price_diff.compare_tables(old_t, new_t)
        self.assertEqual(report.items[0].old_price, 1250.0)
        self.assertEqual(report.items[0].new_price, 1500.0)
        self.assertEqual(report.items[0].delta_pct, 20.0)

    def test_render_markdown_json_csv(self) -> None:
        old_file = self._write_bytes("old.csv", "sku,name,price\nS1,Alpha,10\n".encode("utf-8"))
        new_file = self._write_bytes("new.csv", "sku,name,price\nS1,Alpha,12\n".encode("utf-8"))
        report = price_diff.compare_tables(
            price_diff.load_price_csv(old_file),
            price_diff.load_price_csv(new_file),
        )

        md_out = price_diff.render_markdown(report)
        self.assertIn("# Supplier Price-List Diff & Margin Impact Report", md_out)
        self.assertIn("`S1`", md_out)
        self.assertIn("+20.00%", md_out)

        json_out = price_diff.render_json(report)
        parsed_json = json.loads(json_out)
        self.assertEqual(parsed_json["summary"]["increased"], 1)
        self.assertEqual(parsed_json["items"][0]["sku"], "S1")

        csv_out = price_diff.render_csv(report)
        rows = list(csv.DictReader(io.StringIO(csv_out)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "S1")
        self.assertEqual(rows[0]["delta_pct"], "20.0")

    def test_cli_end_to_end_with_bundled_samples(self) -> None:
        base_dir = Path(__file__).resolve().parent
        old_sample = base_dir / "old_prices.csv"
        new_sample = base_dir / "new_prices.csv"
        out_json = self.tmp_path / "diff.json"

        code = price_diff.main(
            [
                str(old_sample),
                str(new_sample),
                "--min-pct",
                "5",
                "--format",
                "json",
                "--output",
                str(out_json),
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["added"], 2)
        self.assertEqual(data["summary"]["removed"], 2)
        self.assertEqual(data["summary"]["filtered_changes"], 2)

    def test_missing_column_or_file_returns_error(self) -> None:
        bad_csv = self._write_bytes("bad.csv", "sku,title\nA1,Missing Price\n".encode("utf-8"))
        good_csv = self._write_bytes("good.csv", "sku,price\nA1,10\n".encode("utf-8"))

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            exit_code = price_diff.main([str(bad_csv), str(good_csv)])
        self.assertEqual(exit_code, 1)
        self.assertIn("required price column", err_buf.getvalue())

        err_buf2 = io.StringIO()
        with contextlib.redirect_stderr(err_buf2):
            missing_code = price_diff.main([str(self.tmp_path / "nope.csv"), str(good_csv)])
        self.assertEqual(missing_code, 1)
        self.assertIn("File not found", err_buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
