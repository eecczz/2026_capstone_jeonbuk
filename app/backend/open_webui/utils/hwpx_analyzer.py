"""
HWPX 양식 분석 모듈

1) analyze_hwpx()          — HWPX에서 경량 XML 추출
2) build_hwpx_prompt()     — AI에게 보낼 프롬프트 생성
3) parse_actions_from_llm() — AI 응답에서 명령 JSON 파싱
"""

import io
import json
import logging
import re
import zipfile
from lxml import etree
from typing import Optional

from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)

NS_HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
NS_HC = "{http://www.hancom.co.kr/hwpml/2011/core}"

# 제거할 태그 (렌더링 전용, 구조 파악에 불필요)
REMOVE_TAGS = {
    # hp 네임스페이스
    f"{NS_HP}linesegarray",    # 줄 배치 좌표
    f"{NS_HP}renderingInfo",   # 변환 행렬
    f"{NS_HP}imgRect",         # 이미지 좌표
    f"{NS_HP}imgClip",         # 이미지 클리핑
    f"{NS_HP}imgDim",          # 이미지 원본 크기
    f"{NS_HP}effects",         # 효과
    f"{NS_HP}shapeComment",    # 도형 설명 텍스트
    f"{NS_HP}footNotePr",      # 각주 설정
    f"{NS_HP}endNotePr",       # 미주 설정
    f"{NS_HP}pageBorderFill",  # 페이지 테두리
    f"{NS_HP}lineNumberShape", # 줄번호
    f"{NS_HP}sz",              # 크기 (표/이미지)
    f"{NS_HP}pos",             # 위치
    f"{NS_HP}outMargin",       # 외부 여백
    f"{NS_HP}inMargin",        # 내부 여백 (표)
    f"{NS_HP}offset",          # 오프셋
    f"{NS_HP}cellSz",          # 셀 크기
    f"{NS_HP}cellMargin",      # 셀 여백
    f"{NS_HP}pic",             # 이미지 전체 (구조에 불필요)
    # hc 네임스페이스
    f"{NS_HC}img",             # 이미지 참조
    f"{NS_HC}transMatrix",     # 변환 행렬
    f"{NS_HC}scaMatrix",       # 스케일 행렬
    f"{NS_HC}rotMatrix",       # 회전 행렬
}

# 제거할 속성 (렌더링 좌표/표시)
REMOVE_ATTRS = {
    "textpos", "vertpos", "vertsize", "textheight", "baseline",
    "spacing", "horzpos", "horzsize", "flags",
    "zOrder", "dropcapstyle", "lock", "numberingType",
    "textWrap", "textFlow", "pageBreak", "columnBreak", "merged",
    "textWidth", "textHeight", "hasTextRef", "hasNumRef",
    "linkListIDRef", "linkListNextIDRef",
    "noAdjust", "cellSpacing", "repeatHeader",
    "groupLevel", "instid", "reverse", "href",
    "dirty", "editable", "protect",
}


def extract_section_xml(hwpx_source) -> str:
    """
    HWPX 파일에서 Contents/section0.xml을 추출합니다.

    Args:
        hwpx_source: 파일 경로(str), bytes, 또는 file-like object

    Returns:
        section0.xml의 원본 문자열
    """
    if isinstance(hwpx_source, str):
        with open(hwpx_source, "rb") as f:
            data = f.read()
    elif isinstance(hwpx_source, bytes):
        data = hwpx_source
    else:
        data = hwpx_source.read()

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # section0.xml 찾기 (경로가 다를 수 있음)
        section_names = [
            n for n in zf.namelist()
            if "section" in n.lower() and n.endswith(".xml")
        ]
        if not section_names:
            raise ValueError("HWPX 파일에서 section XML을 찾을 수 없습니다")

        return zf.read(section_names[0]).decode("utf-8")


def lighten_xml(xml_str: str) -> str:
    """
    section0.xml에서 렌더링 전용 태그/속성을 제거하여 경량화합니다.
    구조(문단, 표, 셀, 텍스트, 스타일 ID)는 보존됩니다.

    Args:
        xml_str: section0.xml 원본 문자열

    Returns:
        경량화된 XML 문자열
    """
    root = etree.fromstring(xml_str.encode("utf-8"))

    # 1) 불필요한 태그 제거
    for tag in REMOVE_TAGS:
        for elem in root.iter(tag):
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)

    # 2) secPr 전체 제거 (페이지 설정 — AI에게 불필요)
    for secpr in root.iter(f"{NS_HP}secPr"):
        parent = secpr.getparent()
        if parent is not None:
            parent.remove(secpr)

    # 3) header 제거 (머리글 — 양식 본문과 무관)
    for header in root.iter(f"{NS_HP}header"):
        parent = header.getparent()
        if parent is not None:
            parent.remove(header)

    # 4) 빈 run 제거 (텍스트 없는 hp:run)
    for run in root.iter(f"{NS_HP}run"):
        # 자식에 텍스트도 표도 없으면 제거
        has_content = (
            run.find(f"{NS_HP}t") is not None
            or run.find(f"{NS_HP}tbl") is not None
            or run.find(f"{NS_HP}ctrl") is not None
        )
        if not has_content:
            parent = run.getparent()
            if parent is not None:
                parent.remove(run)

    # 5) 불필요한 속성 제거
    for elem in root.iter():
        for attr in list(elem.attrib.keys()):
            attr_local = attr.split("}")[-1] if "}" in attr else attr
            if attr_local in REMOVE_ATTRS:
                del elem.attrib[attr]

    # 6) 섹션 레벨 문단에 _idx 부여 (AI가 정확한 문단 인덱스 사용하도록)
    sec_para_idx = 0
    sections = [root] if root.tag == f"{NS_HP}sec" else root.findall(f".//{NS_HP}sec")
    if not sections:
        sections = [root]
    for section in sections:
        for p in section.findall(f"{NS_HP}p"):  # direct children only (셀 내부 문단 제외)
            p.set("_idx", str(sec_para_idx))
            sec_para_idx += 1

    # 7) 표에 _tbl_idx 부여 (AI가 정확한 표 순번 사용하도록)
    for tbl_i, tbl in enumerate(root.findall(f".//{NS_HP}tbl")):
        tbl.set("_tbl_idx", str(tbl_i))

    # 정리된 XML 출력
    result = etree.tostring(root, encoding="unicode", pretty_print=True)
    return result


