"""본청·도청 메뉴·직속기관안내 페이지에서 외부 도메인 자동 추출.

도청 사이트 안에서 외부로 링크되는 모든 .go.kr / .kr 도메인을 모아 crawl_target 에
등록 가능한 형태로 출력. 등록된 11개 site 와 비교해 누락분 표시.
"""
import re
import sys
import urllib.request
from collections import Counter
from urllib.parse import urlparse

# 도청 메인 + 자주 외부 link 가 있는 메뉴들
SEED_URLS = [
    "https://www.jeonbuk.go.kr/",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000101003005001",  # 직속기관
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000101000000000",  # 도정
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000110000000000",  # 부서별
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000102000000000",  # 민원
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000103000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000104000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000105000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000106000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000107000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000108000000000",
    "https://www.jeonbuk.go.kr/index.jeonbuk?menuCd=DOM_000000109000000000",
]

EXCLUDE_DOMAINS = {
    "www.jeonbuk.go.kr",
    "jeonbuk.go.kr",
}

# 이미 등록된 11개 (DB query 결과)
ALREADY_REGISTERED = {
    "www.jeonbuk.go.kr",
    "www.jbe.go.kr",
    "www.jbstatecouncil.jeonbuk.kr",
    "www.jma.go.kr",
    "www.jbares.go.kr",
    "forest.jb.go.kr",
    "hrd.jeonbuk.go.kr",
    "jihe.jeonbuk.go.kr",
}

LINK_RE = re.compile(r'https?://[a-zA-Z0-9.\-]+\.(?:go\.kr|kr|or\.kr|com|net)(?:/[^\s"\'<>]*)?', re.IGNORECASE)


def fetch(url: str, timeout: int = 10) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (extract-domains)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return data.decode("euc-kr", errors="ignore")
    except Exception as e:
        print(f"  ! fetch fail {url}: {e}", file=sys.stderr)
        return ""


def main():
    domain_count: Counter[str] = Counter()
    domain_sample_url: dict[str, str] = {}

    for seed in SEED_URLS:
        print(f"fetching {seed}", file=sys.stderr)
        html = fetch(seed)
        if not html:
            continue
        for m in LINK_RE.finditer(html):
            url = m.group(0).strip().rstrip('";\'>,')
            try:
                host = urlparse(url).netloc.lower()
            except Exception:
                continue
            if not host or host in EXCLUDE_DOMAINS:
                continue
            # jeonbuk.go.kr 하위 도메인 도 포함 (e.g., hrd.jeonbuk.go.kr 등)
            domain_count[host] += 1
            domain_sample_url.setdefault(host, url)

    # 정렬: 빈도 내림
    items = domain_count.most_common()

    print()
    print("=" * 70)
    print(f"외부/링크 도메인 총 {len(items)} 개 발견:")
    print("=" * 70)
    missing = []
    for host, cnt in items:
        registered = "✓" if host in ALREADY_REGISTERED else " "
        print(f"  [{registered}] {cnt:4d}× {host:40s} {domain_sample_url[host][:80]}")
        if host not in ALREADY_REGISTERED:
            missing.append((host, domain_sample_url[host], cnt))

    print()
    print(f"=== 누락 (등록 안 됨): {len(missing)} 개 ===")
    for host, sample, cnt in missing:
        print(f"  {host}\t{sample}\t{cnt}")

    # tab-sep 파일로 출력 — crawl_target 등록 script 에서 사용
    with open("/tmp/missing_domains.tsv", "w", encoding="utf-8") as f:
        f.write("host\tsample_url\tfreq\n")
        for host, sample, cnt in missing:
            f.write(f"{host}\t{sample}\t{cnt}\n")
    print(f"\n→ /tmp/missing_domains.tsv ({len(missing)} rows)")


if __name__ == "__main__":
    main()
