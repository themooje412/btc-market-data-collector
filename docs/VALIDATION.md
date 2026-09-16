# Doğrulama raporu

Son tam canlı çalışma: **2026-09-16T11:38:25.780Z**.
Çalışma süresi: **602.2 saniye**. Genel durum: **partial**.

## Otomatik testler

**27 test geçti.** Tam kayıt: `unit-tests.txt`. Hesaplamalar, null/stale davranışı, UTC, saatlik tekilleştirme, iki uçtan integration senaryoları ve HTTP hata davranışı test edildi. Test kaydındaki sentetik hata uyarıları, arıza senaryolarının beklenen çıktılarıdır. Workflow YAML dosyaları ayrıca parse edildi.

## Gerçek endpoint smoke kontrolü

| Kontrol | Sonuç |
|---|---|
| Binance spot | ok |
| Binance CVD | ok |
| Coinbase BTC price | error |
| Coinbase USDT price | error |
| Coinbase trades | error |
| Binance OI | ok |
| Binance OI history | ok |
| Binance mark/funding | ok |
| Deribit perpetual | ok |
| Deribit options inventory | ok |
| Deribit futures inventory | ok |

Bu tablo HTTP/alan doğrulamasıdır. Full collector sonucu aşağıdadır. Coinbase BTC fiyatı, USDT fiyatı ve trade akışı bu ortamda zaman aşımı verdi. Coinbase CVD pagination yönü ve maker/taker işareti sentetik testlerle doğrulandı; gerçek Coinbase pagination başarılı diye işaretlenmedi.

## Gerçek verilerle collector sonucu

| Bölüm | Sonuç |
|---|---|
| Spot binance | ok |
| Spot coinbase | error |
| Spot usdt_usd | error |
| binance CVD 15m | ok |
| binance CVD 1h | ok |
| binance CVD 4h | ok |
| binance CVD 24h | ok |
| coinbase CVD 15m | error |
| coinbase CVD 1h | error |
| coinbase CVD 4h | error |
| coinbase CVD 24h | error |
| binance OI değişimi 1h | ok |
| binance OI değişimi 4h | ok |
| binance OI değişimi 24h | ok |
| deribit OI değişimi 1h | error |
| deribit OI değişimi 4h | error |
| deribit OI değişimi 24h | error |

Aktif BTC opsiyon envanteri: **1468 kontrat**.

| Kontrat alanı | Durum dağılımı |
|---|---|
| open_interest | {'ok': 1468} |
| mark_iv | {'ok': 1468} |
| delta | {'ok': 1468} |
| gamma | {'ok': 1468} |
| Opsiyon toplamı: total_oi | ok |
| Opsiyon toplamı: gross_gex_proxy | ok |
| Opsiyon toplamı: put_wall | ok |
| Opsiyon toplamı: call_wall | ok |

`latest.json`: 121,285 byte; `options_chain.json`: 4,392,977 byte.

## GitHub runner doğrulaması

İlk paket doğrulaması bu raporun üst kısmında korunur. Güncel GitHub runner erişimi ve tam collector sonucu her çalışmada `docs/coinbase-runner-check.json`, `docs/binance-futures-runner-check.json` ve `docs/latest-endpoint-check.json` dosyalarına yeniden yazılır. Kaynak bazındaki güncel durum için bu JSON dosyaları ile `latest.json` esas alınmalıdır.

Bu rapor paket hazırlanırken yapılan doğrulamayı gösterir; gelecekteki API erişiminin veya veri tazeliğinin garantisi değildir.
