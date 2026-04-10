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

    # 5) 정리된 XML 출력
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

    para_count = len(root.findall(f".//{NS_HP}p"))
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

## HWPX XML 구조 설명
- <hp:p paraPrIDRef="N"> : 문단. N은 문단 스타일 ID
- <hp:run charPrIDRef="N"> : 텍스트 런. N은 글자 스타일 ID
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
    {"type": "set_cell", "table": 표인덱스, "row": 행, "col": 열, "text": "텍스트"},
    {"type": "clear_body", "from_paragraph": 본문시작인덱스},
    {"type": "add_paragraph", "paraPrIDRef": "스타일ID", "charPrIDRef": "글자ID", "text": "텍스트"},
    {"type": "add_table", "rows": 행수, "cols": 열수, "cells": [["셀1","셀2"],["셀3","셀4"]]},
    {"type": "remove_paragraph", "index": 문단인덱스}
  ]
}
```

## 명령 타입 설명
- set_cell: 기존 표의 셀 텍스트 교체 (table=문서 내 표 순번 0부터)
- clear_body: 지정 문단 인덱스부터 끝까지 기존 본문 삭제
- add_paragraph: 새 문단 추가 (paraPrIDRef/charPrIDRef는 양식에서 확인한 스타일 ID 사용)
- add_table: 새 표 추가
- remove_paragraph: 특정 문단 삭제

## 중요
1. 양식의 표 구조(헤더 표, 정보 표 등)는 최대한 유지하고 셀 내용만 교체하세요
2. 본문 영역은 clear_body로 기존 내용을 삭제한 후 add_paragraph로 새로 작성하세요
3. paraPrIDRef와 charPrIDRef는 반드시 양식 XML에서 확인된 값을 사용하세요
4. 양식에 없는 스타일 ID를 만들어내지 마세요
"""


def build_hwpx_prompt(light_xml: str, content_text: str) -> list[dict]:
    """
    AI에게 보낼 메시지 리스트를 생성합니다.

    Args:
        light_xml: 경량화된 양식 XML
        content_text: 작성할 내용 텍스트

    Returns:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
    """
    user_content = f"""## 양식 XML
아래는 HWPX 양식의 경량화된 XML입니다. 표 구조, 문단 스타일, 텍스트 내용을 분석하세요.

```xml
{light_xml}
```

## 작성할 내용
아래 내용을 위 양식에 맞게 작성하세요. 양식의 구조와 스타일을 유지하면서 내용만 교체합니다.

{content_text}

## 지시사항
1. 양식 XML을 분석하여 표 위치, 본문 시작 위치, 사용된 스타일 ID를 파악하세요
2. 표의 라벨 셀(보도일시, 담당부서 등)은 유지하고 값 셀만 교체하세요
3. 본문은 적절한 문단 인덱스부터 clear_body 후 add_paragraph로 새로 작성하세요
4. 반드시 JSON만 출력하세요
"""

    return [
        {"role": "system", "content": HWPX_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


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


def parse_actions_from_llm(llm_response: str) -> list[dict]:
    """
    LLM 응답 텍스트에서 actions JSON을 파싱합니다.

    Args:
        llm_response: LLM이 출력한 텍스트

    Returns:
        actions 리스트
    """
    # 1) ```json ... ``` 블록 추출 시도
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        # 2) 가장 바깥 { } 추출
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            log.error(f"LLM 응답에서 JSON을 찾을 수 없습니다: {llm_response[:200]}")
            raise ValueError("LLM 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # LLM이 JSON 문자열 값 안에 실제 개행(\n)을 넣는 경우 처리
        # JSON 문자열 내부의 제어문자를 이스케이프
        cleaned = _escape_json_string_newlines(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.error(f"JSON 파싱 실패: {e}\n원문: {raw[:500]}")
            raise ValueError(f"JSON 파싱 실패: {e}")

    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError(f"actions가 리스트가 아닙니다: {type(actions)}")

    log.info(f"LLM 응답에서 {len(actions)}개 명령 파싱 완료")
    return actions
