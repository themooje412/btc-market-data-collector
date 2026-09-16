# Kullanılan public endpoint envanteri

Dokümantasyon kontrolü: **2026-09-16**. Güncel resmi URL'ler README'nin kaynak listesinde. Sonuçlar gerçek HTTP isteklerinden `endpoint-smoke.json` içinde saklanır; bu tablo başarılı yanıt garantisi değildir.

| Host | GET path | Amaç | Smoke kontrolü |
|---|---|---|---|
| data-api.binance.vision | `/api/v3/ticker/24hr?symbol=BTCUSDT` | Fiyat + exchange closeTime | lastPrice sayısal |
| data-api.binance.vision | `/api/v3/klines?symbol=BTCUSDT&interval=1m` | Gerçek taker hacmi | taker buy alanı; tam koşuda 1,440 dakika kapsamı |
| api.exchange.coinbase.com | `/products/BTC-USD/ticker` | BTC/USD fiyat | price sayısal, ISO timestamp |
| api.exchange.coinbase.com | `/products/USDT-USD/ticker` | USDT/USD düzeltmesi | price sayısal |
| api.exchange.coinbase.com | `/products/BTC-USD/trades` | Maker-side trade akışı | side ve CB-AFTER; sonraki sayfa daha eski |
| fapi.binance.com | `/fapi/v1/openInterest?symbol=BTCUSDT` | Anlık BTCUSDT OI | openInterest sayısal |
| fapi.binance.com | `/futures/data/openInterestHist?symbol=BTCUSDT&period=5m` | Aynı piyasanın geçmiş OI değeri | sumOpenInterest sayısal |
| fapi.binance.com | `/fapi/v1/premiumIndex?symbol=BTCUSDT` | Mark, index, funding | lastFundingRate sayısal |
| data.binance.vision | `/data/futures/um/daily/metrics/BTCUSDT/...zip` | Resmi gecikmeli günlük futures metrics arşivine erişim kontrolü | Dosya mevcutsa HTTP 200/206; canlı snapshot girdisi değildir |
| www.deribit.com/api/v2 | `/public/get_instruments?currency=any&kind=option&expired=false` | Bütün BTC opsiyon evrenini filtrele | Envanter + dinamik seçilen opsiyon ticker/Greeks |
| www.deribit.com/api/v2 | `/public/get_instruments?currency=BTC&kind=future&expired=false` | Vadeli futures listesi | Envanter + örnek dated-future ticker |
| www.deribit.com/api/v2 | `/public/ticker?instrument_name=...` | Perpetual, option ve dated-future verileri | Üç ürün tipinde alan kontrolleri |

Bu endpointlerin hiçbiri market-data API key'i gerektirmez. Binance USDⓈ-M REST için belgelenen ana adres `fapi.binance.com`'dur; 451 halinde farklı exchange verisi veya belgelenmemiş host kullanılmaz. `data.binance.vision` resmi olsa da günlük/gecikmeli bir arşivdir ve canlı Binance alanlarını doldurmaz. Belgelenen bir endpointin bu çalışma ortamında timeout vermesi deprecated olduğunu kanıtlamaz. Tersine, örnek bir endpointin 200 dönmesi de bütün kontratların, bütün runner bölgelerinin ve gelecekteki bütün saatlerin başarılı olacağını kanıtlamaz. Tam collector'ın kapsam durumunu `latest.json` ve `options_chain.json` üzerinden kontrol edin.
