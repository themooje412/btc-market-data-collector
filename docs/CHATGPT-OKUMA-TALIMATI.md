# ChatGPT'ye verilecek kısa talimat

Public latest.json adresini aç ve BTC analizini bu snapshot'a dayandır. Önce dosyanın timestamp'ini ve güncel UTC'ye göre yaşını yaz. 90 dakikadan eskiyse veri gecikmesini açıkça belirt; güncel piyasa anı diye sunma. Her ölçümün kendi timestamp'ini de kontrol et.

Sadece status=ok alanları kullan; null/error/stale alanlar için veri yok de. `data_age_seconds` üretim anındaki yaştır, bugün okurken yeniden hesapla. Coinbase Premium'un ham ve USDT/USD-düzeltilmiş değerlerini ayır. CVD'yi ilgili pencere içindeki net taker hacmi olarak yorumla. Binance ve Deribit OI birimlerini/venue'lerini karıştırma.

`skew_25d`, call IV eksi put IV'dir. Hangi vadenin kullanıldığını yaz. Put/call wall OI yoğunluğudur, garanti destek/direnç değildir. Gross GEX proxy'yi Net Dealer GEX diye adlandırma; buradan dealer yönü veya gamma flip çıkarma.

Gerekli olduğunda aynı repo/daldaki history.csv ve options_chain.json dosyalarını kullan. CoinGlass heatmap ve gerçek dealer GEX ekran görüntülerini ayrıca ben sağlayacağım. Collector otomatik emir vermez; bu dosyayı tek başına kesin yön sinyali olarak kullanma.
