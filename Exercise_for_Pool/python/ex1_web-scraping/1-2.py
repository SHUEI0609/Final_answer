import json
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_DIR = Path(__file__).resolve().parent
SEARCH_URL = "https://r.gnavi.co.jp/area/tokyo/rs/"
OUTPUT_FILE = BASE_DIR / "1-2.csv"
CHROMEDRIVER_PATH = BASE_DIR / "chromedriver"
MAX_RECORDS = 50
RESERVE_URLS = 10
WAIT_SECONDS = 3
PAGE_TIMEOUT = 30
HEADLESS = False

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


def create_driver():
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-agent={USER_AGENT}")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-search-engine-choice-screen")

    if HEADLESS:
        options.add_argument("--headless=new")

    if CHROMEDRIVER_PATH.exists():
        service = Service(executable_path=str(CHROMEDRIVER_PATH))
        driver = webdriver.Chrome(service=service, options=options)
    else:
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(PAGE_TIMEOUT)
    return driver


def open_after_wait(driver, url):
    time.sleep(WAIT_SECONDS)
    driver.get(url)


def load_json_ld(driver):
    items = []

    for script in driver.find_elements(By.CSS_SELECTOR, 'script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_attribute("textContent"))
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, list):
            items.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            items.append(data)

    return items


def extract_shop_urls(driver):
    page_urls = []
    shop_url_pattern = re.compile(r"^https://r\.gnavi\.co\.jp/[A-Za-z0-9_-]+/?$")

    for link in driver.find_elements(By.CSS_SELECTOR, 'a[class*="titleLink"]'):
        href = link.get_attribute("href").split("?", 1)[0]
        if shop_url_pattern.match(href):
            page_urls.append(href)

    if page_urls:
        return list(dict.fromkeys(page_urls))

    scripts = driver.find_elements(By.CSS_SELECTOR, "script#__NEXT_DATA__")

    if scripts:
        try:
            next_data = json.loads(scripts[0].get_attribute("textContent"))
            restaurants = next_data["props"]["pageProps"]["data"]["restaurants"]
        except (json.JSONDecodeError, KeyError, TypeError):
            restaurants = []

        for restaurant in restaurants:
            shop_number = restaurant.get("urlShopNo", "")
            if shop_number:
                page_urls.append(urljoin(SEARCH_URL, f"/{shop_number}/"))

    return list(dict.fromkeys(page_urls))


def click_next_page(driver):
    locator = (By.XPATH, "//img[starts-with(@alt, '次（')]/parent::a")
    old_urls = set(extract_shop_urls(driver))
    next_button = WebDriverWait(driver, PAGE_TIMEOUT).until(
        EC.element_to_be_clickable(locator)
    )
    current_url = driver.current_url
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", next_button
    )
    time.sleep(WAIT_SECONDS)
    next_button.click()
    WebDriverWait(driver, PAGE_TIMEOUT).until(EC.url_changes(current_url))
    WebDriverWait(driver, PAGE_TIMEOUT).until(
        lambda current_driver: set(extract_shop_urls(current_driver)) != old_urls
    )


def collect_shop_urls(driver):
    target_count = MAX_RECORDS + RESERVE_URLS
    shop_urls = []
    page = 1

    open_after_wait(driver, SEARCH_URL)

    while len(shop_urls) < target_count:
        if page > 10:
            raise RuntimeError("10ページ以内に必要な店舗URLを取得できませんでした。")

        print(f"検索結果 {page} ページ目を取得しています")
        page_urls = extract_shop_urls(driver)
        if not page_urls:
            raise RuntimeError(
                "検索結果から店舗URLを取得できませんでした。"
                "ぐるなびのHTML構造を確認してください。"
            )

        shop_urls.extend(url for url in page_urls if url not in shop_urls)

        if len(shop_urls) >= target_count:
            break

        click_next_page(driver)
        page += 1

    return shop_urls


def find_table_value(driver, label):
    for heading in driver.find_elements(By.CSS_SELECTOR, "th, dt"):
        heading_text = re.sub(r"\s+", "", heading.text)
        if label not in heading_text:
            continue
        values = heading.find_elements(
            By.XPATH,
            "following-sibling::*[self::td or self::dd][1]",
        )
        if values:
            return values[0]
    return None


def get_restaurant_data(driver):
    for data in load_json_ld(driver):
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