def analyze_hwpx(hwpx_source) -> dict:
    """
    HWPX 파일을 분석하여 경량 XML과 메타정보를 반환합니다.

    Args:
        hwpx_source: 파일 경로(str), bytes, 또는 file-like object

    Returns:
        {
            "light_xml": 경량화된 section0.xml 문자열,
            "original_xml": 원본 section0.xml 문자열,
            "paragraph_count": 문단 수,
            "table_count": 표 수,
        }
    """
    original_xml = extract_section_xml(hwpx_source)
    light_xml = lighten_xml(original_xml)

    # 간단한 메타정보 추출
    root = etree.fromstring(original_xml.encode("utf-8"))

    # 섹션 레벨 문단만 카운트 (표 셀 내부 문단 제외)
    sections = [root] if root.tag == f"{NS_HP}sec" else root.findall(f".//{NS_HP}sec")
    para_count = sum(len(s.findall(f"{NS_HP}p")) for s in (sections or [root]))
    table_count = len(root.findall(f".//{NS_HP}tbl"))

    return {
        "light_xml": light_xml,
        "original_xml": original_xml,
        "paragraph_count": para_count,
        "table_count": table_count,
    }


# ============================================================
# AI 프롬프트 생성 및 응답 파싱
# ============================================================

HWPX_SYSTEM_PROMPT = """당신은 HWPX 문서 생성 전문가입니다.
사용자가 제공하는 HWPX 양식(XML)과 작성할 내용을 분석하여,
양식에 맞게 문서를 채우는 명령(JSON)을 생성합니다.

## 핵심 원칙

### 양식 = 빈 틀. 기존 텍스트는 모두 샘플/플레이스홀더입니다.
양식 XML에 들어있는 제목, 소제목, 본문, 날짜 등 **모든 텍스트는 예시일 뿐**입니다.
반드시 사용자가 제공하는 새 내용으로 **전부 교체**해야 합니다.
양식의 텍스트를 그대로 남기면 안 됩니다.

### 보존하는 것: 서식(폰트, 크기, 배경색, 테두리, 표 구조)
### 교체하는 것: 모든 텍스트 내용(제목, 소제목, 본문, 날짜, 셀 값 등)

절대로 양식의 표/문단 구조를 삭제하고 새로 만들지 마세요. 텍스트만 교체하세요.

## 필수 동작
1. **_idx가 있는 모든 문단에 set_paragraph_text를 생성하세요** — 빠뜨리지 마세요
2. **모든 표의 값 셀에 set_cell을 생성하세요** — 라벨 셀(항목명)은 유지, 값 셀은 교체
3. **날짜 필드**: 소스 자료에 날짜가 있으면 해당 날짜를, 없으면 오늘 날짜를 넣으세요
4. 대응하는 새 내용이 없는 문단은 set_paragraph_text로 빈 문자열("")을 넣어 비우세요

## HWPX XML 구조 설명
- <hp:p paraPrIDRef="N" _idx="I"> : 섹션 레벨 문단. **_idx가 set_paragraph_text / insert_paragraph / remove_paragraph / clear_body에서 사용할 인덱스**입니다. 표 셀 내부의 <hp:p>에는 _idx가 없으므로 set_paragraph_text 대상이 아닙니다 (set_cell 사용). N은 문단 스타일 ID
- <hp:run charPrIDRef="N"> : 텍스트 런. N은 글자 스타일 ID (글꼴/크기/굵기 등)
- <hp:t>텍스트</hp:t> : 실제 텍스트 내용
- <hp:tbl rowCnt="R" colCnt="C"> : 표 (R행 C열)
- <hp:tc> : 표의 셀
- <hp:cellAddr colAddr="C" rowAddr="R"/> : 셀 위치
- <hp:cellSpan colSpan="CS" rowSpan="RS"/> : 셀 병합

## 출력 규칙
반드시 아래 JSON 형식만 출력하세요. 다른 설명은 절대 포함하지 마세요.

```json
{
  "actions": [
    {"type": "set_cell", "table": 0, "row": 0, "col": 1, "text": "텍스트"},
    {"type": "set_paragraph_text", "index": 5, "text": "교체할 텍스트"},
    {"type": "add_row", "table": 0, "count": 2, "cells": [["A","B"],["C","D"]]},
    {"type": "clear_body", "from_paragraph": 15},
    {"type": "add_paragraph", "paraPrIDRef": "5", "charPrIDRef": "12", "text": "추가 텍스트"},
    {"type": "insert_paragraph", "index": 10, "paraPrIDRef": "3", "charPrIDRef": "8", "text": "제목"},
    {"type": "add_table", "rows": 3, "cols": 2, "cells": [["셀1","셀2"],["셀3","셀4"],["셀5","셀6"]]},
    {"type": "remove_table", "table": 3},
    {"type": "remove_paragraph", "index": 20}
  ]
}
```

## 명령 타입 설명 (우선순위순)

### ★ 서식 보존 명령 (우선 사용)
- **set_cell**: 기존 표의 셀 텍스트 교체 (서식 보존). table=문서 내 표 순번 0부터
- **set_paragraph_text**: 기존 문단의 텍스트 교체 (폰트/크기/배경색 등 모든 서식 보존). index=문단 순번 0부터
- **add_row**: 기존 표에 행 추가 (마지막 행 구조/서식 복제, cells로 내용 지정)

### 구조 변경 명령 (필요 시에만)
- **clear_body**: 지정 문단 인덱스부터 끝까지 삭제 (양식 문단 수보다 내용이 적을 때만)
- **add_paragraph**: 문서 끝에 새 문단 추가 (양식 문단 수보다 내용이 많을 때만)
- **insert_paragraph**: 특정 위치 앞에 문단 삽입
- **add_table**: 문서 끝에 새 표 추가
- **remove_table**: 표 삭제
- **remove_paragraph**: 특정 문단 삭제

## 중요 — 반드시 따를 것
1. **표 셀**: 반드시 set_cell로 텍스트만 교체하세요. 표를 삭제하고 새로 만들지 마세요
2. **본문 문단**: 반드시 set_paragraph_text로 텍스트만 교체하세요. clear_body + add_paragraph는 최후의 수단입니다
3. **양식보다 내용이 많을 때만** 초과분에 대해 add_paragraph를 사용하세요
4. **양식보다 내용이 적을 때**: 남는 문단은 set_paragraph_text로 빈 문자열("")을 넣어 비우세요
5. paraPrIDRef와 charPrIDRef는 반드시 양식 XML에서 확인된 값을 사용하세요
6. 양식에 없는 스타일 ID를 만들어내지 마세요
7. set_paragraph_text, insert_paragraph, remove_paragraph, clear_body의 인덱스는 반드시 <hp:p>의 _idx 속성값을 사용하세요. 직접 세지 마세요
8. **양식의 기존 텍스트를 그대로 두지 마세요** — 모든 _idx 문단과 값 셀에 대해 명령을 생성해야 합니다
"""


