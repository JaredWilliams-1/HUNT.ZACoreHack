#!/usr/bin/env python3
"""
za_email_hunter.py

Crawls a list of .za domains, finds email signup/input fields, and tests
them against edge-case but RFC-valid email addresses (new gTLDs, non-ASCII
domains) to find broken validation regexes.

Built for "THE HUNT" bug bounty rules:
  - max 3 logged bugs per domain
  - rate-limited requests (politeness delay + concurrency cap)
  - captures screenshot + rejection evidence for each find

Usage:
    python3 za_email_hunter.py --input domains.txt --output-dir results \
        --concurrency 4 --delay 3 --max-per-domain 3

domains.txt: one URL per line, e.g.
    https://example.co.za
    https://signup.example.africa
"""

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Test payloads: technically valid emails that a bad regex commonly rejects.
# Mix of new gTLDs called out on the slide + a couple of non-ASCII / IDN
# edge cases (Unicode local-part, punycode-able domain).
# ---------------------------------------------------------------------------
TEST_EMAILS = [
    "jared@number.africa",
    "jared@info.joburg",
    "jared@test.capetown",
    "jared@test.durban",
    "jared@mail.web.za",
    "jared@mail.org.za",
    "first.last+tag@number.africa",      # plus-addressing + new TLD combo
    "jaré[email protected]",                       # accented local-part (non-ASCII)
    "jared@xn--nxasmq6b.africa",         # punycode-style label (often breaks naive regexes)
]

EMAIL_FIELD_SELECTORS = [
    'input[type="email"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
    'input[placeholder*="email" i]',
    'input[aria-label*="email" i]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign up")',
    'button:has-text("Subscribe")',
    'button:has-text("Submit")',
    'button:has-text("Register")',
    'button:has-text("Join")',
]


@dataclass
class Finding:
    domain: str
    page_url: str
    field_selector: str
    field_pattern_attr: str
    email_tried: str
    rejection_type: str   # "native_validity" | "js_message" | "no_navigation" | "ascii_block"
    rejection_text: str
    screenshot_path: str
    timestamp: str


def domain_slug(url: str) -> str:
    host = urlparse(url).netloc or url
    return re.sub(r"[^a-zA-Z0-9.]+", "_", host)


class RateLimiter:
    """Per-domain delay + global concurrency cap."""

    def __init__(self, concurrency: int, delay_seconds: float):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.delay = delay_seconds
        self._last_hit: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait_turn(self, domain: str):
        async with self._lock:
            last = self._last_hit.get(domain, 0)
            now = asyncio.get_event_loop().time()
            wait = self.delay - (now - last)
        if wait > 0:
            # jitter so a batch of requests doesn't land in lockstep
            await asyncio.sleep(wait + random.uniform(0.2, 0.8))
        async with self._lock:
            self._last_hit[domain] = asyncio.get_event_loop().time()


async def find_email_inputs(page):
    inputs = []
    for sel in EMAIL_FIELD_SELECTORS:
        try:
            locs = page.locator(sel)
            count = await locs.count()
            for i in range(count):
                inputs.append((sel, locs.nth(i)))
        except Exception:
            continue
    return inputs


async def find_submit_button(page, field_locator):
    # Prefer a submit control inside the same <form> as the field.
    try:
        form = field_locator.locator("xpath=ancestor::form[1]")
        if await form.count() > 0:
            for sel in SUBMIT_SELECTORS:
                btn = form.locator(sel)
                if await btn.count() > 0:
                    return btn.first
    except Exception:
        pass
    # Fallback: page-wide search
    for sel in SUBMIT_SELECTORS:
        btn = page.locator(sel)
        if await btn.count() > 0:
            return btn.first
    return None


