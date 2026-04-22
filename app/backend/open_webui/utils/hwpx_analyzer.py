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
from itertools import combinations, product
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
# 역할 기반 서식 그룹 추출 (Role-based style catalog)
# ============================================================


def _resolve_bg_color(doc, border_fill_id: str) -> str:
    """borderFillIDRef에서 배경색을 추출합니다."""
    bf = doc.border_fill(border_fill_id)
    if not bf:
        return ""
    for child in bf.children:
        if child.name == "fillBrush":
            for brush in child.children:
                if brush.name == "winBrush":
                    fc = brush.attributes.get("faceColor", "none")
                    if fc and fc != "none":
                        return fc
                elif brush.name == "gradFill" or brush.name == "patternFill":
                    return "(그라데이션/패턴)"
    return ""


def _resolve_border_style(doc, border_fill_id: str) -> str:
    """borderFillIDRef에서 테두리 스타일 요약을 추출합니다."""
    bf = doc.border_fill(border_fill_id)
    if not bf:
        return ""
    sides = []
    for child in bf.children:
        if child.name in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
            btype = child.attributes.get("type", "NONE")
            if btype != "NONE":
                sides.append(child.name.replace("Border", ""))
    if not sides:
        return ""
    if len(sides) == 4:
        return "테두리:전체"
    return "테두리:" + "+".join(sides)


def _describe_style(doc, char_pr_id: str, para_pr_id: str,
                     border_fill_id: str = "") -> str:
    """스타일 ID들을 사람이 읽을 수 있는 설명으로 변환합니다."""
    parts = []

    # 글자 속성
    cp = doc.char_property(char_pr_id)
    if cp:
        height = cp.attributes.get("height", "")
        if height:
            parts.append(f"{int(height) / 100:.0f}pt")
        color = cp.attributes.get("textColor", "")
        if color and color != "#000000":
            parts.append(f"색상:{color}")

    # 문단 속성
    pp = doc.paragraph_property(para_pr_id)
    if pp:
        if pp.align and pp.align.horizontal and pp.align.horizontal != "JUSTIFY":
            parts.append(f"정렬:{pp.align.horizontal}")
        if pp.margin:
            if pp.margin.left and int(pp.margin.left) > 0:
                parts.append(f"왼쪽여백:{pp.margin.left}")

    # 배경색 (borderFill)
    if border_fill_id:
        bg = _resolve_bg_color(doc, border_fill_id)
        if bg:
            parts.append(f"배경:{bg}")
        border = _resolve_border_style(doc, border_fill_id)
        if border:
            parts.append(border)

    return ", ".join(parts) if parts else "기본"


def extract_style_groups(hwpx_source) -> dict:
    """
    HWPX 양식에서 서식 그룹(style groups)을 자동 추출합니다.

    각 문단의 서식 속성(paraPrIDRef, charPrIDRef, borderFillIDRef 등) 조합으로
    고유한 "서식 그룹"을 식별하고, 같은 서식의 문단을 묶습니다.

    AI가 아닌 코드로 확정적으로 추출하며, 이후 AI가 각 그룹의 의미적 역할을
    해석하는 데 사용됩니다.

    Args:
        hwpx_source: 파일 경로(str), bytes, 또는 file-like object

    Returns:
        {
            "groups": {
                "g1": {
                    "fingerprint": "p24_c13",
                    "style_desc": "15pt, 색상:#CC0000",
                    "sample_text": "Ⅰ. 추진성과 및 평가",
                    "count": 3,
                    "is_table_box": False,
                    "table_dims": None,
                    "exemplar_idx": 0,
                    "indices": [0, 5, 12],
                },
                ...
            },
            "sequence": [
                {"group_id": "g1", "idx": 0, "text_preview": "2024년 주요..."},
                {"group_id": "g2", "idx": 1, "text_preview": "2024. 2. 1"},
                ...
            ],
            "data_tables": [
                {
                    "idx": 13,
                    "table_idx": 5,
                    "rows": 13, "cols": 5,
                    "sample_headers": ["내용", "일정", "비고"],
                },
            ],
        }
    """
    from hwpx import HwpxDocument

    if isinstance(hwpx_source, str):
        doc = HwpxDocument.open(hwpx_source)
    elif isinstance(hwpx_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(hwpx_source))
    else:
        doc = HwpxDocument.open(hwpx_source)

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    # fingerprint → group info 매핑
    fp_to_group = {}  # fingerprint → group_id
    groups = {}
    sequence = []
    data_tables = []
    group_counter = 0

    for idx, para in enumerate(doc.paragraphs):
        para_pr = str(para.para_pr_id_ref or "0")
        runs = para.runs
        first_char_pr = str(runs[0].char_pr_id_ref) if runs else "0"

        text = (para.text or "").strip()
        text_preview = text[:40] + ("…" if len(text) > 40 else "") if text else "(빈 문단)"

        tables = para.tables
        is_table_box = False
        table_dims = None
        fingerprint = None

        if tables:
            tbl = tables[0]
            row_cnt = int(tbl.element.get("rowCnt", "1"))
            col_cnt = int(tbl.element.get("colCnt", "1"))
            tbl_border = tbl.element.get("borderFillIDRef", "0")

            if row_cnt == 1:
                # 1행 표 (텍스트 박스 / 섹션 헤더) — 서식 그룹으로 취급
                is_table_box = True
                table_dims = f"1x{col_cnt}"

                # 첫 번째 셀 기준으로 fingerprint 계산
                cell = tbl.cell(0, 0)
                cell_paras = cell.paragraphs
                cell_para_pr = str(cell_paras[0].para_pr_id_ref) if cell_paras else "0"
                cell_char_pr = "0"
                if cell_paras and cell_paras[0].runs:
                    cell_char_pr = str(cell_paras[0].runs[0].char_pr_id_ref)

                # 셀 borderFill (배경색 결정)
                cell_border = cell.element.get("borderFillIDRef", "0")

                fingerprint = f"tbl_{col_cnt}_{tbl_border}_{cell_border}_{cell_para_pr}_{cell_char_pr}_{para_pr}_{first_char_pr}"

                # 텍스트는 모든 셀에서 추출하여 합침
                cell_texts = []
                for c in range(col_cnt):
                    try:
                        ct = (tbl.cell(0, c).text or "").strip()
                        if ct:
                            cell_texts.append(ct)
                    except Exception:
                        pass
                if cell_texts:
                    combined = " | ".join(cell_texts)
                    text_preview = combined[:40] + ("…" if len(combined) > 40 else "")
            elif row_cnt > 1:
                # 다중 행/열 데이터 표
                # 헤더 텍스트 샘플 추출
                sample_headers = []
                try:
                    first_row_cells = [tbl.cell(0, c) for c in range(min(col_cnt, 5))]
                    for c in first_row_cells:
                        ht = (c.text or "").strip()[:20]
                        if ht:
                            sample_headers.append(ht)
                except Exception:
                    pass

                data_tables.append({
                    "idx": idx,
                    "rows": row_cnt,
                    "cols": col_cnt,
                    "sample_headers": sample_headers,
                })

                # 데이터 표는 그룹에 포함하지 않고 시퀀스에만 기록
                sequence.append({
                    "group_id": "__data_table__",
                    "idx": idx,
                    "text_preview": f"[표 {row_cnt}x{col_cnt}]",
                })
                continue

        if fingerprint is None:
            # 일반 문단
            fingerprint = f"p_{para_pr}_{first_char_pr}"

        # 그룹 매핑
        if fingerprint not in fp_to_group:
            group_counter += 1
            gid = f"g{group_counter}"
            fp_to_group[fingerprint] = gid

            # 스타일 설명 생성
            if is_table_box:
                # 셀 내부 스타일로 설명
                parts = fingerprint.split("_")  # tbl_border_cellBorder_cellPP_cellCP_pp_cp
                cell_border_id = parts[2] if len(parts) > 2 else "0"
                cell_cp_id = parts[4] if len(parts) > 4 else "0"
                cell_pp_id = parts[3] if len(parts) > 3 else "0"
                style_desc = _describe_style(doc, cell_cp_id, cell_pp_id, cell_border_id)
                style_desc = f"[텍스트박스] {style_desc}"
            else:
                # 문단 border 확인
                pp_obj = doc.paragraph_property(para_pr)
                border_id = ""
                if pp_obj and pp_obj.border:
                    border_id = str(pp_obj.border.border_fill_id_ref or "")
                style_desc = _describe_style(doc, first_char_pr, para_pr, border_id)

            groups[gid] = {
                "fingerprint": fingerprint,
                "style_desc": style_desc,
                "sample_text": text_preview,
                "count": 1,
                "is_table_box": is_table_box,
                "exemplar_idx": idx,
                "indices": [idx],
            }
        else:
            gid = fp_to_group[fingerprint]
            groups[gid]["count"] += 1
            groups[gid]["indices"].append(idx)

        sequence.append({
            "group_id": fp_to_group[fingerprint],
            "idx": idx,
            "text_preview": text_preview,
        })

    log.info(
        f"서식 그룹 추출 완료: {len(groups)}개 그룹, "
        f"{len(sequence)}개 문단, {len(data_tables)}개 데이터 표"
    )

    return {
        "groups": groups,
        "sequence": sequence,
        "data_tables": data_tables,
    }


# ============================================================
# 역할 기반 AI 프롬프트 (v2)
# ============================================================

ROLE_INTERPRET_PROMPT = """당신은 한국 행정문서 양식 전문가입니다.
아래는 HWPX 양식 파일에서 자동 추출한 "서식 그룹" 목록입니다.
각 그룹은 같은 서식(폰트 크기, 배경색, 테두리 등)을 공유하는 문단/표 묶음입니다.

## 작업
각 서식 그룹이 문서에서 어떤 **역할**을 하는지 판별하세요.

## 역할 유형
다음 중 하나를 지정하세요:
- **title**: 문서 전체 제목
- **meta**: 날짜, 기관명 등 메타 정보
- **section_header**: 대분류 제목 (Ⅰ, Ⅱ, Ⅲ 등)
- **subsection_header**: 중분류 제목 (1., 2. 또는 □ 등)
- **item**: 세부 항목 (ㅇ, -, ❍ 등)
- **sub_item**: 하위 항목 (*, 주석, 부연)
- **summary_box**: 요약/핵심 문구 박스
- **spacer**: 빈 줄 (문단 간격용)
- **toc**: 목차
- **other**: 위에 해당 없음

## 출력 형식
반드시 아래 JSON만 출력하세요:

```json
{
  "roles": {
    "g1": {"role": "title", "label": "문서 제목"},
    "g2": {"role": "section_header", "label": "대분류 번호+제목"},
    "g3": {"role": "spacer", "label": "빈 줄"},
    "g4": {"role": "item", "label": "세부 항목 (❍)"}
  }
}
```

## 판별 힌트
- 큰 폰트 + 배경색/테두리 → 보통 제목 또는 섹션 헤더
- 텍스트박스(1x1 표) + 배경색 → 보통 요약 박스 또는 섹션 헤더
- ❍, ㅇ, □, - 같은 마커로 시작 → 항목 또는 소항목
- 빈 문단 → spacer
- 같은 역할인데 서식이 약간 다른 그룹이 있을 수 있음 (같은 label 부여 가능)
"""