def _collect_table_elements(root) -> set:
    """표 내부의 모든 하위 요소를 세트로 수집"""
    table_elems = set()
    for tbl in root.findall(f".//{NS_HP}tbl"):
        for desc in tbl.iter():
            table_elems.add(desc)
    return table_elems


def truncate_xml(light_xml: str, max_chars: int = 100000) -> dict:
    """
    대형 XML을 **구조 기반**으로 축소합니다.

    원칙: 패턴 보존. 반복 구조 압축. 텍스트 축약.
    1단계: 표 셀 내 긴 텍스트 축약
    2단계: 표 밖 본문 문단 — 빈 문단 제거, 텍스트 축약
    3단계: 1x1 표(텍스트 상자) 전역 압축 — 처음 2개만 전체 보존, 나머지 내부 최소화
    4단계: 연속 동일 구조 표 축약
    5단계: 여전히 초과 시 셀 텍스트를 더 짧게
    6단계: 중간 본문 문단 제거 (최후 수단)

    Returns:
        {"xml": 축소된 XML (재번호), "removed_indices": 제거된 원본 _idx 목록}
    """
    # 원본 _idx 전체 수집
    orig_root = etree.fromstring(light_xml.encode("utf-8"))
    all_original_indices = set()
    for p in orig_root.findall(f".//{NS_HP}p"):
        idx_val = p.get("_idx")
        if idx_val is not None:
            all_original_indices.add(int(idx_val))

    if len(light_xml) <= max_chars:
        return {"xml": light_xml, "removed_indices": []}

    root = etree.fromstring(light_xml.encode("utf-8"))
    total_paras = len(root.findall(f".//{NS_HP}p"))
    total_tables = len(root.findall(f".//{NS_HP}tbl"))

    # ── 1단계: 표 셀 내 긴 텍스트 축약 ──
    for tbl in root.findall(f".//{NS_HP}tbl"):
        for tc in tbl.iter(f"{NS_HP}tc"):
            for t_elem in tc.iter(f"{NS_HP}t"):
                if t_elem.text and len(t_elem.text) > 50:
                    t_elem.text = t_elem.text[:50] + "…"

    # ── 2단계: 표 밖 본문 문단 처리 ──
    table_elements = _collect_table_elements(root)
    top_level_paras = []
    for p in root.findall(f".//{NS_HP}p"):
        if p not in table_elements:
            top_level_paras.append(p)

    removed_count = 0
    # 2a: 빈 문단 제거 (텍스트 없고 표도 없는 문단)
    for p in top_level_paras:
        if p.find(f".//{NS_HP}tbl") is not None:
            continue
        texts = [t.text for t in p.iter(f"{NS_HP}t") if t.text and t.text.strip()]
        if not texts:
            parent = p.getparent()
            if parent is not None:
                parent.remove(p)
                removed_count += 1

    # 2b: 남은 본문 문단 텍스트 축약
    table_elements = _collect_table_elements(root)
    for p in root.findall(f".//{NS_HP}p"):
        if p in table_elements:
            continue
        for t_elem in p.iter(f"{NS_HP}t"):
            if t_elem.text and len(t_elem.text) > 60:
                t_elem.text = t_elem.text[:60] + "…"

    # ── 3단계: 1x1 표(텍스트 상자) 전역 압축 ──
    # 처음 2개는 전체 XML 보존 (LLM 패턴 학습용), 나머지는 내부 최소화
    all_1x1 = [
        tbl for tbl in root.findall(f".//{NS_HP}tbl")
        if tbl.get("rowCnt", "1") == "1" and tbl.get("colCnt", "1") == "1"
    ]
    # 텍스트 외 서식이 다르면 다른 패턴 → 패턴별 1개씩 보존
    seen_styles = set()
    compacted_1x1 = 0
    for tbl in all_1x1:
        # 표 자체 서식
        border = tbl.get("borderFillIDRef", "0")
        # 셀 내부 서식
        cell_p = tbl.find(f".//{NS_HP}p")
        cell_run = tbl.find(f".//{NS_HP}run")
        cell_para_pr = cell_p.get("paraPrIDRef", "0") if cell_p is not None else "0"
        cell_char_pr = cell_run.get("charPrIDRef", "0") if cell_run is not None else "0"
        # 상위 문단/run 서식 (표를 감싸는 문단의 스타일)
        parent_run = tbl.getparent()
        parent_char_pr = parent_run.get("charPrIDRef", "0") if parent_run is not None and parent_run.tag == f"{NS_HP}run" else "0"
        parent_p = parent_run.getparent() if parent_run is not None else None
        parent_para_pr = parent_p.get("paraPrIDRef", "0") if parent_p is not None and parent_p.tag == f"{NS_HP}p" else "0"
        style_key = f"{border}_{cell_para_pr}_{cell_char_pr}_{parent_para_pr}_{parent_char_pr}"

        if style_key not in seen_styles:
            seen_styles.add(style_key)
            continue  # 이 서식 패턴의 첫 번째 → 전체 XML 보존

        # 같은 서식의 후속 표 → 내부 최소화
        cell_text = ""
        for t_elem in tbl.iter(f"{NS_HP}t"):
            if t_elem.text:
                cell_text += t_elem.text
        if len(cell_text) > 20:
            cell_text = cell_text[:20] + "…"

        for tc in tbl.iter(f"{NS_HP}tc"):
            for tag in (f"{NS_HP}cellAddr", f"{NS_HP}cellSpan"):
                for elem in tc.findall(tag):
                    tc.remove(elem)
            paras = tc.findall(f"{NS_HP}p")
            for p_extra in paras[1:]:
                tc.remove(p_extra)
            if paras:
                runs = paras[0].findall(f"{NS_HP}run")
                for run_extra in runs[1:]:
                    paras[0].remove(run_extra)
                first_t = paras[0].find(f".//{NS_HP}t")
                if first_t is not None:
                    first_t.text = cell_text or ""

        compacted_1x1 += 1

    if compacted_1x1 > 0:
        log.info(
            f"1x1 표 {compacted_1x1}개 내부 최소화 "
            f"(서식 패턴 {len(seen_styles)}종 각 1개씩 보존)"
        )

    result = etree.tostring(root, encoding="unicode", pretty_print=True)

    # ── 4단계: 연속 동일 구조 표 축약 ──
    # 동일 구조(rowCnt, colCnt)가 3개 이상 연속되면 대표 2개만 남기고 나머지 제거
    if len(result) > max_chars:
        root2 = etree.fromstring(result.encode("utf-8"))
        all_tables = root2.findall(f".//{NS_HP}tbl")
        collapsed_count = 0

        # 연속 동일 구조 표 그룹 찾기
        i = 0
        while i < len(all_tables):
            tbl = all_tables[i]
            rows = tbl.get("rowCnt", "1")
            cols = tbl.get("colCnt", "1")
            key = f"{rows}x{cols}"

            # 같은 구조가 연속되는 범위 찾기
            j = i + 1
            while j < len(all_tables):
                t2 = all_tables[j]
                if t2.get("rowCnt", "1") == rows and t2.get("colCnt", "1") == cols:
                    j += 1
                else:
                    break

            # 1x1 표는 3단계에서 이미 압축됨 → 연속 제거 건너뜀
            if key == "1x1":
                i = j
                continue

            group_size = j - i
            if group_size >= 3:
                # 3개 이상 연속 → 앞 2개 보존, 나머지 제거 + 요약 주석
                to_remove = all_tables[i + 2:j]
                # 마지막 보존 표 옆에 요약 주석 삽입
                last_kept = all_tables[i + 1]
                parent = last_kept.getparent()
                if parent is not None:
                    idx_in_parent = list(parent).index(last_kept) + 1
                    comment = etree.Comment(
                        f" 동일 구조 표({key}) {len(to_remove)}개 생략 "
                        f"(원본에서 표{i+2}~표{j-1}, 위 2개와 동일 구조) "
                    )
                    parent.insert(idx_in_parent, comment)

                for t in to_remove:
                    # 표를 감싸는 문단도 함께 제거
                    tp = t.getparent()
                    while tp is not None and tp.tag != f"{NS_HP}p":
                        tp = tp.getparent()
                    if tp is not None:
                        pp = tp.getparent()
                        if pp is not None:
                            pp.remove(tp)
                            collapsed_count += 1

            i = j

        if collapsed_count > 0:
            result = etree.tostring(root2, encoding="unicode", pretty_print=True)

    # ── 5단계: 여전히 초과 시 셀 텍스트를 더 짧게 ──
    if len(result) > max_chars:
        root3 = etree.fromstring(result.encode("utf-8"))

        # 점진적으로 텍스트 길이를 줄여감
        for limit in [30, 20, 10]:
            for t_elem in root3.iter(f"{NS_HP}t"):
                if t_elem.text and len(t_elem.text) > limit:
                    t_elem.text = t_elem.text[:limit] + "…"
            result = etree.tostring(root3, encoding="unicode", pretty_print=True)
            if len(result) <= max_chars:
                break

    # ── 6단계: 그래도 초과 시 — 중간 본문 문단 제거 (최후 수단) ──
    if len(result) > max_chars:
        root4 = etree.fromstring(result.encode("utf-8"))
        table_elements4 = _collect_table_elements(root4)
        body_paras = [
            p for p in root4.findall(f".//{NS_HP}p")
            if p not in table_elements4 and p.find(f".//{NS_HP}tbl") is None
        ]

        KEEP_FRONT = 30
        KEEP_BACK = 15
        if len(body_paras) > KEEP_FRONT + KEEP_BACK + 10:
            middle = body_paras[KEEP_FRONT:-KEEP_BACK] if KEEP_BACK > 0 else body_paras[KEEP_FRONT:]
            mid_removed = 0
            for p in middle:
                parent = p.getparent()
                if parent is not None:
                    parent.remove(p)
                    mid_removed += 1

            if middle and body_paras[KEEP_FRONT - 1].getparent() is not None:
                anchor = body_paras[KEEP_FRONT - 1]
                parent = anchor.getparent()
                idx = list(parent).index(anchor) + 1
                parent.insert(idx, etree.Comment(
                    f" 본문 문단 {mid_removed}개 생략 (앞 {KEEP_FRONT}개, 뒤 {KEEP_BACK}개 보존) "
                ))

        result = etree.tostring(root4, encoding="unicode", pretty_print=True)

    # ── 살아남은 _idx 수집 및 재번호 부여 ──
    root_final = etree.fromstring(result.encode("utf-8"))

    surviving = []
    sections_f = [root_final] if root_final.tag == f"{NS_HP}sec" else root_final.findall(f".//{NS_HP}sec")
    if not sections_f:
        sections_f = [root_final]
    for section in sections_f:
        for p in section.findall(f"{NS_HP}p"):
            old_idx = p.get("_idx")
            if old_idx is not None:
                surviving.append((int(old_idx), p))
    surviving.sort(key=lambda x: x[0])

    kept_indices = set(old_idx for old_idx, _ in surviving)
    removed_indices = sorted(all_original_indices - kept_indices)

    # 재번호: 0, 1, 2, ...
    for new_idx, (old_idx, p) in enumerate(surviving):
        p.set("_idx", str(new_idx))

    # ── 메타 주석 ──
    remaining_tables = len(root_final.findall(f".//{NS_HP}tbl"))
    remaining_paras = len(root_final.findall(f".//{NS_HP}p"))
    meta = (
        f" 원본: {total_paras}문단, {total_tables}표 ({len(light_xml):,}자). "
        f"축소 후: {remaining_paras}문단, {remaining_tables}표 ({len(result):,}자). "
        f"빈 문단 {removed_count}개 제거. 문단 {len(removed_indices)}개 제거, {len(surviving)}개 보존. "
    )
    root_final.insert(0, etree.Comment(meta))
    result = etree.tostring(root_final, encoding="unicode", pretty_print=True)

    log.info(
        f"XML 축소: {len(light_xml):,}자 → {len(result):,}자 "
        f"({len(result)/len(light_xml)*100:.1f}%) "
        f"표 {remaining_tables}/{total_tables}개 보존, "
        f"문단 {len(surviving)}/{len(all_original_indices)}개 보존"
    )
    return {"xml": result, "removed_indices": removed_indices}