def extract_email(driver):
    for link in driver.find_elements(By.CSS_SELECTOR, 'a[href^="mailto:"]'):
        link_text = re.sub(r"\s+", "", link.text)
        if "お店に直接メールする" not in link_text:
            continue
        href = unquote(link.get_attribute("href"))
        if href.startswith("mailto:"):
            href = href[len("mailto:") :]
        return href.split("?", 1)[0].strip()
    return ""


def get_link_destination(link):
    encoded_value = link.get_attribute("data-o")
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

    href = link.get_attribute("href") or ""
    if not href or href.endswith("#"):
        return ""
    return href


def extract_official_url(driver):
    links = driver.find_elements(By.CSS_SELECTOR, "a")
    for label in HOMEPAGE_LINK_LABELS:
        for link in links:
            link_text = re.sub(r"\s+", "", link.text)
            title = re.sub(r"\s+", "", link.get_attribute("title") or "")
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


def verify_saved_url_ssl(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        context = ssl.create_default_context()
        with socket.create_connection(
            (parsed.hostname, parsed.port or 443), timeout=PAGE_TIMEOUT
        ) as connection:
            with context.wrap_socket(connection, server_hostname=parsed.hostname):
                return True
    except (OSError, ValueError):
        return False


def resolve_official_url(driver, url):
    if not url:
        return "", ""

    try:
        open_after_wait(driver, url)
        final_url = driver.current_url

        if is_unusable_redirect(url, final_url):
            saved_url = url
        else:
            saved_url = final_url

        return saved_url, verify_saved_url_ssl(saved_url)
    except WebDriverException as error:
        print(f"  公式サイトを確認できませんでした: {error.msg.splitlines()[0]}")
        return url, False


def make_full_address(restaurant_data, driver):
    address_data = restaurant_data.get("address", {})
    if isinstance(address_data, dict):
        address = "".join(
            str(address_data.get(key, ""))
            for key in ("addressRegion", "addressLocality", "streetAddress")
        )
        if address:
            return address

    address_cell = find_table_value(driver, "住所")
    if address_cell is None:
        return ""

    address = address_cell.text
    for marker in ("大きな地図で見る", "地図印刷"):
        address = address.split(marker, 1)[0]
    return address.strip()


def scrape_shop(driver, shop_url):
    open_after_wait(driver, shop_url)
    restaurant_data = get_restaurant_data(driver)

    name = str(restaurant_data.get("name", "")).strip()
    telephone = str(restaurant_data.get("telephone", "")).strip()

    if not name:
        name_cell = find_table_value(driver, "店名")
        name = name_cell.text.splitlines()[0].strip() if name_cell else ""
    if not telephone:
        phone_cell = find_table_value(driver, "電話番号")
        phone_text = phone_cell.text if phone_cell else ""
        phone_match = re.search(r"0\d{1,4}-\d{1,4}-\d{3,4}", phone_text)
        telephone = phone_match.group() if phone_match else ""

    full_address = make_full_address(restaurant_data, driver)
    prefecture, municipality, street_number, building = split_address(full_address)
    email = extract_email(driver)
    official_url = extract_official_url(driver)
    final_url, has_ssl = resolve_official_url(driver, official_url)

    return {
        "店舗名": name,
        "電話番号": telephone,
        "メールアドレス": email,
        "都道府県": prefecture,
        "市区町村": municipality,
        "番地": street_number,
        "建物名": building,
        "URL": final_url,
        "SSL": has_ssl,
    }


def main():
    driver = create_driver()

    try:
        shop_urls = collect_shop_urls(driver)
        records = []

        for shop_url in shop_urls:
            if len(records) >= MAX_RECORDS:
                break

            print(f"店舗 {len(records) + 1}/{MAX_RECORDS}: {shop_url}")
            try:
                record = scrape_shop(driver, shop_url)
            except (TimeoutException, WebDriverException) as error:
                message = getattr(error, "msg", str(error)).splitlines()[0]
                print(f"  店舗ページを取得できませんでした: {message}")
                lowered_message = message.lower()
                if any(
                    marker in lowered_message
                    for marker in ("invalid session id", "session deleted", "chrome not reachable")
                ):
                    try:
                        driver.quit()
                    except WebDriverException:
                        pass
                    driver = create_driver()
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
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


if __name__ == "__main__":
    main()