def build_role_interpret_prompt(style_catalog: dict) -> list[dict]:
    """
    1차 AI 호출: 서식 그룹 → 역할 해석 프롬프트

    Args:
        style_catalog: extract_style_groups()의 반환값

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    groups = style_catalog["groups"]
    sequence = style_catalog["sequence"]

    # 그룹 목록 정리
    group_lines = []
    for gid, g in groups.items():
        dims = f" ({g.get('table_dims', '')})" if g.get('table_dims') else ""
        group_lines.append(
            f"- **{gid}**: {g['style_desc']}{dims}, "
            f"출현 {g['count']}회, "
            f"샘플: \"{g['sample_text']}\""
        )

    # 시퀀스 미리보기 (문서 순서 파악용, 앞 40개)
    seq_lines = []
    for s in sequence[:40]:
        seq_lines.append(f"  [{s['group_id']}] \"{s['text_preview']}\"")
    if len(sequence) > 40:
        seq_lines.append(f"  ... (총 {len(sequence)}개)")

    user_msg = (
        "## 서식 그룹 목록\n"
        + "\n".join(group_lines)
        + "\n\n## 문서 순서 (앞부분)\n"
        + "\n".join(seq_lines)
        + "\n\n반드시 JSON만 출력하세요."
    )

    return [
        {"role": "system", "content": ROLE_INTERPRET_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_role_interpret_from_llm(llm_response: str) -> dict:
    """1차 AI 응답에서 역할 해석 JSON을 파싱합니다."""
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("역할 해석 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"역할 해석 JSON 파싱 실패: {e}")

    if "roles" not in data:
        raise ValueError("역할 해석 결과에 'roles' 키가 없습니다")

    return data["roles"]  # {"g1": {"role": "title", "label": "..."}, ...}


ROLE_CONTENT_PROMPT = """당신은 한국 행정문서 작성 전문가입니다.
양식의 서식 그룹(역할)을 사용하여 소스 자료의 내용을 문서로 구성합니다.

## 역할 카탈로그
아래는 양식에서 사용 가능한 서식 역할입니다. 각 역할은 고유한 서식(폰트, 배경, 테두리)을 가집니다.

{catalog}

## 작업
소스 자료의 내용을 위 역할들을 사용하여 문서로 구성하세요.
- 각 내용 항목에 적절한 역할(group_id)을 지정하세요
- 대제목 → section_header 역할, 중제목 → subsection_header 역할, 세부 내용 → item 역할 등
- 소스 자료의 모든 내용을 빠짐없이 포함하세요
- 소스에 없는 내용을 만들어내지 마세요
- 마커(□, ㅇ, - 등)는 역할에 맞게 포함하세요

## 출력 형식
반드시 아래 JSON만 출력하세요:

```json
{{
  "header": {{
    "title": "문서 제목",
    "meta": ["2024. 1. 15.", "기관명"]
  }},
  "body": [
    {{"group": "g2", "text": "Ⅰ. 첫 번째 대분류"}},
    {{"group": "g4", "text": "□ 첫 번째 중분류 항목"}},
    {{"group": "g5", "text": "ㅇ 세부 내용 1"}},
    {{"group": "g5", "text": "ㅇ 세부 내용 2"}},
    {{"group": "g2", "text": "Ⅱ. 두 번째 대분류"}},
    {{"group": "g4", "text": "□ 두 번째 중분류 항목"}}
  ]
}}
```

## 중요
1. group 값은 반드시 역할 카탈로그에 있는 group_id를 사용하세요
2. spacer 역할은 직접 지정하지 마세요 (시스템이 자동 삽입)
3. header.title은 문서 전체 제목, header.meta는 날짜/기관 등 부가정보
4. body는 본문 내용을 문서 순서대로 나열
"""


def build_role_content_prompt(
    style_catalog: dict,
    role_map: dict,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
) -> list[dict]:
    """
    2차 AI 호출: 역할 카탈로그 + 소스 내용 → 역할 태깅 콘텐츠 프롬프트

    Args:
        style_catalog: extract_style_groups()의 반환값
        role_map: parse_role_interpret_from_llm()의 반환값
        content_text: 직접 입력한 내용
        content_images: PDF 페이지 base64 JPEG 이미지
        pdf_text: PDF에서 추출한 텍스트

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    groups = style_catalog["groups"]

    # 역할 카탈로그 텍스트 생성
    catalog_lines = []
    for gid, g in groups.items():
        role_info = role_map.get(gid, {})
        role_name = role_info.get("role", "other")
        label = role_info.get("label", "")
        if role_name == "spacer":
            continue  # spacer는 AI가 사용하지 않음
        dims = f" ({g.get('table_dims', '')})" if g.get('table_dims') else ""
        catalog_lines.append(
            f"- **{gid}** [{role_name}]: {label} — {g['style_desc']}{dims}, "
            f"샘플: \"{g['sample_text']}\""
        )

    catalog_text = "\n".join(catalog_lines)
    system_prompt = ROLE_CONTENT_PROMPT.replace("{catalog}", catalog_text)

    # 소스 내용 구성
    user_parts = []
    text_block = "## 소스 자료\n"

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += f"```\n{pdf_text}\n```\n\n"
        if has_content:
            text_block += f"추가 지시사항: {content_text}\n\n"
    elif has_content:
        text_block += f"{content_text}\n\n"

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

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]


def parse_role_content_from_llm(llm_response: str) -> dict:
    """2차 AI 응답에서 역할 태깅 콘텐츠 JSON을 파싱합니다."""
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("콘텐츠 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"콘텐츠 JSON 파싱 실패: {e}")

    if "header" not in data or "body" not in data:
        raise ValueError("콘텐츠 결과에 'header' 또는 'body' 키가 없습니다")

    return data  # {"header": {...}, "body": [...]}


# ============================================================
# AI 프롬프트 생성 및 응답 파싱 (v1 — 기존 호환용)
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
        identity_map = {int(idx): int(idx) for idx in all_original_indices}
        return {"xml": light_xml, "removed_indices": [], "idx_map": identity_map}

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
    # idx_map: {new_idx → old_idx} — AI가 보는 번호 → 원본 문서의 실제 위치
    idx_map = {}
    for new_idx, (old_idx, p) in enumerate(surviving):
        p.set("_idx", str(new_idx))
        idx_map[new_idx] = old_idx

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
    return {"xml": result, "removed_indices": removed_indices, "idx_map": idx_map}


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


def split_source_by_chapters(
    pdf_text: str,
    chapter_titles: list[str],
) -> list[str]:
    """
    소스 텍스트를 대제목 기준으로 섹션별로 분할합니다.

    각 대제목의 위치를 찾아 그 사이 텍스트를 잘라냅니다.
    대제목을 찾지 못하면 전체 텍스트를 반환합니다.

    Args:
        pdf_text: 전체 소스 텍스트
        chapter_titles: 2a에서 추출한 대제목 리스트 (순서대로)

    Returns:
        각 대제목에 해당하는 텍스트 조각 리스트 (chapter_titles와 같은 길이)
    """
    if not chapter_titles or not pdf_text:
        return [pdf_text] * max(len(chapter_titles), 1)

    # 각 대제목의 소스 텍스트 내 위치 찾기
    positions = []
    for title in chapter_titles:
        pos = _find_title_in_text(pdf_text, title)
        positions.append(pos)

    # 위치 기반으로 텍스트 분할
    sections = []
    for i, pos in enumerate(positions):
        if pos < 0:
            # 대제목을 못 찾은 경우 → 빈 문자열 (2b에서 전체 소스 fallback 가능)
            sections.append("")
            continue

        # 끝 위치: 다음 대제목의 시작 또는 텍스트 끝
        end_pos = len(pdf_text)
        for j in range(i + 1, len(positions)):
            if positions[j] >= 0:
                end_pos = positions[j]
                break

        sections.append(pdf_text[pos:end_pos].strip())

    # 못 찾은 섹션에 전체 텍스트 할당 (fallback)
    for i, sec in enumerate(sections):
        if not sec:
            sections[i] = pdf_text
            log.warning(
                f"대제목 '{chapter_titles[i]}' 위치를 찾지 못함 → 전체 텍스트 사용"
            )

    log.info(
        f"소스 텍스트 분할: {len(sections)}개 섹션, "
        f"길이: {[len(s) for s in sections]}"
    )
    return sections


def _find_title_in_text(text: str, title: str) -> int:
    """
    소스 텍스트에서 대제목 위치를 찾습니다.
    정확 매칭 → 공백 무시 매칭 → 핵심 키워드 매칭 순으로 시도합니다.

    Returns:
        찾은 위치 (0-based), 못 찾으면 -1
    """
    # 1) 정확한 부분 문자열 매칭
    pos = text.find(title)
    if pos >= 0:
        return pos

    # 2) 공백/줄바꿈 무시 매칭
    #    title의 각 문자 사이에 \s* 허용
    escaped_chars = []
    for ch in title.strip():
        if ch in r'\.^$*+?{}[]|()':
            escaped_chars.append(re.escape(ch))
        elif ch.isspace():
            escaped_chars.append(r'\s+')
        else:
            escaped_chars.append(re.escape(ch))
    # 연속 \s+ 합치기
    pattern_parts = []
    for part in escaped_chars:
        if part == r'\s+' and pattern_parts and pattern_parts[-1] == r'\s+':
            continue
        pattern_parts.append(part)
    ws_pattern = r'\s*'.join(
        p for p in pattern_parts if p != r'\s+'
    ) if pattern_parts else re.escape(title)

    try:
        m = re.search(ws_pattern, text)
        if m:
            return m.start()
    except re.error:
        pass

    # 3) 핵심 키워드 매칭 — 마커 제거 후 키워드로 검색
    #    "Ⅰ. 추진 배경 및 필요성" → "추진 배경 및 필요성"
    core = re.sub(r'^[\sⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ\d.)\-–—]+', '', title).strip()
    if core and len(core) >= 4:
        pos = text.find(core)
        if pos >= 0:
            # 마커 포함 가능성 → 앞쪽으로 약간 확장
            line_start = text.rfind('\n', max(0, pos - 30), pos)
            return line_start + 1 if line_start >= 0 else max(0, pos - 20)

    return -1


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
양식 XML을 분석하여 각 필드의 **의미적 역할(role)**, 용도(description), 마커, 표 구조를 JSON으로 출력합니다.

**⚠️ level(계층 깊이)은 이 단계에서 결정하지 않습니다** — 별도 단계에서 처리합니다.

## XML 구조 안내
- <hp:p paraPrIDRef="N" _idx="I">: 섹션 레벨 문단 (I가 인덱스, N이 문단 스타일 ID)
- <hp:run charPrIDRef="N">: 텍스트 런 (N이 글자 스타일 ID)
- <hp:t>텍스트</hp:t>: 실제 텍스트 내용
- <hp:tbl rowCnt="R" colCnt="C" _tbl_idx="T">: 표 (R행 C열, T가 표 순번 인덱스)
- <hp:tc>: 표 셀, <hp:cellAddr colAddr="C" rowAddr="R"/>: 셀 위치

## 분석 규칙

### 문단 분석
_idx가 있는 모든 문단에 대해:
- **marker**: 번호 기호가 있으면 그대로 기록 (➊, 󰊲, Ⅰ., 1., 1), 가., 과제 1 등), 없으면 ""
- **description**: 이 자리에 **어떤 내용이 어떤 형식으로** 들어가야 하는지 구체적으로 설명
- **role**: 이 문단의 의미/구조 역할. **같은 마커 종류는 반드시 같은 role** (paraPrIDRef 차이는 무시)
- **paraPrIDRef**: <hp:p>의 paraPrIDRef 속성값 (참고용)
- **charPrIDRef**: 첫 번째 <hp:run>의 charPrIDRef 속성값 (참고용)

### role 부여 규칙
role은 **마커 종류 + 의미적 역할**이 기준입니다. 서식 속성(paraPrIDRef, charPrIDRef)은 role 분류에 사용하지 마세요.

