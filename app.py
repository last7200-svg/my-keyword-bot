import csv
import json
import re
import ssl
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import streamlit as st


HISTORY_FILE = Path("history.csv")


class VisibleTextParser(HTMLParser):
    """Extract visible text and title from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._ignore_tags = {"script", "style", "noscript", "svg", "head"}
        self._tag_stack = []
        self._chunks = []
        self._title_chunks = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag.lower())
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        elif tag in self._tag_stack:
            self._tag_stack.remove(tag)
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._title_chunks.append(text)
        current_tag = self._tag_stack[-1] if self._tag_stack else ""
        if current_tag not in self._ignore_tags:
            self._chunks.append(text)

    @property
    def title(self) -> str:
        return " ".join(self._title_chunks).strip()

    @property
    def text(self) -> str:
        return " ".join(self._chunks).strip()


KOREAN_STOPWORDS = {
    "그리고",
    "하지만",
    "또한",
    "또는",
    "에서",
    "에게",
    "으로",
    "보다",
    "하는",
    "했다",
    "합니다",
    "대한",
    "위한",
    "관련",
    "있는",
    "없는",
    "이번",
    "지난",
    "최신",
    "공식",
    "정보",
    "안내",
    "사용",
    "서비스",
    "기능",
    "해요",
    "하는",
    "하나의",
    "받아요",
    "합니다",
    "하세요",
    "했다",
    "되어",
    "됐다",
    "됩니다",
    "있어요",
    "없어요",
    "같은",
    "처럼",
    "위해",
    "통해",
    "대한",
    "의한",
    "에서의",
    "으로의",
    "에게서",
    "까지",
    "부터",
    "에서",
    "에게",
    "으로",
    "보다",
    "이며",
    "이고",
    "이다",
    "였다",
    "였다가",
    "한다",
    "한다면",
    "하면서",
    "하지만",
    "하면",
    "하면요",
    "되면",
    "되요",
    "라고",
    "이라고",
    "입니다",
    "있다",
    "없다",
    "같다",
    "된다",
    "됐다",
    "한다는",
    "하는지",
    "하기",
    "하기에",
    "하기를",
    "되기",
    "받기",
    "주기",
    "보기",
    "쓰기",
    "읽기",
    "가기",
    "오기",
}

ENGLISH_STOPWORDS = {
    "the",
    "and",
    "for",
    "you",
    "with",
    "that",
    "this",
    "from",
    "your",
    "are",
    "was",
    "have",
    "has",
    "will",
    "into",
    "about",
    "their",
    "more",
    "than",
    "how",
    "what",
    "when",
    "where",
    "who",
    "which",
}

GENERIC_URL_WORDS = {"www", "http", "https", "com", "co", "kr", "net", "org"}

KOREAN_PARTICLES = (
    "으로부터",
    "에게서",
    "으로는",
    "으로도",
    "으로서",
    "에서의",
    "처럼",
    "까지",
    "부터",
    "에서",
    "에게",
    "으로",
    "로서",
    "로써",
    "로는",
    "로도",
    "이라",
    "라면",
    "라고",
    "이고",
    "이며",
    "이나",
    "나",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "와",
    "과",
    "도",
    "만",
)

KOREAN_EOMI_SUFFIXES = (
    "합니다",
    "하십시오",
    "하세요",
    "했어요",
    "해요",
    "해라",
    "한다",
    "하는",
    "하며",
    "하면",
    "했다",
    "하자",
    "하기",
    "됩니다",
    "됐어요",
    "되요",
    "된다",
    "되는",
    "되어",
    "됐다",
    "받아요",
    "받는",
    "받다",
    "있다",
    "있고",
    "있어",
    "없다",
    "같다",
)


def normalize_keyword(token: str) -> str:
    token = token.strip().lower()
    token = re.sub(r"[^\w\u3131-\u318E\uAC00-\uD7A3]+", "", token)
    return token


def strip_korean_particle(token: str) -> str:
    for suffix in sorted(KOREAN_PARTICLES, key=len, reverse=True):
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def looks_like_noun(token: str) -> bool:
    if token in KOREAN_STOPWORDS:
        return False
    for suffix in KOREAN_EOMI_SUFFIXES:
        if token.endswith(suffix):
            return False
    return True


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        },
    )
    ssl_context = ssl._create_unverified_context()
    with urlopen(request, timeout=12, context=ssl_context) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
        return raw.decode(charset, errors="replace")


def extract_keywords_from_html(html: str, min_len: int, top_n: int):
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()

    full_text = f"{parser.title} {parser.text}".strip()
    # 2글자 이상 순수 한글 토큰만 우선 추출
    tokens = re.findall(r"[가-힣]{2,}", full_text)

    cleaned_tokens = []
    for token in tokens:
        token = normalize_keyword(token)
        token = strip_korean_particle(token)
        if len(token) < min_len:
            continue
        if token in KOREAN_STOPWORDS or token in ENGLISH_STOPWORDS:
            continue
        if token in GENERIC_URL_WORDS:
            continue
        if not re.fullmatch(r"[가-힣]{2,}", token):
            continue
        if not looks_like_noun(token):
            continue
        cleaned_tokens.append(token)

    freq = Counter(cleaned_tokens)
    return parser.title or "(제목 없음)", freq.most_common(top_n)


def summarize_website_context(html: str, min_len: int, top_n: int = 40) -> dict[str, object]:
    page_title, ranked = extract_keywords_from_html(html, min_len=min_len, top_n=top_n)
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()

    visible_text = re.sub(r"\s+", " ", parser.text).strip()
    snippet = visible_text[:4000]

    return {
        "title": page_title,
        "snippet": snippet,
        "seed_keywords": [keyword for keyword, _count in ranked[:20]],
    }


def build_keyword_generation_prompt(url: str, website_context: dict[str, object], keyword_count: int) -> str:
    seed_keywords = ", ".join(website_context["seed_keywords"]) or "없음"
    snippet = website_context["snippet"] or "본문 추출 없음"
    return f"""
