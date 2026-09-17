import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://r.gnavi.co.jp/area/tokyo/rs/"
OUTPUT_FILE = Path(__file__).resolve().parent / "1-1.csv"
MAX_RECORDS = 50
WAIT_SECONDS = 3

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

COLUMNS = [
    "店舗名",
    "電話番号",
    "メールアドレス",
    "都道府県",
    "市区町村",
    "番地",
    "建物名",
    "URL",
    "SSL",
]

HOMEPAGE_LINK_LABELS = ("お店のホームページ", "オフィシャルページ")
CAPTCHA_URL_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-chl",
    "/cdn-cgi/challenge-platform/",
    "/sorry/",
    "challenge-form",
    "bot-check",
    "bot_check",
    "access-denied",
    "access_denied",
)
INTERMEDIATE_HOST_SUFFIXES = ("gnavi.co.jp", "gnst.jp", "gurunavi.com")


PREFECTURE_PATTERN = r"北海道|東京都|京都府|大阪府|.{2,3}県"
ADDRESS_PATTERN = re.compile(
    rf"^(?P<prefecture>{PREFECTURE_PATTERN})"
    r"(?P<municipality>.*?)"
    r"(?P<street_number>"
    r"[0-9]+"
    r"(?:条(?:通|西|東|南|北)[0-9]+(?:[-‐‑‒–—―ー−ｰ][0-9]+)*)?"
    r"(?:(?:丁目|番地?|号|[-‐‑‒–—―ー−ｰのノ])\s*[0-9]+)*"
    r"(?:丁目|番地?|号)?"
    r")"
    r"(?P<building>.*)$"
)
FLOOR_SUFFIX_PATTERN = re.compile(r"^(?:F|[~〜～‐‑‒–—―ー−ｰ-]\s*[0-9]+\s*F)$", re.IGNORECASE)


def get_with_delay(session, url, **kwargs):
    time.sleep(WAIT_SECONDS)
    return session.get(url, timeout=20, **kwargs)


def load_json_ld(soup):
    items = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, list):
            items.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            items.append(data)

    return items


