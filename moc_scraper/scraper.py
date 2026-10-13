"""Sequential Playwright scraper for the public MOC search UI.

The site-specific selectors below were inspected in the rendered production DOM.
Every run checks them again; semantic fallbacks never silently choose an unrelated
search form. Data is read from visible HTML, never from private JSON endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext, Error as PlaywrightError, Locator, Page,
    TimeoutError as PlaywrightTimeoutError, sync_playwright,
)

try:
    from .dom_parser import extract_results, parse_detail
    from .excel_exporter import normalize_date
except ImportError:
    from dom_parser import extract_results, parse_detail
    from excel_exporter import normalize_date


TARGET_URL = "https://businessregistration.moc.gov.kh/kh/view-instance"
SEARCH_BUTTON = "ស្វែងរក"
MAINTENANCE_MESSAGE = (
    "The MOC Business Registration website is currently under maintenance. "
    "Please try again later."
)


class ScraperError(RuntimeError):
    """The scraper cannot establish that all requested records were collected."""


class MaintenanceError(ScraperError):
    """The public website is showing a maintenance page."""


@dataclass
class ScraperConfig:
    target_url: str = TARGET_URL
    search_value: str = "10006"
    headless: bool = False
    debug: bool = True
    business_type: str | None = None
    timeout_ms: int = 60_000
    detail_retries: int = 3
    detail_delay_min: float = 1.0
    detail_delay_max: float = 2.0
    debug_dir: Path = Path(__file__).resolve().parent.parent / "exports" / "debug"
    # The user explicitly chose the Address tab's Start date for this column.
    date_source: str = "address-start"
    checkpoint_path: Path | None = None

    def __post_init__(self) -> None:
        self.debug_dir = Path(self.debug_dir)
        if self.checkpoint_path is not None:
            self.checkpoint_path = Path(self.checkpoint_path)
        if self.timeout_ms <= 0 or self.detail_retries < 1:
            raise ValueError("Timeout and detail attempts must be positive.")
        if not 0 <= self.detail_delay_min <= self.detail_delay_max:
            raise ValueError("Detail delay must satisfy 0 <= minimum <= maximum.")
        if self.date_source not in {"registration", "address-start"}:
            raise ValueError("Unknown date source.")


def _identity_keys(record: dict[str, Any]) -> set[str]:
    """Use both identifiers so duplicate links or renamed companies stay unique."""
    keys = set()
    if record.get("registration_number"):
        keys.add("registration:" + str(record["registration_number"]).strip())
    if record.get("detail_url"):
        url = urlsplit(record["detail_url"])
        # Locale redirects refer to the same record on the observed website.
        path = re.sub(r"^/(?:kh|en)(?=/view-instance/)", "", url.path).rstrip("/")
        keys.add("url:" + url.netloc.lower() + path + ("?" + url.query if url.query else ""))
    if not keys:
        keys.add("record:" + str(record.get("identity") or (
            str(record.get("Company Name", "")).casefold() + "|" +
            str(record.get("Registration Date", ""))
        )))
    return keys


def _signature(records: list[dict[str, Any]]) -> str:
    return json.dumps([sorted(_identity_keys(r)) for r in records], ensure_ascii=False)


class MOCScraper:
    def __init__(self, config: ScraperConfig | None = None):
        self.config = config or ScraperConfig()
        self.records: list[dict[str, Any]] = []
        self.detail_failures: list[dict[str, Any]] = []
        self._pending_search: set[Any] = set()
        self._search_errors: list[str] = []
        self._watched_pages: set[Page] = set()
        self._last_detail_open: float | None = None
        self._selected_business_type: str | None = None
        self._checkpoint_business_type: str | None = None

    def _checkpoint_options(self) -> dict[str, Any]:
        """Options that must match before saved rows can be reused safely."""
        return {
            "target_url": self.config.target_url,
            "search_value": self.config.search_value,
            "business_type": self.config.business_type,
            "date_source": self.config.date_source,
        }

    def _load_checkpoint(self) -> None:
        path = self.config.checkpoint_path
        if path is None or not path.exists():
            return
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ScraperError(
                f"Could not read checkpoint {path}: {exc}. Preserve it for inspection "
                "or use --restart to start a new run."
            ) from exc
        if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
            raise ScraperError(f"Unsupported checkpoint format in {path}; use --restart for a new run.")
        if checkpoint.get("options") != self._checkpoint_options():
            raise ScraperError(
                f"Checkpoint settings differ from this run: {path}. "
                "Use the original options or --restart to start a new run."
            )
        self._checkpoint_business_type = checkpoint.get("selected_business_type")
        records = checkpoint.get("records")
        failures = checkpoint.get("detail_failures")
        if not isinstance(records, list) or not isinstance(failures, list) or any(
            not isinstance(record, dict) or not record.get("Company Name") for record in records
        ) or any(not isinstance(failure, dict) for failure in failures):
            raise ScraperError(f"Checkpoint records are invalid: {path}; use --restart for a new run.")
        self.records = records
        self.detail_failures = failures
        print(f"Resuming from {path}: {len(records)} saved businesses.", flush=True)

    def _save_checkpoint(self) -> None:
        path = self.config.checkpoint_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        snapshot = {
            "version": 1,
            "options": self._checkpoint_options(),
            "selected_business_type": self._selected_business_type,
            "records": self.records,
            "detail_failures": self.detail_failures,
        }
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise ScraperError(f"Could not save checkpoint {path}: {exc}") from exc

    def _debug(self, message: str) -> None:
        if self.config.debug:
            print(message, flush=True)

    def save_debug(self, page: Page, name: str) -> None:
        if not self.config.debug or page.is_closed():
            return
        try:
            self.config.debug_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
            page.screenshot(path=str(self.config.debug_dir / f"{name}.png"), full_page=True)
            (self.config.debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
        except (OSError, PlaywrightError) as exc:
            print(f"Could not save debug snapshot: {exc}", flush=True)

    def check_maintenance(self, page: Page) -> None:
        body = page.locator("body")
        if not body.count():
            return
        # Inspect rendered text, never hidden script payloads.
        text = body.inner_text(timeout=min(self.config.timeout_ms, 5000))
        if re.search(r"service\s+under\s+maintenance|under\s+maintenance", text, re.I):
            raise MaintenanceError(MAINTENANCE_MESSAGE)
        for heading in page.locator("h1,h2,h3,[role='alert']").all():
            if heading.is_visible() and re.fullmatch(
                r"\s*(?:scheduled\s+)?maintenance[.!\s]*", heading.inner_text(), re.I
            ):
                raise MaintenanceError(MAINTENANCE_MESSAGE)
        if re.fullmatch(r"\s*maintenance[.!\s]*", text, re.I):
            raise MaintenanceError(MAINTENANCE_MESSAGE)

    def _check_access(self, page: Page) -> None:
        self.check_maintenance(page)
        text = page.locator("body").inner_text(timeout=min(self.config.timeout_ms, 5000))
        if re.search(r"verify you are human|checking your browser|access denied|just a moment", text, re.I):
            raise ScraperError("The site displayed an access challenge. No records were treated as empty results.")

    def _watch_search(self, page: Page) -> None:
        if page in self._watched_pages:
            return
        self._watched_pages.add(page)

        def is_search(request: Any) -> bool:
            # Observed public UI request, used only for loading/error detection.
            return "/front/public/search/entity" in request.url

        def started(request: Any) -> None:
            if is_search(request):
                self._pending_search.add(request)

        def finished(request: Any) -> None:
            self._pending_search.discard(request)

        def failed(request: Any) -> None:
            if is_search(request):
                self._search_errors.append(str(request.failure or "Search request failed"))
            finished(request)

        def response_received(response: Any) -> None:
            if is_search(response.request) and response.status >= 400:
                self._search_errors.append(f"Search returned HTTP {response.status}")

        page.on("request", started)
        page.on("requestfinished", finished)
        page.on("requestfailed", failed)
        page.on("response", response_received)

    @staticmethod
    def _visible(locator: Locator) -> list[Locator]:
        return [item for item in locator.all() if item.is_visible()]

    def _inspect_form(self, page: Page) -> tuple[Locator, Locator, Locator]:
        self._check_access(page)
        inputs = page.locator("input").evaluate_all("""els => els.map(e => ({
            id:e.id, name:e.name, type:e.type, placeholder:e.placeholder,
            labels:Array.from(e.labels || []).map(l=>l.innerText),
            visible:!!(e.getClientRects().length), value:e.value
        }))""")
        self._debug("Discovered search inputs: " + json.dumps(inputs, ensure_ascii=False))
        self._debug("Button text: " + json.dumps(
            page.locator("button,input[type='submit']").evaluate_all(
                "es=>es.map(e=>e.innerText || e.value || e.getAttribute('aria-label') || '')"
            ), ensure_ascii=False))

        buttons = self._visible(page.get_by_role("button", name=SEARCH_BUTTON, exact=True))
        if len(buttons) != 1:
            raise ScraperError(f"Expected one visible '{SEARCH_BUTTON}' search button; found {len(buttons)}.")
        button = buttons[0]
        forms = button.locator("xpath=ancestor::form[1]")
        if forms.count() != 1:
            raise ScraperError("The Khmer search button is no longer inside a discoverable search form.")
        form = forms.first
        candidates = self._visible(form.locator("input#txt_search,input[name='txt_search']"))
        if not candidates:
            candidates = self._visible(form.get_by_label(re.compile(
                r"លេខសម្គាល់អង្គភាព|registration.*(?:no|number)|entity.*(?:id|number)", re.I
            )))
        if len(candidates) != 1:
            raise ScraperError("Could not identify the labeled MOC entity-number search input unambiguously.")
        search_input = candidates[0]

        # Always print EVERY option before choosing any option, even with DEBUG off.
        for dropdown in form.locator("select").all():
            options = dropdown.evaluate("e=>Array.from(e.options).map(o=>({label:o.label,value:o.value,selected:o.selected,disabled:o.disabled}))")
            print(f"Dropdown {dropdown.get_attribute('id') or dropdown.get_attribute('name') or '(unnamed)'}: "
                  + json.dumps(options, ensure_ascii=False), flush=True)
        types = self._visible(form.locator("select#company_type,select[name='company_type']"))
        if not types:
            types = self._visible(form.get_by_label(re.compile(r"business type|entity type|ប្រភេទអង្គភាព", re.I)))
        if len(types) != 1:
            raise ScraperError("Could not identify the business type dropdown unambiguously.")
        return search_input, types[0], button

    def search(self, page: Page) -> list[dict[str, Any]]:
        self._watch_search(page)
        try:
            self._check_access(page)
            # Loading the input also waits for client-rendered forms.
            page.get_by_role("button", name=SEARCH_BUTTON, exact=True).wait_for(
                state="visible", timeout=self.config.timeout_ms
            )
            try:
                page.wait_for_load_state("networkidle", timeout=min(5000, self.config.timeout_ms))
            except PlaywrightTimeoutError:
                pass  # Telemetry can keep the network busy; DOM readiness is authoritative.
            search_input, dropdown, button = self._inspect_form(page)
            current_type = dropdown.input_value()
            requested_type = self.config.business_type
            if requested_type is not None and requested_type != current_type:
                options = dropdown.locator("option").evaluate_all("es=>es.filter(e=>!e.disabled).map(e=>e.value)")
                if requested_type not in options:
                    raise ScraperError(f"Business type {requested_type!r} is not one of the printed options.")
                dropdown.select_option(value=requested_type)
            else:
                print(f"Keeping business type: {current_type!r}", flush=True)
            self._selected_business_type = dropdown.input_value()
            print(f"Searching for: {self.config.search_value}", flush=True)
            search_input.fill(self.config.search_value)
            if search_input.input_value() != self.config.search_value:
                raise ScraperError("The search field did not retain the numeric search value.")
            self._search_errors.clear()
            button.click()
            return self._wait_results(page)
        except (PlaywrightError, ValueError, ScraperError) as exc:
            self.save_debug(page, "search_failed")
            if isinstance(exc, ScraperError):
                raise
            raise ScraperError(f"Search failed: {exc}") from exc

    def _pagination_state(self, page: Page) -> dict[str, Any] | None:
        """Read the Ant pagination controls observed on the MOC results page."""
        controls = self._visible(page.locator("main .ant-pagination"))
        if not controls:
            return None
        if len(controls) != 1:
            raise ScraperError(f"Expected one MOC pagination control; found {len(controls)}.")
        control = controls[0]
        active = control.locator(".ant-pagination-item-active")
        next_items = control.locator(".ant-pagination-next")
        if active.count() != 1 or next_items.count() != 1:
            raise ScraperError("The MOC pagination control has no unambiguous current page and Next button.")
        current_text = active.inner_text().strip()
        if not current_text.isdecimal():
            raise ScraperError("The current result page number is unreadable.")
        current = int(current_text)
        page_numbers = [int(value) for value in control.locator(".ant-pagination-item").all_inner_texts()
                        if value.strip().isdecimal()]
        if not page_numbers or max(page_numbers) < current:
            raise ScraperError("The last result page number is unreadable.")
        next_item = next_items.first
        next_disabled = self._disabled(next_item)
        size_text = control.locator(".ant-pagination-options").inner_text() if control.locator(
            ".ant-pagination-options").count() == 1 else ""
        size_match = re.search(r"(\d+)\s*/\s*page", size_text, re.I)
        page_size = int(size_match.group(1)) if size_match else None
        if not next_disabled and page_size is None:
            raise ScraperError("The MOC page-size control is missing; result pages cannot be verified.")
        return {"current": current, "last": max(page_numbers), "page_size": page_size,
                "next": next_item, "next_disabled": next_disabled}

    def _wait_results(self, page: Page, previous: str | None = None,
                      expected_page: int | None = None) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.config.timeout_ms / 1000
        stable_since = time.monotonic()
        last_signature = None
        last_issue = ""
        while time.monotonic() < deadline:
            self._check_access(page)
            if self._search_errors:
                raise ScraperError("; ".join(self._search_errors))
            try:
                rows = extract_results(page)
                pagination = self._pagination_state(page)
                last_issue = ""
            except (ValueError, ScraperError) as exc:
                # A React update may briefly contain only part of a page. Keep
                # waiting, then fail with the structural reason if it persists.
                rows, pagination, last_issue = [], None, str(exc)
            signature = _signature(rows)
            if signature != last_signature:
                last_signature, stable_since = signature, time.monotonic()
            busy = bool(self._pending_search) or any(
                item.is_visible() for item in page.locator("[aria-busy='true'],[role='progressbar']").all()
            )
            right_page = (expected_page is None or
                          (pagination is not None and pagination["current"] == expected_page))
            complete_count = (pagination is None or pagination["page_size"] is None or
                              (len(rows) == pagination["page_size"] if not pagination["next_disabled"]
                               else 0 < len(rows) <= pagination["page_size"]))
            if rows and not right_page:
                last_issue = f"Results did not advance to page {expected_page}."
            elif rows and not complete_count:
                last_issue = (f"Page {pagination['current']} shows {len(rows)} cards, but "
                              f"the MOC page-size control shows {pagination['page_size']}.")
            if rows and signature != previous and right_page and complete_count and not busy and time.monotonic() - stable_since >= 0.75:
                self._debug(f"Detected result card count: {len(rows)}")
                return rows
            empty = page.get_by_text(re.compile(
                r"^(?:No (?:results|data|records)(?: found)?[.!]?|Nothing found|មិនមានទិន្នន័យ|គ្មានទិន្នន័យ)$", re.I
            ))
            if not rows and not busy and self._visible(empty) and time.monotonic() - stable_since >= 0.75:
                if previous:
                    raise ScraperError("A subsequent results page unexpectedly became empty; collection is incomplete.")
                return []
            # Polling evaluates actual DOM state; this is not a fixed loading delay.
            page.wait_for_timeout(200)
        reason = last_issue or ("Results never changed after pagination" if previous else
                                "No result cards or explicit empty-results message appeared")
        raise ScraperError(f"{reason} within {self.config.timeout_ms / 1000:g} seconds.")

    @staticmethod
    def _disabled(control: Locator) -> bool:
        return control.evaluate("""e => !!e.closest('[aria-disabled="true"], [disabled], .ant-pagination-disabled')
            || !!e.querySelector('button:disabled')""")

    def next_page(self, page: Page, previous_records: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Follow the verified Next control; only stop at a demonstrable end."""
        pagination = self._pagination_state(page)
        if pagination is None:
            raise ScraperError("Result cards are visible but MOC pagination is missing; refusing to finish early.")
        self._debug("Pagination controls: " + json.dumps({
            key: value for key, value in pagination.items() if key != "next"
        }, ensure_ascii=False))
        if pagination["next_disabled"]:
            if pagination["current"] != pagination["last"]:
                raise ScraperError("Next is disabled before the final numbered result page.")
            return None
        self._search_errors.clear()
        try:
            next_page_number = pagination["current"] + 1
            print(f"Moving to page {next_page_number}...", flush=True)
            button = pagination["next"].locator("button")
            if button.count() != 1:
                raise ScraperError("The MOC Next control has no unique button.")
            button.click()
            return self._wait_results(page, previous=_signature(previous_records),
                                      expected_page=next_page_number)
        except (PlaywrightError, ValueError, ScraperError) as exc:
            self.save_debug(page, "pagination_failed")
            if isinstance(exc, ScraperError):
                raise
            raise ScraperError(f"Pagination failed: {exc}") from exc

    def _wait_detail(self, page: Page) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_ms / 1000
        last_issue = "The labeled registration number did not appear."
        while time.monotonic() < deadline:
            self._check_access(page)
            try:
                fields = parse_detail(page, section="general")
                if fields.get("registration_number"):
                    return fields
            except ValueError as exc:
                last_issue = str(exc)
            page.wait_for_timeout(200)
        raise ScraperError(f"Detail page did not render its registration fields: {last_issue}")

    def _address_tab(self, page: Page) -> tuple[Locator, Locator]:
        # The live MOC page renders each tab as an anchor with role=tablist,
        # rather than a WAI-ARIA tab. Discover the panel ID from its href.
        choices = self._visible(page.locator("main a[href^='#']").filter(
            has_text=re.compile(r"^\s*(?:អាសយដ្ឋាន|Address)\s*$", re.I)))
        if len(choices) != 1:
            raise ScraperError(f"Expected one visible Address tab; found {len(choices)}.")
        href = choices[0].get_attribute("href") or ""
        if not href.startswith("#") or not href[1:]:
            raise ScraperError("The Address tab does not name a detail panel.")
        panel = page.locator("main [id=" + json.dumps(href[1:]) + "]")
        if panel.count() != 1:
            raise ScraperError("The Address tab's detail panel is missing or ambiguous.")
        return choices[0], panel

    def scrape_detail(self, context: BrowserContext, record: dict[str, Any]) -> dict[str, Any]:
        url = record.get("detail_url")
        if not url:
            raise ScraperError("Company has a JavaScript-only detail action without a public detail URL.")
        target = urlsplit(str(url))
        origin = urlsplit(self.config.target_url)
        if target.scheme not in {"http", "https"} or target.netloc != origin.netloc:
            raise ScraperError("The discovered company detail link is not a same-site public HTTP URL.")
        # Preserve the chosen language when following the site's locale-less links.
        if target.path.startswith("/view-instance/") and origin.path.startswith("/kh/"):
            url = target._replace(path="/kh" + target.path).geturl()
        last_error: Exception | None = None
        for attempt in range(1, self.config.detail_retries + 1):
            delay = random.uniform(self.config.detail_delay_min, self.config.detail_delay_max)
            if self._last_detail_open is not None:
                time.sleep(max(0, delay - (time.monotonic() - self._last_detail_open)))
            self._last_detail_open = time.monotonic()
            detail = context.new_page()
            detail.set_default_timeout(self.config.timeout_ms)
            try:
                response = detail.goto(str(url), wait_until="domcontentloaded", timeout=self.config.timeout_ms)
                self._check_access(detail)
                if response is not None and response.status >= 400:
                    raise ScraperError(f"Detail page returned HTTP {response.status}")
                fields = self._wait_detail(detail)
                if str(fields.get("registration_number")) != str(record.get("registration_number")):
                    raise ScraperError(
                        f"Detail registration number {fields.get('registration_number')!r} "
                        f"does not match result card {record.get('registration_number')!r}."
                    )
                address_tab, address_panel = self._address_tab(detail)
                address_tab.click()
                address_panel.wait_for(state="visible", timeout=self.config.timeout_ms)
                address_fields = parse_detail(detail, section="address")
                fields.update({k: v for k, v in address_fields.items() if v not in (None, "", [])})
                if self.config.date_source == "address-start":
                    fields["Registration Date"] = normalize_date(fields.get("address_start_date", ""))
                elif fields.get("Registration Date"):
                    fields["Registration Date"] = normalize_date(fields["Registration Date"])
                for label, key in (("phone", "Phone"), ("email", "Email"),
                                   ("Address Start Date", "address_start_date")):
                    if not fields.get(key):
                        print(f"  No public {label} shown for {record.get('Company Name', '')}.", flush=True)
                fields.pop("present_fields", None)
                return fields
            except MaintenanceError:
                self.save_debug(detail, "detail_maintenance")
                raise
            except (PlaywrightError, ValueError, ScraperError) as exc:
                last_error = exc
                print(f"  Detail attempt {attempt}/{self.config.detail_retries} failed: {exc}", flush=True)
                if attempt == self.config.detail_retries:
                    identity = str(record.get("registration_number") or record.get("No.") or "unknown")
                    self.save_debug(detail, f"detail_{identity}_failed")
            finally:
                detail.close()
        raise ScraperError(f"Failed to read detail after {self.config.detail_retries} attempts: {last_error}")

    def run(self) -> list[dict[str, Any]]:
        self.records.clear()
        self.detail_failures.clear()
        self._pending_search.clear()
        self._search_errors.clear()
        self._watched_pages.clear()
        self._last_detail_open = None
        self._selected_business_type = None
        self._checkpoint_business_type = None
        self._load_checkpoint()
        saved_indices = {
            key: index for index, saved in enumerate(self.records)
            for key in _identity_keys(saved)
        }
        visited_this_run: set[str] = set()
        seen_pages: set[str] = set()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.config.headless)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            try:
                response = page.goto(self.config.target_url, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
                self._check_access(page)
                if response is not None and response.status >= 400:
                    raise ScraperError(f"Search page returned HTTP {response.status}")
                rows = self.search(page)
                if (self._checkpoint_business_type is not None and
                        self._selected_business_type != self._checkpoint_business_type):
                    raise ScraperError(
                        "The website's selected business type changed since the checkpoint. "
                        "Use the earlier type with --business-type or --restart."
                    )
                if not rows and self.records:
                    raise ScraperError(
                        "The current search returned no businesses, but the checkpoint has saved "
                        "businesses. Keeping the checkpoint for a later retry."
                    )
                page_number = 1
                while rows:
                    signature = _signature(rows)
                    if signature in seen_pages:
                        raise ScraperError("Pagination repeated an earlier page; results are incomplete.")
                    seen_pages.add(signature)
                    print(f"Found page {page_number}", flush=True)
                    for index, record in enumerate(rows, 1):
                        keys = _identity_keys(record)
                        if visited_this_run.intersection(keys):
                            print(
                                f"Duplicate result on page {page_number}: "
                                f"{record.get('Company Name')} "
                                f"({record.get('registration_number') or record.get('detail_url')}); "
                                "already processed in this run.",
                                flush=True,
                            )
                            continue
                        visited_this_run.update(keys)
                        saved_index = next((saved_indices[key] for key in keys if key in saved_indices), None)
                        failure = next((item for item in self.detail_failures
                                        if keys.intersection(_identity_keys(item))), None)
                        if saved_index is not None and failure is None:
                            self._debug(f"Already saved; skipping detail: {record.get('Company Name')}")
                            continue
                        if saved_index is not None:
                            record["No."] = self.records[saved_index]["No."]
                        elif record.get("No.") in (None, ""):
                            record["No."] = len(self.records) + 1
                        print(f"[{index}/{len(rows)}] Scraping {record.get('Company Name', '')}", flush=True)
                        if self.config.date_source == "address-start":
                            record["Registration Date"] = ""
                        try:
                            if record.get("detail_url") or record.get("detail_action"):
                                detail_fields = self.scrape_detail(context, record)
                                record.update({k: v for k, v in detail_fields.items() if v not in (None, "")})
                                if failure is not None:
                                    self.detail_failures.remove(failure)
                        except MaintenanceError:
                            raise
                        except (PlaywrightError, ValueError, ScraperError) as exc:
                            print(f"  Could not finish company; keeping its result card: {exc}", flush=True)
                            failure_data = {
                                "Company Name": record.get("Company Name"),
                                "registration_number": record.get("registration_number"),
                                "detail_url": record.get("detail_url"), "error": str(exc),
                            }
                            if failure is not None:
                                failure.update(failure_data)
                            else:
                                self.detail_failures.append(failure_data)
                        record["Registration Date"] = normalize_date(record.get("Registration Date", ""))
                        if saved_index is None:
                            saved_index = len(self.records)
                            self.records.append(record)
                        else:
                            self.records[saved_index] = record
                        for key in keys | _identity_keys(record):
                            saved_indices[key] = saved_index
                        self._save_checkpoint()
                    next_rows = self.next_page(page, rows)
                    if next_rows is None:
                        break
                    rows = next_rows
                    page_number += 1
                return self.records
            except (PlaywrightError, ValueError, ScraperError) as exc:
                self.save_debug(page, "scrape_failed")
                if isinstance(exc, ScraperError):
                    raise
                raise ScraperError(str(exc)) from exc
            finally:
                context.close()
                browser.close()
