import akshare as ak

try:
    print("Fetching Eastern Money latest news for Kweichow Moutai (600519)...")
    news = ak.stock_news_em(symbol="600519")
    print('\n===== Top 10 Latest News =====')
    for idx, row in news.head(10).iterrows():
        print(f"{idx+1}. [{row['发布时间']}] {row['新闻标题']}")
        print(f"   Link: {row['新闻链接']}\n")
except Exception as e:
    print(f'Error fetching news: {e}')