def pdf_to_text(pdf_path: str, max_chars: int = 50000) -> str:
    """
    pdftotext를 사용하여 PDF에서 텍스트를 추출합니다.

    Args:
        pdf_path: PDF 파일 경로
        max_chars: 최대 반환 문자 수

    Returns:
        추출된 텍스트 (max_chars 초과 시 잘림)
    """
    import subprocess

    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext 실패: {result.stderr}")

    text = result.stdout.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (총 {len(result.stdout):,}자 중 {max_chars:,}자만 포함)"
        log.info(f"PDF 텍스트 축소: {len(result.stdout):,}자 → {max_chars:,}자")
    else:
        log.info(f"PDF 텍스트 추출: {len(text):,}자")

    return text


def pdf_to_base64(pdf_path: str) -> str:
    """
    PDF 파일을 base64 문자열로 변환합니다 (이미지 변환 없이 원본 그대로).

    Args:
        pdf_path: PDF 파일 경로

    Returns:
        base64 인코딩된 PDF 문자열
    """
    import base64

    with open(pdf_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    file_size_mb = len(b64) * 3 / 4 / 1024 / 1024  # base64 → 원본 크기 추정
    log.info(f"PDF → base64 변환 완료 ({file_size_mb:.1f}MB)")
    return b64


def pdf_to_base64_images(
    pdf_path: str,
    dpi: int = 100,
    quality: int = 85,
    max_pages: int = 10,
) -> list[str]:
    """
    PDF 파일을 페이지별 base64 JPEG 이미지로 변환합니다.

    Args:
        pdf_path: PDF 파일 경로
        dpi: 해상도 (100이면 문서 텍스트 인식에 충분)
        quality: JPEG 품질 (1-100, 85가 화질/크기 균형점)
        max_pages: 최대 변환 페이지 수 (AI 토큰 제한 방지)

    Returns:
        base64 인코딩된 JPEG 이미지 문자열 리스트
    """
    import base64
    from pdf2image import convert_from_path

    images = convert_from_path(pdf_path, dpi=dpi)
    total_pages = len(images)

    if total_pages > max_pages:
        log.warning(
            f"PDF {total_pages}페이지 중 처음 {max_pages}페이지만 변환 "
            f"(AI 토큰 제한 방지)"
        )
        images = images[:max_pages]

    result = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        result.append(b64)

    total_mb = sum(len(b) for b in result) / 1024 / 1024
    log.info(
        f"PDF → {len(result)}/{total_pages}페이지 JPEG 변환 "
        f"(dpi={dpi}, q={quality}, {total_mb:.1f}MB)"
    )
    return result


def build_hwpx_prompt(
    light_xml: str,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
    auto_truncate: bool = True,
) -> list[dict]:
    """
    AI에게 보낼 메시지 리스트를 생성합니다.
    대형 문서의 경우 XML을 자동 축소하고 텍스트 우선 모드를 사용합니다.

    Args:
        light_xml: 경량화된 양식 XML
        content_text: 작성할 내용 텍스트 (직접 입력)
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트 (pdftotext)
        auto_truncate: 대형 XML 자동 축소 여부

    Returns:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    # 대형 XML 자동 축소
    if auto_truncate:
        tr = truncate_xml(light_xml)
        light_xml = tr["xml"]

    # 유저 메시지 구성
    user_parts = []

    instructions = (
        "## 지시사항\n"
        "1. 양식 XML의 _idx가 있는 **모든** 문단에 대해 set_paragraph_text를 생성하세요 (빠뜨리면 양식 샘플 텍스트가 그대로 남습니다)\n"
        "2. 표의 라벨 셀(항목명)은 유지하고, **값 셀은 전부** set_cell로 교체하세요\n"
        "3. 날짜 필드에는 소스 자료의 날짜를, 없으면 오늘 날짜를 넣으세요\n"
        "4. 본문 문단은 set_paragraph_text로 텍스트만 교체하세요 (서식 보존). 양식보다 내용이 많을 때만 add_paragraph를 사용하세요\n"
        "5. 소스 자료의 모든 내용을 빠짐없이 양식에 반영하세요\n"
        "6. 반드시 JSON만 출력하세요\n"
    )

    xml_text = f"""## 양식 XML
아래는 HWPX 양식의 경량화된 XML입니다. 양식 안의 텍스트는 **샘플/플레이스홀더**이므로 전부 새 내용으로 교체해야 합니다.

```xml
{light_xml}
```

## 작성할 내용
"""

    # 내용 소스 결정: pdf_text > content_images > content_text
    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        # PDF 텍스트 모드 (가장 효율적 — 토큰 절약)
        xml_text += f"아래는 PDF에서 추출한 텍스트입니다. 이 내용으로 양식의 모든 텍스트를 교체하세요.\n\n"
        xml_text += f"```\n{pdf_text}\n```\n\n"

        if has_content:
            xml_text += f"추가 지시사항: {content_text}\n\n"

        xml_text += instructions

        if has_images:
            # 텍스트 + 이미지 병행 (소형 문서만)
            user_parts.append({"type": "text", "text": xml_text})
            for img_b64 in content_images:
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
        else:
            user_parts = xml_text

    elif has_images:
        # 이미지 전용 모드 (소형 PDF)
        xml_text += "아래 첨부된 PDF 이미지의 내용으로 양식의 모든 텍스트를 교체하세요.\n\n"
        if has_content:
            xml_text += f"추가 지시사항: {content_text}\n\n"
        xml_text += instructions

        user_parts.append({"type": "text", "text": xml_text})
        for img_b64 in content_images:
            user_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })

    else:
        # 텍스트 직접 입력 모드
        xml_text += f"{content_text}\n\n"
        xml_text += instructions

        user_parts = xml_text

    return [
        {"role": "system", "content": HWPX_SYSTEM_PROMPT},
        {"role": "user", "content": user_parts},
    ]


# ============================================================
# 2단계 프롬프트: 1차 구조 분석 + 2차 내용 매핑
# ============================================================

STRUCTURE_ANALYSIS_PROMPT = """당신은 HWPX 양식 구조 분석 전문가입니다.
양식 XML을 분석하여 각 필드의 계층 구조, 번호 기호, 용도를 JSON으로 출력합니다.

## XML 구조 안내
- <hp:p paraPrIDRef="N" _idx="I">: 섹션 레벨 문단 (I가 인덱스, N이 문단 스타일 ID)
- <hp:run charPrIDRef="N">: 텍스트 런 (N이 글자 스타일 ID)
- <hp:t>텍스트</hp:t>: 실제 텍스트 내용
- <hp:tbl rowCnt="R" colCnt="C" _tbl_idx="T">: 표 (R행 C열, T가 표 순번 인덱스 — set_cell의 table 값으로 사용)
- <hp:tc>: 표 셀, <hp:cellAddr colAddr="C" rowAddr="R"/>: 셀 위치

## 분석 규칙

### 문단 분석
_idx가 있는 모든 문단에 대해:
- **level**: 계층 수준 (0=최상위, 1=대제목, 2=중제목, 3=소제목, 4=세부항목)
- **marker**: 번호 기호가 있으면 그대로 기록 (➊, 󰊲, Ⅰ., 1., 1), -, 가. 등), 없으면 ""
- **description**: 이 자리에 **어떤 내용이 어떤 형식으로** 들어가야 하는지 구체적으로 설명
- **paraPrIDRef**: <hp:p>의 paraPrIDRef 속성값
- **charPrIDRef**: 첫 번째 <hp:run>의 charPrIDRef 속성값

