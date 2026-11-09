#!/usr/bin/env python3

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

USER_AGENT = "aicdemo SEC company facts comparison camilo.sarmiento.acevedo1@gmail.com"
API_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ANNUAL_FORMS = {"10-K", "10-K/A"}
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
ASSET_TAGS = ("Assets",)
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
NET_INCOME_TAGS = ("NetIncomeLoss",)


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: str
    name: str


@dataclass(frozen=True)
class Fact:
    tag: str
    value: float
    start: str | None
    end: str
    filed: str
    form: str
    accession: str
    fy: int | None

    @property
    def days(self) -> int | None:
        if self.start is None:
            return None
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1


@dataclass
class Analysis:
    company: Company
    revenue: Fact
    prior_revenue: Fact | None
    assets: Fact | None
    operating_income: Fact | None
    net_income: Fact | None

    @property
    def revenue_growth(self) -> float | None:
        if self.prior_revenue is None or self.prior_revenue.value == 0:
            return None
        return self.revenue.value / self.prior_revenue.value - 1

    @property
    def operating_margin(self) -> float | None:
        return ratio(self.operating_income, self.revenue)

    @property
    def net_margin(self) -> float | None:
        return ratio(self.net_income, self.revenue)


COMPANIES = (
    Company("AAPL", "0000320193", "Apple"),
    Company("MSFT", "0000789019", "Microsoft"),
    Company("NVDA", "0001045810", "NVIDIA"),
    Company("GOOGL", "0001652044", "Alphabet"),
    Company("AMZN", "0001018724", "Amazon"),
)


def ratio(numerator: Fact | None, denominator: Fact | None) -> float | None:
    if numerator is None or denominator is None or denominator.value == 0:
        return None
    return numerator.value / denominator.value


def download(company: Company) -> bytes:
    request = urllib.request.Request(
        API_URL.format(cik=company.cik),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(2**attempt)
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Could not download {company.ticker}")


def load_facts(company: Company, data_dir: Path) -> dict:
    path = data_dir / f"{company.ticker}.json"
    if path.exists():
        with path.open("rb") as handle:
            payload = handle.read()
        print(f"Using cached {path}; no file overwritten.")
    else:
        payload = download(company)
        with path.open("xb") as handle:
            handle.write(payload)
        print(f"Downloaded {company.ticker} company facts to {path}.")
        time.sleep(0.2)
    document = json.loads(payload)
    if str(document.get("cik", "")).zfill(10) != company.cik:
        raise ValueError(f"CIK mismatch in {path}")
    return document["facts"]


def make_fact(tag: str, record: dict) -> Fact:
    return Fact(
        tag=tag,
        value=record["val"],
        start=record.get("start"),
        end=record["end"],
        filed=record["filed"],
        form=record["form"],
        accession=record["accn"],
        fy=record.get("fy"),
    )


def usd_records(facts: dict, tag: str) -> list[dict]:
    return facts.get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])


def is_annual(record: dict) -> bool:
    if record.get("form") not in ANNUAL_FORMS or record.get("fp") != "FY":
        return False
    if not record.get("start") or not record.get("end"):
        return False
    days = (date.fromisoformat(record["end"]) - date.fromisoformat(record["start"])).days + 1
    return 330 <= days <= 380


def annual_records(facts: dict, tag: str) -> list[dict]:
    return [record for record in usd_records(facts, tag) if is_annual(record)]


def prior_record(records: list[dict], current: dict, same_filing: bool) -> dict | None:
    candidates = [
        record
        for record in records
        if record["end"] < current["start"]
        and (not same_filing or record["accn"] == current["accn"])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: (record["end"], record["filed"], record["accn"]))


def select_revenues(facts: dict) -> tuple[Fact, Fact | None]:
    candidates = []
    for tag in REVENUE_TAGS:
        records = annual_records(facts, tag)
        for current in records:
            prior = prior_record(records, current, same_filing=True)
            if prior is None:
                prior = prior_record(records, current, same_filing=False)
            candidates.append((tag, current, prior))
    if not candidates:
        raise ValueError("No annual revenue fact in a 10-K filing")

    latest_end = max(current["end"] for _, current, _ in candidates)
    candidates = [candidate for candidate in candidates if candidate[1]["end"] == latest_end]
    with_prior = [candidate for candidate in candidates if candidate[2] is not None]
    if with_prior:
        candidates = with_prior
    tag, current, prior = min(
        candidates,
        key=lambda candidate: (
            REVENUE_TAGS.index(candidate[0]),
            candidate[1]["filed"],
            candidate[1]["accn"],
        ),
    )
    return make_fact(tag, current), make_fact(tag, prior) if prior is not None else None


