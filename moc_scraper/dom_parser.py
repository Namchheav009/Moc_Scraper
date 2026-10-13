"""Extract public fields from the MOC result cards and company detail tabs.

These structures were checked against rendered MOC pages: result anchors point to
``/view-instance/<id>`` from an article, the general detail tab uses table rows,
and the Address tab uses ``dt``/``dd`` pairs. Unknown layouts fail visibly.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urldefrag


EXPORT_FIELDS = ("No.", "Company Name", "Phone", "Email", "Registration Date")


def clean_company_name(value: str, registration_number: str = "") -> str:
    value = " ".join(str(value or "").split())
    if registration_number:
        value = re.sub(r"\s*\(" + re.escape(registration_number) + r"\)\s*$", "", value)
    if re.search(r"[\u1780-\u17ff]", value):
        for part in value.split(" / ")[1:]:
            if re.search(r"[A-Za-z]", part) and not re.search(r"[\u1780-\u17ff]", part):
                return part.strip()
    return value.strip()


def result_identity(record: dict[str, Any]) -> str:
    number = str(record.get("registration_number") or "").strip()
    if number:
        return "registration:" + number
    url = str(record.get("detail_url") or "").strip()
    if url:
        return "url:" + urldefrag(url)[0]
    name = " ".join(str(record.get("Company Name") or "").split()).casefold()
    date = " ".join(str(record.get("Registration Date") or "").split()).casefold()
    if not name:
        raise ValueError("A business has neither a registration number nor a name.")
    return "company:" + name + "|" + date


_EXTRACT_SCRIPT = r"""(mode) => {
    const norm = s => String(s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
    const key = s => norm(s).toLowerCase().replace(/[\s:：.*()\-]/g, '');
    const aliases = {
        'No.': ['No.', 'No', 'Row No.', 'Serial No.', 'ល.រ', 'លេខរៀង'],
        'Company Name': ['Company Name', 'Company Name (English)', 'ឈ្មោះដែលបានស្នើ(ជាភាសាអង់គ្លេស)'],
        'registration_number': ['Registration Number', 'Registration No.', 'អត្តលេខចុះបញ្ជី'],
        'Registration Date': ['Registration Date', 'Registered Date', 'កាលបរិច្ឆេទចុះបញ្ជី'],
        'address_start_date': ['Start Date', 'កាលបរិច្ឆេទចាប់ផ្តើម'],
        'Phone': ['Phone', 'Phone Number', 'Telephone', 'លេខទូរស័ព្ទទំនាក់ទំនង'],
        'Email': ['Email', 'E-mail', 'អ៊ីម៉ែល']
    };
    const lookup = new Map(Object.entries(aliases).flatMap(([field, names]) =>
        names.map(name => [key(name), field])));
    const fieldOf = label => lookup.get(key(label));
    const root = document.querySelector('main,[role="main"]');
    if (!root) return {error: 'The MOC main content is missing.'};
    const excluded = 'nav,footer,header,aside,[role="navigation"],[role="contentinfo"]';
    const visible = node => !!node && !node.closest(excluded) && !node.closest('[hidden],[aria-hidden="true"]')
        && getComputedStyle(node).display !== 'none' && getComputedStyle(node).visibility !== 'hidden'
        && node.getClientRects().length > 0;
    const all = (scope, selector) => Array.from(scope.querySelectorAll(selector)).filter(visible);
    const text = node => visible(node) ? norm(node.innerText || node.textContent) : '';
    const ownText = node => norm(Array.from(node.childNodes)
        .filter(child => child.nodeType === Node.TEXT_NODE).map(child => child.textContent).join(' '));

    function fields(scope) {
        const found = {};
        const present = new Set();
        function add(label, value) {
            const field = fieldOf(label);
            if (!field) return;
            present.add(field);
            value = norm(value);
            if (!value || /^(?:[-—–]|n\/?a|null)$/i.test(value)) return;
            if (!found[field]) found[field] = new Set();
            found[field].add(value);
        }
        // Verified MOC result cards and Address tab: dt followed by dd.
        for (const dt of all(scope, 'dt')) {
            const dd = dt.nextElementSibling;
            if (dd && dd.tagName === 'DD' && visible(dd)) add(text(dt), text(dd));
        }
        // Verified MOC general detail tab: one label th and one value td.
        for (const row of all(scope, 'tr')) {
            const th = row.querySelector(':scope > th');
            const td = row.querySelector(':scope > td');
            if (th && td && visible(th) && visible(td)) add(text(th), text(td));
        }
        // Verified MOC card registration markup: label text with a child span.
        for (const div of all(scope, 'div')) {
            const label = ownText(div);
            if (!fieldOf(label)) continue;
            const spans = Array.from(div.children).filter(child => child.tagName === 'SPAN' && visible(child));
            if (spans.length === 1) add(label, text(spans[0]));
        }
        const record = {};
        for (const [field, values] of Object.entries(found)) {
            if (values.size > 1) return {error: 'Conflicting values for ' + field};
            record[field] = [...values][0];
        }
        record.present_fields = [...present];
        return {record};
    }

    if (mode === 'results') {
        const origin = new URL(document.baseURI).origin;
        const detailPath = /^\/(?:kh\/|en\/)?view-instance\/[^/]+\/?$/;
        const anchors = all(root, 'a[href]').filter(anchor => {
            try {
                const url = new URL(anchor.getAttribute('href'), document.baseURI);
                return url.origin === origin && detailPath.test(url.pathname);
            } catch { return false; }
        });
        // Count the observed card class and structurally equivalent articles.
        // This detects a card whose company link disappeared after a CSS change.
        const cards = all(root, 'article').filter(article => article.matches('.card') ||
            (all(article, 'h3').length === 1 &&
             (all(article, 'div').some(div => fieldOf(ownText(div)) === 'registration_number') ||
              all(article, 'dt,th').some(node => fieldOf(text(node)) === 'registration_number'))));
        if (cards.length && cards.length !== anchors.length) {
            return {error: `Found ${cards.length} MOC cards but ${anchors.length} company detail links.`};
        }
        const records = [];
        const usedCards = new Set();
        for (const anchor of anchors) {
            const article = anchor.closest('article');
            if (!article || !root.contains(article) || !visible(article) || usedCards.has(article)) {
                return {error: 'A company link is missing a unique result article.'};
            }
            usedCards.add(article);
            const heading = anchor.querySelector('h3');
            if (!heading || !visible(heading) || !text(heading)) {
                return {error: 'A company detail link has no visible company heading.'};
            }
            const extracted = fields(article);
            if (extracted.error) return {error: extracted.error};
            const record = extracted.record;
            const headingText = text(heading);
            const headingNumber = /\((\d{6,})\)\s*$/.exec(headingText)?.[1] || '';
            if (record.registration_number && headingNumber && record.registration_number !== headingNumber) {
                return {error: 'Company heading and registration label disagree.'};
            }
            record.registration_number ||= headingNumber;
            if (!record.registration_number) return {error: 'A company card has no registration number.'};
            record['Company Name'] = headingText;
            record.detail_url = new URL(anchor.getAttribute('href'), document.baseURI).href;
            records.push(record);
        }
        return {records, card_count: anchors.length};
    }

    if (mode === 'general') {
        const extracted = fields(root);
        if (extracted.error) return extracted;
        if (!extracted.record.registration_number) {
            return {error: 'The detail page has no labeled registration number.'};
        }
        return extracted;
    }

    if (mode === 'address') {
        const tabs = all(root, 'a[href^="#"]').filter(anchor =>
            /^(?:អាសយដ្ឋាន|address)$/i.test(text(anchor)));
        if (tabs.length !== 1) return {error: `Expected one Address tab; found ${tabs.length}.`};
        const id = tabs[0].getAttribute('href').slice(1);
        const panel = document.getElementById(id);
        if (!panel || !root.contains(panel) || !visible(panel)) {
            return {error: 'The Address tab panel is not visible.'};
        }
        const articles = all(panel, 'article');
        const offices = articles.filter(article =>
            /^(?:អាសយដ្ឋានទីស្នាក់ការចុះបញ្ជី|registered office)/i.test(
                text(article.querySelector('h3'))));
        const candidates = offices.length ? offices : articles;
        if (candidates.length !== 1) {
            return {error: `Expected one business Address article; found ${candidates.length}.`};
        }
        const article = candidates[0];
        const extracted = fields(article);
        if (extracted.error) return extracted;
        const record = extracted.record;
        if (!record.present_fields.some(field => ['Phone', 'Email', 'address_start_date'].includes(field))) {
            return {error: 'The Address article has no labeled contact or Start Date fields.'};
        }
        // Contact fallbacks stay inside the registered business Address article.
        if (!record.Email) {
            const mail = all(article, 'a[href^="mailto:"]').map(a => a.getAttribute('href').slice(7).split('?')[0]);
            record.Email = [...new Set(mail)].join('; ') ||
                (text(article).match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/) || [])[0] || '';
        }
        if (!record.Phone) {
            const phone = all(article, 'a[href^="tel:"]').map(a => a.getAttribute('href').slice(4));
            record.Phone = [...new Set(phone)].join('; ') ||
                (text(article).match(/\+\d[\d ()-]{6,19}\d/) || [])[0] || '';
        }
        return {record};
    }
    return {error: 'Unknown extraction mode.'};
}"""


def _payload(page: Any, mode: str) -> dict[str, Any]:
    result = page.evaluate(_EXTRACT_SCRIPT, mode)
    if result.get("error"):
        raise ValueError(result["error"])
    return result


def extract_results(page: Any) -> list[dict[str, Any]]:
    """Read every MOC card; malformed or unmatched cards stop the run."""
    payload = _payload(page, "results")
    rows = []
    for raw in payload["records"]:
        record = {field: str(raw.get(field) or "").strip() for field in EXPORT_FIELDS}
        record.update({field: str(raw.get(field) or "").strip() for field in
                       ("registration_number", "detail_url")})
        record["Company Name"] = clean_company_name(record["Company Name"], record["registration_number"])
        record["identity"] = result_identity(record)
        rows.append(record)
    if len(rows) != payload["card_count"]:
        raise ValueError("The number of parsed businesses differs from the visible MOC cards.")
    return rows


def parse_detail(page: Any, section: str = "general") -> dict[str, Any]:
    """Read the verified general table or the visible Address article."""
    if section not in {"general", "address"}:
        raise ValueError("Unknown business detail section.")
    raw = _payload(page, section)["record"]
    result: dict[str, Any] = {field: str(raw.get(field) or "").strip() for field in
                              ("Phone", "Email", "Registration Date", "address_start_date",
                               "registration_number")}
    if raw.get("Company Name"):
        result["Company Name"] = clean_company_name(raw["Company Name"], raw.get("registration_number", ""))
    result["present_fields"] = list(raw.get("present_fields") or [])
    return result
