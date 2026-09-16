# Yayına alma ve işletim

Collector kendi public repository'sinde çalışır:

`https://github.com/themooje412/btc-market-data-collector`

## Otomasyon

- Saatlik çalışma: her saatin 17. dakikası UTC.
- Elle çalışma: **Actions → Collect BTC market data → Run workflow**.
- Workflow önce 27 testi çalıştırır, Coinbase ve resmi Binance Futures kaynaklarını runner içinden ölçer, veriyi toplar ve doğrular.
- Değişen `latest.json`, `history.csv`, `options_chain.json`, Coinbase state ve runner tanılama dosyaları `github-actions[bot]` tarafından `main` dalına commit edilir.
- Market-data API key'i veya repository secret'ı kullanılmaz. Yalnız GitHub'ın geçici `GITHUB_TOKEN` yetkisi kendi repository'sine veri commit etmek için kullanılır.

## Kabul kriterleri

- Collector workflow'u yeşil tamamlanır.
- `latest.json` ve `options_chain.json` geçerli JSON'dur; sayısal alanlar finite olur.
- `history.csv` içinde her UTC saat için en fazla bir kayıt bulunur.
- Coinbase yeni isteği başarısızsa Coinbase alanları `null/error` olur; Binance veya Deribit değeri kullanılmaz.
- Binance Futures resmi REST istekleri HTTP 451 alırsa Binance OI/funding/basis alanları `null/error` kalır.
- Resmi gecikmeli Binance data archive yalnız erişim tanılamasıdır; canlı Binance snapshot yerine kullanılmaz.
- Eksik opsiyon kapsamı ve bilinmeyen dealer yönü açıkça etiketlenir.

Public raw adres:

`https://raw.githubusercontent.com/themooje412/btc-market-data-collector/main/latest.json`

GitHub cron kesin zaman SLA'sı vermez. Exchange IP/bölge kısıtları veya geçici API kesintileri kaynak bazında `null/error` üretebilir; workflow'un yeşil olması her exchange alanının dolu olduğu anlamına gelmez.
