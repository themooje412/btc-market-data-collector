# BTC Market Data Collector

Saatlik BTC spot, gerçek taker hacminden CVD, perpetual OI/funding/basis ve Deribit opsiyon verileri. Bilgisayarınızın açık kalması gerekmez: GitHub Actions çalıştırır. Borsa hesabı, market-data API key'i, ücretli veri servisi veya ek Python paketi gerekmez.

**Durum:** Proje kendi public `themooje412/btc-market-data-collector` repository'sinde çalışır. Runner içindeki özel Coinbase kontrolü `docs/coinbase-runner-check.json`, resmi Binance Futures kaynak kontrolü `docs/binance-futures-runner-check.json`, tam çalışmanın sonuçları `docs/latest-endpoint-check.json` dosyasındadır. Erişilemeyen veri boş bırakılır.

## Kurulum ve yeniden çalıştırma — teknik olmayan kullanıcı

Kurulum tamamlandı. Yeniden çalıştırmak isterseniz **Actions → Collect BTC market data → Run workflow → Run workflow** yolunu kullanın. Ayrıntılar `docs/DEPLOYMENT.md` içinde.

Başka bir repository'ye elle kurulum alternatifi:

1. GitHub'a giriş yapın; **New repository** → isim `btc-market-data-collector` → **Public** → **Create repository**.
2. ZIP'i açın. **uploading an existing file** (veya **Add file → Upload files**) ile proje klasörünün **içindekileri** yükleyin. ZIP dosyasının kendisini yüklemek kurulumu yapmaz. `btc_collector`, `tests`, `scripts`, `docs`, `state`, `README.md` ve `.github` repository'nin kökünde olmalı.
3. Dosya seçici `.github` klasörünü gizliyorsa, **Add file → Create new file** ile `.github/workflows/collect.yml` oluşturup paketteki aynı dosyanın içeriğini yapıştırın. `tests.yml` için de aynı işlemi yapın. Varsayılan dala commit edin.
4. **Settings → Actions → General → Workflow permissions**: **Read and write permissions** seçin, **Save**. Organizasyon politikası bu seçimi engelliyorsa yetkili hesabın izin vermesi gerekir.
5. **Actions → Collect BTC market data → Run workflow → Run workflow**. Kurallar/ilk kullanım ekranı varsa Actions'ı etkinleştirin.
6. Tamamlanan çalışmada **Summary** ekranını açın. `partial`, bazı alanların alınamadığını söyler; sebebi JSON içindeki `reason` alanında bulunur. Yeşil workflow tek başına tüm borsa verilerinin geldiği anlamına gelmez.
7. Repository'de `latest.json` → **Raw**. Tarayıcıdaki adresi ChatGPT'ye verin.

Bu kurulumun raw adresi: `https://raw.githubusercontent.com/themooje412/btc-market-data-collector/main/latest.json`

Kurulumdan sonra Python/terminal kullanmanız gerekmez.

## Otomasyon ve maliyet

- Her saatin **17. dakikası UTC**: `17 * * * *`. Tam saat başındaki yoğunluğu azaltır.
- Sıra: unit/integration test → veri çekme → doğrulama → JSON/CSV/state güncelleme → commit/push.
- Aynı anda iki collector çalışmaz; aynı UTC saatinin tekrar çalışması o saatin CSV satırını **değiştirir**, ikinci satır eklemez. Son denemenin hata olması, önceki başarılı değerin yeni değer diye korunmasına yol açmaz. Önceki commit Git geçmişinde kalır.
- Kaynak hataları diğer kaynakları durdurmaz. Yazılım/veri bütünlüğü hataları, bozuk CSV veya commit yetki hatası workflow'u kırmızı yapar.
- GitHub'ın standart runner'ları public repository'lerde ücretsizdir. Private repo veya ücretli/larger runner seçmeyin. Normal çalışmada artifact yüklenmez; altyapı hatasında küçük teşhis dosyaları 1 gün saklanır.
- GitHub'ın **otomatik ve geçici `GITHUB_TOKEN`** yetkisi yalnızca repository'ye commit için kullanılır. Kullanıcı API key'i, PAT veya repository secret'ı girmez. Market API isteklerinde kimlik doğrulama yoktur.
- GitHub cron için kesin çalışma saati/SLA garantisi vermez; gecikme veya atlanan çalıştırma olabilir. İnaktif public repolarda 60 gün sonra schedule devre dışı kalabilir. GitHub uyarı e-postalarını izleyin; gerekirse Actions'tan tekrar etkinleştirin.
- Borsanın IP/bölge kısıtlaması GitHub runner'ını etkileyebilir. HTTP 403/418/451 durumunda veri `null` olur. Engeli aşmak için proxy veya başka gizli servis eklenmez. Binance ve Deribit OI ayrı ayrı tutulur; birinin yerine sessizce diğeri geçirilmez.