### description 작성 규칙
1. 해당 위치에 들어갈 내용의 **용도와 형식**을 구체적으로 적으세요
   - 좋은 예: "문서 전체 제목 (연도+기관+사업명+문서종류 형식)"
   - 좋은 예: "작성일자 (yyyy. m. d. 형식)"
   - 좋은 예: "발신 기관명 또는 작성자 이름"
   - 좋은 예: "대분류 아래 세부 항목 제목"
   - 좋은 예: "세부 항목의 설명 본문"
   - 나쁜 예: "제목", "날짜", "내용" (너무 추상적)

2. **같은 구조적 위치에 있는 필드는 동일한 description을 사용하세요**
   예를 들어 양식이 이런 구조라면:
   * 대분류제목
     1. 소제목A
        - 내용A
     2. 소제목B
        - 내용B
   → "1. 소제목A"와 "2. 소제목B"는 같은 description: "대분류 아래 세부 항목 제목"
   → "- 내용A"와 "- 내용B"는 같은 description: "세부 항목의 설명 본문"

3. **수정하면 안 되는 고정 텍스트**는 description에 "(고정 텍스트, 수정 불필요)"라고 명시하세요
   예: 목차 제목 "목 차", 붙임 표시 "붙임", 구분선 등

### 표 분석
문서 내 모든 표에 대해 (0번부터 순서대로):
- **description**: 표의 용도를 구체적으로 설명
- **headers**: 라벨(항목명) 셀 목록 — 이 셀들은 유지됩니다
- **value_cells**: 데이터가 채워질 셀 목록 — 이 셀들이 교체 대상입니다

