from src.prefilter import detect_assets, is_relevant

cases = {
    "#onsaltın Orta & uzun vade gözüm $3728": ["GOLD"],
    "$Onsaltın $4000 aşağısına kaydıkça #onsgümüş yeni dip yapmıyor": ["GOLD"],
    "S&amp;P'den 4550 , gümüşte 24.20.": ["SPX"],
    "Günaydın, Dow teorisi çok katmanlı": ["SPX"],
    "Kripto rallisine yönelik fikrim değişmedi": ["BTC"],
    "Altının yükselişi sürer mi?": ["GOLD"],
    "Rekor üstüne rekor kıran Borsaları olan ABD Ekonomisi": ["SPX"],
    "Thy’den Aselsan’a tüm büyük hisselerin derin eksi gittiği günde #bist": [],
    "Borsada doğru yatırımda kullanılmayacak cümleler": [],
    "Nasdaq 100 de yeni zirve": ["SPX"],
    "Bitcoin yeni rekorlar kırarken": ["BTC"],
    "Gümüşşşş! #onsgümüş 14 yılın zirvesinde": [],
    "Ortadoğu endeks kapanışları: Mısır Hermes 30": [],
    "The cost of transporting oil is going hockey stick": [],
    "Gold to $5,000 and BTC to 150k by year end. Nvidia is a bubble.": ["BTC", "GOLD", "SPX"],
    "ABD hisse senetleri pahalı": ["SPX"],
    "10-year yield 5%. Don't be fooled. Gold wins.": ["GOLD"],
    # expansion (mined from Jev gate passes the old regex rejected)
    "Not only will a recession rear its head in 2025, but so too will a bear market in stocks and bonds.": ["SPX"],
    "equities were headed back down to August lows": ["SPX"],
    "Ignore the ₿ears": ["BTC"],
    "Should signal a good Q2 for tech and crypto": ["BTC"],
    "we are in deflation! Buy all risk assets!": ["BTC", "SPX"],
    "Money supply is growing at warp speed. Let's sell something scarce: hard assets": ["GOLD"],
    "Goldman Sachs raises its outlook": [],
    "golden cross on the daily": [],
    "Hisseler çok iyi gidiyor": [],
}
generic = {  # no asset named, but market-call language → relevant with assets []
    "No, the bull isn't over and we will be higher by year end.": True,
    "Higher this week imo": True,  # "higher this/from here/by/into" is directional
    "market doesn’t bottom until we get good memes again": True,
    "Boğa daha bitmedi": True,
    "Read the monthly bulletin": False,
    "Nice weather in Manhattan today": False,
}
bad = 0
for text, want in cases.items():
    got = detect_assets(text)
    ok = got == want
    bad += not ok
    print(("ok  " if ok else "FAIL"), got, "|", text[:60])
for text, want in generic.items():
    rel, assets = is_relevant(text)
    ok = rel == want and (not rel or assets == [])
    bad += not ok
    print(("ok  " if ok else "FAIL"), "generic", rel, "|", text[:60])
print("failures:", bad)
