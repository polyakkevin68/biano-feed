#!/usr/bin/env python3
"""Biano termékfeed generátor a futopadoutlet.hu (UNAS) webshophoz.

A sitemap alapján végigmegy a termékoldalakon, kiolvassa a schema.org
JSON-LD adatokat (név, ár, kép, cikkszám, EAN, márka, kategória, készlet),
és Heureka-formátumú XML feedet készít belőle (a Biano ezt elfogadja).

Használat:
    python generate_feed.py                  # -> biano_feed.xml
    python generate_feed.py -o feed.xml --include-out-of-stock

Csak a Python standard könyvtárát használja.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

SHOP = "https://futopadoutlet.hu"
SITEMAP_INDEX = f"{SHOP}/sitemap.xml"
UA = "Mozilla/5.0 (compatible; BianoFeedGenerator/1.0)"

# schema.org availability -> Heureka DELIVERY_DATE (nap); None = kihagyjuk
AVAILABILITY = {
    "InStock": 0,
    "InStoreOnly": 0,
    "LimitedAvailability": 0,
    "OnlineOnly": 0,
    "PreOrder": 14,
    "BackOrder": 14,
    "OutOfStock": None,
    "SoldOut": None,
    "Discontinued": None,
}


def fetch(url: str, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def product_urls() -> list[str]:
    """Termék URL-ek a sitemapből: azok az <url> blokkok, amelyekhez kép tartozik."""
    index = fetch(SITEMAP_INDEX).decode("utf-8")
    sitemaps = re.findall(r"<loc>(.*?)</loc>", index)
    urls: list[str] = []
    for sm in sitemaps:
        body = fetch(html.unescape(sm)).decode("utf-8")
        for block in re.findall(r"<url>(.*?)</url>", body, re.S):
            if "image:loc" in block:
                urls.append(html.unescape(re.search(r"<loc>(.*?)</loc>", block).group(1)))
    return list(dict.fromkeys(urls))


def parse_product(url: str) -> dict | None:
    page = fetch(url).decode("utf-8", errors="replace")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    product, crumbs = None, []
    for b in blocks:
        try:
            d = json.loads(b)
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "Product":
            product = d
        elif d.get("@type") == "BreadcrumbList":
            crumbs = [i.get("name", "") for i in d.get("itemListElement", [])]
    if not product:
        return None

    offer = product.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    price = offer.get("price")
    if price in (None, "", "0", 0):
        return None

    images = product.get("image") or []
    if isinstance(images, str):
        images = [images]
    images = [re.sub(r"\?time=\d+$", "", i) for i in images]
    if not images:
        return None

    avail = (offer.get("availability") or "").rsplit("/", 1)[-1]
    props = {
        (p.get("name") or "").strip(): str(p.get("value") or "").strip()
        for p in product.get("additionalProperty", [])
    }
    brand = (product.get("brand") or {}).get("name") or (product.get("manufacturer") or {}).get("name") or props.get("Márka", "")
    category = [c for c in crumbs if c and c != "Főoldal"] or ([offer["category"]] if offer.get("category") else [])
    desc = re.sub(r"<[^>]+>", " ", product.get("description") or "")
    desc = re.sub(r"\s+", " ", html.unescape(desc)).strip()

    return {
        "id": product.get("sku") or product.get("productID") or url.rsplit("/", 1)[-1],
        "name": html.unescape(product.get("name", "")).strip(),
        "url": product.get("url") or url,
        "price": str(price),
        "images": images,
        "brand": brand,
        "ean": product.get("gtin13") or product.get("gtin") or props.get("EAN Kód", ""),
        "category": " | ".join(category),
        "description": desc,
        "availability": avail,
        "stock": offer.get("inventoryLevel"),
        "params": {k: v for k, v in props.items() if k and v and "Allegro" not in k},
    }


def to_xml(products: list[dict]) -> str:
    def el(tag: str, value) -> str:
        return f"    <{tag}>{escape(str(value))}</{tag}>\n"

    out = ['<?xml version="1.0" encoding="utf-8"?>\n<SHOP>\n']
    for p in products:
        out.append("  <SHOPITEM>\n")
        out.append(el("ITEM_ID", p["id"]))
        out.append(el("PRODUCTNAME", p["name"]))
        out.append(el("PRODUCT", p["name"]))
        if p["description"]:
            out.append(el("DESCRIPTION", p["description"]))
        out.append(el("URL", p["url"]))
        out.append(el("IMGURL", p["images"][0]))
        for img in p["images"][1:]:
            out.append(el("IMGURL_ALTERNATIVE", img))
        out.append(el("PRICE_VAT", p["price"]))
        out.append(el("CURRENCY", "HUF"))
        if p["brand"]:
            out.append(el("MANUFACTURER", p["brand"]))
        if p["ean"]:
            out.append(el("EAN", p["ean"]))
        if p["category"]:
            out.append(el("CATEGORYTEXT", p["category"]))
        delivery = AVAILABILITY.get(p["availability"], 0)
        out.append(el("DELIVERY_DATE", delivery if delivery is not None else 30))
        if p["stock"] is not None:
            out.append(el("STOCK_QUANTITY", p["stock"]))
        for k, v in p["params"].items():
            out.append(f"    <PARAM>\n  {el('PARAM_NAME', k)}  {el('VAL', v)}    </PARAM>\n")
        out.append("  </SHOPITEM>\n")
    out.append("</SHOP>\n")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="biano_feed.xml")
    ap.add_argument("--include-out-of-stock", action="store_true",
                    help="az elfogyott termékeket is betenni (alapból kimaradnak, hogy ne fizessen rájuk kattintást)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-products", type=int, default=10,
                    help="ennél kevesebb termék esetén nem írja felül a feedet (védelem hibás futás ellen)")
    ap.add_argument("--category", default="",
                    help="csak az ezt tartalmazó kategóriájú termékek (kis/nagybetű nem számít), pl. Masszázsfotel")
    ap.add_argument("--brand", default="",
                    help="csak az ezzel kezdődő gyártójú termékek, pl. 'HC Home'")
    args = ap.parse_args()

    urls = product_urls()
    print(f"{len(urls)} termékoldal a sitemapben", file=sys.stderr)

    def work(u):
        try:
            return parse_product(u)
        except Exception as e:
            print(f"HIBA {u}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(work, urls))

    products, seen, skipped_stock = [], set(), 0
    for p in results:
        if not p or p["id"] in seen:
            continue
        if args.category and args.category.lower() not in p["category"].lower():
            continue
        if args.brand and not p["brand"].lower().startswith(args.brand.lower()):
            continue
        if not args.include_out_of_stock and AVAILABILITY.get(p["availability"], 0) is None:
            skipped_stock += 1
            continue
        seen.add(p["id"])
        products.append(p)

    if len(products) < args.min_products:
        print("Túl kevés termék, a feed nem lett felülírva.", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(to_xml(products))
    print(f"{len(products)} termék kiírva -> {args.output} "
          f"({skipped_stock} elfogyott kihagyva, {sum(r is None for r in results)} nem termék/hibás oldal)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
