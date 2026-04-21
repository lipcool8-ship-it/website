from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv

SYSTEM_PROMPT = (
    "You are a hostile, skeptical financial auditor. I will provide you with Item 1A and Item 7 "
    "from a company's SEC filing. You must extract only the worst-case physical supply chain "
    "bottlenecks, inventory bloat, and capital expenditure risks. Do not summarize the whole "
    "document. Output a strict markdown report answering: 1. Worst-case admitted scenario. "
    "2. Supply chain expanding/contracting? 3. Inventory/CapEx bloat. 4. Does management's tone "
    "match the SEC filings? 5. Final Verdict (HOLD/CAUTION/VALIDATED)."
)

SEC_HEADERS = {
    "User-Agent": "trading_os/1.0 (research use)",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}
ARCHIVE_HEADERS = {
    "User-Agent": "trading_os/1.0 (research use)",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}
TARGET_FORMS = {"10-K", "10-Q"}
ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_MAX_TOKENS = 1200
SEC_REQUEST_DELAY_SECONDS = 0.25


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_portfolio_tickers(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing portfolio file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    tickers: set[str] = set()

    def add_ticker(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            tickers.add(value.strip().upper())

    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, str):
                add_ticker(entry)
            elif isinstance(entry, dict):
                add_ticker(entry.get("ticker"))
                add_ticker(entry.get("symbol"))
    elif isinstance(payload, dict):
        for key in ("tickers", "symbols", "positions", "portfolio", "holdings"):
            value = payload.get(key)
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, str):
                        add_ticker(entry)
                    elif isinstance(entry, dict):
                        add_ticker(entry.get("ticker"))
                        add_ticker(entry.get("symbol"))
    if not tickers:
        raise ValueError("No tickers found in portfolio_state.json")

    return sorted(tickers)


def _get_cik_map() -> dict[str, str]:
    response = _get_with_retries("https://www.sec.gov/files/company_tickers.json", headers=ARCHIVE_HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()

    mapping: dict[str, str] = {}
    for record in payload.values():
        ticker = str(record.get("ticker", "")).upper()
        cik = str(record.get("cik_str", "")).zfill(10)
        if ticker and cik:
            mapping[ticker] = cik
    return mapping


def _latest_filing_document(cik: str) -> tuple[str, str, str]:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    response = _get_with_retries(url, headers=SEC_HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()

    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    for form, accession, primary_doc in zip(forms, accessions, primary_docs):
        clean_form = str(form).upper()
        if clean_form in TARGET_FORMS:
            accession_no_dashes = str(accession).replace("-", "")
            cik_stripped = str(int(cik))
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession_no_dashes}/{primary_doc}"
            return clean_form, str(accession), filing_url

    raise ValueError(f"No recent 10-K or 10-Q found for CIK {cik}")


def _strip_text_from_filing(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text("\n")
    return re.sub(r"\n{2,}", "\n", text)


def _extract_between(text: str, start_patterns: Iterable[str], end_patterns: Iterable[str]) -> str:
    start_match = None
    for pattern in start_patterns:
        start_match = re.search(pattern, text, flags=re.IGNORECASE)
        if start_match:
            break
    if not start_match:
        return ""

    start_idx = start_match.start()
    end_idx = len(text)
    after_start = text[start_idx + 1 :]

    for pattern in end_patterns:
        end_match = re.search(pattern, after_start, flags=re.IGNORECASE)
        if end_match:
            candidate = start_idx + 1 + end_match.start()
            if candidate < end_idx:
                end_idx = candidate

    return text[start_idx:end_idx].strip()


def _extract_item_sections(text: str) -> tuple[str, str]:
    item_1a = _extract_between(
        text,
        start_patterns=[r"\bitem\s*1a\b.*risk\s*factors", r"\bitem\s*1a\b"],
        end_patterns=[r"\bitem\s*1b\b", r"\bitem\s*2\b"],
    )
    item_7 = _extract_between(
        text,
        start_patterns=[r"\bitem\s*7\b.*management['’]?s\s*discussion", r"\bitem\s*7\b"],
        end_patterns=[r"\bitem\s*7a\b", r"\bitem\s*8\b"],
    )
    return item_1a, item_7


def _anthropic_audit(client: Anthropic, ticker: str, form: str, item_1a: str, item_7: str) -> str:
    if not item_1a:
        item_1a = "Item 1A not found in filing text."
    if not item_7:
        item_7 = "Item 7 not found in filing text."

    max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Ticker: {ticker}\nForm: {form}\n\n"
                    f"Item 1A (Risk Factors):\n{item_1a}\n\n"
                    f"Item 7 (MD&A):\n{item_7}"
                ),
            }
        ],
    )

    text_blocks = []
    for block in message.content:
        if getattr(block, "type", "") == "text":
            text_blocks.append(block.text)
    return "\n".join(text_blocks).strip()


def _get_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    max_attempts: int = 4,
    initial_backoff_seconds: float = 1.0,
) -> requests.Response:
    last_error: Exception | None = None
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.ok:
                return response
            if response.status_code in retryable_statuses:
                raise requests.HTTPError(f"Retryable HTTP status: {response.status_code}", response=response)
            response.raise_for_status()
            return response
        except (requests.RequestException, ValueError) as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code and status_code not in retryable_statuses:
                raise
            last_error = exc
            if attempt == max_attempts:
                break
            time.sleep(initial_backoff_seconds * (2**attempt))

    if last_error:
        raise last_error
    raise RuntimeError(f"Failed request with unknown error: {url}")


def _append_report(path: Path, ticker: str, form: str, accession: str, filing_url: str, report: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()

    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"## {timestamp} | {ticker} | {form} | {accession}\n\n")
        handle.write(f"Source: {filing_url}\n\n")
        handle.write(f"{report}\n\n---\n\n")


def main() -> None:
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise EnvironmentError("LLM_API_KEY is missing. Set it in trading_os/.env")

    repo_root = _repo_root()
    portfolio_path = repo_root / "portfolio_state.json"
    report_path = repo_root / "trading_os" / "reports" / "audit_log.md"

    tickers = _load_portfolio_tickers(portfolio_path)
    cik_map = _get_cik_map()
    client = Anthropic(api_key=api_key)

    for ticker in tickers:
        try:
            cik = cik_map.get(ticker)
            if not cik:
                raise ValueError(f"Ticker not found in SEC ticker map: {ticker}")

            time.sleep(SEC_REQUEST_DELAY_SECONDS)
            form, accession, filing_url = _latest_filing_document(cik)
            time.sleep(SEC_REQUEST_DELAY_SECONDS)
            filing_response = _get_with_retries(filing_url, headers=ARCHIVE_HEADERS, timeout=45)
            filing_response.raise_for_status()
            filing_text = _strip_text_from_filing(filing_response.text)
            item_1a, item_7 = _extract_item_sections(filing_text)

            report = _anthropic_audit(client, ticker, form, item_1a, item_7)
            _append_report(report_path, ticker, form, accession, filing_url, report)
            print(f"Processed {ticker}: {form} {accession}")
        except Exception as exc:
            error_report = f"Processing failed for {ticker}: {exc}"
            _append_report(
                report_path,
                ticker=ticker,
                form="N/A",
                accession="N/A",
                filing_url="N/A",
                report=error_report,
            )
            print(error_report)


if __name__ == "__main__":
    main()