async def test_field(page, domain, page_url, sel, field, email, out_dir, max_findings, findings):
    if len(findings) >= max_findings:
        return

    try:
        await field.scroll_into_view_if_needed(timeout=3000)
        await field.fill("")
        await field.fill(email, timeout=3000)
    except Exception:
        return

    pattern_attr = ""
    try:
        pattern_attr = await field.get_attribute("pattern") or ""
    except Exception:
        pass

    # 1) Native HTML5 validity check (type="email" + pattern attr)
    try:
        is_valid = await field.evaluate("el => el.checkValidity ? el.checkValidity() : true")
        validation_msg = await field.evaluate("el => el.validationMessage || ''")
    except Exception:
        is_valid, validation_msg = True, ""

    if not is_valid:
        shot = out_dir / f"{domain_slug(domain)}_{len(findings)+1}.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            shot = Path("")
        findings.append(Finding(
            domain=domain, page_url=page_url, field_selector=sel,
            field_pattern_attr=pattern_attr, email_tried=email,
            rejection_type="native_validity", rejection_text=validation_msg,
            screenshot_path=str(shot),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        return

    # 2) Try submitting and watch for an inline JS error message or lack of
    #    navigation/success state.
    btn = await find_submit_button(page, field)
    if btn is None:
        return

    before_url = page.url
    try:
        await btn.click(timeout=3000)
        await page.wait_for_timeout(1500)  # let client-side JS react
    except Exception:
        return

    # Look for common inline error patterns near the field
    error_text = ""
    try:
        error_locator = page.locator(
            'text=/invalid|please enter a valid|not a valid|error/i'
        )
        if await error_locator.count() > 0:
            error_text = (await error_locator.first.inner_text()).strip()
    except Exception:
        pass

    navigated = page.url != before_url

    if error_text:
        shot = out_dir / f"{domain_slug(domain)}_{len(findings)+1}.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            shot = Path("")
        findings.append(Finding(
            domain=domain, page_url=page_url, field_selector=sel,
            field_pattern_attr=pattern_attr, email_tried=email,
            rejection_type="js_message", rejection_text=error_text,
            screenshot_path=str(shot),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
    elif not navigated:
        # Ambiguous — form didn't proceed and gave no visible message.
        # Logged at lower confidence; verify manually before submitting as a bug.
        shot = out_dir / f"{domain_slug(domain)}_{len(findings)+1}.png"
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            shot = Path("")
        findings.append(Finding(
            domain=domain, page_url=page_url, field_selector=sel,
            field_pattern_attr=pattern_attr, email_tried=email,
            rejection_type="no_navigation", rejection_text="(no visible error, no navigation — verify manually)",
            screenshot_path=str(shot),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))


async def process_domain(playwright, url, limiter, out_dir, max_findings, page_timeout_ms):
    domain = domain_slug(url)
    findings: list[Finding] = []

    async with limiter.semaphore:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            await limiter.wait_turn(domain)
            try:
                await page.goto(url, timeout=page_timeout_ms, wait_until="domcontentloaded")
            except PWTimeout:
                print(f"[{domain}] timeout loading page, skipping")
                return findings
            except Exception as e:
                print(f"[{domain}] failed to load: {e}")
                return findings

            fields = await find_email_inputs(page)
            if not fields:
                print(f"[{domain}] no email-like field found")
                return findings

            for sel, field in fields:
                for email in TEST_EMAILS:
                    if len(findings) >= max_findings:
                        break
                    await limiter.wait_turn(domain)
                    await test_field(page, url, page.url, sel, field, email,
                                      out_dir, max_findings, findings)
                if len(findings) >= max_findings:
                    break

            print(f"[{domain}] {len(findings)} potential bug(s) found")
        finally:
            await browser.close()

    return findings


async def run(domains, out_dir: Path, concurrency: int, delay: float,
               max_per_domain: int, page_timeout_ms: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(concurrency=concurrency, delay_seconds=delay)
    all_findings: list[Finding] = []

    async with async_playwright() as playwright:
        tasks = [
            process_domain(playwright, url, limiter, out_dir, max_per_domain, page_timeout_ms)
            for url in domains
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            print(f"task error: {r}", file=sys.stderr)
            continue
        all_findings.extend(r)

    # Write JSON + CSV summary
    json_path = out_dir / "findings.json"
    csv_path = out_dir / "findings.csv"

    with open(json_path, "w") as f:
        json.dump([asdict(x) for x in all_findings], f, indent=2)

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(all_findings[0]).keys()) if all_findings else
                                 ["domain", "page_url", "field_selector", "field_pattern_attr",
                                  "email_tried", "rejection_type", "rejection_text",
                                  "screenshot_path", "timestamp"])
        writer.writeheader()
        for x in all_findings:
            writer.writerow(asdict(x))

    print(f"\nDone. {len(all_findings)} total findings written to:\n  {json_path}\n  {csv_path}")
    print("IMPORTANT: review each finding manually before submitting — "
          "'no_navigation' results are lower-confidence and need a human check.")


def main():
    parser = argparse.ArgumentParser(description="Hunt .za signup forms for broken email/TLD validation.")
    parser.add_argument("--input", required=True, help="Text file, one domain/URL per line")
    parser.add_argument("--output-dir", default="results", help="Where to save screenshots + reports")
    parser.add_argument("--concurrency", type=int, default=4, help="Max domains processed in parallel")
    parser.add_argument("--delay", type=float, default=3.0, help="Min seconds between requests to same domain")
    parser.add_argument("--max-per-domain", type=int, default=3, help="Stop after N findings per domain")
    parser.add_argument("--page-timeout", type=int, default=15000, help="Page load timeout in ms")
    args = parser.parse_args()

    domains_file = Path(args.input)
    if not domains_file.exists():
        print(f"Input file not found: {domains_file}", file=sys.stderr)
        sys.exit(1)

    domains = [
        line.strip() for line in domains_file.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not domains:
        print("No domains found in input file.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(
        domains=domains,
        out_dir=Path(args.output_dir),
        concurrency=args.concurrency,
        delay=args.delay,
        max_per_domain=args.max_per_domain,
        page_timeout_ms=args.page_timeout,
    ))


if __name__ == "__main__":
    main()