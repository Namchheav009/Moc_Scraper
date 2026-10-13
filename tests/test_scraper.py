"""Offline browser tests using the semantic structures inspected on MOC."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from moc_scraper.dom_parser import clean_company_name, extract_results, parse_detail
from moc_scraper.scraper import (
    MOCScraper, MaintenanceError, MAINTENANCE_MESSAGE, ScraperConfig, ScraperError,
    _identity_keys,
)


BASE = "https://fixture.test"


def card(number: int, name: str = "EXAMPLE CO., LTD.") -> str:
    return f"""<article class="card"><a href="/view-instance/company{number}">
    <h3>ឈ្មោះ / {name} ({number})</h3></a>
    <div>អត្តលេខចុះបញ្ជី <span>{number}</span></div>
    <dl><dt>កាលបរិច្ឆេទចុះបញ្ជី</dt><dd>Sep 21, 2026</dd></dl></article>"""


DETAIL = """<main><div class="company-detail">
<a role="tablist" href="#link1">ព័ត៌មានលម្អិតទូទៅ</a>
<a role="tablist" href="#link2" onclick="document.getElementById('link1').hidden=true;
 document.getElementById('link2').hidden=false">អាសយដ្ឋាន</a>
<div id="link1"><table>
<tr><th>ឈ្មោះដែលបានស្នើ(ជាភាសាអង់គ្លេស)</th><td>EXAMPLE CO., LTD.</td></tr>
<tr><th>អត្តលេខចុះបញ្ជី</th><td>100061</td></tr>
<tr><th>កាលបរិច្ឆេទការចុះបញ្ជីឡើងវិញ</th><td>Sep 21, 2026</td></tr>
</table></div>
<div id="link2" hidden><article><h3>អាសយដ្ឋានទីស្នាក់ការចុះបញ្ជី</h3><dl>
<dt>កាលបរិច្ឆេទចាប់ផ្តើម</dt><dd>08-Sep-2026</dd>
<dt>អ៊ីម៉ែល</dt><dd>business@example.test</dd>
<dt>លេខទូរស័ព្ទទំនាក់ទំនង</dt><dd>+855 12 345 678</dd>
</dl></article></div></div></main>
<footer><p>Email: government@example.test</p><p>Phone: +855 99 999 999</p></footer>"""


class IdentityTests(unittest.TestCase):
    def test_identity_survives_locale_and_name_changes(self):
        first = {"registration_number": "10006", "detail_url": BASE + "/view-instance/abc"}
        second = {"registration_number": "10006", "detail_url": BASE + "/kh/view-instance/abc"}
        self.assertEqual(_identity_keys(first), _identity_keys(second))

    def test_bilingual_name_preserves_english_slash(self):
        self.assertEqual(clean_company_name("ខ្មែរ / A/B CO., LTD. (10006)", "10006"), "A/B CO., LTD.")


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body="<main></main>"
        ))
        self.page = self.context.new_page()
        self.page.goto(BASE + "/kh/view-instance")
        self.scraper = MOCScraper(ScraperConfig(
            target_url=BASE + "/kh/view-instance", headless=True, debug=False,
            timeout_ms=2500, detail_delay_min=0, detail_delay_max=0,
        ))

    def tearDown(self):
        self.context.close()

    def test_real_card_structure_and_footer_exclusion(self):
        self.page.set_content("<main>" + card(100061) + card(100062, "SECOND CO., LTD.") +
                              "</main><footer>contact@example.test +855 12 999 999</footer>")
        rows = extract_results(self.page)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Company Name"], "EXAMPLE CO., LTD.")
        self.assertEqual(rows[0]["registration_number"], "100061")
        self.assertEqual(rows[0]["No."], "")
        self.assertEqual(rows[0]["Phone"], "")
        self.assertEqual(rows[0]["Email"], "")
        self.assertEqual(rows[0]["detail_url"], BASE + "/view-instance/company100061")

    def test_saved_live_moc_dom_uses_card_pagination_and_address_labels(self):
        root = Path(__file__).resolve().parents[1] / "exports" / "debug"
        search = root / "search_page.html"
        detail = root / "detail_page.html"
        if not search.exists() or not detail.exists():
            self.skipTest("Local live MOC HTML snapshots are unavailable")

        def main_fragment(path: Path) -> str:
            match = re.search(r"<main\b[^>]*>.*?</main>", path.read_text(encoding="utf-8"), re.S)
            self.assertIsNotNone(match)
            return "<style>.hidden{display:none}</style>" + match.group()

        self.page.set_content(main_fragment(search))
        records = extract_results(self.page)
        self.assertEqual(len(records), 10)
        self.assertEqual(records[0]["registration_number"], "1000660173")
        self.assertEqual(records[0]["Company Name"], "VIVI FOOD CO., LTD.")
        pagination = self.scraper._pagination_state(self.page)
        self.assertEqual(pagination["current"], 1)
        self.assertEqual(pagination["page_size"], 10)
        self.assertFalse(pagination["next_disabled"])

        self.page.set_content(main_fragment(detail))
        general = parse_detail(self.page)
        self.assertEqual(general["registration_number"], "1000660173")
        tab, panel = self.scraper._address_tab(self.page)
        self.assertEqual(tab.get_attribute("href"), "#link2")
        self.assertEqual(panel.count(), 1)
        self.page.evaluate("""() => {
            document.getElementById('link1').classList.add('hidden');
            document.getElementById('link2').classList.remove('hidden');
        }""")
        address = parse_detail(self.page, section="address")
        self.assertEqual(address["Phone"], "+855 96 770 6842")
        self.assertEqual(address["Email"], "845765518@qq.com")
        self.assertEqual(address["address_start_date"], "08-Sep-2026")

    def test_unrelated_table_is_not_treated_as_moc_result_cards(self):
        self.page.set_content("""<main><table><thead><tr><th>No.</th><th>Company Name</th>
            <th>Registration Number</th><th>Phone</th></tr></thead><tbody>
            <tr><td>49</td><td><a href="/view-instance/one">ONE CO., LTD.</a></td>
            <td>100061</td><td>012345678</td></tr></tbody></table></main>""")
        with self.assertRaisesRegex(ValueError, "missing a unique result article"):
            extract_results(self.page)

    def test_result_card_without_detail_link_is_not_silently_dropped(self):
        self.page.set_content("<main>" + card(100061) +
            '<article class="card"><h3>SECOND CO., LTD. (100062)</h3>'
            '<div>អត្តលេខចុះបញ្ជី <span>100062</span></div></article></main>')
        with self.assertRaisesRegex(ValueError, "2 MOC cards but 1 company detail links"):
            extract_results(self.page)

    def test_search_preserves_default_and_waits_for_async_results(self):
        import json
        self.page.set_content("""<main><form onsubmit="event.preventDefault();setTimeout(() => {
          document.getElementById('results').innerHTML = window.cards; }, 350)">
          <label for="txt_search">ឈ្មោះ ឬលេខសម្គាល់អង្គភាព</label>
          <input id="txt_search" name="txt_search" type="text">
          <select id="company_type"><option value="">All</option>
          <option value="INCORP" selected>Companies</option></select>
          <button type="submit">ស្វែងរក</button></form><div id="results"></div></main>
          <script>window.typeChanges=0;document.getElementById('company_type').onchange=()=>typeChanges++;
          window.cards=""" + json.dumps(card(100061)) + ";</script>")
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            rows = self.scraper.search(self.page)
        self.assertEqual(self.page.locator("#txt_search").input_value(), "10006")
        self.assertEqual(self.page.locator("#company_type").input_value(), "INCORP")
        self.assertEqual(self.page.evaluate("typeChanges"), 0)
        self.assertIn('"value": "INCORP"', captured.getvalue())
        self.assertEqual(len(rows), 1)

    def test_next_page_waits_for_changed_cards_and_stops_disabled(self):
        import json
        self.page.set_content("<main><div id='results'>" + card(100061) + "</div>" +
            """<ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>
            <li class="ant-pagination-item">2</li><li class="ant-pagination-next"><button onclick="
            setTimeout(()=>{document.getElementById('results').innerHTML=window.second;
            document.querySelector('.ant-pagination-item-active').classList.remove('ant-pagination-item-active');
            document.querySelectorAll('.ant-pagination-item')[1].classList.add('ant-pagination-item-active');
            this.disabled=true;this.parentElement.classList.add('ant-pagination-disabled')},300)">Next</button>
            </li><li class="ant-pagination-options">1 / page</li></ul></main>
            <script>window.second=""" + json.dumps(card(100062)) + ";</script>")
        first = extract_results(self.page)
        with contextlib.redirect_stdout(io.StringIO()):
            second = self.scraper.next_page(self.page, first)
        self.assertEqual(second[0]["registration_number"], "100062")
        self.assertIsNone(self.scraper.next_page(self.page, second))

    def test_stuck_pagination_is_an_error_not_success(self):
        self.page.set_content("<main>" + card(100061) +
            '<ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>'
            '<li class="ant-pagination-item">2</li><li class="ant-pagination-next"><button>Next</button></li>'
            '<li class="ant-pagination-options">1 / page</li></ul></main>')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ScraperError, "did not advance"):
            self.scraper.next_page(self.page, extract_results(self.page))

    def test_missing_or_early_disabled_next_cannot_end_collection(self):
        self.page.set_content("<main>" + card(100061) + "</main>")
        with self.assertRaisesRegex(ScraperError, "pagination is missing"):
            self.scraper.next_page(self.page, extract_results(self.page))
        self.page.set_content("<main>" + card(100061) +
            '<ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>'
            '<li class="ant-pagination-item">2</li>'
            '<li class="ant-pagination-next ant-pagination-disabled" aria-disabled="true">'
            '<button disabled>Next</button></li><li class="ant-pagination-options">1 / page</li></ul></main>')
        with self.assertRaisesRegex(ScraperError, "before the final"):
            self.scraper.next_page(self.page, extract_results(self.page))

    def test_incomplete_nonfinal_page_is_not_accepted(self):
        self.page.set_content("<main>" + card(100061) +
            '<ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>'
            '<li class="ant-pagination-item">2</li><li class="ant-pagination-next">'
            '<button>Next</button></li><li class="ant-pagination-options">2 / page</li></ul></main>')
        with self.assertRaisesRegex(ScraperError, "shows 1 cards.*page-size control shows 2"):
            self.scraper._wait_results(self.page)

    def test_detail_opens_address_tab_and_closes_without_moving_results(self):
        self.context.route("**/view-instance/company100061", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=DETAIL))
        url = self.page.url
        fields = self.scraper.scrape_detail(self.context, {
            "detail_url": BASE + "/view-instance/company100061", "registration_number": "100061"})
        self.assertEqual(fields["Registration Date"], "08-Sep-26")
        self.assertEqual(fields["Phone"], "+855 12 345 678")
        self.assertEqual(fields["Email"], "business@example.test")
        self.assertEqual(self.page.url, url)
        self.assertEqual(len(self.context.pages), 1)

    def test_detail_retries_transient_http_failure(self):
        attempts = []
        def respond(route):
            attempts.append(1)
            route.fulfill(status=503 if len(attempts) == 1 else 200,
                          content_type="text/html; charset=utf-8",
                          body="<body>Temporary error</body>" if len(attempts) == 1 else DETAIL)
        self.context.route("**/view-instance/company100061", respond)
        with contextlib.redirect_stdout(io.StringIO()):
            fields = self.scraper.scrape_detail(self.context, {
                "detail_url": BASE + "/view-instance/company100061", "registration_number": "100061"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(fields["Email"], "business@example.test")
        self.assertEqual(len(self.context.pages), 1)

    def test_detail_with_wrong_registration_number_is_rejected(self):
        self.scraper.config.detail_retries = 1
        self.context.route("**/view-instance/company100061", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=DETAIL.replace("100061", "100999")))
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ScraperError, "does not match result card"):
            self.scraper.scrape_detail(self.context, {
                "detail_url": BASE + "/view-instance/company100061", "registration_number": "100061"})
        self.assertEqual(len(self.context.pages), 1)

    def test_detail_without_address_tab_is_rejected(self):
        self.scraper.config.detail_retries = 1
        self.context.route("**/view-instance/company100061", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=DETAIL.replace('href="#link2"', 'href="#missing"')))
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ScraperError, "detail panel is missing"):
            self.scraper.scrape_detail(self.context, {
                "detail_url": BASE + "/view-instance/company100061", "registration_number": "100061"})

    def test_detail_does_not_mislabel_reregistration_date(self):
        self.page.set_content(DETAIL)
        fields = parse_detail(self.page)
        self.assertEqual(fields["Registration Date"], "")
        self.assertEqual(fields["Phone"], "")
        self.assertEqual(fields["Email"], "")

    def test_maintenance_is_not_zero_results(self):
        self.page.set_content("<h1>Service Under Maintenance</h1>")
        with self.assertRaises(MaintenanceError) as raised:
            self.scraper.search(self.page)
        self.assertEqual(str(raised.exception), MAINTENANCE_MESSAGE)

    def test_explicit_empty_results(self):
        self.page.set_content("<main><p>No data</p></main>")
        self.assertEqual(self.scraper._wait_results(self.page), [])

    def test_full_run_deduplicates_across_pages_and_numbers_records(self):
        import json
        second = card(100062, "SECOND CO., LTD.") + card(100063, "THIRD CO., LTD.")
        html = """<main><form onsubmit="event.preventDefault();document.getElementById('results').innerHTML=window.first">
            <input id="txt_search" type="text"><select id="company_type"><option value="">All</option></select>
            <button type="submit">ស្វែងរក</button></form><div id="results"></div>
            <ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>
            <li class="ant-pagination-item">2</li><li class="ant-pagination-next"><button onclick="
            document.getElementById('results').innerHTML=window.second;
            document.querySelector('.ant-pagination-item-active').classList.remove('ant-pagination-item-active');
            document.querySelectorAll('.ant-pagination-item')[1].classList.add('ant-pagination-item-active');
            this.disabled=true">Next</button></li><li class="ant-pagination-options">2 / page</li></ul>
            </main><script>window.first=""" + json.dumps(card(100061) + card(100062, "SECOND CO., LTD.")) + ";window.second=" + json.dumps(second) + ";</script>"
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body=DETAIL.replace("100061", route.request.url.rsplit("company", 1)[1])
            if "/view-instance/company" in route.request.url else html))
        # Inject a routed real Chromium context into the complete production loop.
        browser = SimpleNamespace(new_context=lambda **kwargs: self.context, close=lambda: None)
        manager = MagicMock()
        manager.__enter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
        with patch("moc_scraper.scraper.sync_playwright", return_value=manager), contextlib.redirect_stdout(io.StringIO()):
            records = self.scraper.run()
        self.assertEqual([r["registration_number"] for r in records], ["100061", "100062", "100063"])
        self.assertEqual([r["No."] for r in records], [1, 2, 3])
        self.assertTrue(all(r["Registration Date"] == "08-Sep-26" for r in records))
        self.assertEqual(self.scraper.detail_failures, [])

    def test_resume_after_interrupt_skips_saved_details_and_finishes_later_page(self):
        first = card(100061) + card(100062, "SECOND CO., LTD.")
        second = card(100062, "SECOND CO., LTD.") + card(100063, "THIRD CO., LTD.")
        html = """<main><form onsubmit="event.preventDefault();
            document.getElementById('results').innerHTML=window.first">
            <input id="txt_search" type="text"><select id="company_type"><option value="">All</option></select>
            <button type="submit">ស្វែងរក</button></form><div id="results"></div>
            <ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>
            <li class="ant-pagination-item">2</li><li class="ant-pagination-next"><button onclick="
            document.getElementById('results').innerHTML=window.second;
            document.querySelector('.ant-pagination-item-active').classList.remove('ant-pagination-item-active');
            document.querySelectorAll('.ant-pagination-item')[1].classList.add('ant-pagination-item-active');
            this.disabled=true">Next</button></li><li class="ant-pagination-options">2 / page</li></ul>
            </main><script>window.first=""" + json.dumps(first) + ";window.second=" + json.dumps(second) + ";</script>"
        fixture = lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        self.context.route("**/*", fixture)
        browser = SimpleNamespace(new_context=lambda **kwargs: self.context, close=lambda: None)
        manager = MagicMock()
        manager.__enter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
        fields = {"Phone": "+855 12 345 678", "Email": "business@example.test",
                  "address_start_date": "08-Sep-2026", "Registration Date": "08-Sep-26"}
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "MOC_Businesses.checkpoint.json"
            self.scraper.config.checkpoint_path = checkpoint
            self.scraper.scrape_detail = MagicMock(side_effect=[fields, fields, KeyboardInterrupt])
            with patch("moc_scraper.scraper.sync_playwright", return_value=manager), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                self.scraper.run()
            self.assertEqual(self.scraper.scrape_detail.call_count, 3)
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual([r["registration_number"] for r in saved["records"]], ["100061", "100062"])

            self.context = self.browser.new_context()
            self.context.route("**/*", fixture)
            resumed = MOCScraper(self.scraper.config)
            resumed.scrape_detail = MagicMock(return_value=fields)
            with patch("moc_scraper.scraper.sync_playwright", return_value=manager), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                records = resumed.run()
            self.assertIn("Resuming from", output.getvalue())
            self.assertEqual(resumed.scrape_detail.call_count, 1)
            self.assertEqual([r["registration_number"] for r in records], ["100061", "100062", "100063"])
            self.assertEqual([r["No."] for r in records], [1, 2, 3])
            self.assertEqual(len(json.loads(checkpoint.read_text(encoding="utf-8"))["records"]), 3)

    def test_failed_detail_is_retried_from_checkpoint(self):
        html = """<main><form onsubmit="event.preventDefault();
            document.getElementById('results').innerHTML=window.cards">
            <input id="txt_search" type="text"><select id="company_type"><option value="">All</option></select>
            <button type="submit">ស្វែងរក</button></form><div id="results"></div>
            <ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-active">1</li>
            <li class="ant-pagination-next ant-pagination-disabled" aria-disabled="true"><button disabled>Next</button></li>
            <li class="ant-pagination-options">1 / page</li></ul></main>
            <script>window.cards=""" + json.dumps(card(100061)) + ";</script>"
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        self.page.set_content("<main>" + card(100061) + "</main>")
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "MOC_Businesses.checkpoint.json"
            self.scraper.config.checkpoint_path = checkpoint
            self.scraper.records = [extract_results(self.page)[0]]
            self.scraper.records[0]["No."] = 1
            self.scraper.detail_failures = [{
                "Company Name": "EXAMPLE CO., LTD.", "registration_number": "100061",
                "detail_url": BASE + "/view-instance/company100061", "error": "HTTP 503",
            }]
            self.scraper._save_checkpoint()
            browser = SimpleNamespace(new_context=lambda **kwargs: self.context, close=lambda: None)
            manager = MagicMock()
            manager.__enter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
            resumed = MOCScraper(self.scraper.config)
            resumed.scrape_detail = MagicMock(return_value={
                "Phone": "+855 12 345 678", "Email": "business@example.test",
                "address_start_date": "08-Sep-2026", "Registration Date": "08-Sep-26",
            })
            with patch("moc_scraper.scraper.sync_playwright", return_value=manager), \
                    contextlib.redirect_stdout(io.StringIO()):
                records = resumed.run()
            self.assertEqual(resumed.scrape_detail.call_count, 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["No."], 1)
            self.assertEqual(records[0]["Registration Date"], "08-Sep-26")
            self.assertEqual(resumed.detail_failures, [])
            self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["detail_failures"], [])

    def test_checkpoint_rejects_changed_search_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "MOC_Businesses.checkpoint.json"
            self.scraper.config.checkpoint_path = checkpoint
            self.scraper.records = [{"Company Name": "EXAMPLE CO., LTD.",
                                     "registration_number": "100061"}]
            self.scraper._save_checkpoint()
            different = ScraperConfig(target_url=BASE + "/kh/view-instance",
                                      date_source="registration", checkpoint_path=checkpoint)
            with self.assertRaisesRegex(ScraperError, "settings differ"):
                MOCScraper(different)._load_checkpoint()

    def test_resume_rejects_changed_website_default_business_type(self):
        html = """<main><form onsubmit="event.preventDefault();
            document.getElementById('results').innerHTML=window.cards">
            <input id="txt_search" type="text"><select id="company_type">
            <option value="" selected>All</option><option value="INCORP">Companies</option></select>
            <button type="submit">ស្វែងរក</button></form><div id="results"></div></main>
            <script>window.cards=""" + json.dumps(card(100061)) + ";</script>"
        self.context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "MOC_Businesses.checkpoint.json"
            self.scraper.config.checkpoint_path = checkpoint
            self.scraper.records = [{"No.": 1, "Company Name": "EXAMPLE CO., LTD.",
                                     "registration_number": "100061"}]
            self.scraper._selected_business_type = "INCORP"
            self.scraper._save_checkpoint()
            browser = SimpleNamespace(new_context=lambda **kwargs: self.context, close=lambda: None)
            manager = MagicMock()
            manager.__enter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
            resumed = MOCScraper(self.scraper.config)
            with patch("moc_scraper.scraper.sync_playwright", return_value=manager), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaisesRegex(ScraperError, "selected business type changed"):
                resumed.run()
            self.assertTrue(checkpoint.exists())


if __name__ == "__main__":
    unittest.main()
