# 課題1-2 SSL判定の修正・実ブラウザ確認結果

## 修正内容

保存URLの選択とSSL検証を分離した。CAPTCHA等の転送先を採用しない場合は元URLを保持し、その保存URLのホストへTLS接続して証明書チェーンとホスト名を検証する。
Python標準の `ssl.create_default_context()` を使用し、証明書エラーを無視しない。HTTP、TLS接続失敗、証明書エラーはFalse。Chromeのページ取得が失敗した場合も元URLを保持してFalseとする。
転送後ページの `isSecureContext` を元URLの検証結果として流用しない。追加のPythonパッケージは不要。

## 実行環境・方法

- macOS 26.5.2 arm64、Python 3.12.2、Selenium 4.49.0
- 実Chrome 153.0.8010.53（headless）、`acceptInsecureCerts=False`
- `test_ssl_browser.py` から実際の `1-2.py` を読み込み、`create_driver()` と `resolve_official_url()` を実行
- OpenSSLでlocalhost用の自己署名証明書を生成し、ローカルHTTPSサーバーを起動。証明書は信頼ストアへ登録せず、証明書警告も回避しない
- 正常HTTPSにはexample.comを使用。外部サイトの応答やネットワーク状況により再実行結果が変わる場合がある
- 接続失敗はローカルの待受していないポートで再現。実測のChromeエラーは `net::ERR_CONNECTION_TIMED_OUT`

## 実測結果

| ケース | 入力URL | 保存URL | 期待SSL | 実測SSL | 結果 |
|---|---|---|---|---|---|
| 正常HTTPS | https://example.com/ | 入力と同じ | True | True | PASS |
| HTTP | http://localhost:63319/ | 入力と同じ | False | False | PASS |
| 自己署名証明書 | https://localhost:63320/ | 入力と同じ | False | False | PASS |
| 自己署名＋captcha | https://localhost:63320/?captcha=1 | 入力と同じ | False | False | PASS |
| 自己署名＋access-denied | https://localhost:63320/access-denied | 入力と同じ | False | False | PASS |
| 接続失敗 | https://127.0.0.1:63321/ | 入力と同じ | False | False | PASS |
| 有効な証明書＋captcha | https://example.com/?captcha=1 | 入力と同じ | True | True | PASS |
| 有効な証明書＋access-denied | https://example.com/access-denied | 入力と同じ | True | True | PASS |
| HTTPからHTTPSのcaptcha URLへ302転送 | http://localhost:63319/redirect | 元のHTTP URL | False | False | PASS |

最後のケースではChromeの到達URLが `https://example.com/?captcha=1` であることも確認した。保存URLは元のHTTP URLなので、転送先がHTTPSでもSSLはFalse。
CAPTCHAケースはURL文字列およびHTTP 302による転送判定の検証であり、実際のCAPTCHA画面を突破する試験ではない。

実行結果: `PASS: 9 real Chrome cases`

UbuntuコンテナのPython 3.8.10でも、変更コードとテストの構文チェックに成功し、標準SSLコンテキストのホスト名検証が有効であることを確認した。Chromeの実行テスト自体は上記macOS/Python 3.12.2で実施した。

## 再実行

Chrome・OpenSSLと、Python環境にselenium・pandasを用意し、このフォルダで以下を実行する。

```sh
python test_ssl_browser.py
```

ローカルサーバーのポートは実行ごとに変わる。テスト終了時にChrome・サーバーを停止し、一時証明書を削除する。CSVやDBは変更しない。