### 1x1 표 (텍스트 상자)
rowCnt="1" colCnt="1"인 표는 데이터 표가 아니라 **텍스트 상자/강조 박스**입니다.
- tables 배열에 포함하되, description에 "(텍스트 상자)"를 붙이세요
- value_cells는 반드시 [{"row": 0, "col": 0}]만 사용 (다른 좌표 없음)
- headers는 빈 배열 []

## 출력 형식
반드시 아래 JSON만 출력하세요. 다른 설명은 포함하지 마세요.

```json
{
  "paragraphs": [
    {"idx": 0, "level": 0, "marker": "", "description": "문서 전체 제목 (연도+기관+사업명+문서종류 형식)", "paraPrIDRef": "5", "charPrIDRef": "12"},
    {"idx": 1, "level": 0, "marker": "", "description": "작성일자 (yyyy. m. d. 형식)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 2, "level": 0, "marker": "", "description": "발신 기관명", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 3, "level": 1, "marker": "Ⅰ.", "description": "대분류 제목", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 4, "level": 2, "marker": "1.", "description": "대분류 아래 세부 항목 제목", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 5, "level": 3, "marker": "-", "description": "세부 항목의 설명 본문", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 6, "level": 2, "marker": "2.", "description": "대분류 아래 세부 항목 제목", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 7, "level": 3, "marker": "-", "description": "세부 항목의 설명 본문", "paraPrIDRef": "0", "charPrIDRef": "0"}
  ],
  "tables": [
    {"table": 0, "rows": 5, "cols": 3, "description": "사업별 예산 배분 현황표",
     "headers": [{"row": 0, "col": 0, "text": "구분"}, {"row": 0, "col": 1, "text": "금액"}],
     "value_cells": [{"row": 1, "col": 1}, {"row": 2, "col": 1}]}
  ]
}
```

## 중요
- 양식의 텍스트는 샘플입니다. 샘플 텍스트 자체를 description에 넣지 마세요
- _idx가 있는 문단을 하나도 빠뜨리지 마세요
- 같은 구조적 패턴의 필드는 반드시 같은 description을 사용하세요
- 표의 headers(라벨)와 value_cells(데이터)를 정확히 구분하세요
"""

CONTENT_MAPPING_PROMPT = """당신은 HWPX 문서 작성 전문가입니다.
양식의 구조 분석 결과를 바탕으로, 소스 자료의 내용을 양식 구조에 맞게 배치하는 JSON 명령을 생성합니다.

## 핵심 원칙

### 소스 자료의 내용만 사용하세요
구조 분석의 description은 "어떤 내용이 들어갈 자리"를 알려줍니다. 거기에 들어갈 실제 텍스트는 반드시 소스 자료(PDF)에서 찾으세요.

### marker(번호 기호)는 보존하세요
구조 분석에 marker가 있으면 (➊, 󰊲, Ⅰ. 등) 텍스트 앞에 그대로 붙이세요.
예: marker "➊" + 소스 내용 "추진 배경" → "➊ 추진 배경"

### description과 level을 보고 매핑하세요
각 필드의 description이 어떤 내용을 요구하는지 읽고, 소스 자료에서 대응하는 내용을 찾으세요.
- level이 같고 description이 같은 필드들은 같은 종류의 내용이 반복되는 자리입니다
  → 소스 자료에서 대응하는 항목들을 순서대로 매핑하세요
- "(고정 텍스트, 수정 불필요)"라고 표시된 필드는 건드리지 마세요 (set_paragraph_text 생성하지 않음)
- 날짜 필드는 소스 자료의 날짜를, 없으면 오늘 날짜를 넣으세요

### 대응 내용이 없으면 비우세요
소스 자료에 대응하는 내용이 없는 필드는 빈 문자열("")을 넣으세요.

## 매핑 전략 (반드시 준수)

