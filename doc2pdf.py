#!/usr/bin/env python3
"""
doc2pdf.py — Crawl a documentation site, print each page to PDF (via Playwright/Chromium),
and merge them into a single PDF with bookmarks.

Usage:
  1) Install deps:
       pip install -r requirements.txt
       playwright install chromium

  2) Run either with a sitemap or a start URL:
       # Using sitemap.xml (recommended if available)
       python doc2pdf.py --sitemap https://example.com/sitemap.xml --out docs.pdf --include /docs/

       # Using a start URL for crawling (BFS, same-domain only)
       python doc2pdf.py --start https://example.com/docs/ --out docs.pdf --max-pages 300

  3) (Optional) Narrow/expand scope:
       --include "/docs/,/v1/"  (comma-separated substrings that must appear in the URL)
       --exclude "/blog/,?ref="  (comma-separated substrings to skip)

Notes:
 - Respects robots.txt (disallows are skipped).
 - Applies polite rate limiting.
 - Renders pages with print CSS and background graphics for fidelity.
 - Produces a single merged PDF with per-URL bookmarks.
"""

import argparse
import asyncio
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import List, Set, Tuple
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pypdf import PdfReader, PdfWriter

DEFAULT_HEADERS = {"User-Agent": "doc2pdf/1.0 (+https://github.com/)"}


def load_robots(base_url: str) -> RobotFileParser:
    """Fetch and parse robots.txt for the given base URL.

    Args:
        base_url: The root URL of the site.

    Returns:
        A RobotFileParser object with rules from robots.txt.
    """
    parsed = urllib.parse.urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=10) as client:
            r = client.get(robots_url)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])
    except httpx.HTTPError:
        rp.parse([])
    return rp


def allowed_by_robots(rp: RobotFileParser, url: str) -> bool:
    """Check if the URL is allowed by robots.txt.

    Args:
        rp: Parsed RobotFileParser.
        url: URL to check.

    Returns:
        True if allowed, False otherwise.
    """
    try:
        return rp.can_fetch(DEFAULT_HEADERS["User-Agent"], url)
    except httpx.HTTPError:
        return True


def normalize_url(url: str) -> str:
    """Normalize a URL by removing fragments.

    Args:
        url: Input URL.

    Returns:
        Normalized URL without fragment.
    """
    parsed = urllib.parse.urlparse(url)
    fragless = parsed._replace(fragment="")
    return urllib.parse.urlunparse(fragless)


def same_domain(a: str, b: str) -> bool:
    """Check if two URLs belong to the same domain.

    Args:
        a: First URL.
        b: Second URL.

    Returns:
        True if both URLs share scheme and netloc.
    """
    pa = urllib.parse.urlparse(a)
    pb = urllib.parse.urlparse(b)
    return pa.scheme == pb.scheme and pa.netloc == pb.netloc


def match_filters(url: str, includes: List[str], excludes: List[str]) -> bool:
    """Check if URL matches include/exclude filters.

    Args:
        url: Target URL.
        includes: List of substrings that must be present.
        excludes: List of substrings that must NOT be present.

    Returns:
        True if URL matches filters.
    """
    if includes and not any(s in url for s in includes):
        return False
    if excludes and any(s in url for s in excludes):
        return False
    return True


def extract_links(html: str, base_url: str) -> List[str]:
    """Extract absolute links from HTML content.

    Args:
        html: HTML source.
        base_url: Base URL for resolving relative links.

    Returns:
        List of absolute URLs found in the page.
    """
    soup = BeautifulSoup(html, "lxml")
    return [
        urllib.parse.urljoin(base_url, a["href"]) for a in soup.find_all("a", href=True)
    ]


def retrieve_from_sitemap(sitemap_url: str) -> List[str]:
    """Extract URLs from sitemap.xml.

    Args:
        sitemap_url: URL of the sitemap.

    Returns:
        List of URLs found in <loc> tags.
    """
    urls = []
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, timeout=20) as client:
            r = client.get(sitemap_url)
            r.raise_for_status()
            text = r.text
        for m in re.finditer(r"<loc>(.*?)</loc>", text):
            urls.append(m.group(1).strip())
    except (httpx.HTTPError, httpx.TimeoutException, httpx.RequestError, re.error) as e:
        print(f"[warn] Failed to read sitemap {sitemap_url}: {e}", file=sys.stderr)
    return urls


def crawl(
    start_url: str, includes: List[str], excludes: List[str], max_pages: int
) -> List[str]:
    """Breadth-first crawl of a site to collect URLs.

    Args:
        start_url: Root URL to start crawling.
        includes: Substrings to keep URLs.
        excludes: Substrings to filter out.
        max_pages: Max number of pages to crawl.

    Returns:
        List of collected URLs.
    """
    visited: Set[str] = set()
    queue: List[str] = [start_url]
    out: List[str] = []
    rp = load_robots(start_url)
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=15) as client:
        while queue and len(out) < max_pages:
            url = normalize_url(queue.pop(0))
            if url in visited:
                continue
            visited.add(url)

            if not same_domain(start_url, url):
                continue
            if not match_filters(url, includes, excludes):
                continue
            if not allowed_by_robots(rp, url):
                print(f"[skip robots] {url}")
                continue

            try:
                r = client.get(url)
                if r.status_code >= 400:
                    continue
                out.append(url)
                links = extract_links(r.text, url)
                for link in links:
                    nl = normalize_url(link)
                    if nl not in visited and same_domain(start_url, nl):
                        if match_filters(nl, includes, excludes):
                            queue.append(nl)
                time.sleep(0.3)  # polite delay
            except (httpx.HTTPError, httpx.TimeoutException, httpx.RequestError) as e:
                print(f"[warn] fetch error for {url}: {e}", file=sys.stderr)
    return out

