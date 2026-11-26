"""Pure-logic tests for removing prices from content tables (no DB).

Run with: pytest app/services/table_prices_spec.py
"""

import pytest

from app.services.table_prices import strip_table_prices

FLAVOR_ROW = "<tr><td>Flavor</td><td>Cherry</td></tr>"
COUNT_ROW = "<tr><td>Count</td><td>20</td></tr>"
INTRO = "<p>Rich cherry cigarillos.</p>"
OUTRO = '<p>See <a href="/collections/cigarillos">cigarillos</a>.</p>'


def spec_table(*rows):
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


def result(html, rows=0, columns=0, skipped=()):
    return {"html": html, "rows": rows, "columns": columns, "skipped": list(skipped)}


class TestPriceRows:
    def test_removes_price_row_and_keeps_the_rest_of_the_page(self):
        html = INTRO + spec_table(FLAVOR_ROW, "<tr><td>Price</td><td>$27.00</td></tr>", COUNT_ROW) + OUTRO
        assert strip_table_prices(html) == result(INTRO + spec_table(FLAVOR_ROW, COUNT_ROW) + OUTRO, rows=1)

    def test_removes_the_whitespace_before_the_row(self):
        html = "<table>\n  <tr>\n    <td>Flavor</td><td>Cherry</td>\n  </tr>\n  <tr>\n    <td>Price</td>\n    <td>$27.00</td>\n  </tr>\n</table>"
        expected = "<table>\n  <tr>\n    <td>Flavor</td><td>Cherry</td>\n  </tr>\n</table>"
        assert strip_table_prices(html) == result(expected, rows=1)

    @pytest.mark.parametrize("label_cell", [
        "<th>Price</th>",
        "<td><strong>Price</strong></td>",
        "<td>Price:</td>",
        "<td> PRICE </td>",
        "<td>Price&nbsp;Range</td>",
        "<td>Price Per Cigar</td>",
        "<td>Typical Price Range</td>",
        "<td>Price Point</td>",
    ])
    def test_recognises_price_label_spellings(self, label_cell):
        html = spec_table(FLAVOR_ROW, f"<tr>{label_cell}<td>$1.90–$59.50</td></tr>")
        assert strip_table_prices(html) == result(spec_table(FLAVOR_ROW), rows=1)

    @pytest.mark.parametrize("label", [
        "Pre-Priced",
        "Mix Berry - 15 Packs of 5 ($1.49 Pre-Priced)",
        "Manufacturer Pre-Priced (Guaranteed Retail)",
        "Priced For Value",
    ])
    def test_keeps_rows_whose_label_only_mentions_price(self, label):
        html = spec_table(FLAVOR_ROW, f"<tr><td>{label}</td><td>Yes</td></tr>")
        assert strip_table_prices(html) == result(html)

    def test_keeps_a_table_whose_only_row_is_the_price(self):
        html = spec_table("<tr><td>Price</td><td>$5.00</td></tr>")
        assert strip_table_prices(html) == result(html, skipped=["only_price"])

    def test_removes_price_rows_from_every_table(self):
        price_row = "<tr><td>Price</td><td>$5.00</td></tr>"
        html = spec_table(FLAVOR_ROW, price_row) + INTRO + spec_table(price_row, COUNT_ROW)
        assert strip_table_prices(html) == result(spec_table(FLAVOR_ROW) + INTRO + spec_table(COUNT_ROW), rows=2)

    def test_merged_title_row_does_not_stop_row_removal(self):
        title = '<tr><th colspan="2">Specifications</th></tr>'
        html = spec_table(title, FLAVOR_ROW, "<tr><td>Price</td><td>$5.00</td></tr>")
        assert strip_table_prices(html) == result(spec_table(title, FLAVOR_ROW), rows=1)


class TestStockRows:
    @pytest.mark.parametrize("label_cell", ["<td>Availability</td>", "<th>Stock</th>", "<td>In stock?</td>",
                                            "<td><strong>Stock status:</strong></td>"])
    def test_removes_stock_rows(self, label_cell):
        html = spec_table(FLAVOR_ROW, f"<tr>{label_cell}<td>Out of stock</td></tr>", COUNT_ROW)
        assert strip_table_prices(html) == result(spec_table(FLAVOR_ROW, COUNT_ROW), rows=1)

    def test_keeps_rows_that_only_mention_stock(self):
        html = spec_table(FLAVOR_ROW, "<tr><td>Stock up and save</td><td>Yes</td></tr>")
        assert strip_table_prices(html) == result(html)


class TestPriceColumns:
    HEADER = "<thead><tr><th>Product</th><th>Brand</th><th>Price</th><th>Availability</th></tr></thead>"
    ROW = "<tr><td>{name}</td><td>Swisher</td><td>{price}</td><td>In stock</td></tr>"

    def comparison(self, header, *rows):
        return f'<table class="cmp">{header}<tbody>{"".join(rows)}</tbody></table>'

    def test_removes_price_and_stock_headers_and_the_cells_under_them(self):
        html = self.comparison(
            self.HEADER,
            self.ROW.format(name="Peach ($1.49 Pre-Priced)", price="$32.99"),
            self.ROW.format(name="Grape", price="$30.75"),
        )
        expected = self.comparison(
            "<thead><tr><th>Product</th><th>Brand</th></tr></thead>",
            "<tr><td>Peach ($1.49 Pre-Priced)</td><td>Swisher</td></tr>",
            "<tr><td>Grape</td><td>Swisher</td></tr>",
        )
        assert strip_table_prices(html) == result(expected, columns=2)

    def test_removes_average_price_column(self):
        html = ("<table><tr><th>Brand</th><th>Avg. Price</th></tr>"
                "<tr><td>Swisher</td><td>$1.10</td></tr></table>")
        expected = "<table><tr><th>Brand</th></tr><tr><td>Swisher</td></tr></table>"
        assert strip_table_prices(html) == result(expected, columns=1)

    def test_skips_a_table_with_merged_cells(self):
        html = self.comparison(
            self.HEADER,
            '<tr><td colspan="4">Pre-priced packs</td></tr>',
            self.ROW.format(name="Grape", price="$30.75"),
        )
        assert strip_table_prices(html) == result(html, skipped=["merged_cells"])

    def test_skips_a_table_with_ragged_rows(self):
        html = self.comparison(self.HEADER, "<tr><td>Grape</td><td>$30.75</td></tr>")
        assert strip_table_prices(html) == result(html, skipped=["merged_cells"])

    def test_keeps_a_table_whose_only_column_is_the_price(self):
        html = "<table><tr><th>Price</th></tr><tr><td>$5.00</td></tr></table>"
        assert strip_table_prices(html) == result(html)


class TestUntouched:
    @pytest.mark.parametrize("html", [
        None,
        "",
        INTRO + OUTRO,
        "<p>Price: $27.00 for a box of 20.</p>",
        spec_table(FLAVOR_ROW, COUNT_ROW),
    ])
    def test_html_without_price_tables_comes_back_unchanged(self, html):
        assert strip_table_prices(html) == result(html or "")

    def test_skips_nested_tables(self):
        inner = spec_table("<tr><td>Price</td><td>$5.00</td></tr>")
        html = f"<table><tr><td>Outer</td><td>{inner}</td></tr></table>"
        assert strip_table_prices(html) == result(html, skipped=["nested"])

    def test_is_idempotent(self):
        html = INTRO + spec_table(FLAVOR_ROW, "<tr><td>Price</td><td>$27.00</td></tr>")
        once = strip_table_prices(html)["html"]
        assert strip_table_prices(once) == result(once)
