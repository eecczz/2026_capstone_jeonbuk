"""
전북도청 + 직속기관 크롤링 대상 사이트 설정.

각 사이트마다:
- code: 내부 식별자 (snake_case)
- name: 공식 기관명 (답변에 출처로 사용)
- base_url: 크롤링 시작점
- sitemap_urls: sitemap.xml URL 목록 (있으면 여기서 URL 수집)
- priority_paths: sitemap 없을 때 우선 탐색할 경로
- allowed_path_patterns: 이 정규식에 매칭되는 URL만 수집
- excluded_path_patterns: 이 정규식에 매칭되면 제외
- contact: 기관 대표 연락처 (crawl 시 metadata에 주입)
- default_category: 사이트 전체에 적용할 기본 카테고리
- crawler_engine: "playwright" | "safe_web" (기존 WEB_LOADER_ENGINE 값과 동일)
- max_pages: 사이트당 최대 페이지 수
- max_depth: 링크 추적 깊이

일부 정보는 공개된 데이터로 부분적으로만 채워져 있으며, 운영 시 도청 담당자가
검증·보완해야 한다. URL 패턴이나 연락처가 잘못되면 여기만 수정하면 된다.
"""

from typing import Any


SITES: list[dict[str, Any]] = [
    # ─────────────── 본청 & 포털 ───────────────
    {
        "code": "jeonbuk_main",
        "name": "전북특별자치도",
        "base_url": "https://www.jeonbuk.go.kr",
        "sitemap_urls": [
            "https://www.jeonbuk.go.kr/sitemap.xml",
        ],
        "priority_paths": [
            "/index.jeonbuk",
            "/board/list.jeonbuk?boardId=BBS_0000114",  # 공지사항
            "/board/list.jeonbuk?boardId=BBS_0000115",  # 보도자료
            "/index.jeonbuk?menuCd=DOM_000000110029",  # 직속기관
        ],
        "allowed_path_patterns": [
            r"^/board/",
            r"^/index\.jeonbuk",
        ],
        "excluded_path_patterns": [
            r"/download\.jeonbuk",
            r"/fileDown",
            r"/print\.jeonbuk",
            r"\.(pdf|hwp|hwpx|zip|xlsx?|pptx?|docx?)(\?|$)",
        ],
        "contact": {
            "phone": "063-280-2114",
            "email": "jeonbuk@korea.kr",
            "address": "전북 전주시 완산구 효자로 225",
            "homepage": "https://www.jeonbuk.go.kr",
        },
        "default_category": "행정",
        "crawler_engine": "playwright",
        "max_pages": 800,
        "max_depth": 3,
    },
    {
        "code": "tour_jb",
        "name": "투어전북",
        "base_url": "https://tour.jb.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [
            r"\.(pdf|hwp|zip)(\?|$)",
            r"/fileDown",
        ],
        "contact": {
            "phone": "063-280-2114",
            "homepage": "https://tour.jb.go.kr",
        },
        "default_category": "관광",
        "crawler_engine": "playwright",
        "max_pages": 500,
        "max_depth": 3,
    },
    # ─────────────── 직속기관 (독립 홈페이지) ───────────────
    {
        "code": "jbares",
        "name": "전북특별자치도 농업기술원",
        "base_url": "https://www.jbares.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-6000",
            "address": "전북 익산시 농업기술로 100",
            "homepage": "https://www.jbares.go.kr",
        },
        "default_category": "농업",
        "crawler_engine": "playwright",
        "max_pages": 400,
        "max_depth": 3,
    },
    {
        "code": "jihe_jeonbuk",
        "name": "전북특별자치도 보건환경연구원",
        "base_url": "https://jihe.jeonbuk.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-5300",
            "homepage": "https://jihe.jeonbuk.go.kr",
        },
        "default_category": "보건환경",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
    {
        "code": "hrd_jeonbuk",
        "name": "전북특별자치도 인재개발원",
        "base_url": "https://hrd.jeonbuk.go.kr",
        "sitemap_urls": [],
        "priority_paths": [
            "/",
            "/index.training",
        ],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-4000",
            "homepage": "https://hrd.jeonbuk.go.kr",
        },
        "default_category": "교육",
        "crawler_engine": "playwright",
        "max_pages": 400,
        "max_depth": 3,
    },
    {
        "code": "forest_jb",
        "name": "전북특별자치도 산림환경연구원",
        "base_url": "https://forest.jb.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/main/main.action"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-6460",
            "homepage": "https://forest.jb.go.kr",
        },
        "default_category": "산림",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
    {
        "code": "kukakwon",
        "name": "전북특별자치도립국악원",
        "base_url": "https://kukakwon.jb.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-254-2391",
            "homepage": "https://kukakwon.jb.go.kr",
        },
        "default_category": "문화예술",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
    {
        "code": "jma",
        "name": "전북특별자치도립미술관",
        "base_url": "https://www.jma.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-6888",
            "homepage": "https://www.jma.go.kr",
        },
        "default_category": "문화예술",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
    {
        "code": "jbchild",
        "name": "전북특별자치도 어린이창의체험관",
        "base_url": "https://www.jbchild.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-220-3800",
            "homepage": "https://www.jbchild.kr",
        },
        "default_category": "교육체험",
        "crawler_engine": "playwright",
        "max_pages": 200,
        "max_depth": 3,
    },
    {
        "code": "agriacademy",
        "name": "전북특별자치도 농식품인력개발원",
        "base_url": "https://agriacademy.jeonbuk.go.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-290-6800",
            "homepage": "https://agriacademy.jeonbuk.go.kr",
        },
        "default_category": "교육",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
    {
        "code": "jbba",
        "name": "전북특별자치도 경제통상진흥원",
        "base_url": "https://www.jbba.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "063-711-2000",
            "address": "전북 전주시 덕진구 팔과정로 164",
            "homepage": "https://www.jbba.kr",
        },
        "default_category": "경제지원",
        "crawler_engine": "playwright",
        "max_pages": 400,
        "max_depth": 3,
    },
    {
        "code": "jb_jobcenter",
        "name": "전북특별자치도 일자리센터",
        "base_url": "https://www.1577-0365.or.kr",
        "sitemap_urls": [],
        "priority_paths": ["/"],
        "allowed_path_patterns": [r"^/"],
        "excluded_path_patterns": [r"\.(pdf|hwp|zip)(\?|$)", r"/fileDown"],
        "contact": {
            "phone": "1577-0365",
            "homepage": "https://www.1577-0365.or.kr",
        },
        "default_category": "일자리",
        "crawler_engine": "playwright",
        "max_pages": 300,
        "max_depth": 3,
    },
]


# ─────────────── 본청 하위 기관 (jeonbuk.go.kr 내부 페이지) ───────────────
# jeonbuk_main 사이트를 크롤링할 때 URL 경로 패턴으로 기관명을 자동 분류
SUB_INSTITUTION_MAP = {
    "DOM_000000110029009": "전북특별자치도 동물위생시험소",
    "DOM_000000110029010": "전북특별자치도 수산기술연구소",
    "DOM_000000110029012": "전북특별자치도 도로관리사업소",
    "DOM_000000110029014": "전북특별자치도 축산연구소",
    "DOM_000000110029008": "전북특별자치도중앙협력본부",
}


def infer_sub_institution(url: str, site_code: str) -> str | None:
    """도청 본청 URL 경로에서 하위 기관명을 추론. 못 찾으면 None 반환."""
    if site_code != "jeonbuk_main":
        return None
    for key, name in SUB_INSTITUTION_MAP.items():
        if key in url:
            return name
    return None


def get_site(code: str) -> dict[str, Any] | None:
    for s in SITES:
        if s["code"] == code:
            return s
    return None


def list_site_codes() -> list[str]:
    return [s["code"] for s in SITES]