async def render_to_pdf(
    urls: List[str], out_dir: Path, concurrency: int = 4, timeout: int = 45
) -> List[Tuple[str, Path]]:
    """Render multiple URLs to individual PDF files.

    Args:
        urls: List of URLs to render.
        out_dir: Directory to store individual PDFs.
        concurrency: Max parallel rendering tasks.
        timeout: Page load timeout (seconds).

    Returns:
        List of (url, pdf_path) pairs in order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        sem = asyncio.Semaphore(concurrency)
        results: List[Tuple[str, Path]] = []

        async def worker(u: str):
            async with sem:
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(u, wait_until="networkidle", timeout=timeout * 1000)
                    await page.emulate_media(media="print")
                    safe = re.sub(
                        r"[^A-Za-z0-9]+",
                        "_",
                        urllib.parse.urlparse(u).path.strip("/") or "index",
                    )[:100]
                    pdf_path = out_dir / f"{safe}.pdf"
                    await page.pdf(
                        path=str(pdf_path),
                        print_background=True,
                        format="A4",
                        margin={
                            "top": "12mm",
                            "bottom": "12mm",
                            "left": "12mm",
                            "right": "12mm",
                        },
                    )
                    await context.close()
                    results.append((u, pdf_path))
                    print(f"[ok] {u} -> {pdf_path.name}")
                except (asyncio.TimeoutError, RuntimeError, ValueError) as e:
                    print(f"[warn] pdf fail {u}: {e}", file=sys.stderr)

        await asyncio.gather(*(worker(u) for u in urls))
        await browser.close()

    path_map = {u: p for (u, p) in results}
    return [(u, path_map[u]) for u in urls if u in path_map]


def merge_pdfs(pairs: List[Tuple[str, Path]], out_pdf: Path):
    """Merge individual PDFs into a single file with bookmarks.

    Args:
        pairs: List of (url, pdf_path) pairs.
        out_pdf: Output merged PDF file.
    """
    writer = PdfWriter()
    page_offset = 0
    for url, path in pairs:
        try:
            reader = PdfReader(str(path))
            writer.add_outline_item(url, page_number=page_offset)
            for page in reader.pages:
                writer.add_page(page)
            page_offset = len(writer.pages)
        except (OSError, ValueError) as e:
            print(f"[warn] merge fail {path}: {e}", file=sys.stderr)

    with open(out_pdf, "wb") as f:
        writer.write(f)


def parse_csv(s: str) -> List[str]:
    """Convert comma-separated string into list of values."""
    return [x.strip() for x in s.split(",") if x.strip()]


def main():
    """_summary_"""
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--sitemap", help="Sitemap.xml URL")
    grp.add_argument("--start", help="Start URL to crawl (same-domain)")

    ap.add_argument(
        "--include", help="Comma-separated substrings URLs must include", default=""
    )
    ap.add_argument(
        "--exclude", help="Comma-separated substrings URLs must exclude", default=""
    )
    ap.add_argument("--out", help="Output PDF path", required=True)
    ap.add_argument(
        "--max-pages", type=int, default=500, help="Max pages to include (crawl mode)"
    )
    ap.add_argument("--concurrency", type=int, default=4, help="Parallel PDF renders")
    ap.add_argument("--timeout", type=int, default=45, help="Per-page load timeout (s)")
    ap.add_argument(
        "--keep", action="store_true", help="Keep individual page PDFs in ./_build"
    )
    args = ap.parse_args()

    includes = parse_csv(args.include)
    excludes = parse_csv(args.exclude)

    # Gather URLs
    if args.sitemap:
        all_urls = retrieve_from_sitemap(args.sitemap)
        # filter domain from sitemap root
        if not all_urls:
            print("[error] sitemap yielded no URLs", file=sys.stderr)
            sys.exit(2)
        # Same-domain scoping based on sitemap's own domain
        root = args.sitemap
        root_domain = f"{urllib.parse.urlparse(root).scheme}://{urllib.parse.urlparse(root).netloc}"
        filtered = []
        for u in all_urls:
            if not u.startswith(root_domain):
                continue
            u = normalize_url(u)
            if match_filters(u, includes, excludes):
                filtered.append(u)
        urls = sorted(set(filtered))
    else:
        urls = crawl(args.start, includes, excludes, max_pages=args.max_pages)

    if not urls:
        print("[error] No URLs after filtering.", file=sys.stderr)
        sys.exit(2)

    print(f"[info] {len(urls)} URLs selected")

    # Render
    build_dir = Path("./_build")
    pairs = asyncio.run(
        render_to_pdf(
            urls, build_dir, concurrency=args.concurrency, timeout=args.timeout
        )
    )

    if not pairs:
        print("[error] No PDFs rendered successfully.", file=sys.stderr)
        sys.exit(3)

    # Merge
    out_pdf = Path(args.out)
    merge_pdfs(pairs, out_pdf)
    print(f"[done] Wrote {out_pdf.resolve()}")

    # Cleanup
    if not args.keep:
        for _, p in pairs:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            build_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