### 1단계: description을 읽고 역할에 맞게 매핑하세요
각 슬롯의 description이 그 자리의 **역할**을 알려줍니다. 소스 자료에서 해당 역할에 맞는 내용을 찾아 넣으세요.
- description에 "제목"이 있으면 → 소스의 제목을 넣으세요
- description에 "세부 항목 본문"이 있으면 → 소스의 세부 내용을 넣으세요
- description에 "비전" 또는 "요약"이 있으면 → 소스의 요약/방향을 넣으세요
- description에 "과제" 또는 "전략"이 있으면 → 소스의 해당 섹션 내용을 넣으세요
**순서가 아니라 역할(description)을 기준으로 매핑하세요.**

### 2단계: 모든 슬롯에 명령을 생성하세요
"(고정 텍스트, 수정 불필요)"가 아닌 모든 paragraph idx에 대해 set_paragraph_text를 생성하세요.
모든 value_cells에 대해 set_cell을 생성하세요.
대응 내용이 없는 슬롯은 빈 문자열("")을 넣으세요.
**이 단계를 건너뛰고 add_paragraph를 사용하는 것은 금지입니다.**

### 3단계: 초과분만 add_paragraph
2단계에서 모든 기존 슬롯을 채운 뒤에도 소스 내용이 남으면, 해당 섹션의 마지막 슬롯과 같은 서식(paraPrIDRef, charPrIDRef)으로 add_paragraph하세요.

### 양식보다 소스가 적으면
남는 슬롯은 빈 문자열("")로 비우세요.

## 명령 타입

### ★ 서식 보존 명령 (우선 사용)
- **set_paragraph_text**: 기존 문단 텍스트 교체 (서식 보존). {"type": "set_paragraph_text", "index": N, "text": "내용"}
- **set_cell**: 표 셀 텍스트 교체 (서식 보존). {"type": "set_cell", "table": N, "row": R, "col": C, "text": "내용"}
- **add_row**: 기존 표에 행 추가. {"type": "add_row", "table": N, "count": 1, "cells": [["A","B"]]}

### 1x1 표 (텍스트 상자) 처리
description에 "(텍스트 상자)"가 있는 표는 반드시 set_cell(row=0, col=0)만 사용하세요.
내용이 길더라도 row=0, col=0 하나에 전부 넣으세요. row=1 이상은 절대 사용하지 마세요.
여러 행으로 나누지 말고, 줄바꿈(\n)으로 구분하여 하나의 셀에 넣으세요.

### 구조 변경 명령 (필요시만)
- **clear_body**: 지정 문단부터 끝까지 삭제. {"type": "clear_body", "from_paragraph": N}
- **add_paragraph**: 문서 끝에 문단 추가. {"type": "add_paragraph", "paraPrIDRef": "N", "charPrIDRef": "N", "text": "내용"}
- **insert_paragraph**: 특정 위치 앞에 삽입. {"type": "insert_paragraph", "index": N, "paraPrIDRef": "N", "charPrIDRef": "N", "text": "내용"}
- **add_table**: 문서 끝에 표 추가
- **remove_table**: 표 삭제. {"type": "remove_table", "table": N}
- **remove_paragraph**: 문단 삭제. {"type": "remove_paragraph", "index": N}

## 출력 규칙
반드시 아래 JSON 형식만 출력하세요. 다른 설명은 절대 포함하지 마세요.

```json
{
  "actions": [
    {"type": "set_paragraph_text", "index": 0, "text": "소스 자료의 제목"},
    {"type": "set_cell", "table": 0, "row": 1, "col": 1, "text": "소스 데이터"}
  ]
}
```

