from __future__ import annotations
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yfinance as yf
from anthropic import Anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_MAX_TOKENS = 1800
SEC_DELAY = 0.25
TARGET_FORMS = {"10-Q", "10-K"}

SEC_HEADERS = {"User-Agent": "trading_os_v3 research contact@example.com", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
ARCHIVE_HEADERS = {"User-Agent": "trading_os_v3 research contact@example.com", "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}

SYSTEM_PROMPT = """
You are an elite hedge fund analyst combining:
- forensic short seller skepticism
- deep value discipline
- earnings quality analysis
- supply chain realism
- valuation discipline

You will receive:
1. Current market valuation metrics
2. Most recent SEC filing sections (Item 1A Risk Factors, Item 7 MD&A)
3. Prior SEC filing sections for quarter-over-quarter comparison

Your task is to identify what changed, what matters, and whether price reflects reality.

STRICT OUTPUT FORMAT IN MARKDOWN:
# {TICKER} Investment Memo

## 1. Risk Scorecard (1 = low risk, 10 = severe risk)
- Demand Risk: X/10
- Balance Sheet Risk: X/10
- Inventory Risk: X/10
- Valuation Stretch Risk: X/10

## 2. QoQ Delta (Most Important)
List risks that were Added, Removed, Worsened, or Softened.

## 3. Narrative vs Reality
What management implies versus what filing language actually suggests.

## 4. Valuation Context
Explain whether valuation looks Cheap, Fair, Rich, or Dangerous based on the sector metrics provided.

## 5. Asymmetric Verdict
Choose one: [LONG WATCHLIST / VALUE PLAY / NEUTRAL / VULNERABLE / SHORT WATCHLIST]

## 6. What Would Change My Mind
Give 3 measurable conditions that would invalidate your thesis.

Be concise, sharp, evidence-based, and skeptical. Never use generic praise.
"""

def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent

def load_tickers(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8") as f: data = json.load(f)
    tickers = set()
    def add(v):
        if isinstance(v, str) and v.strip(): tickers.add(v.strip().upper())
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str): add(x)
            elif isinstance(x, dict):
                add(x.get("ticker"))
                add(x.get("symbol"))
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str): add(item)
                    elif isinstance(item, dict):
                        add(item.get("ticker"))
                        add(item.get("symbol"))
    return sorted(tickers)

def get_json(url: str, headers: dict) -> dict:
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def get_text(url: str, headers: dict) -> str:
    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    return r.text

def get_cik_map() -> dict[str, str]:
    data = get_json("https://www.sec.gov/files/company_tickers.json", ARCHIVE_HEADERS)
    out = {}
    for row in data.values():
        ticker = str(row["ticker"]).upper()
        cik = str(row["cik_str"]).zfill(10)
        out[ticker] = cik
    return out

def get_recent_filings(cik: str, count: int = 2):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = get_json(url, SEC_HEADERS)
    recent = data["filings"]["recent"]
    forms, acc, docs = recent["form"], recent["accessionNumber"], recent["primaryDocument"]
    out = []
    for form, accession, doc in zip(forms, acc, docs):
        if form.upper() in TARGET_FORMS:
            clean = accession.replace("-", "")
            cik_strip = cik.lstrip("0")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_strip}/{clean}/{doc}"
            out.append({"form": form, "accession": accession, "url": filing_url})
            if len(out) >= count: break
    return out

def strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    return re.sub(r"\n{2,}", "\n", text)

def extract_between(text: str, starts: list[str], ends: list[str]) -> str:
    start_idx = None
    for s in starts:
        m = re.search(s, text, re.I)
        if m:
            start_idx = m.start()
            after = m.end()
            break
    if start_idx is None: return ""
    end_idx = len(text)
    tail = text[after:]
    for e in ends:
        m2 = re.search(e, tail, re.I)
        if m2:
            candidate = after + m2.start()
            end_idx = min(end_idx, candidate)
    return text[start_idx:end_idx].strip()