def extract_shop_urls(soup):
    page_urls = []

    for data in load_json_ld(soup):
        if data.get("@type") != "ItemList":
            continue

        for item in data.get("itemListElement", []):
            url = item.get("url", "")
            if url and url not in page_urls:
                page_urls.append(urljoin(SEARCH_URL, url))

    if page_urls:
        return page_urls

    next_data_script = soup.select_one("script#__NEXT_DATA__")
    if next_data_script is None:
        return []

    try:
        next_data = json.loads(next_data_script.string or next_data_script.get_text())
        restaurants = next_data["props"]["pageProps"]["data"]["restaurants"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

    for restaurant in restaurants:
        shop_number = restaurant.get("urlShopNo", "")
        if shop_number:
            url = urljoin(SEARCH_URL, f"/{shop_number}/")
            if url not in page_urls:
                page_urls.append(url)

    return page_urls


def collect_shop_urls(session):
    shop_urls = []
    page = 1

    while len(shop_urls) < MAX_RECORDS:
        print(f"検索結果 {page} ページ目を取得しています")
        params = {} if page == 1 else {"p": page}
        response = get_with_delay(session, SEARCH_URL, params=params)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        page_urls = extract_shop_urls(soup)

        if not page_urls:
            raise RuntimeError(
                "検索結果から店舗URLを取得できませんでした。"
                "ぐるなびのHTML構造を確認してください。"
            )

        shop_urls.extend(url for url in page_urls if url not in shop_urls)
        page += 1

    return shop_urls


def find_table_value(soup, label):
    for heading in soup.find_all(["th", "dt"]):
        heading_text = re.sub(r"\s+", "", heading.get_text(" ", strip=True))
        if label not in heading_text:
            continue
        if heading.name == "th":
            return heading.find_next_sibling("td")
        return heading.find_next_sibling("dd")
    return None


def get_restaurant_data(soup):
    for data in load_json_ld(soup):
        if data.get("@type") == "Restaurant":
            return data
    return {}


def normalize_address(address):
    address = re.sub(r"〒?\s*[0-9０-９]{3}-?[0-9０-９]{4}", "", address)
    address = re.sub(r"\s+", " ", address).strip()

    translation_table = str.maketrans(
        "０１２３４５６７８９－―‐",
        "0123456789---",
    )
    return address.translate(translation_table)


def split_address(address):
    address = normalize_address(address)
    match = ADDRESS_PATTERN.match(address)

    if match:
        street_number = match.group("street_number").strip()
        building = match.group("building").strip()

        if FLOOR_SUFFIX_PATTERN.match(building) and re.search(r"[0-9]{2}$", street_number):
            street_number, building = street_number[:-1], street_number[-1] + building

        return (
            match.group("prefecture").strip(),
            match.group("municipality").strip(),
            street_number,
            building,
        )

    prefecture_match = re.match(rf"^(?P<prefecture>{PREFECTURE_PATTERN})", address)
    if prefecture_match:
        prefecture = prefecture_match.group("prefecture")
        municipality = address[prefecture_match.end() :].strip()
        return prefecture, municipality, "", ""

    return "", address, "", ""


def extract_email(soup):
    for email_link in soup.select('a[href^="mailto:"]'):
        link_text = re.sub(r"\s+", "", email_link.get_text(" ", strip=True))
        if "お店に直接メールする" not in link_text:
            continue
        href = unquote(email_link.get("href", ""))
        if href.startswith("mailto:"):
            href = href[len("mailto:") :]
        return href.split("?", 1)[0].strip()
    return ""


def get_link_destination(link):
    encoded_value = link.get("data-o")
    if encoded_value:
        try:
            encoded = json.loads(encoded_value)
            scheme = encoded.get("b", "https")
            destination = encoded.get("a", "")
            if destination:
                if destination.startswith(("http://", "https://")):
                    return destination
                return f"{scheme}://{destination.lstrip('/')}"
        except (json.JSONDecodeError, TypeError):
            pass

    href = link.get("href", "")
    if not href or href == "#":
        return ""
    return urljoin(SEARCH_URL, href)


def extract_official_url(soup):
    links = soup.select("a")
    for label in HOMEPAGE_LINK_LABELS:
        for link in links:
            link_text = re.sub(r"\s+", "", link.get_text(" ", strip=True))
            title = re.sub(r"\s+", "", link.get("title", ""))
            if label not in link_text and label not in title:
                continue
            destination = get_link_destination(link)
            if destination:
                return destination
    return ""


def is_unusable_redirect(original_url, final_url):
    parsed_original = urlparse(original_url)
    parsed_final = urlparse(final_url)
    final_host = (parsed_final.hostname or "").lower()
    original_host = (parsed_original.hostname or "").lower()
    final_value = f"{final_host}{parsed_final.path}?{parsed_final.query}".lower()

    if not final_host:
        return True
    if any(marker in final_value for marker in CAPTCHA_URL_MARKERS):
        return True

    is_intermediate = any(
        final_host == suffix or final_host.endswith(f".{suffix}")
        for suffix in INTERMEDIATE_HOST_SUFFIXES
    )
    return is_intermediate and final_host != original_host


def resolve_official_url(session, url):
    if not url:
        return "", ""

    try:
        response = get_with_delay(session, url, allow_redirects=True, stream=True)
        final_url = response.url
        response.close()
        if is_unusable_redirect(url, final_url):
            return url, urlparse(url).scheme == "https"
        has_ssl = urlparse(final_url).scheme == "https"
        return final_url, has_ssl
    except requests.exceptions.SSLError:
        return url, False
    except requests.RequestException as error:
        print(f"  公式サイトを確認できませんでした: {error}")
        return url, False


def make_full_address(restaurant_data, soup):
    address_data = restaurant_data.get("address", {})
    if isinstance(address_data, dict):
        address = "".join(
            str(address_data.get(key, ""))
            for key in ("addressRegion", "addressLocality", "streetAddress")
        )
        if address:
            return address

    address_cell = find_table_value(soup, "住所")
    if address_cell is None:
        return ""

    address = address_cell.get_text(" ", strip=True)
    for marker in ("大きな地図で見る", "地図印刷"):
        address = address.split(marker, 1)[0]
    return address.strip()


def scrape_shop(session, shop_url):
    response = get_with_delay(session, shop_url)
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    restaurant_data = get_restaurant_data(soup)

    name = str(restaurant_data.get("name", "")).strip()
    telephone = str(restaurant_data.get("telephone", "")).strip()

    if not name:
        name_cell = find_table_value(soup, "店名")
        name = next(name_cell.stripped_strings, "") if name_cell else ""
    if not telephone:
        phone_cell = find_table_value(soup, "電話番号")
        phone_text = phone_cell.get_text(" ", strip=True) if phone_cell else ""
        phone_match = re.search(r"0\d{1,4}-\d{1,4}-\d{3,4}", phone_text)
        telephone = phone_match.group() if phone_match else ""

    full_address = make_full_address(restaurant_data, soup)
    prefecture, municipality, street_number, building = split_address(full_address)

    official_url = extract_official_url(soup)
    final_url, has_ssl = resolve_official_url(session, official_url)

    return {
        "店舗名": name,
        "電話番号": telephone,
        "メールアドレス": extract_email(soup),
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": building,
        "URL": final_url,
        "SSL": has_ssl,
    }


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    shop_urls = collect_shop_urls(session)
    records = []

    for shop_url in shop_urls:
        if len(records) >= MAX_RECORDS:
            break

        print(f"店舗 {len(records) + 1}/{MAX_RECORDS}: {shop_url}")
        try:
            record = scrape_shop(session, shop_url)
        except requests.RequestException as error:
            print(f"  店舗ページを取得できませんでした: {error}")
            continue

        if not record["店舗名"]:
            print("  店舗名を取得できなかったため、この店舗を除外します")
            continue

        records.append(record)

    if len(records) < MAX_RECORDS:
        raise RuntimeError(f"50件に届きませんでした。取得件数: {len(records)}件")

    dataframe = pd.DataFrame(records, columns=COLUMNS)
    dataframe.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print(f"{OUTPUT_FILE} に {len(dataframe)} 件保存しました")


if __name__ == "__main__":
    main()
