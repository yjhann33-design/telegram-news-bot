import os
import json
import time
import hashlib
import feedparser
import requests
from datetime import datetime

from dotenv import load_dotenv
from newspaper import Article
import anthropic

# =========================
# 설정
# =========================
load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

NEWS_PER_SYMBOL = 2
SENT_FILE = "sent_telegram.json"

# =========================
# 종목 설정
# =========================
TRACKERS = [
    {
        "name": "Rocket Lab",
        "display": "RKLB",
        "keywords": ["Rocket Lab", "RKLB", "Neutron"],
        "rss": [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=RKLB&region=US&lang=en-US",
            "https://news.google.com/rss/search?q=Rocket+Lab+OR+RKLB&hl=en-US&gl=US&ceid=US:en",
        ],
    },
    {
        "name": "MicroStrategy",
        "display": "MSTR",
        "keywords": ["MicroStrategy", "MSTR", "Strategy"],
        "rss": [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=MSTR&region=US&lang=en-US",
            "https://news.google.com/rss/search?q=MicroStrategy+OR+MSTR&hl=en-US&gl=US&ceid=US:en",
        ],
    },
    {
        "name": "로킷헬스케어",
        "display": "로킷헬스케어",
        "keywords": ["로킷헬스케어", "Rokit Healthcare", "ROKIT"],
        "rss": [
            "https://news.google.com/rss/search?q=%EB%A1%9C%ED%82%B7%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4&hl=ko&gl=KR&ceid=KR:ko",
        ],
    },
    {
        "name": "Bitcoin",
        "display": "비트코인",
        "keywords": ["Bitcoin", "BTC", "crypto"],
        "rss": [
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US",
            "https://news.google.com/rss/search?q=Bitcoin+OR+BTC&hl=en-US&gl=US&ceid=US:en",
        ],
    },
]


# =========================
# 중복 방지
# =========================
def load_sent():
    if not os.path.exists(SENT_FILE):
        return set()
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(sent):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent), f, ensure_ascii=False, indent=2)


def make_key(link, title):
    base = (link or "").strip() or " ".join((title or "").strip().lower().split())
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# =========================
# RSS 뉴스 수집
# =========================
def fetch_news(tracker, sent_keys):
    collected = []
    seen = set()

    for rss_url in tracker["rss"]:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title and not link:
                continue

            # 범용 소스는 키워드 필터링
            if "google.com" in rss_url:
                t = title.lower()
                if not any(kw.lower() in t for kw in tracker["keywords"]):
                    continue

            key = make_key(link, title)
            if key in sent_keys or key in seen:
                continue

            # 날짜 추출
            pub_date = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                dt = datetime(*entry.published_parsed[:6])
                pub_date = f"{dt.year}년 {dt.month}월 {dt.day}일"

            seen.add(key)
            collected.append({"title": title, "link": link, "key": key, "date": pub_date})

            if len(collected) >= NEWS_PER_SYMBOL:
                return collected

        time.sleep(0.3)

    return collected[:NEWS_PER_SYMBOL]


# =========================
# 기사 본문 추출
# =========================
def get_article_text(url):
    try:
        article = Article(url)
        article.download()
        article.parse()
        text = article.text.strip()
        return text[:5000] if text else "본문 추출 실패"
    except Exception as e:
        return f"본문 추출 실패: {e}"


# =========================
# AI 분석
# =========================
def analyze(company, title, link, text):
    prompt = f"""다음 뉴스를 한국어로 간결하게 분석해줘.

대상: {company}

형식 (JSON만 출력):
{{
  "title_kr": "한국어 제목",
  "summary": ["핵심 1", "핵심 2"],
  "impact": "펀더멘털 영향 한줄",
  "conclusion": "한줄 결론"
}}

원칙:
- 과장/매수매도 금지
- 사업·제품·경쟁력·수요·규제 관점으로 요약
- 비트코인은 네트워크/제도/매크로 관점

기사 제목: {title}
링크: {link}
본문: {text}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw)
        return data
    except Exception:
        return {"title_kr": title, "summary": ["분석 실패"], "impact": "-", "conclusion": "-"}


# =========================
# 텔레그램 발송
# =========================
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 4096):
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[i:i+4096],
        })
        if not resp.ok:
            print(f"텔레그램 발송 실패: {resp.text}")


def format_message(display, article, ai):
    summary = "\n".join(f"  • {s}" for s in ai.get("summary", []))
    date_line = f"📅 {article['date']}\n" if article.get("date") else ""
    return (
        f"📌 [{display}] {ai.get('title_kr', article['title'])}\n"
        f"{date_line}"
        f"\n"
        f"{summary}\n"
        f"\n"
        f"💡 {ai.get('conclusion', '-')}\n"
        f"🔗 {article['link']}"
    )


# =========================
# 메인
# =========================
def main():
    sent_keys = load_sent()
    new_keys = set(sent_keys)
    total = 0

    today = datetime.now().strftime("%Y-%m-%d")
    send_telegram(f"📩 투자 뉴스 리포트 ({today})")

    for tracker in TRACKERS:
        articles = fetch_news(tracker, sent_keys)
        if not articles:
            print(f"[{tracker['display']}] 새 뉴스 없음")
            continue

        for art in articles:
            print(f"[{tracker['display']}] 처리 중: {art['title'][:40]}...")
            text = get_article_text(art["link"])
            ai = analyze(tracker["name"], art["title"], art["link"], text)
            msg = format_message(tracker["display"], art, ai)
            send_telegram(msg)
            new_keys.add(art["key"])
            total += 1
            time.sleep(0.5)

    if total == 0:
        send_telegram("새로운 뉴스가 없습니다.")

    save_sent(new_keys)
    print(f"완료: {total}개 뉴스 발송")


if __name__ == "__main__":
    main()