Kaynak: [GitHub ücretlendirme](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [schedule davranışı](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Dosyalar

| Dosya | İçerik |
|---|---|
| `latest.json` | ChatGPT için güncel, özet veri; gross GEX, signed dealer-GEX tahmini, zero-gamma flip ve vade bazında opsiyon özetleri |
| `options_chain.json` | Bütün aktif BTC opsiyon kontratları, her kontratın OI/IV/delta/gamma değeri, tam strike ve vade kırılımları |
| `history.csv` | Saatlik fiyat/OI/funding/basis/CVD/IV/skew/wall/GEX özetleri; alan başına durum, kaynak, zaman ve birim |
| `state/coinbase.json` | Tamamı gözlenmiş tarihsel Coinbase dakika hacimleri; yaklaşık 26 saat |
| `docs/latest-endpoint-check.json` | Son collector çalışmasının HTTP istekleri, sonuçları ve zamanları |
| `docs/coinbase-runner-check.json` | GitHub runner içinden Coinbase ticker, trades ve pagination doğrulaması |
| `docs/binance-futures-runner-check.json` | GitHub runner içinden resmi Binance Futures REST ve resmi gecikmeli veri arşivi kontrolü |
| `docs/endpoint-smoke.json` | Kurulum sırasında ayrı canlı endpoint/pagination/Greeks kontrolünün kanıtı |
| `docs/VALIDATION.md` | Teslim sürümünün doğrulama sonucu ve açık sınırlamaları |

Tüm kontrat ayrıntıları güncel chain dosyasında, eski sürümleri Git commit geçmişindedir. `history.csv` tek kontrat başına saatlik veri tabanı değildir; analizde kullanılan özet zaman serisidir. Git geçmişi büyür; uzun vadeli kullanımda repository boyutunu izleyin. Sistem otomatik olarak Git geçmişini silmez veya force-push yapmaz.

## JSON ve tazelik sözleşmesi

Her ölçüm mümkün olduğunca `value`, `source`, UTC `timestamp`, `unit`, `status`, `data_age_seconds` içerir. Hata/gecikme durumunda `value: null`, `status: error/stale` ve `reason` vardır. Hesap matematiksel olarak tanımsızsa `not_applicable` kullanılır (perpetual annualized basis gibi).

`timestamp` yayının bitişidir; `collection_started_at` başlangıçtır. Snapshot atomik tek bir piyasa anı değildir: kontratlar sıra ile taranır ve her ölçüm kendi zamanını taşır. Spot/futures zamanları alınırken 180 saniye, opsiyonlar 900 saniye sınırına tabi tutulur; yayın anında 15 dakikayı geçen ölçümler ayrıca null yapılır. Gelecek zaman/clock skew için 30 saniyeden fazla sapma reddedilir.

`data_age` **dosyanın üretildiği andaki** yaştır. ChatGPT okuduğunda güncel UTC ile alanın `timestamp`'ini yeniden karşılaştırmalıdır. Eski JSON içinde `status: ok` bulunması verinin şimdi de güncel olduğunu göstermez. Son başarılı üretimin yaşı 90 dakikayı aşıyorsa önce veri gecikmesini belirtin. Dosyanın önbellekten gelmesi halinde timestamp'i esas alın.

## Hesaplamalar

### Spot ve Coinbase Premium

- Binance: BTCUSDT son fiyat, `/api/v3/ticker/24hr`, borsa `closeTime` zaman damgasıyla. Public market-data hostu `data-api.binance.vision`.
- Coinbase: BTC-USD son işlem fiyatı ve son işlem zamanı, Exchange API `/products/BTC-USD/ticker`.
- Ham premium USD = `Coinbase BTC-USD − Binance BTCUSDT`; bu alışıldık karşılaştırma **1 USDT = 1 USD varsayımı** taşır.
- Ham premium bp = `(Coinbase / Binance − 1) × 10,000`.
- `raw_coinbase_premium`: ham farkı USD, bp ve yüzde olarak açıkça yayımlar.
- `fx_adjusted_coinbase_premium`: Binance fiyatını Coinbase USDT-USD fiyatıyla çarpar, ardından aynı farkı USD, bp ve yüzde olarak hesaplar. USDT/USD kotasyonu alınamıyorsa bu alanlar null kalır; ham premium bağımsız kalır.
- Eski `coinbase_premium.usd`, `.bps` ve `.fx_adjusted` yolları geriye uyumluluk için korunur. Bu ölçümler CoinGlass Coinbase Premium Index olarak etiketlenmez ve o ücretli endeksle metodolojik eşdeğerlik iddia etmez.
- Fiyatların zamanları 60 saniyeden fazla farklıysa premium null/stale olur. Pozitif değer Coinbase'in daha pahalı olduğunu gösterir. Son işlem fiyatlarının farkı, eşzamanlı uygulanabilir arbitraj getirisi değildir.

### CVD — agresif işlemler

CVD pencereleri 15m, 1h, 4h ve 24h'dir. Her biri **pencere içindeki net agresif hacim**, başlangıçtan beri sınırsız birikmiş CVD değildir. Pozitif değer taker alış baskısını gösterir. Ana birim BTC, `quote_cvd` USDT/USD'dir.

**Binance:** 1 dakikalık normal `/api/v3/klines` içindeki gerçek borsa taker hacimleri kullanılır:

`minute_delta_BTC = 2 × taker_buy_base_volume − total_base_volume`

`minute_delta_USDT = 2 × taker_buy_quote_volume − total_quote_volume`

Bu, gerçekleşen işlemlerin borsa tarafından toplulaştırılmış agresif taraf hacmidir. Mum rengi, fiyat yönü veya OHLC tahmini kullanılmaz; milyonlarca trade'i ayrı ayrı indirmek aynı hacim toplamı için gerekli değildir. Borsa yayınının kapsadığı işlem evreni esas alınır. Eksik dakika, yinelenen çelişkili mum, hatalı birim veya toplamı aşan taker hacmi reddedilir.

**Coinbase:** `/products/BTC-USD/trades`, en fazla 1,000 işlem/sayfa; `CB-AFTER` ile eskiye doğru gidilir. API'deki `side` **maker tarafıdır**: `sell` → taker alış → pozitif; `buy` → taker satış → negatif. Trade ID ile mükerrer kayıtlar ayıklanır. `size × price` USD hacmidir.

İlk çalışmada 24 saate kadar geçmiş taranır. Sonraki çalışmalarda tamamı gözlenmiş dakika kovaları saklanır, son iki dakika örtüşmeli yeniden çekilerek değiştirilir. Bu tarihsel pencere verisidir; başarısız yeni isteğin yerine eski CVD değerini kullanmak değildir. Yeni istek başarısızsa o çalışmanın Coinbase CVD alanları null olur. Sayfa limiti/zaman bütçesi dolarsa sadece bütün dakikaları kapsanan pencereler yayımlanır. Coinbase yoğunluğuna göre 24h CVD'nin dolması ilk başarılı çalışmadan sonra 24 saati bulabilir.

Pencere sonu çalışmanın başladığı andan önce kapanmış son dakikadır; `[başlangıç, bitiş)` kullanılır. Devam eden dakika dahil değildir. Bu seçim bütün CVD pencerelerini aynı zamana hizalar. Her pencerenin başlangıcı/bitişi ve kapsanan/beklenen dakika sayısı yayımlanır.

### Futures, OI ve funding

- Binance **BTCUSDT USDⓈ-M perpetual** OI: BTC biriminde. Deribit **BTC-PERPETUAL inverse** OI: USD kontrat notional'ı. İkisi küresel BTC OI toplamı değildir; birbirine eklenmez ve değişim hesabında venue değiştirilmez.
- Binance'in belgelenmiş USDⓈ-M REST ana adresi `https://fapi.binance.com`'dur. Runner bu adres için HTTP 451 alırsa Binance futures/OI/funding/basis alanları `null/error` kalır; Deribit, Coinbase veya başka bir venue bu alanlara yazılmaz.
- Resmi `data.binance.vision` arşivi ayrıca erişim açısından kontrol edilir. Günlük futures metrics dosyaları gecikmeli tarihsel arşiv olduğundan canlı saatlik snapshot yerine kullanılmaz.
- OI yüzde değişimi = `(şimdiki OI / aynı piyasanın geçmiş OI değeri − 1) × 100`.
- Binance 5m OI geçmişi ilk çalışmada 1h/4h/24h hesabını destekler. Ayrıca CSV geçmişi kullanılır. Deribit değişimleri kendi snapshotları biriktikçe dolar.
- Hedef geçmiş zamana en yakın nokta ancak **±20 dakika** içindeyse kabul edilir; gerçek lookback süresi ve baseline zamanı kaydedilir. Eski satır sayarak “24 satır = 24 saat” varsayımı yapılmaz.
- Funding `fraction` birimindedir: `0.0001 = %0.01`. Binance `lastFundingRate` son raporlanan oran olarak etiketlenir; doğrulanmış gelecek funding tahmini denmez. Deribit'in `funding_8h` alanı ayrı tanımıyla kaydedilir. Funding aralığının her piyasada sabit 8 saat olduğu varsayılmaz; yıllık funding getirisi uydurulmaz.
- Fiyat ve OI timestampleri CSV'de ayrı saklanır; fiyat/OI kombinasyonu bu geçmişten incelenebilir.

### Basis

`basis = mark_price − index_price`

`basis_bp = (mark_price / index_price − 1) × 10,000`

Aynı piyasa yanıtındaki mark ve spot endeksi kullanılır; farklı timestampteki Coinbase fiyatı ile karıştırılmaz. Perpetual'ın vadesi olmadığı için yıllıklandırılmış basis **null/not_applicable**'dır.

Deribit'in aktif BTC-settled vadeli futures kontratlarında:

`annualized_basis_pct = (mark/index − 1) × 365.25 / vadeye_kalan_gün × 100`

Bu basit yıllıklandırmadır, bileşik getiri veya funding değildir.

### Opsiyon evreni, OI, put/call wall

`get_instruments(currency=any, kind=option, expired=false)` → `base_currency=BTC`, aktif, vadesi geçmemiş vanilla kontratlar. BTC ve USDC gibi farklı settlement currency'ler dahil edilir; option combo'lar dahil edilmez. Her kontrattan public ticker ile OI, mark IV, delta, gamma, index ve forward alınır. Tek kontrat isteği bozulsa da diğerleri devam eder.

Deribit options OI **BTC baz miktarıdır**; kontrat sayısı sanılıp `contract_size` ile ikinci kez çarpılmaz. Toplam ve strike/vade toplamları BTC miktarıyla verilir.

Put wall = en çok put OI bulunan strike. Call wall = en çok call OI bulunan strike. Tüm vadeler toplamı ve her vade ayrı verilir. Eşitlikte küçük strike gösterilir; `tied_strikes` diğerlerini korur. Eksik OI varsa tamamlanmamış toplam/wall null'dır. Bu seviyeler kesin destek/direnç veya dealer hedge yönü değildir.

### ATM IV ve 25-delta skew

IV alanları **yüzde puan** birimindedir: `40 = %40 yıllık volatilite`; `0.40` değildir.

Her vade **ve settlement currency** ayrı değerlendirilir. ATM, o vadenin `underlying_price` forward'ına log mesafesi en yakın ortak call/put strike'ıdır. O strike'ın call/put mark IV ortalaması yayımlanır. Bu nearest-strike yaklaşımıdır; kesintisiz vol yüzeyi modeli değildir.

25D call için delta `+0.25`, put için `−0.25` hedeflenir. Aynı vade/settlement'teki borsa delta değerleri arasında mark IV doğrusal interpolasyonu yapılır. Hedef çevrelenmiyorsa extrapolasyon yapılmaz: null döner. Mark IV bir borsa model değeridir; o fiyattan gerçek hacimli işlem yapılabileceği anlamına gelmez.

`risk_reversal_25d = call_25d_IV − put_25d_IV`

Negatif RR put volatilitesinin daha pahalı olduğunu gösterir. Tersi `put_minus_call_skew_25d` adıyla ayrıca verilir. Üst seviye `skew_25d` alanı **call-minus-put RR** kullanır.

Üst seviye ATM/skew için 7–60 gün aralığında 30 güne en yakın **BTC-settled** vade seçilir. Vade dosyada belirtilir; vadeler karıştırılmaz. Tam vade yapısı `options.by_expiry` içindedir. Vade değiştiğinde headline serisinde roll etkisi olabilir; CSV `option_surface_expiry` bunu kaydeder.

### Gamma concentration ve gross GEX proxy

Her kontrat için:

`gross_gex_proxy = abs(gamma) × OI_BTC × index_price² × 0.01`

Birim: **BTC fiyatında %1 hareket başına USD eşdeğeri delta-notional değişimi**. Gelen long-option gamma negatifse reddedilir. Aynı strike üzerindeki call ve put gamma büyüklükleri toplanır. `share_pct`, strike büyüklüğünün toplam içindeki payıdır. OI zaten BTC olduğu için ikinci kez kontrat büyüklüğü çarpılmaz. Lineer stablecoin kontratlarının USD eşdeğerinde stablecoin/USD paritesi varsayılır; depeg sırasında bu proxy'nin sınırlamasıdır. Gamma borsanın Black–Scholes modeline dayanır; bu hesap gerçek hedge akışını ölçmez.

**Bu gross veri Net Dealer GEX değildir ve ayrı korunur.** Dealer'ın gerçek long/short pozisyonu public OI'dan gözlenemez. Pozitif OI olan bir kontratın gamma'sı eksikse toplam proxy null olur; eksik gamma sıfır varsayılmaz. Sıfır OI kontratlarının eksik gamma'sı toplamı etkilemez.

### Signed dealer-GEX tahmini ve zero-gamma flip

`net_gex_estimate_usd_per_1pct` gözlenmiş dealer pozisyonu değil, açıkça etiketlenmiş bir model tahminidir. Bütün aktif Deribit BTC opsiyonları için Deribit `mark_iv` değeri yüzde puandan ondalığa çevrilir ve Black–Scholes spot gamma yeniden hesaplanır. Risksiz faiz ve temettü oranı sıfırdır; her kontratın gerçek strike'ı ve vade zamanı kullanılır. Spot, zincirde alınmış en yeni geçerli Deribit BTC `index_price` değeridir.

Varsayım: dealer müşteriye karşı call'larda net short, put'larda net long'dur. Buna göre call katkısı negatif, put katkısı pozitiftir:

`signed_gex = dealer_sign × BS_gamma × OI_BTC × spot² × 0.01`

Deribit BTC option OI zaten BTC baz miktarıdır; `contract_size` yeniden çarpılmaz. `gex_by_strike` tüm aktif vadeleri strike bazında toplar. Spotun ±%25 çevresindeki en büyük pozitif ve negatif yoğunluklar ayrıca özetlenir. Pozitif OI'lı bir kontratta OI, IV veya vade girdisi eksikse signed toplam ve flip null/error olur; kısmi zincir tam veri gibi yayımlanmaz.

`zero_gamma_flip`, mevcut spottaki net GEX değerine eşit değildir. Bütün kontratlar mevcut OI ve IV ile spotun %50–%150 aralığında 201 noktada yeniden değerlenir. Toplam signed GEX işaret değiştirdiğinde iki grid noktası arasında doğrusal interpolasyon yapılır; birden fazla kök varsa mevcut spota en yakın olan seçilir. Aralıkta kök yoksa değer `null/not_applicable` ve sebebi açık olur. `spot_to_gamma_flip_pct = (spot − flip) / flip × 100`; spot flip'in üstündeyse `long_gamma`, altındaysa `short_gamma` etiketi verilir. Bu rejim, yalnız belirtilen pozisyon varsayımının sonucudur.

Hesap RetailInterest'i scrape etmez veya ona bağımlı değildir. RetailInterest değerleri ancak dışarıdan manuel sanity check olarak karşılaştırılabilir; farklı kontrat evreni, OI anı, IV yüzeyi, spot zamanı, grid ve dealer pozisyon varsayımları nedeniyle sonuçların eşit olması beklenmez.

CoinGlass liquidation heatmap ve gerçek dealer GEX vendor screenshot'ı **manuel kalır**.

## Rate limit ve hata yaklaşımı

HTTPS GET dışında market işlemi yoktur. TLS doğrulaması açıktır. İstek başına 30 saniye üst sınır, 20 saniye bağlantı sınırı, en fazla 3 deneme ve ortak host bazlı hız sınırı kullanılır. Coinbase/Deribit yaklaşık 3.8 istek/sn, Binance yaklaşık 8.3 istek/sn; kullanılan Binance endpoint ağırlıkları düşük ve tek sembollüdür. Deribit instruments çağrıları ayrıca aralıklanır. Retry-After dikkate alınır; 30 saniyeyi aşan talep bir sonraki saatlik çalışmaya bırakılır. Tekrarlanan bağlantı/5xx hataları devre kesiciyi açar. HTTP 401/403/418/451 yeniden zorlanmaz.

Coinbase için 600 sayfa/480 saniye, option taraması için 720 saniye bütçe vardır; eksik kapsam açıkça işaretlenir. Metadata ile ticker aynı anda atomik değildir; tarama sırasında expire olan kontratın ölçümleri stale yapılır. Bütün değerler önce doğrulanır; NaN/Infinity JSON'a yazılamaz.

## Geliştirici komutları

Python 3.11+ ve curl bulunan Linux/macOS:

```bash
python -m unittest discover -s tests -v
python scripts/probe_endpoints.py
python -m btc_collector.main
python scripts/check_output.py
```

Canlı probe herhangi bir erişim hatasında raporu yazıp exit 1 verir. Normal collector kaynak hatalarında kısmi dosyaları üretir; programlama/veri bütünlüğü hatalarını gizlemez.

## Resmi kaynaklar

Dokümantasyon 16 Eylül 2026'da incelendi. Kullanılan pathler güncel resmi sayfalarda yer alıyor; kullanım dışı futures `/fapi/v1/ticker/price` endpoint'i kullanılmıyor.

- [Binance Spot market-data endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)
- [Binance public market-data base host](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md)
- [Binance USDⓈ-M market data: OI, OI history, mark/funding](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Binance USDⓈ-M resmi REST ana adresi](https://developers.binance.com/en/docs/derivatives/usds-margined-futures/general-info)
- [Binance resmi public veri arşivi](https://github.com/binance/binance-public-data)
- [Coinbase Exchange public APIs](https://docs.cdp.coinbase.com/exchange/introduction/welcome)
- [Coinbase ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)
- [Coinbase trades ve maker-side tanımı](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-trades)
- [Coinbase pagination](https://docs.cdp.coinbase.com/exchange/rest-api/pagination)
- [Coinbase rate limits](https://docs.cdp.coinbase.com/exchange/rest-api/rate-limits)
- [Deribit instruments](https://docs.deribit.com/api-reference/market-data/public-get_instruments)
- [Deribit ticker: OI birimleri, IV, delta ve gamma](https://docs.deribit.com/api-reference/market-data/public-ticker)
- [Deribit rate limits](https://docs.deribit.com/articles/rate-limits)
