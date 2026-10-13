"""Command-line entry point for the public MOC business registration scraper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

try:
    from .excel_exporter import export_results
    from .scraper import MOCScraper, MaintenanceError, ScraperConfig, ScraperError
except (ImportError, ValueError):
    from excel_exporter import export_results
    from scraper import MOCScraper, MaintenanceError, ScraperConfig, ScraperError


HEADLESS = False
DEBUG = True
SEARCH_VALUE = "10006"
BUSINESS_TYPE = None  # Keep the website's selected default; or set an exact option value.
DATE_SOURCE = "address-start"  # User-requested Address tab Start Date, matching the examples.
TARGET_URL = "https://businessregistration.moc.gov.kh/kh/view-instance"
TIMEOUT_MS = 60_000
DETAIL_RETRIES = 3
DETAIL_DELAY_MIN = 1.0
DETAIL_DELAY_MAX = 2.0
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "exports"
MAINTENANCE_MESSAGE = (
    "The MOC Business Registration website is currently under maintenance. "
    "Please try again later."
)


def _positive_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive integer in milliseconds") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive integer in milliseconds")
    return timeout


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the MOC registration form for the fixed value 10006 and export every result."
    )
    parser.add_argument("--headless", action="store_true", default=HEADLESS,
                        help="Run Chromium without a visible window (default: visible).")
    parser.add_argument("--no-debug", action="store_false", dest="debug", default=DEBUG,
                        help="Disable verbose DOM diagnostics and failure screenshots.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Output folder for the workbook, CSV, and debug/ (default: project/exports).")
    parser.add_argument("--business-type", default=BUSINESS_TYPE, metavar="VALUE",
                        help="Select this exact live dropdown option value; default preserves its selection.")
    parser.add_argument("--date-source", choices=("address-start", "registration"), default=DATE_SOURCE,
                        help="Source for Registration Date: Address Start Date (default) or registration date.")
    parser.add_argument("--timeout", type=_positive_timeout, default=TIMEOUT_MS, metavar="MS",
                        help="Playwright operation timeout in milliseconds (default: 60000).")
    parser.add_argument("--restart", action="store_true",
                        help="Discard the saved checkpoint and start a new collection.")
    return parser.parse_args(argv)


def _configure_console() -> None:
    """Keep Khmer diagnostics and company names readable on Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _write_detail_failures(scraper: MOCScraper, debug_dir: Path) -> None:
    if not scraper.detail_failures:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        failure_path = debug_dir / "detail_failures.json"
        failure_path.write_text(
            json.dumps(scraper.detail_failures, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"Detail failure report: {failure_path}", file=sys.stderr)
    except OSError as exc:
        print(f"Could not write detail failure report: {exc}", file=sys.stderr)


def _export_partial(scraper: MOCScraper, output_dir: Path, debug_dir: Path) -> None:
    _write_detail_failures(scraper, debug_dir)
    if not scraper.records:
        print("No records were collected; no result files were written.", file=sys.stderr)
        return
    try:
        excel_path, csv_path = export_results(
            scraper.records, output_dir, basename="MOC_Businesses.partial"
        )
        print(f"Saved {len(scraper.records)} records as incomplete results:", file=sys.stderr)
        print(f"Excel: {excel_path}\nCSV: {csv_path}", file=sys.stderr)
    except Exception as exc:
        print(f"Could not export incomplete results: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _arguments(argv)
    output_dir = args.output_dir.expanduser().resolve()
    debug_dir = output_dir / "debug"
    checkpoint_path = output_dir / "MOC_Businesses.checkpoint.json"
    if args.restart:
        try:
            checkpoint_path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Could not remove the saved checkpoint: {exc}", file=sys.stderr)
            return 1
    config = ScraperConfig(
        target_url=TARGET_URL,
        search_value=SEARCH_VALUE,
        headless=args.headless,
        debug=args.debug,
        business_type=args.business_type,
        date_source=args.date_source,
        timeout_ms=args.timeout,
        detail_retries=DETAIL_RETRIES,
        detail_delay_min=DETAIL_DELAY_MIN,
        detail_delay_max=DETAIL_DELAY_MAX,
        debug_dir=debug_dir,
        checkpoint_path=checkpoint_path,
    )
    scraper = MOCScraper(config)
    try:
        records = scraper.run()
    except MaintenanceError:
        print(MAINTENANCE_MESSAGE, file=sys.stderr)
        _export_partial(scraper, output_dir, debug_dir)
        return 1
    except KeyboardInterrupt:
        print("Scraping interrupted. The run is incomplete.", file=sys.stderr)
        _export_partial(scraper, output_dir, debug_dir)
        return 130
    except Exception as exc:
        print(f"Scraping failed: {exc}", file=sys.stderr)
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            print("Install the browser with: python -m playwright install chromium", file=sys.stderr)
        elif args.debug and not isinstance(exc, ScraperError):
            traceback.print_exc()
        _export_partial(scraper, output_dir, debug_dir)
        return 1

    if scraper.detail_failures:
        print(
            f"Scraping incomplete: {len(scraper.detail_failures)} business detail page(s) "
            "could not be read after retries. Missing fields remain blank.",
            file=sys.stderr,
        )
        _export_partial(scraper, output_dir, debug_dir)
        return 1

    try:
        excel_path, csv_path = export_results(records, output_dir)
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1

    # Keep the checkpoint until both complete exports have been written.
    try:
        checkpoint_path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"Could not clear the completed checkpoint {checkpoint_path}: {exc}", file=sys.stderr)
        return 1

    print("========================================")
    print("MOC SCRAPING COMPLETED")
    print("========================================")
    print(f"Search value: {SEARCH_VALUE}")
    print(f"Total businesses: {len(records)}")
    print(f"Excel: {excel_path.name}")
    print(f"CSV: {csv_path.name}")
    print("========================================")
    if output_dir != Path.cwd().resolve():
        print(f"Output folder: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