def select_period_fact(
    facts: dict, tags: tuple[str, ...], start: str | None, end: str
) -> Fact | None:
    for tag in tags:
        records = []
        for record in usd_records(facts, tag):
            if record.get("form") not in ANNUAL_FORMS or record.get("end") != end:
                continue
            if start is not None and not is_annual(record):
                continue
            if record.get("start") != start:
                continue
            records.append(record)
        if records:
            return make_fact(
                tag, max(records, key=lambda record: (record["filed"], record["accn"]))
            )
    return None


def analyze(company: Company, facts: dict) -> Analysis:
    revenue, prior_revenue = select_revenues(facts)
    return Analysis(
        company=company,
        revenue=revenue,
        prior_revenue=prior_revenue,
        assets=select_period_fact(facts, ASSET_TAGS, None, revenue.end),
        operating_income=select_period_fact(
            facts, OPERATING_INCOME_TAGS, revenue.start, revenue.end
        ),
        net_income=select_period_fact(facts, NET_INCOME_TAGS, revenue.start, revenue.end),
    )


def money(fact: Fact | None) -> str:
    return "n/a" if fact is None else f"{fact.value / 1_000_000_000:.1f}"


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fy_label(fact: Fact) -> str:
    return "n/a" if fact.fy is None else f"FY{fact.fy}"


def print_report(analyses: list[Analysis]) -> None:
    print("\n# Latest-fiscal-year comparison")
    print("Source: SEC EDGAR company facts API only; USD facts from 10-K/10-K/A filings.")
    print("Size = revenue and total assets; growth = YoY revenue; margins = operating income/revenue and net income/revenue.")

    print("\n## Fiscal years")
    print("| Ticker | Company | FY | Fiscal-year end | Days | Filing date |")
    print("|---|---|---:|---|---:|---|")
    for analysis in analyses:
        revenue = analysis.revenue
        print(
            f"| {analysis.company.ticker} | {analysis.company.name} | {fy_label(revenue)} "
            f"| {revenue.end} | {revenue.days} | {revenue.filed} |"
        )

    print("\n## Financial comparison")
    print("Dollar values are $B to one decimal; n/a means a required tag or period is unavailable.")
    print("| Ticker | Revenue ($B) | Total assets ($B) | Revenue growth | Operating margin | Net margin |")
    print("|---|---:|---:|---:|---:|---:|")
    for analysis in analyses:
        print(
            f"| {analysis.company.ticker} | {money(analysis.revenue)} | {money(analysis.assets)} "
            f"| {percent(analysis.revenue_growth)} | {percent(analysis.operating_margin)} "
            f"| {percent(analysis.net_margin)} |"
        )

    print("\n## XBRL provenance")
    print("Growth and margins are derived from these source values; every source fact is listed.")
    print("| Ticker | Metric | XBRL tag | Period start | Period end | Value ($B) | Filed | Form | Accession |")
    print("|---|---|---|---|---|---:|---|---|---|")
    for analysis in analyses:
        facts = (
            ("Revenue", analysis.revenue),
            ("Prior-year revenue", analysis.prior_revenue),
            ("Total assets", analysis.assets),
            ("Operating income", analysis.operating_income),
            ("Net income", analysis.net_income),
        )
        for metric, fact in facts:
            if fact is None:
                print(f"| {analysis.company.ticker} | {metric} | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
                continue
            print(
                f"| {analysis.company.ticker} | {metric} | us-gaap:{fact.tag} "
                f"| {fact.start or 'instant'} | {fact.end} | {money(fact)} | {fact.filed} "
                f"| {fact.form} | {fact.accession} |"
            )

    print("\n## Notes")
    ends = {analysis.revenue.end for analysis in analyses}
    if len(ends) > 1:
        details = ", ".join(
            f"{analysis.company.ticker} {analysis.revenue.end}" for analysis in analyses
        )
        print(f"* Fiscal-year mismatch: {details}. These are not common-calendar periods.")
    lengths = {analysis.revenue.days for analysis in analyses}
    if len(lengths) > 1:
        print("* Fiscal-year lengths differ; each Days value includes both the start and end dates.")
    for analysis in analyses:
        if (
            analysis.prior_revenue is not None
            and analysis.prior_revenue.accession != analysis.revenue.accession
        ):
            print(
                f"* {analysis.company.ticker} revenue growth uses current and prior revenue "
                "from different filings; see accessions above."
            )
    print("* Prior-year revenue uses the same tag and, where available, the same filing as current revenue.")


def main() -> None:
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    analyses = [analyze(company, load_facts(company, data_dir)) for company in COMPANIES]
    print_report(analyses)


if __name__ == "__main__":
    main()