## 중요
1. "(고정 텍스트, 수정 불필요)"가 아닌 **모든** paragraph idx에 대해 set_paragraph_text를 생성하세요. 빠뜨리면 원본 양식 텍스트가 남아 내용이 섞입니다. 대응 내용이 없으면 빈 문자열("")을 넣으세요
2. 표의 value_cells에 대해 **모두** set_cell을 생성하세요. 표 번호는 _tbl_idx 값을 사용하세요
3. add_paragraph는 **모든 기존 슬롯에 set_paragraph_text를 생성한 뒤에만** 사용하세요. paraPrIDRef, charPrIDRef는 같은 level의 문단 값을 복사하세요
4. 양식보다 소스 내용이 적으면 남는 문단을 빈 문자열로 비우세요
5. 소스 자료의 내용을 빠짐없이 반영하되, 반드시 기존 슬롯을 먼저 활용하세요
"""


def build_structure_analysis_prompt(
    light_xml: str,
    auto_truncate: bool = True,
) -> list[dict]:
    """
    1차 호출: 양식 XML → 구조 분석 프롬프트

    Args:
        light_xml: 경량화된 양식 XML
        auto_truncate: 대형 XML 자동 축소 여부

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    if auto_truncate:
        tr = truncate_xml(light_xml)
        light_xml = tr["xml"]

    user_msg = (
        "아래 HWPX 양식 XML의 구조를 분석하세요.\n"
        "각 _idx 문단의 역할, 계층, 번호 기호를 파악하고, "
        "표의 라벨/값 셀을 구분하세요.\n\n"
        f"```xml\n{light_xml}\n```\n\n"
        "반드시 JSON만 출력하세요."
    )

    return [
        {"role": "system", "content": STRUCTURE_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def build_content_mapping_prompt(
    structure_json: dict,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
) -> list[dict]:
    """
    2차 호출: 구조 분석 결과 + 소스 내용 → JSON 명령 프롬프트

    Args:
        structure_json: 1차에서 파싱한 구조 분석 결과
        content_text: 작성할 내용 텍스트 (직접 입력)
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    structure_str = json.dumps(structure_json, ensure_ascii=False, indent=2)

    user_parts = []

    text_block = (
        "## 양식 구조 분석 결과\n"
        "아래는 양식의 구조를 분석한 결과입니다. "
        "각 필드의 설명(description), 계층(level), 번호 기호(marker)를 참고하여 "
        "소스 자료의 내용을 매핑하세요.\n\n"
        f"```json\n{structure_str}\n```\n\n"
        "## 소스 자료\n"
    )

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += (
            "아래는 PDF에서 추출한 텍스트입니다. "
            "이 내용만으로 양식을 채우세요.\n\n"
            f"```\n{pdf_text}\n```\n\n"
        )
        if has_content:
            text_block += f"추가 지시사항: {content_text}\n\n"
        text_block += "반드시 JSON만 출력하세요.\n"

        if has_images:
            user_parts.append({"type": "text", "text": text_block})
            for img_b64 in content_images:
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
        else:
            user_parts = text_block

    elif has_images:
        text_block += (
            "아래 첨부된 PDF 이미지의 내용만으로 양식을 채우세요.\n\n"
        )
        if has_content:
            text_block += f"추가 지시사항: {content_text}\n\n"
        text_block += "반드시 JSON만 출력하세요.\n"

        user_parts.append({"type": "text", "text": text_block})
        for img_b64 in content_images:
            user_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })

    else:
        text_block += f"{content_text}\n\n"
        text_block += "반드시 JSON만 출력하세요.\n"
        user_parts = text_block

    return [
        {"role": "system", "content": CONTENT_MAPPING_PROMPT},
        {"role": "user", "content": user_parts},
    ]


def parse_structure_from_llm(llm_response: str) -> dict:
    """
    1차 LLM 응답에서 구조 분석 JSON을 파싱합니다.

    Args:
        llm_response: LLM이 출력한 텍스트

    Returns:
        {"paragraphs": [...], "tables": [...]}
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("구조 분석 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError as e:
            raise ValueError(f"구조 분석 JSON 파싱 실패: {e}")

    if not isinstance(data, dict) or "paragraphs" not in data:
        raise ValueError("구조 분석 결과에 'paragraphs' 키가 없습니다")

    log.info(
        f"구조 분석 완료: 문단 {len(data.get('paragraphs', []))}개, "
        f"표 {len(data.get('tables', []))}개"
    )
    return data


def _escape_json_string_newlines(raw: str) -> str:
    """JSON 문자열 값 내부의 실제 개행/탭을 이스케이프 처리"""
    result = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
                continue
            elif ch == '\r':
                result.append('\\r')
                continue
            elif ch == '\t':
                result.append('\\t')
                continue
        result.append(ch)
    return ''.join(result)


def _repair_json(raw: str) -> str:
    """
    LLM이 흔히 만드는 JSON 오류를 복구합니다.

    처리하는 오류:
    - 후행 쉼표 (trailing comma): [1, 2,] → [1, 2]
    - 누락 쉼표: }"action" → },"action"  또는 ]"text" → ],"text"
    - 누락 쉼표: "value""key" → "value","key" (문자열-문자열 사이)
    - 단일 따옴표 → 이중 따옴표 (문자열 밖에서만)
    """
    # 1단계: 문자열 내부 개행 이스케이프
    raw = _escape_json_string_newlines(raw)

    # 2단계: 후행 쉼표 제거 — ,] 또는 ,}
    raw = re.sub(r',\s*([\]}])', r'\1', raw)

    # 3단계: 누락 쉼표 삽입
    # 패턴: } 뒤에 공백/개행 후 { 또는 " 가 오면 쉼표 삽입
    raw = re.sub(r'(\})\s*(\{)', r'\1,\2', raw)
    raw = re.sub(r'(\})\s*(")', r'\1,\2', raw)
    # 패턴: ] 뒤에 공백/개행 후 { 또는 " 또는 [ 가 오면
    raw = re.sub(r'(\])\s*(\{)', r'\1,\2', raw)
    raw = re.sub(r'(\])\s*(")', r'\1,\2', raw)
    raw = re.sub(r'(\])\s*(\[)', r'\1,\2', raw)

    # 패턴: 문자열 닫힌 " 뒤에 공백/개행 후 " 가 오면 (연속 문자열 사이 쉼표 누락)
    # 단, ":"는 제외 (key: value 구분자)
    # "value"  "next_key" → "value", "next_key"
    # 주의: "key": "value" 패턴은 건드리지 않도록 look-behind 사용
    raw = re.sub(r'(")\s*\n\s*(")', r'\1,\2', raw)

    # 패턴: 숫자/true/false/null 뒤에 개행 후 " 또는 { 또는 [ 오면
    raw = re.sub(r'(\d|true|false|null)\s*\n\s*(")', r'\1,\2', raw)
    raw = re.sub(r'(\d|true|false|null)\s*\n\s*(\{)', r'\1,\2', raw)

    return raw


def _extract_json_objects(text: str) -> list[dict]:
    """
    깨진 JSON에서 유효한 개별 객체를 하나씩 추출합니다.
    json.JSONDecoder.raw_decode()로 순차 파싱하여 "type" 키가 있는 객체만 수집합니다.
    """
    decoder = json.JSONDecoder()
    objects = []
    idx = 0
    while idx < len(text):
        # 다음 { 찾기
        brace_pos = text.find('{', idx)
        if brace_pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, brace_pos)
            if isinstance(obj, dict) and "type" in obj:
                objects.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx = brace_pos + 1
    if objects:
        log.info(f"개별 객체 추출 성공: {len(objects)}개 액션")
    return objects


def parse_actions_from_llm(llm_response: str) -> list[dict]:
    """
    LLM 응답 텍스트에서 actions JSON을 파싱합니다.

    Args:
        llm_response: LLM이 출력한 텍스트

    Returns:
        actions 리스트
    """
    # 1) ```json ... ``` 블록 추출 시도 (객체 또는 배열)
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        # 2) 가장 바깥 [ ] 또는 { } 추출
        bracket_match = re.search(r'\[[\s\S]*\]', llm_response)
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if bracket_match and brace_match:
            # 더 먼저 시작하는 쪽 사용
            raw = bracket_match.group(0) if bracket_match.start() < brace_match.start() else brace_match.group(0)
        elif bracket_match:
            raw = bracket_match.group(0)
        elif brace_match:
            raw = brace_match.group(0)
        else:
            log.error(f"LLM 응답에서 JSON을 찾을 수 없습니다: {llm_response[:200]}")
            raise ValueError("LLM 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e1:
        log.warning(f"JSON 1차 파싱 실패 ({e1}), 복구 시도...")
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired)
            log.info("JSON 복구 성공")
        except json.JSONDecodeError as e2:
            log.warning(f"JSON _repair_json 후에도 실패 ({e2}), 개별 객체 추출 시도...")
            # 최후 fallback: 개별 JSON 객체를 하나씩 추출
            data = _extract_json_objects(repaired)
            if not data:
                log.error(f"JSON 복구 최종 실패\n원문(앞500자): {raw[:500]}")
                raise ValueError(f"JSON 파싱 실패: {e2}")

    # data가 직접 리스트(배열)이면 그대로 사용, 아니면 "actions" 키에서 추출
    if isinstance(data, list):
        actions = data
    else:
        actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError(f"actions가 리스트가 아닙니다: {type(actions)}")

    log.info(f"LLM 응답에서 {len(actions)}개 명령 파싱 완료")
    return actions
