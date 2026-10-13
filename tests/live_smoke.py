"""Opt-in live validation; samples five companies, never a full collection.

Run from the workspace parent: python -m moc_scraper.tests.live_smoke
Outputs live_sample.xlsx/csv and live_validation.json under debug/validation/.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from playwright.sync_api import sync_playwright

from moc_scraper.excel_exporter import export_results
from moc_scraper.scraper import MOCScraper, ScraperConfig, _signature


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    output = Path(__file__).resolve().parents[1] / "exports" / "debug" / "validation"
    output.mkdir(parents=True, exist_ok=True)
    config = ScraperConfig(headless=False, debug=False)
    scraper = MOCScraper(config)
    sample = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(config.timeout_ms)
        try:
            page.goto(config.target_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
            first = scraper.search(page)
            assert first, "The live search returned no companies"
            first_signature = _signature(first)
            print(f"Live page 1: {len(first)} cards", flush=True)
            for record in first[:4]:
                item = dict(record)
                fields = scraper.scrape_detail(context, record)
                item.update({k: v for k, v in fields.items() if v})
                item["No."] = len(sample) + 1
                assert all(item.get(key) for key in ("Phone", "Email", "Registration Date")), item
                assert len(context.pages) == 1
                sample.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
            second = scraper.next_page(page, first)
            assert second and _signature(second) != first_signature
            print(f"Live page 2: {len(second)} cards", flush=True)
            item = dict(second[0])
            item.update({k: v for k, v in scraper.scrape_detail(context, item).items() if v})
            item["No."] = len(sample) + 1
            sample.append(item)

            paginator = page.locator(".ant-pagination")
            numbers = [int(text.strip()) for text in paginator.locator("li").all_inner_texts()
                       if re.fullmatch(r"\d+", text.strip())]
            last_number = max(numbers)
            paginator.get_by_text(str(last_number), exact=True).click()
            last_rows = scraper._wait_results(page, previous=_signature(second))
            assert scraper.next_page(page, last_rows) is None
            print(f"Live final page {last_number}: {len(last_rows)} cards; Next is disabled", flush=True)
            page.screenshot(path=str(output / "last_page.png"), full_page=True)
            xlsx, csv = export_results(sample, output, basename="live_sample")
            summary = {
                "scope": "Limited live verification only; not a complete dataset",
                "search_value": config.search_value,
                "date_source": config.date_source,
                "first_page_cards": len(first), "second_page_cards": len(second),
                "last_page": last_number, "last_page_cards": len(last_rows),
                "sample_count": len(sample), "detail_tabs_closed": len(context.pages) == 1,
                "excel": str(xlsx), "csv": str(csv), "records": sample,
            }
            (output / "live_validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Live verification passed. SAMPLE ONLY: {xlsx}", flush=True)
        except Exception:
            page.screenshot(path=str(output / "live_failed.png"), full_page=True)
            (output / "live_failed.html").write_text(page.content(), encoding="utf-8")
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