def extract_sections(text: str):
    item1a = extract_between(text, [r"\bitem\s*1a\b.*risk factors", r"\bitem\s*1a\b"], [r"\bitem\s*1b\b", r"\bitem\s*2\b"])
    item7 = extract_between(text, [r"\bitem\s*7\b.*management", r"\bitem\s*7\b"], [r"\bitem\s*7a\b", r"\bitem\s*8\b"])
    return item1a, item7

def get_market_data(ticker: str) -> str:
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return f"Sector: {info.get('sector', 'Unknown')}\nIndustry: {info.get('industry', 'Unknown')}\nForward PE: {info.get('forwardPE', 'N/A')}\nTrailing PE: {info.get('trailingPE', 'N/A')}\nPrice/Sales: {info.get('priceToSalesTrailing12Months', 'N/A')}\nPrice/Book: {info.get('priceToBook', 'N/A')}\nEV/EBITDA: {info.get('enterpriseToEbitda', 'N/A')}\nMarket Cap: {info.get('marketCap', 'N/A')}"
    except Exception:
        return "Market data unavailable."

def analyze(client: Anthropic, ticker: str, market_data: str, latest_1a: str, latest_7: str, prior_1a: str, prior_7: str) -> str:
    if not latest_1a.strip(): latest_1a = "[EXTRACTION FAILED - SEC formatting prevented parsing of Item 1A]"
    if not latest_7.strip(): latest_7 = "[EXTRACTION FAILED - SEC formatting prevented parsing of Item 7]"
    if not prior_1a.strip(): prior_1a = "[EXTRACTION FAILED - SEC formatting prevented parsing of Item 1A]"
    if not prior_7.strip(): prior_7 = "[EXTRACTION FAILED - SEC formatting prevented parsing of Item 7]"

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        system=SYSTEM_PROMPT.replace("{TICKER}", ticker),
        messages=[{"role": "user", "content": f"Ticker: {ticker}\n\n=== CURRENT MARKET DATA ===\n{market_data}\n\n=== LATEST FILING ITEM 1A ===\n{latest_1a}\n\n=== LATEST FILING ITEM 7 ===\n{latest_7}\n\n=== PRIOR FILING ITEM 1A ===\n{prior_1a}\n\n=== PRIOR FILING ITEM 7 ===\n{prior_7}"}]
    )
    out = [block.text for block in msg.content if getattr(block, "type", "") == "text"]
    return "\n".join(out)

def append_report(path: Path, ticker: str, report: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## {ts} | {ticker}\n\n{report}\n\n---\n\n")

def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key: raise ValueError("Missing LLM_API_KEY")
    client = Anthropic(api_key=api_key)
    root = repo_root()
    portfolio = root / "portfolio_state.json"
    report_path = root / "trading_os" / "reports" / "audit_log.md"
    tickers = load_tickers(portfolio)
    cik_map = get_cik_map()

    for ticker in tickers:
        try:
            print(f"[{ticker}] Processing...")
            cik = cik_map[ticker]
            market_data = get_market_data(ticker)
            filings = get_recent_filings(cik, 2)
            if len(filings) < 2: raise Exception("Need two recent filings")
            latest, prior = filings[0], filings[1]

            time.sleep(SEC_DELAY)
            latest_text = strip_html(get_text(latest["url"], ARCHIVE_HEADERS))
            time.sleep(SEC_DELAY)
            prior_text = strip_html(get_text(prior["url"], ARCHIVE_HEADERS))

            latest_1a, latest_7 = extract_sections(latest_text)
            prior_1a, prior_7 = extract_sections(prior_text)

            report = analyze(client, ticker, market_data, latest_1a, latest_7, prior_1a, prior_7)
            append_report(report_path, ticker, report)
            print(f"[{ticker}] Done.")
        except Exception as e:
            append_report(report_path, ticker, f"Failed: {e}")
            print(f"[{ticker}] Failed: {e}")

if __name__ == "__main__":
    main()