1. **같은 마커 종류 → 반드시 같은 role** (예: 모든 "□" 문단 → section_header)
2. **paraPrIDRef/charPrIDRef가 달라도 마커가 같으면 같은 role을 사용하세요** — 서식 미세 차이로 role을 나누지 마세요
3. **다른 마커 종류이면 다른 role** (예: ㅇ와 ➊는 다른 role)
4. **🚫 alt1, alt2, _sub, _1, _2 같은 숫자/접미사로 role 세분화 금지** — 같은 의미면 하나의 role 이름만 사용
5. 마커가 없는 문단은 **위치와 의미**로 판단: cover_title, cover_date, cover_org, toc, chapter_title, strategy_header 등
6. role 이름은 자유롭게 지정하되, 의미있는 이름을 사용하세요
   예: "chapter_title", "section_header", "detail_item", "note", "summary_box", "task_header", "task_detail"
7. 특수 역할:
   - "spacer": 빈 문단 (구분용 빈 줄)
   - "fixed": 페이지 번호, 머리글/바닥글 등 수정 불필요한 레이아웃 요소

### description 작성 규칙
1. 해당 위치의 **구조적·관계적 역할**을 기술하세요. **주제/도메인은 절대 언급 금지**.
   - ❌ 주제 기반(잘못됨): "과일 가격 변동 설명", "교육정책 추진 현황", "조달청 사업 목록"
   - ✓ 구조 기반(맞음): "상위 항목에 대한 구체적 사례 또는 수치 제시 (단문)"
   - 이유: 양식과 전혀 다른 주제의 소스를 매핑해야 하므로, 주제가 들어가면 매핑 혼란
2. 기술해야 할 것:
   - **함수** (제목/요약/세부 항목/보충/결론/참고/강조 등)
   - **관계** (부모와의 관계: 설명/근거/예시/반대 사례/부연/요약)
   - **형식 단서** (짧은 한 줄 / 한 문장 / 여러 문장 / 수치 포함 / 인용문 등)
   - **옵션**: 시간·인과·열거 등 관계 패턴
3. 좋은 예:
   - "문서 최상위 제목 (한 줄, 핵심 주제 명시)"
   - "작성일자 (yyyy. m. d. 형식, 순수 날짜)"
   - "장 시작부 서두 박스 (전체 요지 1~2문장)"
   - "중분류 항목 제목 (짧은 한 줄, 하위 세부 항목의 주제)"
   - "상위 항목 아래 구체 사실/수치 (한 문장, 증거성)"
   - "보충 설명 또는 예시 (부모 내용에 대한 부연, 선택적)"
   - "관련 법령·규정 인용 박스 (원문 인용형)"
   - "장 종료 전환 요약 박스 (다음 장으로의 흐름)"
4. **같은 구조적 위치의 필드는 동일한 description 사용**
5. **"(고정 텍스트, 수정 불필요)"는 극히 제한적으로만 사용** — 페이지 번호, 머리글/바닥글 같은 순수 레이아웃만

### 표 분석
문서 내 모든 표에 대해 (0번부터 순서대로):
- **description**: 표의 용도를 구체적으로 설명
- **headers**: 라벨(항목명) 셀 목록
- **value_cells**: 데이터가 채워질 셀 목록

### 1x1 표 (텍스트 상자)
rowCnt="1" colCnt="1"인 표는 **텍스트 상자/강조 박스**입니다.
- tables 배열에 포함하되, description에 "(텍스트 상자)" 추가
- **value_cells는 반드시 [{"row": 0, "col": 0}]** — 빈 배열 금지
- headers는 빈 배열 []

## 출력 형식
반드시 아래 JSON만 출력하세요. **level은 출력하지 마세요** (다음 단계에서 결정).

```json
{
  "paragraphs": [
    {"idx": 0, "marker": "", "role": "cover_title", "description": "문서 전체 제목 (연도+기관+사업명+문서종류 형식)", "paraPrIDRef": "5", "charPrIDRef": "12"},
    {"idx": 1, "marker": "", "role": "cover_date", "description": "작성일자 (yyyy. m. d. 형식)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 2, "marker": "", "role": "cover_org", "description": "발신 기관명", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 3, "marker": "", "role": "toc", "description": "목차 (텍스트 상자)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 4, "marker": "Ⅰ.", "role": "chapter_title", "description": "대분류 제목 (텍스트 상자)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 5, "marker": "□", "role": "section_header", "description": "중분류 항목 제목", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 6, "marker": "ㅇ", "role": "detail_item", "description": "세부 항목의 설명 본문", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 7, "marker": "*", "role": "note", "description": "참고/보충 설명", "paraPrIDRef": "0", "charPrIDRef": "0"}
  ],
  "tables": [
    {"table": 0, "rows": 5, "cols": 3, "description": "사업별 예산 배분 현황표",
     "headers": [{"row": 0, "col": 0, "text": "구분"}, {"row": 0, "col": 1, "text": "금액"}],
     "value_cells": [{"row": 1, "col": 1}, {"row": 2, "col": 1}]}
  ]
}
```

## 중요
- **level은 절대 출력하지 마세요** — 별도 단계에서 결정합니다
- 양식의 텍스트는 샘플입니다. 샘플 텍스트 자체를 description에 넣지 마세요
- _idx가 있는 문단을 하나도 빠뜨리지 마세요
- **같은 마커 종류는 반드시 같은 role** — paraPrIDRef 차이로 나누지 마세요
- **🚫 alt1, alt2, _sub 같은 숫자/접미사로 role 세분화 금지**
- 표의 headers(라벨)와 value_cells(데이터)를 정확히 구분하세요
- **1x1 표의 value_cells는 [{"row": 0, "col": 0}]** (빈 배열 금지)
"""


LEVEL_ANALYSIS_PROMPT = """당신은 HWPX 양식의 계층 구조(level) 분석 전문가입니다.
이미 role과 marker가 부여된 문단 목록을 받아, 각 문단의 **level(계층 깊이)**을 결정합니다.

## level이란?
문서의 계층적 깊이를 나타내는 정수값입니다.
- 0: 최상위 (표지, 날짜, 기관명, 목차 등)
- 1: 대제목 (장)
- 2: 중제목
- 3: 소제목
- 4, 5, 6...: 더 깊은 세부 항목

## 판단 원리: 마커 시퀀스 연속성

**level은 마커의 "시퀀스 연속성"으로 판단합니다**. 특정 마커 리스트를 외우지 말고, **양식의 실제 마커 패턴**을 관찰하세요.

### 원리 1: 같은 마커 시퀀스의 형제 = 같은 level
- 예: "과제 1", "과제 2", "과제 3"이 연속되면 모두 같은 level
- 예: "󰊱", "󰊲", "󰊳"이 연속되면 모두 같은 level
- 예: "➊", "➋", "➌"이 연속되면 모두 같은 level

### 원리 2: 새로운 마커 시퀀스가 시작되면 = 직전 문단의 자식 (level +1)
- 예: "과제 1" 다음에 "󰊱"이 나오면 → 󰊱는 과제의 자식 (level +1)
- 예: "󰊱" 다음에 "➊"이 나오면 → ➊는 󰊱의 자식 (level +1)
- 예: "ㅇ" 다음에 "*"이 나오면 → *는 ㅇ의 자식 (level +1)
- **핵심**: "새 마커 시퀀스의 첫 항목은 직전 문단의 자식"

### 원리 3: 이전 시퀀스로 돌아오면 = level도 돌아감
- 예: "과제 1" → "󰊱" → "➊" → "➋" → **"󰊲"** 가 나오면 → 󰊲는 "과제 1" 아래 "󰊱"의 형제 (level 복귀)
- 즉 더 깊은 시퀀스가 끝나고 얕은 시퀀스가 다시 나타나면, 그 얕은 시퀀스는 이전 level로 돌아감

### 원리 4: 마커 없는 문단 (marker="")
- role이 "cover_title", "cover_date", "cover_org", "toc" 등이면 level 0
- role이 "chapter_title" 같은 장 제목이면 level 1
- role이 "strategy_header", "task_header" 같은 전략/과제 제목이면 문맥상 level 결정
- 마커 없지만 같은 role이면 같은 level

### 원리 5: 들여쓰기/공백 힌트 (참고용)
양식 XML에 indent 정보가 있으면 참고하되, **원리 1~4의 시퀀스 연속성을 우선**하세요.

## 주의사항
- 마커 리스트(Ⅰ, 󰊱, ➊ 등)를 외우지 말고 **실제 양식의 마커 패턴**을 보세요
- **보충 마커(*, **, ※)도 시퀀스 연속성으로 판단** — 직전 문단(ㅇ 또는 ➊ 등)의 자식인 경우가 많지만, 시퀀스 흐름을 보고 결정
- level은 **양식 내 상대적 깊이**입니다. 절대값이 아닙니다.
- 같은 role이면 대부분 같은 level (예외: 문맥에 따라 다를 수 있음)

## 출력 형식
반드시 아래 JSON만 출력하세요. 다른 설명은 포함하지 마세요.

```json
{
  "levels": [
    {"idx": 0, "level": 0},
    {"idx": 1, "level": 0},
    {"idx": 2, "level": 0},
    {"idx": 3, "level": 0},
    {"idx": 4, "level": 1},
    {"idx": 5, "level": 2},
    {"idx": 6, "level": 3},
    {"idx": 7, "level": 4}
  ]
}
```

## 중요
- **모든 idx의 level을 출력하세요** — 하나도 빠뜨리지 마세요
- level은 정수. 최상위는 0
- 반드시 JSON만 출력. 다른 설명 포함 금지
"""

CONTENT_MAPPING_PROMPT = """당신은 HWPX 문서 작성 전문가입니다.
양식의 구조를 먼저 이해한 뒤, 소스 자료의 내용을 양식 구조에 맞게 배치합니다.

## 핵심 전략: 양식 구조 먼저, 소스 내용 나중

작업 순서:
1. **양식 구조 파악**: 양식에 어떤 role이 있고, 어떤 계층 관계인지 이해
2. **소스 내용 읽기**: 소스 자료 전체를 읽고, 내용의 주제/구조 파악
3. **role별 채우기**: 양식의 각 role에 맞는 소스 내용을 찾아 배치

양식은 **서식 틀**입니다. 양식의 내용(조달, 과제 등)은 무시하세요.
양식의 **계층 구조**(대제목 → 중제목 → 내용 → 보충)를 소스 내용으로 채우세요.

## role별 채우기 방법

양식 구조 패턴을 보고, 각 role에 소스 내용을 대응시키세요:

- **toc**: 소스에 목차가 있으면 그 내용으로 채움. 없으면 소스 내용의 대제목 목록으로 생성
- **대제목 role** (chapter_title 등): 소스에서 가장 큰 주제 단위를 찾아 넣음
- **중제목 role** (section_header 등): 대제목 아래의 세부 주제를 찾아 넣음
- **내용 role** (detail_item 등): 중제목 아래의 구체적 내용을 찾아 넣음
- **보충 role** (note 등): 내용의 보충 설명, 참고사항(※, * 등)을 찾아 넣음
- **요약 role** (summary_box 등): 해당 섹션의 핵심을 요약하여 넣음

**양식에 있는 role은 가능한 한 모두 사용하세요.**
소스에 정확히 대응하는 내용이 없는 role은 생략해도 됩니다.
개수는 자유 — 양식보다 많아도, 적어도 됩니다. 시스템이 자동 조절합니다.

## 출력 형식

```json
{
  "header": {
    "cover_title": "소스 문서 제목",
    "cover_date": "작성일자",
    "cover_org": "기관명"
  },
  "body": [
    {"role": "toc", "text": "목차 내용"},
    {"role": "chapter_title", "text": "대제목"},
    {"role": "section_header", "text": "□ 중제목"},
    {"role": "detail_item", "text": "ㅇ 내용"},
    {"role": "note", "text": "* 보충 설명"}
  ]
}
```

