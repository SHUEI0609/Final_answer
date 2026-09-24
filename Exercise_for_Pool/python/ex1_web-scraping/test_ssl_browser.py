"""実ChromeによるSSL回帰テスト。実行: python test_ssl_browser.py

必要: selenium, pandas, Chrome, openssl。外部のexample.comに接続する。
一時証明書は信頼ストアに登録せず、Chromeの証明書エラーも無視しない。
"""
import importlib.util
import json
import platform
import socket
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/?captcha=1")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"SSL test page")

    def log_message(self, *args):
        pass


def main():
    spec = importlib.util.spec_from_file_location("scraper", Path(__file__).with_name("1-2.py"))
    scraper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scraper)
    scraper.HEADLESS = True
    servers = []
    driver = None
    with tempfile.TemporaryDirectory(prefix="ssl-browser-test-") as directory:
        cert = str(Path(directory) / "cert.pem")
        key = str(Path(directory) / "key.pem")
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost",
        ], check=True, capture_output=True)
        try:
            for secure in (False, True):
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                if secure:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(cert, key)
                    server.socket = context.wrap_socket(server.socket, server_side=True)
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            http = "http://localhost:{}".format(servers[0].server_port)
            https = "https://localhost:{}".format(servers[1].server_port)
            # ポートを確保したままlistenしないため、接続は拒否される。
            with socket.socket() as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                failed = "https://127.0.0.1:{}/".format(unavailable.getsockname()[1])
                driver = scraper.create_driver()
                print(json.dumps({"python": platform.python_version(), "platform": platform.platform(),
                    "chrome": driver.capabilities.get("browserVersion"),
                    "acceptInsecureCerts": driver.capabilities.get("acceptInsecureCerts")}, ensure_ascii=False), flush=True)
                assert not driver.capabilities.get("acceptInsecureCerts")
                cases = [
                    ("valid_https", "https://example.com/", "https://example.com/", True),
                    ("http", http + "/", http + "/", False),
                    ("self_signed", https + "/", https + "/", False),
                    ("self_signed_captcha", https + "/?captcha=1", https + "/?captcha=1", False),
                    ("self_signed_access_denied", https + "/access-denied", https + "/access-denied", False),
                    ("connection_refused", failed, failed, False),
                    ("valid_https_captcha", "https://example.com/?captcha=1", "https://example.com/?captcha=1", True),
                    ("valid_https_access_denied", "https://example.com/access-denied", "https://example.com/access-denied", True),
                    ("http_to_https_captcha", http + "/redirect", http + "/redirect", False),
                ]
                failures = []
                for name, url, expected_url, expected_ssl in cases:
                    saved_url, actual_ssl = scraper.resolve_official_url(driver, url)
                    passed = (saved_url, actual_ssl) == (expected_url, expected_ssl)
                    final_url = driver.current_url
                    if name == "http_to_https_captcha":
                        passed = passed and final_url == "https://example.com/?captcha=1"
                    print(json.dumps({"case": name, "input_url": url, "browser_url": final_url,
                        "saved_url": saved_url, "expected_ssl": expected_ssl, "actual_ssl": actual_ssl,
                        "passed": passed}, ensure_ascii=False), flush=True)
                    if not passed:
                        failures.append(name)
                assert not failures, failures
                print("PASS: {} real Chrome cases".format(len(cases)), flush=True)
        finally:
            if driver:
                driver.quit()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    main()
