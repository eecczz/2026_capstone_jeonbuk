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
        # 본청은 게시판 수십만 페이지가 잠재 대상.
        # max_pages 는 사실상 무제한 (50만 — 안전망 수준).
        # max_depth 는 BFS discover_urls 가 합리적 시간 안에 끝나는 범위.
        # 도청 메뉴 트리는 5 depth 면 거의 모든 페이지 도달 가능.
        # (max_depth 가 너무 크면 discover 단계만 며칠 걸려 첫 fetch 가 시작 안 됨)
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
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
        "max_pages": 500000,
        "max_depth": 5,
    },
    # ─────────────── Phase 3: 공공기관·협회·연구원 (자동 등록, minimum config) ───────────────
    {
        "code": "jbct",
        "name": "www.jbct.or.kr",
        "base_url": "https://www.jbct.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbiles",
        "name": "www.jbiles.or.kr",
        "base_url": "https://www.jbiles.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbgoods",
        "name": "www.jbgoods.or.kr",
        "base_url": "http://www.jbgoods.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbwelfare",
        "name": "www.jbwelfare.or.kr",
        "base_url": "https://www.jbwelfare.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "kogl",
        "name": "www.kogl.or.kr",
        "base_url": "http://www.kogl.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "biennale",
        "name": "www.biennale.or.kr",
        "base_url": "http://www.biennale.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jb2030",
        "name": "www.jb2030.or.kr",
        "base_url": "https://www.jb2030.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jblc",
        "name": "www.jblc.or.kr",
        "base_url": "http://www.jblc.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "lsns",
        "name": "www.lsns.or.kr",
        "base_url": "https://www.lsns.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jcid",
        "name": "jcid.or.kr",
        "base_url": "http://jcid.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbtp",
        "name": "www.jbtp.or.kr",
        "base_url": "https://www.jbtp.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jiat",
        "name": "www.jiat.re.kr",
        "base_url": "https://www.jiat.re.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "kictex",
        "name": "www.kictex.re.kr",
        "base_url": "https://www.kictex.re.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbcredit",
        "name": "www.jbcredit.or.kr",
        "base_url": "https://www.jbcredit.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jif",
        "name": "www.jif.re.kr",
        "base_url": "https://www.jif.re.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "kunmed",
        "name": "www.kunmed.or.kr",
        "base_url": "https://www.kunmed.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "namwonmed",
        "name": "www.namwonmed.or.kr",
        "base_url": "https://www.namwonmed.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbwf",
        "name": "www.jbwf.or.kr",
        "base_url": "https://www.jbwf.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jcon",
        "name": "www.jcon.or.kr",
        "base_url": "https://www.jcon.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jeonbuk_pass",
        "name": "jeonbuk.pass.or.kr",
        "base_url": "https://jeonbuk.pass.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbtti",
        "name": "www.jbtti.or.kr",
        "base_url": "https://www.jbtti.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbcc",
        "name": "www.jbcc.or.kr",
        "base_url": "http://www.jbcc.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbpsa",
        "name": "jbpsa.or.kr",
        "base_url": "https://jbpsa.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbsports",
        "name": "www.jbsports.or.kr",
        "base_url": "http://www.jbsports.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "tpf",
        "name": "www.tpf.or.kr",
        "base_url": "https://www.tpf.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "lx",
        "name": "www.lx.or.kr",
        "base_url": "https://www.lx.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "nrev",
        "name": "nrev.or.kr",
        "base_url": "http://nrev.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jicc",
        "name": "www.jicc.or.kr",
        "base_url": "http://www.jicc.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "global_jbdream",
        "name": "global.jbdream.or.kr",
        "base_url": "http://global.jbdream.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbkira_kira",
        "name": "jbkira.kira.or.kr",
        "base_url": "http://jbkira.kira.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbsos",
        "name": "www.jbsos.or.kr",
        "base_url": "http://www.jbsos.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbvolo",
        "name": "www.jbvolo.or.kr",
        "base_url": "http://www.jbvolo.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "data_nps",
        "name": "data.nps.or.kr",
        "base_url": "https://data.nps.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "jbci",
        "name": "www.jbci.or.kr",
        "base_url": "https://www.jbci.or.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },

    # ─────────────── Phase 1: 전북 산하 도메인 (자동 등록, minimum config) ───────────────
    {
        "code": "policy_jb",
        "name": "policy.jb.go.kr",
        "base_url": "https://policy.jb.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "stat_jeonbuk",
        "name": "stat.jeonbuk.go.kr",
        "base_url": "http://stat.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "library_jeonbuk",
        "name": "library.jeonbuk.go.kr",
        "base_url": "http://library.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "archives_jb",
        "name": "archives.jb.go.kr",
        "base_url": "https://archives.jb.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "bojo_jeonbuk",
        "name": "bojo.jeonbuk.go.kr",
        "base_url": "https://bojo.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "usafe_jeonbuk",
        "name": "u-safe.jeonbuk.go.kr",
        "base_url": "http://u-safe.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "airimage_jeonbuk",
        "name": "airimage.jeonbuk.go.kr",
        "base_url": "http://airimage.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },
    {
        "code": "lmis_jeonbuk",
        "name": "lmis.jeonbuk.go.kr",
        "base_url": "http://lmis.jeonbuk.go.kr",
        "priority_paths": ['/'],
        "max_pages": 500,
        "max_depth": 2,
    },

    # ─────────────── Phase 2: 도내 시·군 14개 (자동 등록, minimum config) ───────────────
    {
        "code": "jeonju_city",
        "name": "www.jeonju.go.kr",
        "base_url": "http://www.jeonju.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "gunsan_city",
        "name": "www.gunsan.go.kr",
        "base_url": "http://www.gunsan.go.kr",
        # BFS time budget 60s 가 max_depth=4 펼치기엔 짧아 페이지네이션 못 닿음 →
        # 각 게시판의 list?s_idx=1..5 를 직접 priority_paths 에 명시해 BFS 첫 layer 부터
        # detail 글에 닿게 함. m{N} 자체는 list?s_idx=1 의 alias 라 BFS 가 dedup.
        # 보도자료/고시공고/입찰 은 별도 sub-domain (eminwon, gyeyak) 이라 본 도메인 BFS 가 못 따라감.
        "priority_paths": [
            '/main',
            # m140 시정소식 (648 페이지 중 최근 5)
            '/main/m140', '/main/m140/list?s_idx=2', '/main/m140/list?s_idx=3',
            '/main/m140/list?s_idx=4', '/main/m140/list?s_idx=5',
            # m141 시험/채용
            '/main/m141', '/main/m141/list?s_idx=2', '/main/m141/list?s_idx=3',
            '/main/m141/list?s_idx=4', '/main/m141/list?s_idx=5',
            # m149 읍면동소식
            '/main/m149', '/main/m149/list?s_idx=2', '/main/m149/list?s_idx=3',
            '/main/m149/list?s_idx=4', '/main/m149/list?s_idx=5',
            # m143 행사안내
            '/main/m143', '/main/m143/list?s_idx=2', '/main/m143/list?s_idx=3',
            '/main/m143/list?s_idx=4', '/main/m143/list?s_idx=5',
            # m144 교육안내
            '/main/m144', '/main/m144/list?s_idx=2', '/main/m144/list?s_idx=3',
            '/main/m144/list?s_idx=4', '/main/m144/list?s_idx=5',
            # m146 군산시보
            '/main/m146', '/main/m146/list?s_idx=2', '/main/m146/list?s_idx=3',
            '/main/m146/list?s_idx=4', '/main/m146/list?s_idx=5',
            # m154 시장에게 바란다
            '/main/m154', '/main/m154/list?s_idx=2', '/main/m154/list?s_idx=3',
            '/main/m154/list?s_idx=4', '/main/m154/list?s_idx=5',
            # m156 나도한마디
            '/main/m156', '/main/m156/list?s_idx=2', '/main/m156/list?s_idx=3',
            '/main/m156/list?s_idx=4', '/main/m156/list?s_idx=5',
        ],
        "max_pages": 800,
        "max_depth": 4,
    },
    {
        "code": "iksan_city",
        "name": "www.iksan.go.kr",
        "base_url": "http://www.iksan.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "jeongeup_city",
        "name": "www.jeongeup.go.kr",
        "base_url": "http://www.jeongeup.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "namwon_city",
        "name": "www.namwon.go.kr",
        "base_url": "http://www.namwon.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "gimje_city",
        "name": "www.gimje.go.kr",
        "base_url": "http://www.gimje.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "wanju_county",
        "name": "www.wanju.go.kr",
        "base_url": "http://www.wanju.go.kr",
        # `/` 직접 요청은 빈 응답 → 완주군 SSR 엔트리 `/index.9is` + 본 도메인 안 게시판 entry.
        # contentUid 해시는 게시판 list 페이지 직접 진입점.
        # 페이지네이션: ./list.9is?page=N&contentUid=… (새소식만 103페이지)
        # detail: ./view.9is?dataUid=…&contentUid=…
        "priority_paths": [
            '/index.9is',                                                           # 메인
            '/index.9is?contentUid=ff8080818b024d8e018b274f3fdd2ae2',              # 새소식 (103페이지)
            '/index.9is?contentUid=ff8080818b024d8e018b274f41dd2af8',              # 고시공고
            '/index.9is?contentUid=ff8080818b024d8e018b274f41ad2af6',              # 시험/채용
            '/index.9is?contentUid=ff8080818b024d8e018b274f40092ae4',              # 읍면소식
            '/index.9is?contentUid=ff8080818b024d8e018b274f414d2af2',              # 주간행사
            '/news/planweb/board/view.9is',                                        # 보도자료
        ],
        "max_pages": 800,
        # depth 4 = entry → list?page=N → view?dataUid=… → 내부. depth 2 에선
        # 첫 페이지 글 link 까지만 → 페이지네이션 못 따라감.
        "max_depth": 4,
    },
    {
        "code": "jinan_county",
        "name": "www.jinan.go.kr",
        "base_url": "http://www.jinan.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "muju_county",
        "name": "www.muju.go.kr",
        "base_url": "http://www.muju.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "jangsu_county",
        "name": "www.jangsu.go.kr",
        "base_url": "http://www.jangsu.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "imsil_county",
        "name": "www.imsil.go.kr",
        "base_url": "http://www.imsil.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "sunchang_county",
        "name": "www.sunchang.go.kr",
        "base_url": "http://www.sunchang.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "gochang_county",
        "name": "www.gochang.go.kr",
        "base_url": "http://www.gochang.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
    },
    {
        "code": "buan_county",
        "name": "www.buan.go.kr",
        "base_url": "http://www.buan.go.kr",
        "priority_paths": ['/'],
        "max_pages": 800,
        "max_depth": 2,
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