## 규칙

### header
- 문서 앞부분에 한 번만 나오는 요소 (표지 제목, 날짜, 기관명 등)
- header의 key는 양식의 role 이름 그대로 사용
- 소스에 해당 정보가 없으면 생략

### body
- 문서에 나타날 순서대로 나열
- **양식의 계층 구조를 따르세요** — 대제목 안에 중제목, 중제목 안에 내용
- spacer, fixed role은 사용하지 마세요 — 시스템이 자동 처리
- toc role이 양식에 있으면 **반드시 body에 포함**

### 마커
- 소스 원문의 마커(◇, ◆, ⇒ 등)는 **무시**하고 해당 role의 **양식 마커**를 사용
- ※로 시작하는 보충 설명은 `note` role로 분리

### 항목 길이
- 양식 샘플과 비슷한 길이로 유지
- 길면 같은 role로 여러 항목으로 나누세요

### 문체
- 양식 샘플의 문체와 말투를 따르세요

## 중요
1. 소스 자료에 없는 텍스트를 만들어내지 마세요
2. **소스 자료의 모든 내용을 빠짐없이 반영하세요** — 요약하거나 생략하지 마세요
3. **role은 반드시 1차 구조 분석에서 부여된 role만 사용 — 새 role을 만들지 마세요**
4. **양식에 있는 role을 최대한 다양하게 사용하세요**
5. 반드시 JSON만 출력. 다른 설명 포함 금지
"""


def _extract_texts_by_idx(truncated_xml: str) -> dict:
    """축소된 XML에서 각 _idx의 텍스트를 추출합니다."""
    root = etree.fromstring(truncated_xml.encode("utf-8"))
    texts = {}
    sections = [root] if root.tag == f"{NS_HP}sec" else root.findall(f".//{NS_HP}sec")
    if not sections:
        sections = [root]
    for section in sections:
        for p in section.findall(f"{NS_HP}p"):
            idx_val = p.get("_idx")
            if idx_val is None:
                continue
            idx = int(idx_val)
            # 모든 <hp:t> 텍스트 수집 (표/container 내부 포함)
            all_text = []
            for t in p.iter(f"{NS_HP}t"):
                if t.text and t.text.strip():
                    all_text.append(t.text.strip())
            texts[idx] = " ".join(all_text)[:80]  # 80자 제한
    return texts


def build_structure_analysis_prompt(
    light_xml: str,
    auto_truncate: bool = True,
) -> list[dict]:
    """
    1차 호출: 양식 XML → 구조 분석 프롬프트 (role + description + marker + table)

    level은 별도 단계(build_level_analysis_prompt)에서 결정합니다.

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
        "각 _idx 문단의 **role, description, marker, paraPrIDRef, charPrIDRef**를 파악하고, "
        "표의 라벨/값 셀을 구분하세요.\n"
        "**level은 이 단계에서 출력하지 마세요** — 별도 단계에서 결정합니다.\n\n"
        f"```xml\n{light_xml}\n```\n\n"
        "반드시 JSON만 출력하세요."
    )

    return [
        {"role": "system", "content": STRUCTURE_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def build_level_analysis_prompt(structure_json: dict, signals: dict = None) -> list[dict]:
    """
    1.5차 호출: 구조 분석 결과 → 각 문단의 level 결정

    Args:
        structure_json: build_structure_analysis_prompt/parse_structure_from_llm의 결과
                        paragraphs에 idx/role/marker/description이 있어야 함
        signals: compute_role_context_signals() 결과 (선택, 있으면 프롬프트에 포함)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    paragraphs = structure_json.get("paragraphs", [])

    # signals에서 paragraph 텍스트 맵
    text_by_idx = {}
    if signals:
        for pt in signals.get("paragraph_texts", []):
            text_by_idx[pt.get("idx")] = pt.get("text", "")

    # 문단 입력: idx, role, marker, text(있으면), description
    para_lines = []
    for p in paragraphs:
        idx = p.get("idx", -1)
        role = p.get("role", "")
        marker = p.get("marker", "")
        desc = p.get("description", "")
        marker_str = f'"{marker}"' if marker else '""'
        text_preview = text_by_idx.get(idx, "")[:80] if text_by_idx else ""
        if text_preview:
            text_esc = (
                text_preview.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            )
            para_lines.append(
                f'{{"idx": {idx}, "marker": {marker_str}, "role": "{role}", '
                f'"text": "{text_esc}", "description": "{desc}"}}'
            )
        else:
            para_lines.append(
                f'{{"idx": {idx}, "marker": {marker_str}, "role": "{role}", "description": "{desc}"}}'
            )
    para_text = "[\n  " + ",\n  ".join(para_lines) + "\n]"

    # signals 섹션 (선택적)
    signals_section = ""
    if signals:
        compressed = signals.get("compressed_sequence", "")
        role_to_letter = signals.get("role_to_letter", {})
        role_stats = signals.get("role_stats", {})
        adjacency = signals.get("adjacency", {})

        signals_section += "\n## 구조 시그널 (코드 추출)\n\n"
        if compressed:
            signals_section += f"### 압축 시퀀스\n`{compressed}`\n\n"
            if role_to_letter:
                signals_section += "### role → letter 매핑\n"
                for r, l in role_to_letter.items():
                    signals_section += f"- {l} = {r}\n"
                signals_section += "\n"
        if role_stats:
            signals_section += "### Role별 등장 통계\n"
            for role, stats in role_stats.items():
                cnt = stats.get("count", 0)
                markers = stats.get("markers", [])
                mk_preview = markers[:3]
                if len(markers) > 3:
                    mk_preview.append(f"...외 {len(markers) - 3}개")
                signals_section += f"- {role}: {cnt}회, markers={mk_preview}\n"
            signals_section += "\n"
        if adjacency.get("prev"):
            signals_section += "### 인접 role 통계 (각 role 직전에 무엇이 왔나)\n"
            for r, prevs in adjacency["prev"].items():
                signals_section += f"- {r} ← {prevs}\n"
            signals_section += "\n"

    user_msg = (
        "아래는 양식의 문단 목록 + 구조 분석 시그널입니다. "
        "각 문단에 대해 **level(계층 깊이)**을 결정하세요.\n"
        + signals_section
        + "## ⚠️ level 값이 어떻게 쓰이는지 (반드시 숙지)\n"
        "당신이 주는 level 숫자는 **스택 알고리즘**으로 parent-child 트리를 만드는 데 쓰입니다:\n"
        "- **같은 level 값** = 서로 **형제** 관계 (공통 부모의 자식들)\n"
        "- **직전 문단 level + 1** = 그 직전 문단의 **자식**\n"
        "- **이전 level로 복귀** = 공통 부모의 또 다른 자식 블록 시작\n\n"
        "예를 들어 `chapter_title(1) → summary_box(2) → section_header(3)` 이렇게 주면\n"
        "코드는 **section_header가 summary_box의 자식**이라고 해석합니다.\n"
        "반면 `chapter_title(1) → summary_box(2) → section_header(2)`로 주면\n"
        "둘 다 chapter_title의 자식(형제 관계)으로 해석됩니다.\n\n"
        "## ⚠️ 자주 틀리는 케이스 — 서두/결어 박스 오인\n"
        "양식 첫머리나 끝에 나오는 **요약/서두/결어성 박스**는 뒤의 본문과 **형제**입니다.\n"
        "- 서두 박스(summary, intro, abstract 등): 챕터/섹션 시작 직후 등장, 자식 없음, 1개만 등장\n"
        "- 결어 박스(transition, conclusion 등): 챕터/섹션 끝에 등장, 자식 없음, 1개만 등장\n"
        "- 이런 박스는 **자신과 형제인 실 본문 role과 같은 level**이어야 합니다.\n\n"
        "### 잘못된 예 ❌\n"
        "```\nchapter_title (1)\n  summary_box (2)  ← 서두\n    section_header (3) ← summary 자식으로 잘못 해석됨!\n      detail_item (4)\n  transition_box (2) ← 결어\n```\n"
        "### 올바른 예 ✓\n"
        "```\nchapter_title (1)\n  summary_box (2)  ← chapter의 자식\n  section_header (2) ← chapter의 자식 (summary와 형제)\n    detail_item (3)\n  transition_box (2) ← chapter의 자식\n```\n\n"
        "### 서두/결어 박스 판별 팁\n"
        "1. 바로 뒤에 여러 role이 반복 등장하는가? → 뒤의 것들이 본문, 박스는 서두\n"
        "2. 챕터 내에서 1회만 등장하는가? (role_stats count가 작음) → 서두/결어 후보\n"
        "3. 자신의 '자식'으로 보이는 role들이 다른 챕터에서는 박스 없이도 등장하는가? → 형제 관계\n\n"
        "## 기타 판단 원리\n"
        "- **실제 텍스트 내용(text 필드)을 보고 의미적 계층 파악** — 제일 중요\n"
        "- 압축 시퀀스와 인접 role 통계로 자식 관계 추정\n"
        "- 같은 role이라도 맥락 다르면 인스턴스별로 다른 level 가능\n"
        "- 같은 마커 시퀀스의 연속은 같은 level\n"
        "- 마커 없는 cover_title/date/org/toc은 level 0\n"
        "- 마커 없는 chapter_title은 level 1\n"
        "- 문단 순서대로 시퀀스 흐름을 추적해서 판단\n\n"
        f"## 문단 목록\n```json\n{para_text}\n```\n\n"
        "반드시 JSON만 출력하세요 (levels 배열).\n"
    )

    return [
        {"role": "system", "content": LEVEL_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_level_from_llm(llm_response: str) -> dict:
    """
    1.5차 LLM 응답에서 levels를 파싱합니다.

    Returns:
        {idx: level} dict
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("level 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"level JSON 파싱 실패: {e}")

    levels_list = data.get("levels", []) if isinstance(data, dict) else data
    if not isinstance(levels_list, list):
        raise ValueError(f"levels가 배열이 아닙니다: {type(levels_list)}")

    result = {}
    for entry in levels_list:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        level = entry.get("level")
        if idx is not None and level is not None:
            result[int(idx)] = int(level)

    log.info(f"level 파싱: {len(result)}개 문단")
    return result


def merge_levels_into_structure(structure: dict, level_map: dict) -> dict:
    """
    parse_structure_from_llm 결과에 level_map을 병합합니다.

    Args:
        structure: paragraphs/tables를 포함하는 dict (level 없음)
        level_map: {idx: level}

    Returns:
        paragraphs에 level이 추가된 dict
    """
    paragraphs = structure.get("paragraphs", [])
    for p in paragraphs:
        idx = p.get("idx", -1)
        if idx in level_map:
            p["level"] = level_map[idx]
        else:
            # level 없으면 기본값 (보수적으로 가장 깊은 레벨)
            p.setdefault("level", 0)
    return structure


def build_content_mapping_prompt(
    structure_json: dict,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
    truncated_xml: str = "",
) -> list[dict]:
    """
    2차 호출: 구조 분석 결과 + 소스 내용 → role 기반 콘텐츠 JSON 프롬프트

    Args:
        structure_json: 1차에서 파싱한 구조 분석 결과 (role 포함)
        content_text: 작성할 내용 텍스트 (직접 입력)
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트
        truncated_xml: 축소된 양식 XML (role 시퀀스 + 샘플 텍스트 추출용)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # XML에서 각 idx의 샘플 텍스트 추출
    idx_texts = {}
    if truncated_xml:
        try:
            idx_texts = _extract_texts_by_idx(truncated_xml)
        except Exception as e:
            log.warning(f"XML 텍스트 추출 실패: {e}")

    # 구조에서 role 카탈로그 추출 (중복 제거, 샘플 포함)
    role_catalog = {}
    for p in structure_json.get("paragraphs", []):
        role = p.get("role", "")
        if role and role not in role_catalog:
            sample = idx_texts.get(p.get("idx", -1), "")
            role_catalog[role] = {
                "description": p.get("description", ""),
                "marker": p.get("marker", ""),
                "level": p.get("level", 0),
                "sample": sample,
            }

    # role 시퀀스 생성 (양식 구조 패턴)
    skip_roles = {"spacer", "toc", "fixed", "spacer_text"}
    sequence_lines = []
    for p in structure_json.get("paragraphs", []):
        role = p.get("role", "")
        if not role or role in skip_roles:
            continue
        level = p.get("level", 0)
        indent = "  " * level
        sample = idx_texts.get(p.get("idx", -1), "")
        if sample:
            sample = sample[:60] + ("…" if len(sample) > 60 else "")
            sequence_lines.append(f'{indent}[{role}] "{sample}"')
        else:
            sequence_lines.append(f'{indent}[{role}]')
    sequence_text = "\n".join(sequence_lines)

    # 카탈로그 텍스트
    catalog_lines = []
    for role_name, info in role_catalog.items():
        if role_name in skip_roles:
            continue
        marker = f', marker: "{info["marker"]}"' if info["marker"] else ""
        sample = f'\n  샘플: "{info["sample"]}"' if info["sample"] else ""
        catalog_lines.append(
            f"- **{role_name}** (level {info['level']}{marker}): {info['description']}{sample}"
        )
    catalog_text = "\n".join(catalog_lines)

    user_parts = []

    text_block = (
        "## 양식 역할(role) 카탈로그\n"
        f"{catalog_text}\n\n"
        "## 양식 구조 패턴 (role 시퀀스)\n"
        "아래는 양식의 원본 구조입니다. **이 계층 관계를 소스 내용에 적용하세요.**\n\n"
        f"```\n{sequence_text}\n```\n\n"
        "## 소스 자료\n"
    )

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += (
            "아래는 PDF에서 추출한 텍스트입니다. "
            "이 내용을 위 role에 맞게 태깅하세요.\n\n"
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
            "아래 첨부된 PDF 이미지의 내용을 위 role에 맞게 태깅하세요.\n\n"
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
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"구조 분석 JSON 파싱 실패: {e}")

    if not isinstance(data, dict) or "paragraphs" not in data:
        raise ValueError("구조 분석 결과에 'paragraphs' 키가 없습니다")

    log.info(
        f"구조 분석 완료: 문단 {len(data.get('paragraphs', []))}개, "
        f"표 {len(data.get('tables', []))}개"
    )

    # 후처리: 같은 role인데 마커가 다르면 자동 분리 — 임시 비활성화
    # 1차 AI가 role 분류를 이미 잘 하고 있고, 단일 숫자 마커 등에서 과분리 이슈가 있어
    # 일단 끄고 결과 확인. 필요 시 다시 켜기.
    # data["paragraphs"] = _split_roles_by_marker(data.get("paragraphs", []))

    # chapter_types는 여기서 생성하지 않음 — level이 아직 없음
    # 흐름:
    #   1차 (parse_structure_from_llm) → role + marker + description
    #   1.5차 (parse_level_from_llm + merge_levels_into_structure) → level 추가
    #   build_chapter_types_from_structure() → chapter_types 생성

    return data


TEMPLATE_CACHE_DIR = "/tmp/hwpx_cache"


def get_template_cache_path(template_file_id: str) -> str:
    """템플릿 분석 결과 캐시 파일 경로"""
    import os
    safe_id = template_file_id.replace("/", "_").replace("..", "_")
    return os.path.join(TEMPLATE_CACHE_DIR, f"{safe_id}.json")


def save_template_cache(template_file_id: str, data: dict) -> bool:
    """양식 분석 결과를 캐시에 저장.

    Args:
        template_file_id: 양식 파일의 DB id
        data: 저장할 구조 (structure, signals, chapter_types, truncated_xml 등)
    """
    import os
    path = get_template_cache_path(template_file_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        log.info(f"[CACHE] 템플릿 캐시 저장: {path} ({os.path.getsize(path):,}B)")
        return True
    except Exception as e:
        log.warning(f"[CACHE] 저장 실패: {e}")
        return False


def load_template_cache(template_file_id: str) -> dict | None:
    """캐시에서 양식 분석 결과 로드.

    Returns:
        데이터 dict 또는 None(캐시 없음/로드 실패)
    """
    import os
    path = get_template_cache_path(template_file_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"[CACHE] 템플릿 캐시 로드: {path} ({os.path.getsize(path):,}B)")
        return data
    except Exception as e:
        log.warning(f"[CACHE] 로드 실패 ({path}): {e}")
        return None


def clear_template_cache(template_file_id: str) -> bool:
    """캐시 파일 삭제 (강제 재분석 용도)"""
    import os
    path = get_template_cache_path(template_file_id)
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info(f"[CACHE] 삭제: {path}")
            return True
    except Exception as e:
        log.warning(f"[CACHE] 삭제 실패: {e}")
    return False


def compute_role_context_signals(paragraphs: list[dict], idx_texts: dict = None) -> dict:
    """
    1차 AI 결과(paragraphs)로부터 level/parent/exclusive 판단용 시그널을 추출.

    Args:
        paragraphs: [{"idx", "role", "marker", "description", ...}, ...]
        idx_texts: {idx: text} — _extract_texts_by_idx() 결과 (선택)

    Returns:
        {
            "role_to_letter": {role: letter, ...},
            "compressed_sequence": "abcdddec...",
            "role_stats": {role: {count, positions, markers, marker_types}},
            "adjacency": {"prev": {...}, "next": {...}},
            "role_scope_children": {role: [[children in each scope], ...]},
            "paragraph_texts": [{idx, role, marker, text}, ...]
        }
    """
    from collections import Counter, defaultdict
    import string

    # 본문 필터: 이 함수는 1.5차 AI 이전에 호출되므로 level이 없음.
    # role 이름 매칭 대신 "실제 텍스트가 없는 문단"만 제외.
    # cover/toc 같은 도입부 문단은 signals에 포함해도 AI가 level 0으로 판단 가능.
    def _is_empty(para: dict) -> bool:
        text = ""
        if idx_texts:
            text = (idx_texts.get(para.get("idx", -1)) or "").strip()
        # 텍스트도 없고 마커도 없고 description도 없는 경우 = spacer로 간주
        return (
            not text
            and not para.get("marker", "").strip()
            and not para.get("description", "").strip()
        )

    body = [p for p in paragraphs if not _is_empty(p)]
    role_sequence = [p.get("role", "") for p in body]

    role_to_letter = {}
    letters = iter(string.ascii_lowercase)
    for r in role_sequence:
        if r not in role_to_letter:
            try:
                role_to_letter[r] = next(letters)
            except StopIteration:
                role_to_letter[r] = "?"
    compressed = "".join(role_to_letter.get(r, "?") for r in role_sequence)

    role_stats = {}
    for i, p in enumerate(body):
        role = p.get("role", "")
        marker = p.get("marker", "")
        if role not in role_stats:
            role_stats[role] = {
                "count": 0,
                "positions": [],
                "markers": [],
                "marker_types": set(),
            }
        role_stats[role]["count"] += 1
        role_stats[role]["positions"].append(i)
        if marker and marker not in role_stats[role]["markers"]:
            role_stats[role]["markers"].append(marker)
        if marker:
            role_stats[role]["marker_types"].add(_normalize_marker_type(marker))

    for s in role_stats.values():
        s["marker_types"] = sorted(list(s["marker_types"]))

    prev_counts = defaultdict(Counter)
    next_counts = defaultdict(Counter)
    for i, p in enumerate(body):
        role = p.get("role", "")
        if i > 0:
            prev_counts[role][body[i - 1].get("role", "")] += 1
        if i < len(body) - 1:
            next_counts[role][body[i + 1].get("role", "")] += 1

    adjacency = {
        "prev": {r: dict(c.most_common(5)) for r, c in prev_counts.items()},
        "next": {r: dict(c.most_common(5)) for r, c in next_counts.items()},
    }

    # 각 role을 잠정 부모로 가정했을 때, 그 role 인스턴스 사이 구간에 나타나는 자식 role들
    role_scope_children = {}
    for parent_role, stats in role_stats.items():
        positions = stats["positions"]
        if len(positions) < 1:
            continue
        scopes_children = []
        for i, pos in enumerate(positions):
            start = pos + 1
            end = positions[i + 1] if i + 1 < len(positions) else len(body)
            children = []
            for j in range(start, end):
                r = body[j].get("role", "")
                if r != parent_role:
                    children.append(r)
            scopes_children.append(children)
        role_scope_children[parent_role] = scopes_children

    paragraph_texts = []
    for p in paragraphs:
        idx = p.get("idx", -1)
        text = ""
        if idx_texts and idx in idx_texts:
            text = idx_texts[idx]
        paragraph_texts.append(
            {
                "idx": idx,
                "role": p.get("role", ""),
                "marker": p.get("marker", ""),
                "text": (text or "")[:150],
            }
        )

    return {
        "role_to_letter": role_to_letter,
        "compressed_sequence": compressed,
        "role_stats": role_stats,
        "adjacency": adjacency,
        "role_scope_children": role_scope_children,
        "paragraph_texts": paragraph_texts,
    }


def build_chapter_types_from_structure(structure: dict) -> dict:
    """
    level이 포함된 structure로부터 chapter_types를 생성하여 structure에 추가합니다.

    merge_levels_into_structure() 이후에 호출하세요.

    Args:
        structure: paragraphs (with level)를 포함하는 dict

    Returns:
        chapter_types가 추가된 structure
    """
    structure["chapter_types"] = _build_chapter_types(
        structure.get("paragraphs", [])
    )
    return structure


def _build_chapter_types(paragraphs: list[dict]) -> dict:
    """
    paragraphs의 level/role 순서를 분석하여 chapter_types를 코드로 생성.

    1. level 1 문단으로 챕터 경계를 나눔
    2. 각 챕터 안에서 level 순서를 보고 부모-자식 트리를 만듦
    3. 같은 부모 아래 배타적 자식(서로 다른 마커 경로)이 있으면 별도 타입으로 분리
    4. 동일한 트리 구조를 가진 챕터는 같은 타입으로 묶음

    Returns:
        {"type_name": {"title_role": ..., "description": ..., "pattern": {...}}, ...}
    """
    def _should_skip(role: str) -> bool:
        """호환용 wrapper — 실제 필터는 level == 0 기반"""
        return False

    # 1단계: level 1 문단으로 챕터 경계 나누기 (level 0은 표지/목차/spacer)
    chapters = []  # [(title_para, [body_paras])]
    current_title = None
    current_body = []

    for p in paragraphs:
        role = p.get("role", "")
        level = p.get("level", 0)
        if level == 0:
            continue
        if level == 1:
            if current_title is not None:
                chapters.append((current_title, current_body))
            current_title = p
            current_body = []
        elif current_title is not None:
            current_body.append(p)

    if current_title is not None:
        chapters.append((current_title, current_body))

    if not chapters:
        log.warning("chapter_types 생성 실패: level 1 문단이 없습니다")
        return {}

    # 2단계: 내부 도우미 함수들

    def _build_role_info(body_paras: list[dict]) -> dict:
        """body 문단에서 role별 정보 추출.

        기본: level, count, parent
        추가: observed_counts (부모 인스턴스별 자식 개수 리스트),
              per_parent ('single'|'multiple'),
              optional (부모 인스턴스 중 자식 0개인 경우 있으면 True),
              suggested_count (non-zero count의 최빈값, 힌트용)
        """
        from collections import Counter as _Counter

        role_info = {}
        # 스택에 (level, role, instance_id) 저장하여 인스턴스 구분
        stack = []
        instance_counter = 0
        parent_inst_children = {}  # (parent_role, parent_inst_id) -> {child_role: count}
        role_instance_ids = {}     # role -> [instance_ids]

        for p in body_paras:
            role = p.get("role", "")
            level = p.get("level", 0)
            if not role or _should_skip(role):
                continue

            if role not in role_info:
                role_info[role] = {"level": level, "count": 0, "parent": None}
            role_info[role]["count"] += 1

            while stack and stack[-1][0] >= level:
                stack.pop()

            if stack:
                parent_role = stack[-1][1]
                parent_inst_id = stack[-1][2]
                if role_info[role]["parent"] is None:
                    role_info[role]["parent"] = parent_role
                # 자식 count 증가
                key = (parent_role, parent_inst_id)
                if key not in parent_inst_children:
                    parent_inst_children[key] = {}
                parent_inst_children[key][role] = parent_inst_children[key].get(role, 0) + 1

            inst_id = instance_counter
            instance_counter += 1
            role_instance_ids.setdefault(role, []).append(inst_id)
            stack.append((level, role, inst_id))

        # per-parent-instance 통계
        for role, info in role_info.items():
            parent = info.get("parent")
            if not parent:
                # body 안에 parent가 없는 top-level role (= chapter_title의 직속 자식 등)
                # parent 인스턴스별 count는 못 세지만, 전체 count로 single/multiple 추정
                total = info.get("count", 0)
                info["observed_counts"] = []
                info["per_parent"] = "multiple" if total >= 2 else "single"
                info["optional"] = False
                info["suggested_count"] = total
                continue

            parent_inst_ids = role_instance_ids.get(parent, [])
            counts = []
            for pid in parent_inst_ids:
                c = parent_inst_children.get((parent, pid), {}).get(role, 0)
                counts.append(c)

            info["observed_counts"] = counts
            has_zero = any(c == 0 for c in counts)
            has_multiple = any(c >= 2 for c in counts)
            info["per_parent"] = "multiple" if has_multiple else "single"
            info["optional"] = has_zero
            non_zero = [c for c in counts if c > 0]
            info["suggested_count"] = (
                _Counter(non_zero).most_common(1)[0][0] if non_zero else 0
            )

        return role_info

    def _build_pattern(role_info: dict, children_filter: dict = None) -> dict:
        """role_info로부터 패턴 트리 생성.

        children_filter: {parent_role: set(allowed_children)} — 해당 부모의 자식만 포함
        """
        top_roles = [r for r, info in role_info.items() if info["parent"] is None]

        def _subtree(parent_role: str) -> dict:
            info = role_info[parent_role]
            children_roles = [
                r for r, ri in role_info.items()
                if ri["parent"] == parent_role
                and (children_filter is None
                     or parent_role not in children_filter
                     or r in children_filter[parent_role])
            ]
            node = {
                "repeat": info["count"] >= 2,  # 기존 호환
                "per_parent": info.get("per_parent", "single"),
                "optional": info.get("optional", False),
                "observed_counts": info.get("observed_counts", []),
                "suggested_count": info.get("suggested_count", 1),
            }
            if children_roles:
                node["children"] = {cr: _subtree(cr) for cr in children_roles}
            return node

        return {tr: _subtree(tr) for tr in top_roles}

    def _detect_exclusive_children(
        body_paras: list[dict], role_info: dict
    ) -> dict:
        """
        부모 role의 인스턴스별로 직접 자식을 추적하여 배타적 자식 관계를 감지.
        같은 부모의 서로 다른 인스턴스가 겹치지 않는 자식 집합을 가지면 배타적.

        Returns:
            {parent_role: [frozenset(variant1_children), ...]}
            비어있으면 배타적 관계 없음
        """
        parent_children = {}
        for role, info in role_info.items():
            parent = info["parent"]
            if parent:
                parent_children.setdefault(parent, set()).add(role)

        multi_child_parents = {
            p: c for p, c in parent_children.items() if len(c) >= 2
        }
        if not multi_child_parents:
            return {}

        results = {}
        for parent_role, all_children in multi_child_parents.items():
            parent_level = role_info[parent_role]["level"]

            # 각 부모 인스턴스에서 나타나는 직접 자식 추적
            instances = []
            current_children = set()
            in_scope = False

            for p in body_paras:
                role = p.get("role", "")
                level = p.get("level", 0)
                if not role or _should_skip(role):
                    continue

                if role == parent_role:
                    if in_scope and current_children:
                        instances.append(frozenset(current_children))
                    current_children = set()
                    in_scope = True
                elif in_scope:
                    if level <= parent_level:
                        if current_children:
                            instances.append(frozenset(current_children))
                        current_children = set()
                        in_scope = False
                    elif role in all_children:
                        current_children.add(role)

            if in_scope and current_children:
                instances.append(frozenset(current_children))

            # 고유 변형 추출 (등장 순서 유지)
            unique_variants = []
            for inst in instances:
                if inst not in unique_variants:
                    unique_variants.append(inst)

            if len(unique_variants) < 2:
                continue

            # 공통 요소(core) 추출 — 모든 variant에 나타나는 자식
            core = set(unique_variants[0])
            for v in unique_variants[1:]:
                core &= set(v)

            # 각 variant의 특유 부분 (공통 요소 제외)
            non_core_variants = [
                frozenset(set(v) - core) for v in unique_variants
            ]

            # ⚠️ 빈 variant가 하나라도 있으면 배타적 분리 안 함
            # (다른 variant의 상위집합에 포함되므로 합쳐서 optional로 처리 가능)
            # 예: {note, circled_detail_item} vs {circled_detail_item}
            #     특유: {note} vs {} → 하나의 variant에 모든 children 포함 가능
            if any(len(v) == 0 for v in non_core_variants):
                continue

            # 모든 variant가 각자의 특유 부분을 가지고 서로 disjoint일 때만 분리
            # 예: {detail_item, note} vs {circled_detail_item, note}
            #     특유: {detail_item} vs {circled_detail_item} → disjoint → 진짜 배타적
            is_disjoint = all(
                v1.isdisjoint(v2)
                for v1, v2 in combinations(non_core_variants, 2)
            )
            if is_disjoint:
                results[parent_role] = unique_variants

        return results

    def _get_variant_marker_desc(
        body_paras: list[dict], parent_role: str, variant_children: frozenset
    ) -> str:
        """변형의 마커 경로 설명 생성 (예: '□→ㅇ 블록')"""
        parent_marker = ""
        child_markers = []

        for p in body_paras:
            role = p.get("role", "")
            marker = p.get("marker", "")
            if not marker:
                continue
            if role == parent_role and not parent_marker:
                parent_marker = marker.strip()
            elif role in variant_children and marker.strip() not in child_markers:
                child_markers.append(marker.strip())

        parts = []
        if parent_marker:
            parts.append(parent_marker)
        parts.extend(child_markers[:2])
        return "→".join(parts) + " 블록" if parts else ""

    # 3단계: 각 챕터의 트리를 비교해서 같은 구조면 같은 타입으로 묶기
    #        배타적 자식이 있으면 변형별로 타입 분리 (type_Na, type_Nb)

    def _pattern_signature(pattern: dict) -> str:
        """패턴의 구조적 시그니처 (role 이름 + 계층)"""
        parts = []
        for role, info in sorted(pattern.items()):
            children_sig = ""
            if "children" in info:
                children_sig = _pattern_signature(info["children"])
            parts.append(f"{role}({children_sig})")
        return "|".join(parts)

    def _pattern_depth(pattern: dict) -> int:
        """패턴 트리의 최대 깊이"""
        if not pattern:
            return 0
        max_d = 0
        for role, info in pattern.items():
            children = info.get("children", {})
            if children:
                d = 1 + _pattern_depth(children)
            else:
                d = 1
            if d > max_d:
                max_d = d
        return max_d

    def _pattern_total_roles(pattern: dict) -> int:
        """패턴 트리의 전체 role 개수 (중첩 포함)"""
        count = 0
        for role, info in pattern.items():
            count += 1
            children = info.get("children", {})
            if children:
                count += _pattern_total_roles(children)
        return count

    def _pattern_summary(pattern: dict) -> str:
        """
        패턴을 요약한 설명 문자열 생성.
        2a AI가 chapter_types를 구분할 수 있도록 구조적 특성을 압축.

        예: "3단 깊이, 8개 role, 최상위: section_header, detail_item"
        """
        depth = _pattern_depth(pattern)
        total = _pattern_total_roles(pattern)
        top_roles = list(pattern.keys())
        top_str = ", ".join(top_roles) if top_roles else "(없음)"
        return (
            f"{depth}단 깊이, {total}개 role, 최상위: {top_str}"
        )

    chapter_types = {}
    sig_to_type = {}  # signature → type_name
    type_counter = 0

    for title_para, body_paras in chapters:
        title_role = title_para.get("role", "chapter_title")
        title_desc = title_para.get("description", "")

        role_info = _build_role_info(body_paras)
        if not role_info:
            continue

        exclusive = _detect_exclusive_children(body_paras, role_info)

        if exclusive:
            # 배타적 자식 → 변형별로 타입 분리
            exclusive_items = list(exclusive.items())
            variant_combos = list(product(
                *[variants for _, variants in exclusive_items]
            ))
            variant_combos = variant_combos[:8]  # 변형 수 제한

            type_counter += 1
            base_num = type_counter

            log.info(
                f"배타적 자식 감지 → {len(variant_combos)}개 변형 분리: "
                + ", ".join(
                    f"{pr}={[set(v) for v in vs]}"
                    for pr, vs in exclusive_items
                )
            )

            for i, combo in enumerate(variant_combos):
                children_filter = {}
                marker_descs = []
                for (parent_role, _), variant in zip(exclusive_items, combo):
                    children_filter[parent_role] = variant
                    md = _get_variant_marker_desc(
                        body_paras, parent_role, variant
                    )
                    if md:
                        marker_descs.append(md)

                variant_pattern = _build_pattern(role_info, children_filter)
                sig = _pattern_signature(variant_pattern)

                if sig in sig_to_type:
                    continue

                suffix = chr(ord('a') + i)
                type_name = f"type_{base_num}{suffix}"
                marker_info = " / ".join(marker_descs)
                pattern_summary = _pattern_summary(variant_pattern)
                variant_desc = (
                    f"{pattern_summary} · {marker_info}"
                    if marker_info else pattern_summary
                )
                sig_to_type[sig] = type_name
                chapter_types[type_name] = {
                    "title_role": title_role,
                    "description": variant_desc,
                    "pattern": variant_pattern,
                }
        else:
            # 일반 케이스: 배타적 자식 없음
            pattern = _build_pattern(role_info)
            sig = _pattern_signature(pattern)

            if sig not in sig_to_type:
                type_counter += 1
                type_name = f"type_{type_counter}"
                sig_to_type[sig] = type_name
                chapter_types[type_name] = {
                    "title_role": title_role,
                    "description": _pattern_summary(pattern),
                    "pattern": pattern,
                }

    log.info(
        f"chapter_types 코드 생성: {len(chapters)}개 챕터 → "
        f"{len(chapter_types)}개 타입 ({list(chapter_types.keys())})"
    )
    for type_name, info in chapter_types.items():
        log.info(
            f"  {type_name}: title_role={info['title_role']}, "
            f"pattern={json.dumps(info['pattern'], ensure_ascii=False)}"
        )

    return chapter_types


def _normalize_marker_type(marker: str) -> str:
    """마커를 종류별로 정규화. 같은 시퀀스의 마커는 같은 타입으로 취급."""
    if not marker:
        return ""
    first = marker.strip()[0] if marker.strip() else ""
    cp = ord(first) if first else 0

    # 󰊱~󰊹 시퀀스 (PUA)
    if 0xF02B1 <= cp <= 0xF02B9:
        return "circle_num_pua"
    # ➊~➓ 시퀀스
    if 0x278A <= cp <= 0x2793:
        return "dingbat_neg_circle"
    # ①~⑳ 시퀀스
    if 0x2460 <= cp <= 0x2473:
        return "circle_num"
    # ❶~❿ 시퀀스
    if 0x2776 <= cp <= 0x277F:
        return "dingbat_neg_circle2"
    # Ⅰ~Ⅻ 로마숫자
    if 0x2160 <= cp <= 0x216B:
        return "roman"
    # 1), 2), 3) 등
    if re.match(r'^\d+\)', marker.strip()):
        return "num_paren"
    # 가., 나., 다. 등
    if re.match(r'^[가-힣]\.', marker.strip()):
        return "hangul_dot"
    # 단일 문자 마커 (□, ㅇ, *, ※, ◈, ◇, ◆, ⇒, →, ▪, -)
    return f"char_{first}"


def _split_roles_by_marker(paragraphs: list[dict]) -> list[dict]:
    """
    같은 role인데 마커 종류가 다른 문단들을 자동으로 다른 role로 분리.

    예: detail_item 중 marker="ㅇ"인 것과 marker="➊"인 것이 섞여 있으면
        detail_item (ㅇ) / detail_item_sub1 (➊) 로 분리.
    """
    skip_roles = {"spacer", "toc", "fixed", "spacer_text"}

    # 1단계: role별 마커 종류 수집
    role_markers = {}  # role → {marker_type → [markers]}
    for p in paragraphs:
        role = p.get("role", "")
        marker = p.get("marker", "")
        if not role or role in skip_roles:
            continue
        mt = _normalize_marker_type(marker)
        if role not in role_markers:
            role_markers[role] = {}
        if mt not in role_markers[role]:
            role_markers[role][mt] = set()
        if marker:
            role_markers[role][mt].add(marker)

    # 2단계: 마커 종류가 2개 이상인 role 찾기
    roles_to_split = {}
    for role, mt_dict in role_markers.items():
        # 빈 마커("")와 실제 마커가 섞인 건 무시 (빈 마커는 분리 대상 아님)
        actual_types = {mt for mt in mt_dict if mt}
        if len(actual_types) >= 2:
            roles_to_split[role] = mt_dict

    if not roles_to_split:
        return paragraphs

    log.info(f"마커 기반 role 분리 대상: {list(roles_to_split.keys())}")

    # 3단계: 분리 실행
    # 마커 타입별로 suffix를 부여: 첫 번째 타입은 원래 이름 유지, 나머지는 _sub1, _sub2...
    role_type_order = {}
    for role in roles_to_split:
        # 등장 순서대로 정렬
        seen = []
        for p in paragraphs:
            if p.get("role") == role:
                mt = _normalize_marker_type(p.get("marker", ""))
                if mt and mt not in seen:
                    seen.append(mt)
        role_type_order[role] = seen

    result = []
    for p in paragraphs:
        role = p.get("role", "")
        if role not in roles_to_split:
            result.append(p)
            continue

        marker = p.get("marker", "")
        mt = _normalize_marker_type(marker)
        if not mt:
            result.append(p)
            continue

        order = role_type_order[role]
        idx = order.index(mt) if mt in order else 0
        if idx == 0:
            # 첫 번째 마커 타입 → 원래 role 이름 유지
            result.append(p)
        else:
            # 이후 마커 타입 → role 이름에 suffix 추가
            new_p = dict(p)
            new_p["role"] = f"{role}_sub{idx}"
            result.append(new_p)
            log.debug(
                f"role 분리: idx={p.get('idx')} {role}(marker={marker}) → {new_p['role']}"
            )

    # 분리 결과 로그
    split_count = sum(1 for p in result if "_sub" in p.get("role", ""))
    if split_count:
        new_roles = set(p.get("role", "") for p in result if "_sub" in p.get("role", ""))
        log.info(f"마커 기반 role 분리 완료: {split_count}개 문단 → 새 role: {new_roles}")

    return result


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
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError as e1:
        log.warning(f"JSON 1차 파싱 실패 ({e1}), 복구 시도...")
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
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


def parse_role_content_from_structure_llm(llm_response: str) -> dict:
    """
    2차 LLM 응답에서 role 기반 콘텐츠 JSON을 파싱합니다.
    (하이브리드 방식: v1 구조 분석 + role 기반 콘텐츠 출력)

    Args:
        llm_response: LLM이 출력한 텍스트

    Returns:
        {"header": {"role_name": "text", ...}, "body": [{"role": ..., "text": ...}, ...]}
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("콘텐츠 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"콘텐츠 JSON 파싱 실패: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"콘텐츠 결과가 dict가 아닙니다: {type(data)}")

    if "header" not in data:
        data["header"] = {}
    if "body" not in data:
        data["body"] = []

    log.info(
        f"role 콘텐츠 파싱: header={list(data['header'].keys())}, "
        f"body={len(data['body'])}개 항목"
    )
    return data


# ──────────────────────────────────────────────────────────────────────
# 2a AI: 소스 PDF → 대제목 추출 + 타입 분류
# ──────────────────────────────────────────────────────────────────────

CHAPTER_CLASSIFY_PROMPT = """당신은 문서 구조 분석 전문가입니다.
**소스 문서의 주제별 경계를 먼저 식별**한 뒤, 각 주제에 **가장 적합한 양식 type을 배정**합니다.

## 핵심 관점

- 양식은 여러 chapter_type을 제공합니다 (구조 템플릿의 "카탈로그").
- 당신의 임무는 **소스의 주제 구성을 존중하면서 각 주제에 맞는 type을 고르는 것**.
- 양식 type 개수를 억지로 맞추거나 소스 내용을 쪼갤 필요 없음.

## 작업 순서

### Step 1: 소스 구조 먼저 파악
소스의 목차·대제목·주제 경계를 우선 식별. 비정형이면 내용상 주제 전환점 찾기.
→ **소스의 자연스러운 chapter 단위 개수를 결정** (N개)

### Step 2: 각 chapter의 "복잡도" 평가
각 chapter를 다음 기준으로 분석:
- **분량**: 문단 수, 대략 페이지 수
- **내부 계층 깊이**: 중제목/소제목/세부항목 단계 (1단 / 2-3단 / 4단+)
- **항목 개수**: 소제목 몇 개, 세부 bullet 몇 개

### Step 3: 각 chapter에 적합한 type 배정
양식 type들의 구조 특성과 매칭:
- **깊은 type** (단 깊이 4+, role 6개+) → 분량 많고 다단 계층의 chapter
- **중간 type** (2-3단, 4-6 role) → 중간 분량·복잡도
- **단순 type** (1-2단, 2-3 role) → 짧은 요약성·단순 열거 chapter

**같은 type을 여러 chapter에 반복 사용 OK** (소스에 비슷한 성격 주제가 여럿이면).
**사용 안 하는 type이 있어도 OK** (소스에 그런 복잡도 내용이 없으면).

## 출력 규칙

### chapters 배열
- **개수 = 소스의 자연스러운 chapter 개수 (N)**, type 개수와 무관
- 소스 원문의 **대제목/주제명을 title에 그대로 사용** (마커 포함)
- 소스에 명확한 제목 없으면 그 chapter의 핵심을 한 줄로 요약

### confidence
- `high`: 소스 주제와 선택한 type이 복잡도·성격 모두 잘 맞음
- `medium`: 약간 어긋나지만 이 type이 가장 낫다고 판단
- `low`: 적합한 type이 없어서 불가피한 선택 (새 type 만들지 말 것)

### header
- `header`의 key는 user 메시지의 "양식 header role 목록"만 사용
- 소스에서 표지 정보(제목/날짜/기관명 등) 추출하여 채움
- 소스에 없으면 해당 key 생략 가능

## 출력 형식

```json
{
  "chapters": [
    {"type": "type_X", "title": "소스 원문 제목(마커 포함)", "confidence": "high"},
    ...
  ],
  "header": {
    "<양식 header role 이름>": "소스에서 추출한 값",
    ...
  }
}
```

## 예시 상황별 동작

**상황 A**: 양식 type 3개(단순/중간/복잡), 소스 chapter 3개
→ 각 chapter를 복잡도 맞는 type에 1:1 (단, 순서대로 아님! 복잡도대로)

**상황 B**: 양식 type 3개, 소스 chapter 5개 (3개는 유사한 중간 복잡도, 2개는 단순)
→ chapters 5개: [중간, 중간, 중간, 단순, 단순] 같이 type 반복 사용

**상황 C**: 양식 type 3개, 소스 chapter 1개 (단순한 내용)
→ chapters 1개: 단순 type 하나만 사용. 나머지 2개 type은 사용 안 함.

**상황 D**: 소스가 비정형(회의록, 메모 등)
→ 주제 전환점을 찾아 N개로 나눈 뒤 각각 type 배정

## 금지사항

- ❌ 양식에 없는 새 type 이름 만들기
- ❌ 소스에 없는 내용 창작하기
- ❌ "type 개수에 맞춰" 억지로 chapter 쪼개기/합치기
- ❌ 복잡도 무시하고 순서대로 type_1, type_2, type_3 배정

## 중요
- 반드시 JSON만 출력. 다른 설명 포함 금지
- chapters의 type은 user 메시지의 "양식 대제목 타입 목록"에 있는 이름만 사용
"""


def build_chapter_classify_prompt(
    chapter_types: dict,
    header_roles: list[str],
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
) -> list[dict]:
    """
    2a 호출: 소스 PDF → 대제목 추출 + 양식 타입 분류

    Args:
        chapter_types: 1차 AI가 출력한 chapter_types dict
        header_roles: 양식의 header role 이름 목록 (cover_title 등)
        content_text: 직접 입력 텍스트
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # 양식 타입 카탈로그 구성
    type_lines = []
    valid_type_names = list(chapter_types.keys())
    for type_name, info in chapter_types.items():
        desc = info.get("description", "")
        title_role = info.get("title_role", "")
        pattern = info.get("pattern", {})
        top_roles = list(pattern.keys())
        roles_str = ", ".join(top_roles) if top_roles else "(단순 구조)"
        type_lines.append(
            f"- **{type_name}**: {desc} (대제목 role: {title_role}, 하위: {roles_str})"
        )
    types_text = "\n".join(type_lines)
    type_count = len(valid_type_names)
    type_names_str = ", ".join(valid_type_names) if valid_type_names else "(없음)"

    # header role 목록
    if header_roles:
        header_text = ", ".join(header_roles)
        header_rule = (
            f"**header에는 다음 key만 사용 가능** (필수): {header_text}\n"
            f"- 위 목록의 각 key에 대해 소스에서 적절한 값을 찾아 채우세요\n"
            f"- 위 목록에 없는 key를 만들지 마세요\n"
        )
    else:
        header_text = "(없음)"
        header_rule = "**양식에 header role이 없습니다. header는 빈 객체 `{}`로 출력하세요.**\n"

    user_parts = []
    text_block = (
        "## 양식 대제목 타입 목록 (카탈로그)\n"
        f"{types_text}\n\n"
        f"양식이 제공하는 type: **{type_count}개** ({type_names_str})\n"
        f"이 중 소스 chapter 개수만큼 적절히 선택(중복 사용/일부 생략 모두 가능).\n\n"
        f"## 양식 header role 목록\n"
        f"{header_text}\n\n"
        f"{header_rule}\n"
        "## 소스 자료\n"
    )

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += f"```\n{pdf_text}\n```\n\n"
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
        text_block += "아래 첨부된 PDF 이미지에서 대제목을 추출하고 분류하세요.\n\n"
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
        text_block += f"{content_text}\n\n반드시 JSON만 출력하세요.\n"
        user_parts = text_block

    return [
        {"role": "system", "content": CHAPTER_CLASSIFY_PROMPT},
        {"role": "user", "content": user_parts},
    ]


def parse_chapter_classify_from_llm(llm_response: str) -> dict:
    """
    2a LLM 응답에서 대제목 분류 JSON을 파싱합니다.

    Returns:
        {"chapters": [...], "header": {...}}
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("2a 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"2a JSON 파싱 실패: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"2a 결과가 dict가 아닙니다: {type(data)}")

    if "chapters" not in data:
        data["chapters"] = []
    if "header" not in data:
        data["header"] = {}

    log.info(
        f"2a 파싱: {len(data['chapters'])}개 대제목, "
        f"header={list(data['header'].keys())}"
    )
    return data


# ──────────────────────────────────────────────────────────────────────
# 2b AI: 패턴 + 소스 → 섹션별 텍스트 채우기
# ──────────────────────────────────────────────────────────────────────

SECTION_FILL_PROMPT = """당신은 한국 행정문서 작성 전문가입니다.
하나의 대제목 섹션에 대해, 주어진 **role 패턴**에 따라 소스 내용을 배치합니다.

## 핵심 규칙 (강제)

1. **패턴에 명시된 role만 사용하세요** — 새 role 생성 금지
2. **개수 제약**:
   - `정확히 1개/부모`: 부모 인스턴스 아래 딱 1개만 생성. 2개 이상 절대 금지.
   - `여러 개 가능`: 내용에 맞게 1개~여러 개 생성 가능.
   - `권장 개수 약 N`: 양식에서 관찰된 최적 개수. 소스 내용이 충분하면 이 근처로 맞추는 것이 자연스러움. 강제는 아님.
3. **필수/선택**:
   - `필수(최소 1개)`: 반드시 1개 이상 포함
   - `선택(생략 가능)`: 해당 내용이 소스에 없으면 생략
4. **children 관계를 지키세요** — 부모 role 뒤에 자식 role이 와야 합니다

## ⚠️ 소스와 양식의 주제가 완전히 다를 수 있음

양식은 **어떤 주제**(예: 과일 가격)를 다뤘더라도, 당신이 채울 소스는 **전혀 다른 주제**(예: 야구장 관객 수)일 수 있습니다.

- **role의 description은 구조적·관계적 역할만 기술**합니다. 주제는 무관.
- **role의 sample text는 스타일(문장 길이/포맷) 참고용**입니다. **주제는 완전히 무시**하세요.
- sample이 "딸기 가격이 15% 상승"이라도 당신 소스가 야구라면 "관중 수가 15% 증가"처럼 **해당 소스 주제로 작성**
- sample의 **길이/문체/마커/숫자 포함 여부** 같은 형식만 따르세요

## role의 성격: 제목 vs 본문

**children이 있는 role = 짧은 제목** (한 줄, 20~40자 내외)
**children이 없는 말단 role = 실제 본문** (한 문장~여러 문장)

예를 들어 패턴이 task_title → task_detail → sub_detail 이면:
- task_title: "과제 제목" (짧은 제목)
- task_detail: "세부 과제 제목" (짧은 한 줄 제목)
- sub_detail: "실제 실행 내용 상세 설명" (본문)

**하나의 role에 여러 계층의 내용을 합치지 마세요.**
소스에서 상위 내용과 하위 내용이 함께 있으면, 상위는 부모 role에, 하위는 children role에 분리하세요.

## 출력 순서

패턴의 계층 구조를 flat하게 펼친 순서로 출력하세요.
예: pattern이 section_header → (sub_task → (detail_item, note)) 이면:
```
section_header
  sub_task
    detail_item
    detail_item
    note
  sub_task
    detail_item
section_header
  sub_task
    detail_item
```

## role 선택 기준 — 내용의 성격으로 판단

**role을 선택할 때 소스의 마커가 아닌 내용의 성격을 기준으로 하세요.**
각 role의 description과 예시를 보고, 소스 내용이 어떤 role의 성격에 가장 맞는지 판단하세요.

- 소스 내용이 **새로운 주제/소제목**을 시작하면 → description에 "제목", "항목 제목" 등이 있는 role
- 소스 내용이 **구체적 사실, 경과, 현황**을 설명하면 → description에 "실행", "본문", "내용" 등이 있는 role
- 소스 내용이 **보충 설명, 참고, 통계, 예시**이면 → description에 "보충", "참고", "설명" 등이 있는 role
- 소스 내용이 **결론, 방향, 요약**이면 → description에 "요약", "방향", "선언" 등이 있는 role

**소스의 원래 마커(※, □, ⇒, - 등)는 role 선택의 기준이 아닙니다.**
소스에서 ※로 시작하더라도 내용이 주제 설명이면 detail_item일 수 있고,
소스에서 ㅇ로 시작하더라도 내용이 보충 설명이면 note일 수 있습니다.

## 마커 규칙
- **양식 마커를 사용하세요** — 소스 원문의 마커(◇, ◆, ⇒, ※, □ 등)는 제거하고 해당 role의 양식 마커로 교체
- 각 role의 양식 마커가 제공됩니다. 해당 마커로 시작하세요
- 마커가 없는 role은 마커 없이 내용만 작성
- **마커 순번은 시스템이 자동 처리합니다** — 첫 번째 마커만 사용하세요 (예: 󰊱만 사용, 시스템이 󰊱→󰊲→󰊳 순서로 교체)

## 텍스트 작성 규칙
- **role의 description이나 번호("과제 1", "전략 2" 등)를 텍스트에 넣지 마세요** — description은 role 선택의 참고용이며 출력 텍스트에 포함하면 안 됩니다
- 소스의 실제 내용만 작성하세요
- 소스의 원래 마커는 제거하고 양식 마커로 교체하세요

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "items": [
    {"role": "section_header", "text": "□ 중제목 텍스트"},
    {"role": "detail_item", "text": "ㅇ 세부 내용 1"},
    {"role": "detail_item", "text": "ㅇ 세부 내용 2"},
    {"role": "note", "text": "※ 보충 설명"},
    {"role": "section_header", "text": "□ 다른 중제목"},
    {"role": "detail_item", "text": "ㅇ 세부 내용 3"}
  ]
}
```

## 중요
- **소스에 없는 내용을 만들어내지 마세요**
- **소스의 해당 섹션 내용을 빠짐없이 반영하세요**
- **하나의 role 항목에는 하나의 계층 내용만** — 여러 계층을 합치지 마세요
- 양식 샘플과 비슷한 길이/문체를 유지하세요
- 반드시 JSON만 출력. 다른 설명 포함 금지
"""


def _format_pattern_tree(pattern: dict, role_markers: dict, indent: int = 0) -> str:
    """패턴 트리를 사람이 읽기 좋은 텍스트로 변환. children 유무로 제목/본문 표시."""
    lines = []
    prefix = "  " * indent
    for role_name, info in pattern.items():
        marker = role_markers.get(role_name, "")
        marker_str = f' (마커: "{marker}")' if marker else ""
        per_parent = info.get("per_parent", "single")
        optional = info.get("optional", False)
        suggested = info.get("suggested_count", 1)
        observed = info.get("observed_counts", [])
        children = info.get("children", {})
        flags = []
        # 개수 제약 (강제)
        if per_parent == "single":
            flags.append("정확히 1개/부모")
        else:
            flags.append("여러 개 가능")
        if optional:
            flags.append("선택(생략 가능)")
        else:
            flags.append("필수(최소 1개)")
        # 개수 힌트 (권장)
        if suggested and suggested > 0:
            flags.append(f"권장 개수 약 {suggested}")
        if observed:
            observed_preview = observed[:6]
            more = "…" if len(observed) > len(observed_preview) else ""
            flags.append(f"관찰={observed_preview}{more}")
        # children 유무로 성격 표시
        if children:
            flags.append("짧은 제목만")
        else:
            flags.append("본문 내용")
        flags_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"{prefix}- {role_name}{marker_str}{flags_str}")
        if children:
            lines.append(_format_pattern_tree(children, role_markers, indent + 1))
    return "\n".join(lines)


def build_section_fill_prompt(
    chapter_title: str,
    chapter_type_name: str,
    pattern: dict,
    role_catalog: dict,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
) -> list[dict]:
    """
    2b 호출: 한 섹션의 패턴 + 소스 → role 태그된 콘텐츠

    Args:
        chapter_title: 이 섹션의 대제목 텍스트 (2a에서 결정)
        chapter_type_name: 양식 타입 이름
        pattern: 이 타입의 하위 role 패턴 (계층/반복 정보)
        role_catalog: 패턴에 포함된 role들의 정보 {role: {marker, description, ...}}
        content_text: 직접 입력 텍스트
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # role 마커 매핑
    role_markers = {}
    for role_name, info in role_catalog.items():
        role_markers[role_name] = info.get("marker", "")

    # 패턴 트리 텍스트
    pattern_text = _format_pattern_tree(pattern, role_markers)

    # role 카탈로그 텍스트
    catalog_lines = []
    for role_name, info in role_catalog.items():
        marker = info.get("marker", "")
        desc = info.get("description", "")
        sample = info.get("sample", "")
        marker_str = f', 마커: "{marker}"' if marker else ""
        sample_str = f'\n  예시: "{sample}"' if sample else ""
        catalog_lines.append(f"- **{role_name}**{marker_str}: {desc}{sample_str}")
    catalog_text = "\n".join(catalog_lines)

    user_parts = []
    text_block = (
        f"## 대제목\n"
        f"**{chapter_title}** (타입: {chapter_type_name})\n\n"
        f"## 이 섹션의 role 패턴\n"
        f"아래 패턴에 따라 내용을 배치하세요:\n{pattern_text}\n\n"
        f"## 사용 가능한 role 상세\n"
        f"{catalog_text}\n\n"
        f"## 소스 자료\n"
        f"아래 소스에서 **\"{chapter_title}\"** 섹션에 해당하는 내용을 찾아 배치하세요.\n\n"
    )

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += f"```\n{pdf_text}\n```\n\n"
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
        text_block += "아래 PDF 이미지에서 해당 섹션 내용을 찾아 배치하세요.\n\n"
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
        text_block += f"{content_text}\n\n반드시 JSON만 출력하세요.\n"
        user_parts = text_block

    return [
        {"role": "system", "content": SECTION_FILL_PROMPT},
        {"role": "user", "content": user_parts},
    ]


def parse_section_fill_from_llm(llm_response: str) -> list[dict]:
    """
    2b LLM 응답에서 섹션 콘텐츠 items를 파싱합니다.

    Returns:
        [{"role": ..., "text": ...}, ...]
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        bracket_match = re.search(r'\[[\s\S]*\]', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        elif bracket_match:
            raw = bracket_match.group(0)
        else:
            raise ValueError("2b 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"2b JSON 파싱 실패: {e}")

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", [])
    else:
        raise ValueError(f"2b 결과 형식 오류: {type(data)}")

    log.info(f"2b 파싱: {len(items)}개 항목")
    return items