당신은 한국어 SEO/퍼포먼스 마케팅 키워드 전략가입니다.
입력된 홈페이지 텍스트를 바탕으로 해당 비즈니스의 업종, 고객군, 서비스 목적을 추론한 뒤,
홈페이지에 직접 쓰이지 않았더라도 실제 마케팅에 활용할 수 있는 연관 키워드를 생성하세요.

규칙:
1. 출력은 반드시 JSON만 반환하세요.
2. JSON 형식은 다음과 같습니다:
{{
  "business_summary": "한 줄 요약",
  "keywords": [
    {{
      "keyword": "키워드",
      "relevance": 1~100 사이 정수,
      "reason": "왜 이 비즈니스와 관련 있는지 짧게 설명"
    }}
  ]
}}
3. 키워드는 모두 한국어로 작성하세요.
4. 키워드는 검색 의도가 분명한 마케팅 키워드 위주로 생성하세요.
5. 단순 브랜드명 반복, 너무 일반적인 단어, 중복 키워드는 제외하세요.
6. 업종 관련 정보성/비교형/구매의도형 키워드를 적절히 섞으세요.
7. 총 {keyword_count}개 키워드를 생성하세요.

홈페이지 URL: {url}
페이지 제목: {website_context["title"]}
핵심 후보어: {seed_keywords}
본문 발췌:
{snippet}
""".strip()


def extract_json_object(text: str) -> dict[str, object]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or start >= end:
        raise ValueError("AI 응답에서 JSON 객체를 찾지 못했습니다.")
    return json.loads(text[start : end + 1])


def call_openai_keyword_api(api_key: str, model: str, prompt: str) -> dict[str, object]:
    payload = {
        "model": model,
        "input": prompt,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    ssl_context = ssl._create_unverified_context()
    with urlopen(request, timeout=45, context=ssl_context) as response:
        result = json.loads(response.read().decode("utf-8"))
    output_text = result.get("output_text", "").strip()
    if not output_text:
        raise ValueError("OpenAI 응답에서 텍스트 결과를 받지 못했습니다.")
    return extract_json_object(output_text)


def call_gemini_keyword_api(api_key: str, model: str, prompt: str) -> dict[str, object]:
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ssl_context = ssl._create_unverified_context()
    with urlopen(request, timeout=45, context=ssl_context) as response:
        result = json.loads(response.read().decode("utf-8"))

    candidates = result.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini 응답 후보가 없습니다.")
    parts = candidates[0].get("content", {}).get("parts", [])
    output_text = "".join(part.get("text", "") for part in parts).strip()
    if not output_text:
        raise ValueError("Gemini 응답에서 텍스트 결과를 받지 못했습니다.")
    return extract_json_object(output_text)


def generate_related_keywords_with_ai(
    url: str,
    html: str,
    provider: str,
    api_key: str,
    model: str,
    min_len: int,
    keyword_count: int,
) -> dict[str, object]:
    website_context = summarize_website_context(html, min_len=min_len)
    prompt = build_keyword_generation_prompt(
        url=url,
        website_context=website_context,
        keyword_count=keyword_count,
    )
    if provider == "OpenAI":
        result = call_openai_keyword_api(api_key=api_key, model=model, prompt=prompt)
    else:
        result = call_gemini_keyword_api(api_key=api_key, model=model, prompt=prompt)

    keywords = []
    for item in result.get("keywords", []):
        keyword = str(item.get("keyword", "")).strip()
        reason = str(item.get("reason", "")).strip()
        try:
            relevance = int(item.get("relevance", 0))
        except (TypeError, ValueError):
            relevance = 0
        if not keyword:
            continue
        keywords.append(
            {
                "keyword": keyword,
                "relevance": max(0, min(100, relevance)),
                "reason": reason,
            }
        )

    if not keywords:
        raise ValueError("AI가 유효한 키워드를 반환하지 않았습니다.")

    return {
        "page_title": website_context["title"],
        "business_summary": str(result.get("business_summary", "")).strip(),
        "keywords": keywords,
        "seed_keywords": website_context["seed_keywords"],
    }


def ensure_history_file_exists() -> None:
    if HISTORY_FILE.exists():
        return
    with HISTORY_FILE.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(["saved_at", "url", "keyword", "count"])


def load_history_keywords() -> set[str]:
    if not HISTORY_FILE.exists():
        return set()
    keywords = set()
    with HISTORY_FILE.open("r", newline="", encoding="utf-8-sig") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            keyword = normalize_keyword(row.get("keyword", ""))
            if keyword:
                keywords.add(keyword)
    return keywords


def append_keywords_to_history(url: str, keyword_items: list[tuple[str, int]]) -> int:
    ensure_history_file_exists()
    existing = load_history_keywords()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows = []
    for keyword, count in keyword_items:
        norm = normalize_keyword(keyword)
        if not norm or norm in existing:
            continue
        new_rows.append([now, url, norm, count])
        existing.add(norm)

    if not new_rows:
        return 0

    with HISTORY_FILE.open("a", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerows(new_rows)
    return len(new_rows)


def valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def main():
    st.set_page_config(page_title="URL 키워드 추출기", page_icon="🔎", layout="wide")

    st.title("🔎 AI 연관 마케팅 키워드 생성기")
    st.caption("홈페이지 텍스트를 분석한 뒤 AI가 업종을 추론해 연관 마케팅 키워드를 생성하고, history.csv 기반으로 중복을 자동 제외합니다.")

    with st.sidebar:
        st.header("AI 설정")
        provider = st.selectbox("모델 제공사", options=["OpenAI", "Gemini"])
        default_model = "gpt-4.1-mini" if provider == "OpenAI" else "gemini-2.5-flash"
        model = st.text_input("모델명", value=default_model)
        api_key = st.text_input(
            "API 키",
            type="password",
            help="세션 동안만 사용되며 파일에 저장하지 않습니다.",
        )
        keyword_count = st.slider("생성 키워드 수", min_value=10, max_value=50, value=30, step=5)

    left, right = st.columns([2, 1])
    with left:
        url = st.text_input(
            "URL 입력",
            placeholder="https://example.com/blog/marketing",
            help="http 또는 https로 시작하는 URL을 입력하세요.",
        )
    with right:
        st.metric("표시 키워드", 30)

    col1, col2, col3 = st.columns(3)
    with col1:
        min_len = st.selectbox("최소 글자 수", options=[2, 3, 4], index=1)
    with col2:
        auto_save = st.toggle("새 키워드 자동 저장", value=True)
    with col3:
        if st.button("히스토리 초기화", use_container_width=True):
            if HISTORY_FILE.exists():
                HISTORY_FILE.unlink()
            st.success("history.csv를 초기화했습니다.")

    run = st.button("AI 키워드 생성 실행", type="primary", use_container_width=True)

    if run:
        if not valid_url(url):
            st.error("올바른 URL 형식이 아닙니다. http(s) URL을 입력해 주세요.")
            return
        if not api_key.strip():
            st.error("API 키를 입력해 주세요.")
            return

        with st.spinner("홈페이지를 읽고 AI가 연관 키워드를 생성하는 중..."):
            try:
                html = fetch_html(url)
                ai_result = generate_related_keywords_with_ai(
                    url=url,
                    html=html,
                    provider=provider,
                    api_key=api_key.strip(),
                    model=model.strip() or ("gpt-4.1-mini" if provider == "OpenAI" else "gemini-2.5-flash"),
                    min_len=min_len,
                    keyword_count=keyword_count,
                )
            except HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                st.error(f"HTTP 오류: {e.code} ({e.reason})")
                if detail:
                    st.code(detail[:1500])
                return
            except URLError as e:
                st.error(f"URL 접근 실패: {e.reason}")
                return
            except Exception as e:
                st.error(f"처리 중 오류가 발생했습니다: {e}")
                return

        history_keywords = load_history_keywords()
        keyword_items = ai_result["keywords"]
        fresh_rows = [
            item for item in keyword_items if normalize_keyword(item["keyword"]) not in history_keywords
        ]
        fresh_items = [(item["keyword"], item["relevance"]) for item in fresh_rows]

        st.subheader("분석 결과")
        st.write(f"**페이지 제목:** {ai_result['page_title']}")
        if ai_result["business_summary"]:
            st.write(f"**비즈니스 요약:** {ai_result['business_summary']}")
        if ai_result["seed_keywords"]:
            st.write("**홈페이지에서 감지한 핵심 단서:** " + ", ".join(ai_result["seed_keywords"][:10]))

        m1, m2, m3 = st.columns(3)
        m1.metric("AI 생성 후보", len(keyword_items))
        m2.metric("히스토리 제외 후", len(fresh_items))
        m3.metric("기존 히스토리 개수", len(history_keywords))

        if not fresh_items:
            st.info("모든 키워드가 history.csv에 이미 존재합니다. 새로운 키워드가 없습니다.")
            return

        result_rows = []
        for item in fresh_rows:
            result_rows.append(
                {
                    "키워드": item["keyword"],
                    "관련도": item["relevance"],
                    "선정 이유": item["reason"],
                }
            )
        st.dataframe(result_rows, use_container_width=True, hide_index=True)

        if auto_save:
            saved_count = append_keywords_to_history(url, fresh_items)
            st.success(f"새 키워드 {saved_count}개를 history.csv에 저장했습니다.")
        else:
            if st.button("현재 결과를 history.csv에 저장", use_container_width=True):
                saved_count = append_keywords_to_history(url, fresh_items)
                st.success(f"새 키워드 {saved_count}개를 history.csv에 저장했습니다.")


if __name__ == "__main__":
    main()
