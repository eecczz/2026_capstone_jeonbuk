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
    HWPX 파일에서 첫 번째 section XML을 추출합니다 (backward compat).

    13.7b B1: section0만 처리하는 기존 호출자 (analyze_hwpx, legacy
    files.py endpoint)를 위해 유지. multi-section 처리는
    extract_all_sections_xml() 사용.

    Args:
        hwpx_source: 파일 경로(str), bytes, 또는 file-like object

    Returns:
        첫 번째 section XML의 원본 문자열
    """
    sections = extract_all_sections_xml(hwpx_source)
    if not sections:
        raise ValueError("HWPX 파일에서 section XML을 찾을 수 없습니다")
    return sections[0][1]


def extract_all_sections_xml(hwpx_source) -> list[tuple[str, str]]:
    """
    HWPX 파일에서 모든 section XML을 추출합니다 (13.7b B1).

    section name으로 sorted된 list. document-global 순서 보장 (section0,
    section1, ...). multi-section 1a baseline (B2)에서 사용.

    Args:
        hwpx_source: 파일 경로(str), bytes, 또는 file-like object

    Returns:
        list of (section_name, section_xml_text) tuples.
        빈 list (section 없으면).
    """
    if isinstance(hwpx_source, str):
        with open(hwpx_source, "rb") as f:
            data = f.read()
    elif isinstance(hwpx_source, bytes):
        data = hwpx_source
    else:
        data = hwpx_source.read()

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        section_names = sorted(
            n for n in zf.namelist()
            if "section" in n.lower() and n.endswith(".xml")
        )
        return [
            (n, zf.read(n).decode("utf-8"))
            for n in section_names
        ]


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
    from hwpx.document import HwpxDocument

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


def _smart_truncate(text: str, limit: int) -> str:
    """
    텍스트를 limit 자 이하로 축약하되, 마커와 내용 사이 공백을 보존.

    예: "① 국내 시장 현황..." → "① …"  (공백 유지)
        "본문123..." → "본문1…"            (공백 없으면 그냥 잘림)
    """
    if len(text) <= limit:
        return text
    # 첫 공백 찾기 (마커 경계)
    first_space = text.find(" ", 0, limit + 1)
    if 0 < first_space <= limit:
        # 마커 + 공백까지 보존, 그 뒤는 …로 축약
        return text[: first_space + 1] + "…"
    # 공백 없으면 단순 축약
    return text[:limit] + "…"


def _truncate_paragraph_text(para_elem, limit: int):
    """
    문단 전체 텍스트를 합쳐 본 뒤 축약 (여러 <hp:t> 경계 때문에 공백이
    잘리는 문제 방지). 첫 <hp:t>에 축약 결과 넣고 나머지는 비움.

    NOTE: 문단 직계 <hp:run> 안의 <hp:t>만 대상 (표 내부 제외).
    """
    t_elements = []
    for run in para_elem.findall(f"{NS_HP}run"):
        t = run.find(f"{NS_HP}t")
        if t is not None:
            t_elements.append(t)
    if not t_elements:
        return
    full_text = "".join(t.text or "" for t in t_elements)
    if len(full_text) <= limit:
        return
    new_text = _smart_truncate(full_text, limit)
    t_elements[0].text = new_text
    for t in t_elements[1:]:
        t.text = ""


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

    # ── Stage 5/6 (텍스트 축약) 제거됨 ──
    # gpt-5.4 컨텍스트 500K+라 축약 불필요.
    # 마커와 본문 경계가 텍스트 축약 과정에서 사라지면 1차 AI가 혼란.
    # 현재는 blank 제거 + 동일 표 묶기까지만 하고 텍스트는 원본 그대로 전달.

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


def pdf_to_text(pdf_path: str, max_chars: int = 0) -> str:
    """
    pdftotext를 사용하여 PDF에서 텍스트를 추출합니다.

    Args:
        pdf_path: PDF 파일 경로
        max_chars: 최대 반환 문자 수. 0 또는 None이면 자르지 않음.
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
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (총 {len(result.stdout):,}자 중 {max_chars:,}자만 포함)"
        log.info(f"PDF 텍스트 축소: {len(result.stdout):,}자 → {max_chars:,}자")
    else:
        log.info(f"PDF 텍스트 추출: {len(text):,}자")

    return text


def hwpx_to_text(hwpx_path: str, max_chars: int = 0) -> str:
    """
    HWPX 파일에서 paragraph 텍스트만 추출합니다 (XML/스타일 제외).

    소스로 사용할 HWPX를 본문 흐름 텍스트로 환원. 표 셀 안의 paragraph도
    별도 줄로 포함. lineBreak는 줄바꿈, tab은 탭으로 변환.

    Args:
        hwpx_path: HWPX 파일 경로
        max_chars: 최대 반환 문자 수. 0 또는 None이면 자르지 않음.
    """
    paragraphs: list[str] = []
    buf: list[str] = []

    def _flush():
        s = "".join(buf).strip()
        if s:
            paragraphs.append(s)
        buf.clear()

    def _walk(elem):
        tag = elem.tag
        if not isinstance(tag, str):
            for ch in elem:
                _walk(ch)
            return
        if tag == f"{NS_HP}p":
            _flush()
            for ch in elem:
                _walk(ch)
            _flush()
            return
        if tag == f"{NS_HP}t":
            if elem.text:
                buf.append(elem.text)
        elif tag == f"{NS_HP}lineBreak":
            buf.append("\n")
        elif tag == f"{NS_HP}tab":
            buf.append("\t")
        for ch in elem:
            _walk(ch)

    with zipfile.ZipFile(hwpx_path, "r") as zf:
        section_names = sorted(
            n for n in zf.namelist()
            if "section" in n.lower() and n.endswith(".xml")
        )
        for name in section_names:
            try:
                root = etree.fromstring(zf.read(name))
            except etree.XMLSyntaxError as e:
                log.warning(f"HWPX 텍스트 추출 — {name} 파싱 실패: {e}")
                continue
            _walk(root)
            _flush()

    full = "\n".join(paragraphs)
    if max_chars and len(full) > max_chars:
        truncated = full[:max_chars] + f"\n\n... (총 {len(full):,}자 중 {max_chars:,}자만 포함)"
        log.info(f"HWPX 텍스트 축소: {len(full):,}자 → {max_chars:,}자")
        return truncated
    log.info(f"HWPX 텍스트 추출: {len(full):,}자 (paragraph {len(paragraphs)}개)")
    return full


def split_source_by_chapters(
    pdf_text: str,
    chapter_titles: list[str],
) -> tuple[list[str], dict]:
    """
    소스 텍스트를 대제목 기준으로 섹션별로 분할합니다.

    Returns:
        (sections, decision_log) 튜플
        - sections: 각 대제목에 해당하는 텍스트 조각 리스트
        - decision_log: split 결정 상세 로그 (07b debug용)
    """
    _src_len = len(pdf_text) if pdf_text else 0
    _empty_log = {
        "chapter_count": len(chapter_titles),
        "source_length": _src_len,
        "per_chapter": [],
        "titles_found": 0, "titles_not_found": len(chapter_titles),
        "fallback_used": False,
        "source_concentration_ratio": 0,
    }
    if not chapter_titles or not pdf_text:
        return [pdf_text] * max(len(chapter_titles), 1), _empty_log

    # 각 대제목의 소스 텍스트 내 위치 찾기
    decisions = []
    for title in chapter_titles:
        d = _find_title_in_text(pdf_text, title)
        d["searched_title"] = title
        decisions.append(d)

    # 위치 기반으로 텍스트 분할
    positions = [d["position"] for d in decisions]
    sections = []
    for i, pos in enumerate(positions):
        if pos < 0:
            sections.append("")
            continue
        end_pos = len(pdf_text)
        for j in range(i + 1, len(positions)):
            if positions[j] >= 0:
                end_pos = positions[j]
                break
        sections.append(pdf_text[pos:end_pos].strip())

    # 못 찾은 섹션에 전체 텍스트 할당 (fallback)
    fallback_used = False
    for i, sec in enumerate(sections):
        if not sec:
            sections[i] = pdf_text
            fallback_used = True
            log.warning(
                f"대제목 '{chapter_titles[i]}' 위치를 찾지 못함 → 전체 텍스트 사용"
            )

    # decision log 구성
    chunk_lengths = [len(s) for s in sections]
    for i, d in enumerate(decisions):
        d["chunk_length"] = chunk_lengths[i]
        d["fallback_applied"] = d["position"] < 0

    titles_found = sum(1 for d in decisions if d["position"] >= 0)
    max_chunk = max(chunk_lengths) if chunk_lengths else 0

    decision_log = {
        "chapter_count": len(chapter_titles),
        "source_length": _src_len,
        "per_chapter": decisions,
        "titles_found": titles_found,
        "titles_not_found": len(chapter_titles) - titles_found,
        "fallback_used": fallback_used,
        "source_concentration_ratio": round(max_chunk / _src_len, 3) if _src_len > 0 else 0,
        "chunk_lengths": chunk_lengths,
    }

    log.info(
        f"소스 텍스트 분할: {len(sections)}개 섹션, "
        f"길이: {chunk_lengths}"
    )
    return sections, decision_log


def _find_title_in_text(text: str, title: str) -> dict:
    """
    소스 텍스트에서 대제목 위치를 찾습니다.
    정확 매칭 → 공백 무시 매칭 → 핵심 키워드 매칭 순으로 시도합니다.

    Returns:
        {"position": int, "match_method": str, "core_form": str, "context_preview": str}
    """
    result = {"position": -1, "match_method": "none", "core_form": "", "context_preview": ""}

    def _ctx(pos: int) -> str:
        s, e = max(0, pos - 20), min(len(text), pos + 60)
        return text[s:e].replace("\n", "\\n")

    # 1) 정확한 부분 문자열 매칭
    pos = text.find(title)
    if pos >= 0:
        result.update(position=pos, match_method="exact", context_preview=_ctx(pos))
        return result

    # 2) 공백/줄바꿈 무시 매칭
    escaped_chars = []
    for ch in title.strip():
        if ch in r'\.^$*+?{}[]|()':
            escaped_chars.append(re.escape(ch))
        elif ch.isspace():
            escaped_chars.append(r'\s+')
        else:
            escaped_chars.append(re.escape(ch))
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
            result.update(position=m.start(), match_method="whitespace", context_preview=_ctx(m.start()))
            return result
    except re.error:
        pass

    # 3) 핵심 키워드 매칭 — 마커 제거 후 키워드로 검색
    core = re.sub(r'^[\sⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ\d.)\-–—]+', '', title).strip()
    result["core_form"] = core
    if core and len(core) >= 4:
        pos = text.find(core)
        if pos >= 0:
            line_start = text.rfind('\n', max(0, pos - 30), pos)
            final_pos = line_start + 1 if line_start >= 0 else max(0, pos - 20)
            result.update(position=final_pos, match_method="keyword", context_preview=_ctx(final_pos))
            return result

    return result


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
양식을 분석하여 각 필드의 **의미적 역할(role)**, 용도(description), 마커, 표 구조를 JSON으로 출력합니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (description / 분석 / 추론 / 자연어 설명) 은 반드시 한국어.
- 자체 표현에 한자 (`業務`, `行政`, `情報` 등) / 일본어 가나 (`付き`, `あり` 등) / 외국어 단어 (`cloud`, `system` 등) 사용 금지.
- 양식 sample 글자 인용은 그대로 옮김 (sample 이 한자·영어 포함하면 보존) — 자체 표현과 인용 구분.

**⚠️ level(계층 깊이)은 이 단계에서 결정하지 않습니다** — 별도 단계에서 처리합니다.

## 입력 포맷 (컴팩트 텍스트 — XML 아님)

**문단 한 줄**: `idx|pN|cM[|Ttbl_ids] | 텍스트`
- `idx`: 문단 번호 (0부터)
- `p<N>`: paraPrIDRef (문단 스타일 ID). 예: `p5` = paraPrIDRef 5
- `c<M>`: 첫 run의 charPrIDRef (문자 스타일 ID). 예: `c12` = charPrIDRef 12
- `T<id>[,T<id>]`: 이 문단에 포함된 표 id (선택)
- `|` 뒤: 문단 텍스트. 내용 없으면 `()`, 표만 있으면 `(표만 포함)`

**표 블록**: `[T<id>] <rows>x<cols> in_para=<idx> [borderFill=<id>]`
- 뒤에 `  row<N>: 셀1 | 셀2 | ...` 형식으로 각 행 내용

## 분석 규칙

### 문단 분석 (1a의 책임은 관찰만 — role 분류는 별도 단계)

_idx가 있는 모든 문단에 대해:
- **marker**: 텍스트 앞에 번호/기호가 보이면 그대로 기록, 없으면 "" (마커 정밀 분류는 별도 단계에서 수행)
- **description**: 이 자리에 **어떤 내용이 어떤 형식으로** 들어가야 하는지 구체적으로 설명

**role, paraPrIDRef, charPrIDRef는 출력하지 마세요.** role은 1b, style ID는 코드가 자동 처리합니다.

### description 작성 규칙
1. 해당 위치의 **자리 함수 + 입력 형식**을 짧게 기술하세요. **주제/도메인은 절대 언급 금지**.
   - ❌ 주제 기반(잘못됨): "과일 가격 변동 설명", "교육정책 추진 현황", "조달청 사업 목록"
   - ✓ 구조 기반(맞음): "구체 사실 또는 수치가 들어가는 짧은 본문"
   - 이유: 양식과 전혀 다른 주제의 소스를 매핑해야 하므로, 주제가 들어가면 매핑 혼란.
2. 기술해야 할 것 (단순화):
   - **자리 함수** (제목 / 요약 / 세부 본문 / 보충 / 결론 / 인용 / 강조 박스 등)
   - **입력 형식** (짧은 한 줄 / 한 문장 / 여러 문장 / 수치 포함 / 인용문 / 날짜 등)
3. 기술하지 말 것:
   - ❌ **부모와의 관계**: "설명/근거/예시/반대 사례" 같은 부모 관계 분류 (이건 1c / 1e 책임).
   - ❌ **시간/인과/열거 관계 패턴**: 관계 추론은 다른 단계가 함.
4. 좋은 예 (짧고 자리 함수 + 입력 형식만):
   - "문서 최상위 제목 (한 줄)"
   - "작성일자 (yyyy. m. d. 형식)"
   - "장 시작부 서두 박스 (한~두 문장)"
   - "중분류 항목 제목 (짧은 한 줄)"
   - "구체 사실 또는 수치가 들어가는 짧은 본문"
   - "보충 설명 또는 예시 (선택적)"
   - "관련 법령·규정 인용 박스 (원문 인용형)"
   - "장 종료 전환 요약 박스"
5. **같은 자리 함수의 필드는 동일한 description 사용**
6. **"(고정 텍스트, 수정 불필요)"는 극히 제한적으로만 사용** — 페이지 번호, 머리글/바닥글 같은 순수 레이아웃만

### 표 분석 (1a 책임 = 위치·구조. 표 종류는 1f 가 최종)
문서 내 모든 표에 대해 (0번부터 순서대로):
- **description**: 표의 자리 함수를 짧게 (예: "데이터 표", "텍스트 상자")
- **headers**: 라벨(항목명) 셀 위치 목록
- **value_cells**: 데이터가 채워질 셀 위치 목록

※ 1a 는 **셀 위치 기록**만. 이 표가 진짜 데이터 표인지 (real_table) 장식 박스인지 (decorative_box) 의 최종 판단은 **1f table_kind 가 함**.

### 1x1 표 (텍스트 상자 / 강조 박스)
rowCnt="1" colCnt="1"인 표:
- tables 배열에 포함하되 description 에 "(텍스트 상자)" 추가.
- **value_cells = [{"row": 0, "col": 0}]** (빈 배열 금지).
- headers = [].

## 출력 형식
반드시 아래 JSON만 출력하세요. **level은 출력하지 마세요** (다음 단계에서 결정).

```json
{
  "paragraphs": [
    {"idx": 0, "marker": "", "description": "문서 전체 제목 (한 줄, 핵심 주제 명시)"},
    {"idx": 1, "marker": "", "description": "작성일자 (순수 날짜)"},
    {"idx": 2, "marker": "", "description": "발신 기관명"},
    {"idx": 3, "marker": "", "description": "목차 (텍스트 상자)"},
    {"idx": 4, "marker": "Ⅰ", "description": "대분류 제목 (텍스트 상자)"},
    {"idx": 5, "marker": "□", "description": "중분류 항목 제목"},
    {"idx": 6, "marker": "ㅇ", "description": "세부 항목의 설명 본문"},
    {"idx": 7, "marker": "*", "description": "참고/보충 설명"}
  ],
  "tables": [
    {"table": 0, "rows": 5, "cols": 3, "description": "사업별 예산 배분 현황표",
     "headers": [{"row": 0, "col": 0, "text": "구분"}, {"row": 0, "col": 1, "text": "금액"}],
     "value_cells": [{"row": 1, "col": 1}, {"row": 2, "col": 1}]}
  ]
}
```

## 중요
- **role, level, paraPrIDRef, charPrIDRef 출력 금지** — 각각 1b, 1c, 코드에서 별도 처리합니다
- 양식의 텍스트는 샘플입니다. 샘플 텍스트 자체를 description에 넣지 마세요
- _idx가 있는 문단을 하나도 빠뜨리지 마세요
- 표의 headers(라벨)와 value_cells(데이터)를 정확히 구분하세요
- **1x1 표의 value_cells는 [{"row": 0, "col": 0}]** (빈 배열 금지)
"""


LEVEL_ANALYSIS_PROMPT = """당신은 HWPX 양식의 **level 판단** 전문가입니다 (1c).
1b가 제공한 role 후보 + features를 받아 **각 문단의 level과 후보 index**를 결정합니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (분석 / 추론 / 자연어 설명) 은 반드시 한국어.
- 자체 표현에 한자 (`業務`, `行政`) / 일본어 가나 (`付き`) / 외국어 단어 (`cloud`) 사용 금지.
- 양식 sample 글자 인용은 그대로 옮김 — 자체 표현과 인용 구분.

## 역할 분담
- 1b (이전): semantic_role 후보 + 점수 (per-paragraph)
- **1c (이 단계)**: 전체 시퀀스 → level + 후보 index 선택
- code (다음 단계): level 시퀀스로부터 parent_idx + sibling_group_id + tree 자동 계산

⚠️ **parent_idx, sibling_group_id 출력하지 마라**. 코드가 level만으로 계산함. 너는 level 판정에 집중.
⚠️ **role 이름 직접 만들지 마라**. 1b가 준 후보 중 **index만 고른다**.

## 입력
각 문단마다:
- role_candidates: 1b 후보 리스트 (인덱스 0부터)
- marker, marker_family, description
- features: paraPrIDRef, prev/next marker(family), same_paraPr_run

## 임무

각 문단에 대해 **level + parent_hint_idx** 중심으로 결정:

1. **level**: 계층 깊이 (0=최상위, 1=대제목, 2,3,...)
2. **parent_hint_idx**: 이 문단이 의미상 어느 문단의 자식인지 idx. 최상위면 `null`.
   - level 결정 직전에 "이 문단이 무엇의 자식인가" 를 명시적으로 생각하면 level 정확도가 올라가는 효과.
   - 항상 자기 idx 보다 작은 정수 (forward reference 금지). self-loop 금지.
3. **selected_role_candidate_index** (optional): 1b 후보 중 어느 것 채택할지.
   - 1b 가 후보 1 개만 줬으면 출력 생략 (default = 0).
   - 1b 후보 여러 개 + 1 순위가 위치 / 구조상 어색하면 다른 index 선택.
   - 0 아닌 index 출력 시 `selection_reason_code` 필수.

⚠️ 1c 의 핵심 책임은 **level + parent_hint_idx**. role 선택은 1b 후보 여러 개일 때만 부가 작업.

## 결정 원칙

### A. 구조 신호 + 의미 흐름 같이 검토 — level 결정

**구조 신호** (형식):
- **same_paraPr_run = true 연속**: 양식 작성자가 같은 위계로 묶음 → 같은 level (강한 신호)
- **marker_family 같은 연속**: enumeration siblings → 같은 level
- **marker_family 전환 (interleaved)**: 기존 family 사이 끼어 있으면 → 자식 (level+1)
- **marker_family 전환 (replace)**: 기존 family 끝나고 통째 교체 → 같은 level 가능

**의미 흐름** (description 보고 판단):
- 단락 description 을 읽고 의미상 "이 단락이 무엇의 정리 / 요약 / 보충 / 자식인가" 를 판단.
- **구조 신호와 의미 흐름이 어긋나면 둘 다 의심**. 자동으로 한 쪽 우선 X — parent_hint_idx 로 의미상 부모를 명시한 뒤 level 을 그에 맞게 결정.
- 예: paraPrIDRef 같아도 의미상 직전 단락의 정리 / 보충 / 자식 성격이면 level+1. 형식만 보고 같은 level 로 두지 X.

**구조 + 의미 둘 다 같은 결론** → 안정. 어긋나면 parent_hint_idx 가 가리키는 부모에 맞게 level 결정.

### B. level 일관성 체크 (코드 알고리즘 이해)

코드는 너의 level만 보고 다음 알고리즘으로 parent를 만든다:
```
parent = 현재 문단보다 앞에 나온 문단 중,
         level이 더 낮은 가장 가까운 문단
```

따라서 level만 정확하면 부모-자식 관계가 자동 생성됨. 너의 책임은:
- **연속된 형제는 같은 level** (예: 같은 enumeration의 변형들)
- **자식은 부모의 level + 1**
- **상위 위계로 돌아가면 그만큼 level이 작아짐** (한 그룹의 자식들이 끝나고 새 상위 위계 paragraph가 나오면 그 위계의 level)

### C. selected_role_candidate_index 선택

기본 0. 다음 경우 다른 index:
- 1순위 후보가 위치상 어색 → 2순위·3순위 중 더 맞는 것 (`marker_family_fit`)
- 같은 위치(=같은 level) 형제들과 다른 종류 → 형제 그룹에 맞는 후보 (`sibling_group_consistency`)
- 명백한 자식 관계인데 1순위가 sibling-like 후보 → 자식다운 후보 (`child_role_fit`)

### selection_reason_code 종류 (index != 0일 때 필수)
- `marker_family_fit`: marker_family와 더 잘 맞는 후보
- `sibling_group_consistency`: 같은 level 형제들과 같은 종류 맞춤
- `child_role_fit`: 부모-자식 관계에 더 맞춤
- `position_top_level`: 표지·대제목 등 최상위 위치 맞춤
- `other`: 기타

### D. 금지
- ❌ parent_idx, sibling_group_id 출력 금지 (코드가 함)
- ❌ role 이름 새로 만들지 마라 (1b 후보만 골라라)
- ❌ marker_family·level을 role 이름에 박지 마라 (코드가 자동 합성)

## 출력 형식 (JSON만)

```json
{
  "paragraphs": [
    {
      "idx": 0,
      "level": 0,
      "parent_hint_idx": null
    },
    {
      "idx": 5,
      "level": 2,
      "parent_hint_idx": 4,
      "selected_role_candidate_index": 1,
      "selection_reason_code": "marker_family_fit"
    },
    {
      "idx": 10,
      "level": 3,
      "parent_hint_idx": 6
    }
  ]
}
```

(첫 번째와 세 번째 예시: 1순위 채택이라 selected_role_candidate_index 생략. 두 번째 예시: 2순위 선택이라 명시 + reason_code.)

## 중요
- **모든 idx 출력**
- **필수 필드**: level, parent_hint_idx
- **선택 필드**: selected_role_candidate_index (1b 후보 여러 개 + 1순위 어색할 때만). 출력 안 하면 default 0 (1순위 채택).
- selected_role_candidate_index != 0 이면 selection_reason_code 필수.
- parent_hint_idx 는 의미상 부모 idx. 최상위면 null. forward reference 금지.
- parent_idx, sibling_group_id 출력 금지 (있어도 코드가 무시).
- 반드시 JSON 만 출력.
"""

LEVEL_ANALYSIS_HYBRID_PROMPT = LEVEL_ANALYSIS_PROMPT + """

## 추가 임무 (Hybrid 측정 모드)

기존 level + selected_index 외에 다음을 추가로 출력:

3. **parent_hint_idx** (nullable): 직접 부모로 확신하는 paragraph idx
   - 항상 자기 idx보다 작은 정수
   - 모르면 null. 강제로 채우지 마라
   - self-loop 금지, forward reference 금지

4. **confidence** (필수, 0~1): 자신의 level + parent_hint 신뢰도 종합
   - 모든 paragraph 필수, null 금지
   - 자신 없으면 0.3 같이 낮게. 매우 확실하면 0.9+

5. **parent_hint_reason_code** (parent_hint_idx not null일 때 필수)
   - `paraPr_match`: paraPrIDRef 일치 / 같은 paraPr series
   - `marker_continue`: 같은 marker family 시리즈
   - `marker_subordinate`: marker family 변환 (자식 신호)
   - `chapter_boundary`: chapter root
   - `semantic`: 텍스트 의미상 종속
   - `other`

## 출력 형식 (Hybrid)

```json
{
  "paragraphs": [
    {
      "idx": 0,
      "level": 0,
      "selected_role_candidate_index": 0,
      "parent_hint_idx": null,
      "parent_hint_reason_code": null,
      "confidence": 0.95
    },
    {
      "idx": 195,
      "level": 5,
      "selected_role_candidate_index": 0,
      "parent_hint_idx": 194,
      "parent_hint_reason_code": "marker_subordinate",
      "confidence": 0.82
    }
  ]
}
```

## Hybrid 모드 중요
- 모든 idx 출력. 필수: level, selected_role_candidate_index, confidence
- nullable: parent_hint_idx, parent_hint_reason_code
- parent_hint_idx not null이면 parent_hint_reason_code 필수
- self-loop, forward reference 절대 금지
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


def _extract_texts_by_idx(truncated_xml: str, max_chars: int = 80) -> dict:
    """축소된 XML에서 각 _idx의 텍스트를 추출합니다.

    Args:
        max_chars: 텍스트 최대 길이. None이면 truncation 없이 전체 반환.
    """
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
            joined = " ".join(all_text)
            texts[idx] = joined[:max_chars] if max_chars is not None else joined
    return texts


def serialize_to_compact(light_xml: str, cell_text_limit: int = 60) -> dict:
    """
    Light XML을 AI 전용 컴팩트 텍스트 포맷으로 변환.

    XML 태그 오버헤드(96%)를 제거하고 AI가 role 판단에 쓸 핵심 정보만 뽑음:
    문단 idx, paraPrIDRef, charPrIDRef, 텍스트, 표 참조.

    Returns:
        {
            "text": 컴팩트 텍스트,
            "paragraph_count": N,
            "table_count": M,
        }
    """
    root = etree.fromstring(light_xml.encode("utf-8"))

    # 섹션 레벨 문단만 수집 (표 내부 문단 제외)
    sections = root.findall(f".//{NS_HP}sec")
    if not sections:
        # root 자체가 sec인 경우 (section namespace)
        sections = [root]

    paragraphs = []
    for section in sections:
        for p in section.findall(f"{NS_HP}p"):
            paragraphs.append(p)

    # 표 수집 (문단별 포함 표)
    tables_by_idx = []  # [(tbl_elem, in_para_idx)]
    for p_idx, p in enumerate(paragraphs):
        for tbl in p.iter(f"{NS_HP}tbl"):
            tables_by_idx.append((tbl, p_idx))

    lines = []
    lines.append("# 양식 구조 (컴팩트 포맷)")
    lines.append("#")
    lines.append("# 문단 형식: idx|paraPr|charPr[|Ttable_id,...] | 텍스트")
    lines.append("#   - idx: 문단 번호 (0부터)")
    lines.append("#   - paraPr: paraPrIDRef (문단 스타일 ID)")
    lines.append("#   - charPr: 첫 run의 charPrIDRef (문자 스타일 ID)")
    lines.append("#   - Ttable_id: 이 문단에 포함된 표 (여러 개면 쉼표로)")
    lines.append("#")
    lines.append("# 표 형식: [T<id>] <rows>x<cols> in_para=<idx> [borderFill=<id>]")
    lines.append("#   각 행은 'row<N>: 셀1 | 셀2 | ...'로 표시 (셀 텍스트는 일부 축약)")
    lines.append("")

    lines.append(f"## 문단 목록 (총 {len(paragraphs)}개)")
    lines.append("")

    _para_styles = {}  # idx → {"paraPrIDRef": str, "charPrIDRef": str, "body_first_charpr": str}

    def _find_body_first_charpr(p_elem) -> str:
        """paragraph 안(텍스트박스 cell 안 run 포함)에서 실제 본문 첫 글자가 박힌
        run의 charPrIDRef를 반환. ctrl/tbl 컨테이너 run(text 없음)이나 공백·탭만
        있는 run은 건너뜀. 사람 눈에 보이는 본문 글자의 형식 ID — paragraph 외부
        형식(paraPrIDRef)이 같아도 글자 크기·폰트가 다른 case를 잡기 위한 신호."""
        for run in p_elem.iter(f"{NS_HP}run"):
            for t in run.iter(f"{NS_HP}t"):
                if t.text and t.text.strip():
                    return run.get("charPrIDRef", "")
        return ""

    for p_idx, p in enumerate(paragraphs):
        para_pr = p.get("paraPrIDRef", "0")
        first_run = p.find(f"{NS_HP}run")
        char_pr = first_run.get("charPrIDRef", "0") if first_run is not None else "0"
        body_first_cp = _find_body_first_charpr(p)
        _para_styles[p_idx] = {
            "paraPrIDRef": para_pr,
            "charPrIDRef": char_pr,
            "body_first_charpr": body_first_cp,
        }

        # 표 참조 — 실제 데이터 표만 T 태그 부착 (꾸미기 박스는 제외)
        tbls_in_p = list(p.iter(f"{NS_HP}tbl"))
        table_refs = []
        for t in tbls_in_p:
            rows = int(t.get("rowCnt", "1"))
            cols = int(t.get("colCnt", "1"))
            if rows > 2 and cols > 2:
                table_refs.append(f"T{t.get('_tbl_idx', '?')}")
        table_str = ",".join(table_refs) if table_refs else ""

        # 텍스트: 직접 run 텍스트 우선, 없으면 표 셀 내부 첫 텍스트 fallback
        text_parts = []
        for run in p.findall(f"{NS_HP}run"):
            if run.find(f"{NS_HP}tbl") is not None:
                continue
            for t in run.iter(f"{NS_HP}t"):
                if t.text:
                    text_parts.append(t.text)
        text = "".join(text_parts).strip()
        if not text:
            # 1x1 표 = 텍스트박스 → 내부 텍스트를 문단 텍스트로 취급
            for tbl in p.iter(f"{NS_HP}tbl"):
                rows = int(tbl.get("rowCnt", "1"))
                cols = int(tbl.get("colCnt", "1"))
                cell_texts = []
                for t in tbl.iter(f"{NS_HP}t"):
                    if t.text and t.text.strip():
                        cell_texts.append(t.text.strip())
                if cell_texts:
                    text = " ".join(cell_texts)
                    break
        if len(text) > 200:
            text = text[:200] + "…"

        # 한 줄 생성
        header_parts = [str(p_idx), f"p{para_pr}", f"c{char_pr}"]
        if table_str:
            header_parts.append(table_str)
        header = "|".join(header_parts)

        if text:
            lines.append(f"{header} | {text}")
        elif table_str:
            lines.append(f"{header} | (표만 포함)")
        else:
            lines.append(f"{header} | ()")

    lines.append("")
    lines.append(f"## 표 목록 (총 {len(tables_by_idx)}개)")
    lines.append("")

    for tbl, in_para in tables_by_idx:
        tbl_idx = tbl.get("_tbl_idx", "?")
        rows = int(tbl.get("rowCnt", "1"))
        cols = int(tbl.get("colCnt", "1"))
        border = tbl.get("borderFillIDRef", "0")

        header = f"[T{tbl_idx}] {rows}x{cols} in_para={in_para}"
        if border and border != "0":
            header += f" borderFill={border}"
        lines.append(header)

        for r_idx, tr in enumerate(tbl.findall(f"{NS_HP}tr")):
            row_texts = []
            for tc in tr.findall(f"{NS_HP}tc"):
                cell_text_parts = []
                for t in tc.iter(f"{NS_HP}t"):
                    if t.text:
                        cell_text_parts.append(t.text)
                cell_text = "".join(cell_text_parts).strip().replace("\n", " ")
                if len(cell_text) > cell_text_limit:
                    cell_text = cell_text[:cell_text_limit] + "…"
                row_texts.append(cell_text)
            lines.append(f"  row{r_idx}: " + " | ".join(row_texts))

        lines.append("")

    result_text = "\n".join(lines)
    return {
        "text": result_text,
        "paragraph_count": len(paragraphs),
        "table_count": len(tables_by_idx),
        "paragraph_styles": _para_styles,
    }


def build_structure_analysis_prompt(
    light_xml: str,
    auto_truncate: bool = True,
    use_compact_format: bool = True,
) -> list[dict]:
    """
    1차 호출: 양식 → 구조 분석 프롬프트 (role + description + marker + table)

    Args:
        light_xml: 경량화된 양식 XML
        auto_truncate: XML 포맷 사용 시에만 적용 (compact 포맷은 불필요)
        use_compact_format: True면 컴팩트 텍스트 포맷으로 전달 (토큰 효율 ↑)
                            False면 기존 XML 그대로 전달

    Returns:
        ([{"role": "system", ...}, {"role": "user", ...}], paragraph_styles)
        paragraph_styles: {idx: {"paraPrIDRef": str, "charPrIDRef": str}} or None
    """
    _paragraph_styles = None
    if use_compact_format:
        compact = serialize_to_compact(light_xml)
        _paragraph_styles = compact.get("paragraph_styles")
        user_msg = (
            "아래는 HWPX 양식의 구조를 **컴팩트 텍스트 포맷**으로 정리한 것입니다.\n"
            "각 문단의 **description, marker**를 파악하고, "
            "표의 라벨/값 셀을 구분하세요.\n"
            "**level, paraPrIDRef, charPrIDRef는 이 단계에서 출력하지 마세요** — 별도 처리됩니다.\n\n"
            "### 입력 포맷 설명\n"
            "- 문단: `idx|paraPr|charPr[|Ttable_ids] | 텍스트`\n"
            "  - `p` 접두사: paraPrIDRef (참고용, 출력 불필요)\n"
            "  - `c` 접두사: charPrIDRef (참고용, 출력 불필요)\n"
            "  - `T<id>`: 이 문단이 포함한 표 (예: `T0` = table id 0)\n"
            "- 표: `[T<id>] rows x cols in_para=N` 뒤에 각 행 내용\n\n"
            f"```\n{compact['text']}\n```\n\n"
            "반드시 JSON만 출력하세요."
        )
    else:
        # 기존 XML 방식 (백업 옵션)
        if auto_truncate:
            tr = truncate_xml(light_xml)
            light_xml = tr["xml"]
        user_msg = (
            "아래 HWPX 양식 XML의 구조를 분석하세요.\n"
            "각 _idx 문단의 **description, marker**를 파악하고, "
            "표의 라벨/값 셀을 구분하세요.\n"
            "**level, paraPrIDRef, charPrIDRef는 출력하지 마세요** — 별도 처리됩니다.\n\n"
            f"```xml\n{light_xml}\n```\n\n"
            "반드시 JSON만 출력하세요."
        )

    messages = [
        {"role": "system", "content": STRUCTURE_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    return messages, _paragraph_styles


def build_level_analysis_prompt(structure_json: dict, signals: dict = None, hybrid: bool = False) -> list[dict]:
    """
    1b 호출 (AI 2, global): role 후보 + features → final_role + level + parent_idx + sibling_group_id

    Args:
        structure_json: paragraphs에 role_candidates + features (compute_paragraph_features 적용)
                        가 있어야 함
        signals: 옵션 (text preview 용)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    paragraphs = structure_json.get("paragraphs", [])

    text_by_idx = {}
    if signals:
        for pt in signals.get("paragraph_texts", []):
            text_by_idx[pt.get("idx")] = pt.get("text", "")

    para_lines = []
    for p in paragraphs:
        idx = p.get("idx", -1)
        marker = p.get("marker", "")
        marker_family = p.get("marker_family", "")
        desc = p.get("description", "")
        prev_marker = p.get("prev_marker", "")
        next_marker = p.get("next_marker", "")
        prev_family = p.get("prev_marker_family", "")
        next_family = p.get("next_marker_family", "")
        same_paraPr = p.get("same_paraPr_run", False)
        same_body_charpr = p.get("same_body_charpr_run", False)
        para_pr = p.get("paraPrIDRef", "")
        body_cp = p.get("body_first_charpr", "")
        cands = p.get("role_candidates", [])

        text_preview = text_by_idx.get(idx, "")[:80] if text_by_idx else ""

        # 후보 압축 표시: [(role, score), ...]
        cands_str = json.dumps(
            [{"role": c.get("role"), "score": c.get("score")} for c in cands],
            ensure_ascii=False
        )

        marker_str = f'"{marker}"' if marker else '""'
        feature_parts = [
            f'"idx": {idx}',
            f'"marker": {marker_str}',
            f'"marker_family": "{marker_family}"',
            f'"description": {json.dumps(desc, ensure_ascii=False)}',
            f'"paraPrIDRef": "{para_pr}"',
            f'"body_charpr": "{body_cp}"',
            f'"prev_marker_family": "{prev_family}"',
            f'"next_marker_family": "{next_family}"',
            f'"same_paraPr_run": {str(same_paraPr).lower()}',
            f'"same_body_charpr_run": {str(same_body_charpr).lower()}',
            f'"role_candidates": {cands_str}',
        ]
        if text_preview:
            feature_parts.append(f'"text": {json.dumps(text_preview, ensure_ascii=False)}')
        para_lines.append("{" + ", ".join(feature_parts) + "}")

    para_text = "[\n  " + ",\n  ".join(para_lines) + "\n]"

    user_msg = (
        "아래는 AI 1이 분석한 문단 목록 + role 후보 + features입니다.\n"
        "전체 시퀀스를 보고 각 문단의 final_role + level + parent_idx + sibling_group_id를 결정하세요.\n\n"
        "## 결정 단계\n"
        "1. 시퀀스 흐름 + features로 parent-child 관계 파악 (parent_idx)\n"
        "2. parent_idx에서 level 도출 (parent의 level + 1, 최상위는 0)\n"
        "3. AI 1 후보 1순위 채택. 위치/구조상 어색하면 다른 후보 또는 새 role (override)\n"
        "4. 같은 부모 아래 자식들의 sibling_group_id 부여\n\n"
        "## features 활용\n"
        "- **body_charpr** = paragraph 안 실제 첫 글자가 박힌 run의 글자 형식 ID. "
        "사람 눈에 보이는 글자 크기·폰트·굵기 자체를 나타냄. "
        "**paraPrIDRef는 paragraph 외부 형식(들여쓰기·줄간격)만 표현하므로 글자 크기·폰트 차이를 못 잡음.** "
        "두 paragraph가 같은 paraPr여도 body_charpr가 다르면 시각적으로 완전히 다른 형식.\n"
        "- **same_body_charpr_run = true**: 직전 paragraph와 본문 글자 형식 동일 → 같은 위계의 형제 가능성 매우 높음 (시각적 동급).\n"
        "- **same_body_charpr_run = false 이지만 same_paraPr_run = true**: paragraph 외부 형식만 같고 본문 글자 형식 다름 → 사람 눈에 다른 형식. 같은 위계의 형제 가능성 낮음. **직전 paragraph의 자식**이거나 다른 level일 가능성 높음.\n"
        "- same_paraPr_run = true + same_body_charpr_run = true: 두 신호 모두 일치 → 형제 가능성 강함.\n"
        "- marker_family 같은 연속 → enumeration siblings (같은 level)\n"
        "- marker_family 다른 등장 (interleaved 패턴) → 자식 가능성\n"
        "- marker_family 다른 등장 (replace 패턴) → 같은 level 가능\n\n"
        f"## 문단 목록\n```json\n{para_text}\n```\n\n"
        "반드시 JSON만 출력 (paragraphs 배열, 각 문단의 final_role/level/parent_idx/sibling_group_id)."
    )

    system_prompt = LEVEL_ANALYSIS_HYBRID_PROMPT if hybrid else LEVEL_ANALYSIS_PROMPT
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]


def parse_level_from_llm(llm_response: str, hybrid: bool = False) -> dict:
    """
    1c (AI 2) LLM 응답 파싱 — selected_role_candidate_index 방식.

    Returns:
        {
          "decisions": {idx: {level, parent_idx, sibling_group_id,
                              selected_index, selection_reason_code}},
          "level_map": {idx: level},  # 하위 호환
        }
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

    paras_list = data.get("paragraphs", []) if isinstance(data, dict) else data

    # 하위 호환 — 옛 levels 형식
    if not paras_list and isinstance(data, dict) and "levels" in data:
        legacy = data.get("levels", [])
        decisions, level_map = {}, {}
        for e in legacy:
            if isinstance(e, dict) and e.get("idx") is not None and e.get("level") is not None:
                idx = int(e["idx"]); lv = int(e["level"])
                decisions[idx] = {"level": lv, "selected_index": 0}
                level_map[idx] = lv
        log.info(f"level 파싱 (legacy): {len(level_map)}개 문단")
        return {"decisions": decisions, "level_map": level_map}

    if not isinstance(paras_list, list):
        raise ValueError(f"paragraphs가 배열이 아닙니다: {type(paras_list)}")

    decisions = {}
    level_map = {}
    non_default_index = 0
    for entry in paras_list:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        if idx is None:
            continue
        idx = int(idx)
        level = entry.get("level")
        parent_idx = entry.get("parent_idx")
        sib_group = entry.get("sibling_group_id")
        selected_idx = entry.get("selected_role_candidate_index", 0)
        reason_code = entry.get("selection_reason_code", "")
        # 하위 호환: 옛 final_role 필드도 받아둠 (있으면 보조 정보)
        legacy_final_role = entry.get("final_role")

        if level is not None:
            try:
                level = int(level)
                level_map[idx] = level
            except Exception:
                level = None

        if parent_idx is not None and parent_idx != "null":
            try:
                parent_idx = int(parent_idx)
            except Exception:
                parent_idx = None
        else:
            parent_idx = None

        try:
            selected_idx = int(selected_idx)
        except Exception:
            selected_idx = 0

        decisions[idx] = {
            "level": level,
            "parent_idx": parent_idx,
            "sibling_group_id": str(sib_group) if sib_group else None,
            "selected_index": selected_idx,
            "selection_reason_code": str(reason_code) if reason_code else "",
            "legacy_final_role": str(legacy_final_role) if legacy_final_role else None,
        }
        if hybrid:
            parent_hint = entry.get("parent_hint_idx")
            if parent_hint is None or parent_hint == "null":
                parent_hint = None
            else:
                try:
                    parent_hint = int(parent_hint)
                except Exception:
                    parent_hint = None
            confidence = entry.get("confidence")
            try:
                confidence = float(confidence) if confidence is not None else None
            except Exception:
                confidence = None
            hint_reason = entry.get("parent_hint_reason_code")
            decisions[idx]["parent_hint_idx"] = parent_hint
            decisions[idx]["confidence"] = confidence
            decisions[idx]["parent_hint_reason_code"] = (
                str(hint_reason) if hint_reason and hint_reason != "null" else None
            )
        if selected_idx != 0:
            non_default_index += 1

    log.info(
        f"1c (AI 2) 파싱: {len(decisions)}개 문단, "
        f"non-default candidate index {non_default_index}개"
    )
    return {"decisions": decisions, "level_map": level_map}


def merge_levels_into_structure(
    structure: dict, parsed: dict, exclusive_rules: list = None,
    canonical_mode: str = "on",
) -> dict:
    """
    1c (AI 2) 결과를 structure에 병합 + structure_role 자동 합성 + validator 적용.

    적용 순서:
    1. AI 2 decisions로 level/parent_idx/sibling_group_id 채움
    2. selected_index로 1b 후보 중 final semantic_role 확정 (또는 legacy_final_role)
    3. structure_role = marker_family + semantic_role 합성
    4. validator로 marker_family 충돌 등 자동 split

    Args:
        structure: paragraphs (1b의 role_candidates + features 포함)
        parsed: parse_level_from_llm 결과
        exclusive_rules: 1d 결과 (선택)
        canonical_mode: _FAMILY_DEFAULT_CANONICAL 적용 모드
            - "on": fallback 적용 (현재 방식)
            - "report_only": fallback 적용 안 함, log만
            - "off": fallback 적용 안 함, log도 없음

    Returns:
        paragraphs에 level/role/structure_role/parent_idx/sibling_group_id 추가
    """
    # 하위 호환 — 옛 호출 (level_map만 dict)
    if isinstance(parsed, dict) and "decisions" not in parsed and "level_map" not in parsed:
        legacy_map = parsed
        for p in structure.get("paragraphs", []):
            idx = p.get("idx", -1)
            if idx in legacy_map:
                p["level"] = legacy_map[idx]
            else:
                p.setdefault("level", 0)
        if exclusive_rules:
            structure["exclusive_rules"] = exclusive_rules
        return structure

    decisions = parsed.get("decisions", {})
    level_map = parsed.get("level_map", {})

    # 1단계: decisions 적용 + selected_index 검증 + semantic_role 확정
    fallback_count = 0
    for p in structure.get("paragraphs", []):
        idx = p.get("idx", -1)
        d = decisions.get(idx) or decisions.get(str(idx))
        candidates = p.get("role_candidates", [])

        if d:
            if d.get("level") is not None:
                p["level"] = d["level"]
            # parent_idx, sibling_group_id는 코드가 계산 (1c가 줘도 무시)

            # selected_index 임시 적용
            sel_idx = d.get("selected_index", 0)
            sel_idx = max(0, min(sel_idx, len(candidates) - 1)) if candidates else 0
            p["selected_role_candidate_index"] = sel_idx
            if d.get("selection_reason_code"):
                p["selection_reason_code"] = d["selection_reason_code"]

            # validator: 억지 후보 방지 (score, score_diff, reason_code 검사)
            v = _validate_selected_index(p)
            if not v["valid"] and v["fallback"]:
                log.info(
                    f"[VALIDATOR] idx={idx}: selected_index {sel_idx} → 0 fallback "
                    f"({v['issue']})"
                )
                p["selected_role_candidate_index"] = 0
                p["selection_fallback_reason"] = v["issue"]
                sel_idx = 0
                fallback_count += 1

            if candidates:
                p["semantic_role"] = candidates[sel_idx].get("role", "unknown")
            elif d.get("legacy_final_role"):
                p["semantic_role"] = d["legacy_final_role"]
            else:
                p["semantic_role"] = p.get("role", "unknown")
        elif idx in level_map:
            p["level"] = level_map[idx]
            if candidates:
                p["semantic_role"] = candidates[0].get("role", "unknown")
        else:
            p.setdefault("level", 0)
            if candidates:
                p["semantic_role"] = candidates[0].get("role", "unknown")

    if fallback_count:
        log.info(f"[VALIDATOR] selected_index fallback: {fallback_count}개")

    # 2단계: 임시 role/structure_role 부여
    # - 1e (structural canonicalization)이 후에 cluster_id로 덮어씀
    # - 1e 비활성/실패 시 raw semantic_role 그대로 사용 (마커→role 하드코딩 X)
    for p in structure.get("paragraphs", []):
        sem_role = p.get("semantic_role") or p.get("role", "unknown")
        family = p.get("marker_family", "") or ""

        # canonical 합성 — _FAMILY_DEFAULT_CANONICAL 사용 안 함
        # 1b의 raw semantic_role 그대로 보존
        p["canonical_role"] = sem_role

        family_for_label = family or "no_marker"
        if family.startswith("char_"):
            family_short = family[5:]
            family_label = f"char{family_short}"
        else:
            family_label = family_for_label
        structure_role = f"{family_label}__{sem_role}" if family else sem_role
        p["structure_role"] = structure_role
        p["role"] = structure_role  # 1e가 cluster_id로 덮어쓸 예정

    # 3단계: 코드가 parent_idx + sibling_group_id 자동 계산 (level 시퀀스 기반)
    # canonical_role 합성 후라 _can_be_parent 필터가 정확히 동작
    structure["paragraphs"] = compute_parent_and_sibling_from_levels(
        structure.get("paragraphs", [])
    )

    # 4단계: validator
    structure = _validate_and_split(structure)

    if exclusive_rules:
        structure["exclusive_rules"] = exclusive_rules
    return structure


# marker_family별 canonical role 매핑.
# 양식 구조 관점의 안정적 통합용. semantic_role의 세부 의미는 description으로 보존.
# 1b가 다양한 semantic_role을 줘도 코드가 같은 양식 역할로 묶음.
_FAMILY_DEFAULT_CANONICAL = {
    # 별표 계열: 원칙적으로 보충 항목 (실제 양식에선 거의 항상 보강용)
    "char_*": "supplement_item",
    # 작은 사각: 보통 실행/이행 항목
    "char_▪": "action_subitem",
    # 이응: 보통 본문 bullet
    "char_ㅇ": "bullet_item",
    # 큰 사각: 보통 섹션 헤더
    "char_□": "section_header",
    # 화살표: 결과/요약
    "char_⇒": "summary_arrow",
    "char_→": "summary_arrow",
    # enumeration 시리즈
    "dingbat_neg_circle": "numbered_item",   # ➊➋➌
    "dingbat_neg_circle2": "numbered_item",  # ❶❷❸
    "circle_num": "enumerated_item",          # ①②③
    "circle_num_pua": "numbered_item",        # 󰊱󰊲
    "num_paren": "enumerated_detail",         # 1)2)3) — 각주·하위 enumeration
    "hangul_dot": "enumerated_item",          # 가.나.다.
    "roman": "section_header",                # ⅠⅡⅢ
}

# override는 일단 비활성화 — canonical 정규화 효과를 깨끗하게 검증한 뒤
# 진짜 필요한 케이스만 선별해서 조건부 복구할 예정.
# (단순 semantic_role 매칭으로 열어두는 건 위험 — 반복 패턴·description 시그널 등
# 추가 조건과 함께 다뤄야 함)
_ALLOWED_OVERRIDES = {}


def canonicalize_role(marker_family: str, semantic_role: str,
                       mode: str = "on") -> tuple[str, dict | None]:
    """
    marker_family + semantic_role → canonical_role 정규화.

    mode:
        - "on": family default가 있고 semantic_role과 다르면 default로 override (현재 방식)
        - "report_only": override 안 함 (semantic_role 반환). 발생했을 fallback은 log.
        - "off": override 안 함. fallback log도 None.

    Returns:
        (final_canonical_role, fallback_info)
        - fallback_info: None or dict
            - None: family default 없음 / semantic_role과 일치 / mode="off"
            - dict: {family_default, applied (bool)}
    """
    if not marker_family or marker_family == "":
        return semantic_role, None

    default = _FAMILY_DEFAULT_CANONICAL.get(marker_family)
    if not default:
        return semantic_role, None

    overrides = _ALLOWED_OVERRIDES.get(marker_family, set())
    if semantic_role in overrides:
        return semantic_role, None

    if semantic_role == default:
        # 1b가 이미 family default와 같은 role 줌 — fallback 발생 안 함
        return semantic_role, None

    # 여기서부터 fallback이 발동 가능 (mode에 따라 적용 여부 달라짐)
    if mode == "on":
        return default, {"family_default": default, "applied": True}
    elif mode == "report_only":
        return semantic_role, {"family_default": default, "applied": False}
    else:  # "off"
        return semantic_role, None


def _compute_container_scores(paragraphs: list[dict]) -> dict:
    """
    양식 데이터 자체에서 role의 container 적합도를 multi-signal로 점수화 (하드코딩 X).

    Signal (양식 무관):
      - child_having_ratio: 인스턴스 중 자식 가진 비율
      - avg_child_count: 인스턴스당 평균 자식 수 (전체 기준)
      - avg_child_when_present: 자식 가진 인스턴스의 평균 자식 수
      - dominant_signature_ratio: 가장 흔한 non-empty 자식 set 비율 (전체 기준)
      - intro_pattern_ratio: 자식 1개인 인스턴스 비율 (intro/summary 의심)

    score = ratio*0.4 + min(avg_child/2, 1)*0.3 + dominant_ratio*0.3

    Args:
        paragraphs: level이 배정된 paragraph list

    Returns:
        {role: {score, instance_count, with_kids_count, child_having_ratio,
                avg_child_count, avg_child_when_present, dominant_signature,
                dominant_signature_ratio, intro_pattern_ratio}}
    """
    from collections import defaultdict, Counter

    role_instances = defaultdict(list)  # role → [list of child-role lists per inst]
    stack = []  # [(level, role, kids_list_ref)]

    for p in paragraphs:
        level = p.get("level")
        role = p.get("role", "")
        if level is None or not role:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            parent_level, parent_role, parent_kids = stack[-1]
            if level == parent_level + 1:
                parent_kids.append(role)
        my_kids = []
        role_instances[role].append(my_kids)
        stack.append((level, role, my_kids))

    scores = {}
    for role, instances in role_instances.items():
        total = len(instances)
        if total == 0:
            continue
        with_kids = sum(1 for inst in instances if inst)
        ratio = with_kids / total
        all_count = sum(len(inst) for inst in instances)
        avg_count = all_count / total
        avg_when_present = (all_count / with_kids) if with_kids else 0.0

        non_empty_sigs = [tuple(sorted(set(inst))) for inst in instances if inst]
        if non_empty_sigs:
            sig_counter = Counter(non_empty_sigs)
            top_sig, top_count = sig_counter.most_common(1)[0]
            dominant_ratio = top_count / total
        else:
            top_sig, dominant_ratio = (), 0.0

        single_child_count = sum(1 for inst in instances if len(inst) == 1)
        intro_ratio = single_child_count / total

        score = (
            ratio * 0.4
            + min(avg_count / 2.0, 1.0) * 0.3
            + dominant_ratio * 0.3
        )

        scores[role] = {
            "score": round(score, 3),
            "instance_count": total,
            "with_kids_count": with_kids,
            "child_having_ratio": round(ratio, 3),
            "avg_child_count": round(avg_count, 3),
            "avg_child_when_present": round(avg_when_present, 3),
            "dominant_signature": list(top_sig),
            "dominant_signature_ratio": round(dominant_ratio, 3),
            "intro_pattern_ratio": round(intro_ratio, 3),
        }
    return scores


def _is_strong_container(role: str, scores: dict) -> bool:
    """
    Strong container 조건 — 3-way OR (어느 하나 만족):
      A) score >= 0.6 (multi-signal 종합 확실히 강함)
      B) with_kids_count >= 5 AND avg_child_when_present >= 1.0
         (충분한 인스턴스 + 일관된 자식 보유 — 데이터로 보강)
      C) score >= 0.55 AND dominant_signature_ratio >= 0.4
         (borderline score 도 자식 패턴 일관되면 살림 — 데이터 적은 role 구제)

    M 같은 unstable parent는 score borderline + 자식 패턴 비일관 (dom 낮음) →
    셋 다 fail → weak 분류.
    """
    s = scores.get(role)
    if not s:
        return False
    score = s["score"]
    with_kids = s["with_kids_count"]
    awp = s["avg_child_when_present"]
    dom = s["dominant_signature_ratio"]

    if score >= 0.6:
        return True
    if with_kids >= 5 and awp >= 1.0:
        return True
    if score >= 0.55 and dom >= 0.4:
        return True
    return False


def _compute_container_roles(paragraphs: list[dict], threshold: float = 0.3) -> set:
    """
    호환 wrapper. _compute_container_scores + _is_strong_container 사용.
    threshold 인자는 더 이상 사용하지 않음 (multi-signal 분류로 대체).
    """
    scores = _compute_container_scores(paragraphs)
    return {role for role in scores if _is_strong_container(role, scores)}


# 화살표 marker family — 결과/요약/귀결 의미. 일반적으로 leaf, 직전 enumeration 그룹 결론.
_ARROW_MARKER_FAMILIES = {"char_⇒", "char_→"}

# Enumeration marker family — 번호 매기기 시리즈. 화살표 reattach 대상.
_ENUMERATION_MARKER_FAMILIES = {
    "dingbat_neg_circle", "dingbat_neg_circle2",
    "circle_num", "circle_num_pua",
    "num_paren", "hangul_dot",
}


def reattach_arrow_markers(paragraphs: list[dict]) -> tuple:
    """
    화살표 marker family (char_⇒/→) 문단의 parent_idx를 직전 enumeration
    형제의 parent로 재설정 (= enumeration sibling 위치).

    marker-family 기반 기본 룰. arrow가 enumeration 시퀀스의 trailing
    summary로 등장하는 양식이 일반적이라 채택. Stack 알고리즘이 arrow를
    enum 자식으로 잘못 둘 때 보정.

    한계: arrow가 직전 enum 하나의 세부 설명으로 쓰이는 양식도 가능 —
    그 경우 예외/검증 필요. Phase 2 ordered sibling pattern 설계
    이후 case-by-case 처리.

    in-place 수정. log 반환.
    """
    from collections import defaultdict

    siblings_map = defaultdict(list)
    for p in paragraphs:
        siblings_map[p.get("parent_idx")].append(p)
    for k in siblings_map:
        siblings_map[k].sort(key=lambda x: x.get("idx", 0))

    idx_to_para = {p.get("idx"): p for p in paragraphs}

    log = []
    for p in paragraphs:
        family = p.get("marker_family", "")
        if family not in _ARROW_MARKER_FAMILIES:
            continue
        parent_idx = p.get("parent_idx")
        sibs = siblings_map.get(parent_idx, [])
        my_pos = next((i for i, s in enumerate(sibs) if s.get("idx") == p.get("idx")), None)
        if my_pos is None or my_pos == 0:
            continue
        prev_enum = None
        for s in reversed(sibs[:my_pos]):
            if s.get("marker_family") in _ENUMERATION_MARKER_FAMILIES:
                prev_enum = s
                break
        if prev_enum is None:
            continue
        new_parent_idx = prev_enum.get("parent_idx")
        if new_parent_idx is None:
            continue
        new_parent = idx_to_para.get(new_parent_idx)
        log.append({
            "arrow_idx": p.get("idx"),
            "arrow_marker": p.get("marker"),
            "arrow_family": family,
            "old_parent_idx": parent_idx,
            "new_parent_idx": new_parent_idx,
            "new_parent_role": new_parent.get("role") if new_parent else None,
            "via_enum_idx": prev_enum.get("idx"),
            "via_enum_role": prev_enum.get("role"),
        })
        p["parent_idx"] = new_parent_idx
        p["level"] = prev_enum.get("level", 0) or 0
        p["sibling_group_id"] = f"children_of_{new_parent_idx}"
    return paragraphs, log


def validate_parent_hints(decisions: dict, paragraphs: list[dict]) -> dict:
    """
    parent_hint_idx 검증. 각 idx별로 다음 분류:
      - "valid": hint < idx + paragraph에 존재
      - "self_loop": hint == idx
      - "forward_ref": hint > idx
      - "out_of_range": paragraph에 없는 idx
      - "no_hint": parent_hint_idx is None

    Returns:
        {"per_idx": {idx: status}, "counts": {valid, self_loop, forward_ref, out_of_range, no_hint}}
    """
    valid_idx_set = {p.get("idx") for p in paragraphs}
    per_idx = {}
    counts = {"valid": 0, "self_loop": 0, "forward_ref": 0,
              "out_of_range": 0, "no_hint": 0}
    for idx, d in decisions.items():
        try:
            idx = int(idx)
        except Exception:
            continue
        hint = d.get("parent_hint_idx")
        if hint is None:
            per_idx[idx] = "no_hint"
            counts["no_hint"] += 1
            continue
        if hint == idx:
            per_idx[idx] = "self_loop"
            counts["self_loop"] += 1
            continue
        if hint > idx:
            per_idx[idx] = "forward_ref"
            counts["forward_ref"] += 1
            continue
        if hint not in valid_idx_set:
            per_idx[idx] = "out_of_range"
            counts["out_of_range"] += 1
            continue
        per_idx[idx] = "valid"
        counts["valid"] += 1
    return {"per_idx": per_idx, "counts": counts}


def classify_hint_conflicts(paragraphs: list[dict], decisions: dict,
                             hint_validation: dict) -> dict:
    """
    Stack tree의 parent_idx vs hint의 parent_idx 비교. 충돌 방향성 분류.
    Hint가 valid인 paragraph에 대해서만:
      - "match": hint == stack
      - "hint_is_ancestor": hint가 stack parent의 ancestor (nesting up — hint가 더 얕음)
      - "hint_is_descendant": stack parent가 hint의 ancestor (hint가 더 깊음)
      - "unrelated": 둘이 ancestor 관계 X (형제 관계 등)

    Returns:
        {"per_idx": {idx: {hint, stack, kind}}, "counts": {match, ancestor, descendant, unrelated}}
    """
    para_by_idx = {p.get("idx"): p for p in paragraphs}

    def ancestors_of(idx):
        result = []
        cur = para_by_idx.get(idx, {}).get("parent_idx")
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            result.append(cur)
            cur = para_by_idx.get(cur, {}).get("parent_idx")
        return result

    per_idx = {}
    counts = {"match": 0, "hint_is_ancestor": 0,
              "hint_is_descendant": 0, "unrelated": 0}
    for idx, status in hint_validation["per_idx"].items():
        if status != "valid":
            continue
        d = decisions.get(idx) or decisions.get(str(idx))
        if not d:
            continue
        hint = d["parent_hint_idx"]
        stack = para_by_idx.get(idx, {}).get("parent_idx")
        if hint == stack:
            kind = "match"
        else:
            stack_ancestors = ancestors_of(idx)
            hint_ancestors = ancestors_of(hint)
            if hint in stack_ancestors:
                kind = "hint_is_ancestor"
            elif stack is not None and stack in hint_ancestors:
                kind = "hint_is_descendant"
            else:
                kind = "unrelated"
        per_idx[idx] = {"hint": hint, "stack": stack, "kind": kind}
        counts[kind] += 1
    return {"per_idx": per_idx, "counts": counts}


def build_hint_override_tree(paragraphs: list[dict], decisions: dict,
                              hint_validation: dict) -> list[dict]:
    """
    단순 (a) override: valid hint paragraph의 parent_idx만 hint로 변경.
    propagation 없음. sibling_group_id 재계산.

    Returns:
        paragraphs 복사본 (parent_idx, sibling_group_id 변경됨)
    """
    import copy
    para_copy = copy.deepcopy(paragraphs)
    for p in para_copy:
        idx = p.get("idx")
        status = hint_validation["per_idx"].get(idx)
        if status != "valid":
            continue
        d = decisions.get(idx) or decisions.get(str(idx))
        if not d:
            continue
        p["parent_idx"] = d["parent_hint_idx"]
    for p in para_copy:
        pi = p.get("parent_idx")
        p["sibling_group_id"] = "roots" if pi is None else f"children_of_{pi}"
    return para_copy


def build_hint_tree(paragraphs: list[dict], decisions: dict,
                     hint_validation: dict) -> list[dict]:
    """
    Hint-first 트리 구성. valid hint면 hint parent, 그 외(no_hint/self_loop/
    forward_ref/out_of_range)는 stack parent fallback. BFS로 level 재계산.

    입력:
      - paragraphs: stack tree 상태 (parent_idx, level 이미 계산된 결과)
      - decisions: 1c decisions (parent_hint_idx 포함)
      - hint_validation: validate_parent_hints 결과 (per_idx status)

    출력:
      - paragraphs deepcopy. parent_idx (hint or stack fallback),
        level (BFS 재계산), sibling_group_id 일관됨.

    Cycle 보장: validate_parent_hints가 forward_ref/self_loop를 invalid
    분류하므로 hint는 backward only. stack도 backward only.
    → 모든 parent_idx < idx → DAG.

    read-only 측정용. 1d/2a/2b/조립 파이프라인엔 사용하지 말 것.
    """
    import copy
    from collections import defaultdict, deque

    para_copy = copy.deepcopy(paragraphs)
    idx_to_p = {p.get("idx"): p for p in para_copy}

    # 1) parent_idx 결정 (valid hint면 hint, 아니면 stack 유지)
    for p in para_copy:
        idx = p.get("idx")
        status = hint_validation.get("per_idx", {}).get(idx)
        if status == "valid":
            d = decisions.get(idx) or decisions.get(str(idx)) or {}
            hint = d.get("parent_hint_idx")
            if hint is not None and hint in idx_to_p:
                p["parent_idx"] = hint

    # 2) BFS level 재계산
    children_of = defaultdict(list)
    roots = []
    for p in para_copy:
        pi = p.get("parent_idx")
        if pi is None:
            roots.append(p.get("idx"))
        else:
            children_of[pi].append(p.get("idx"))

    for p in para_copy:
        p["level"] = None
    queue = deque()
    for r in roots:
        rp = idx_to_p.get(r)
        if rp is not None:
            rp["level"] = 0
            queue.append(r)
    visited = set(roots)
    while queue:
        pi = queue.popleft()
        plevel = idx_to_p[pi].get("level", 0) or 0
        for ci in children_of.get(pi, []):
            if ci in visited:
                continue
            cp = idx_to_p.get(ci)
            if cp is None:
                continue
            cp["level"] = plevel + 1
            visited.add(ci)
            queue.append(ci)

    # 3) sibling_group_id
    for p in para_copy:
        pi = p.get("parent_idx")
        p["sibling_group_id"] = "roots" if pi is None else f"children_of_{pi}"

    return para_copy


def compute_parent_instance_children_by_parent_idx(paragraphs: list[dict]) -> dict:
    """
    parent_idx 기반 parent_instance_children 계산. 출력 형식은
    compute_parent_instance_children(level 기반)과 동일.

    hint_tree처럼 parent_idx와 level이 일관된 트리 비교용.
    compute_parent_instance_children은 level 기반 stack 재구성이라
    parent_idx 변화를 반영 못 함 — 그래서 별도 함수 필요.

    Returns:
        {parent_role: [frozenset(children)×N]}
        - 인스턴스 < 2 인 role 제외
        - 자식 종류 < 2 인 role 제외
    """
    from collections import defaultdict

    role_instance_ids = defaultdict(list)
    instance_children = defaultdict(set)
    idx_to_inst = {}

    for i, p in enumerate(paragraphs):
        role = p.get("role", "")
        if not role:
            continue
        role_instance_ids[role].append(i)
        idx_to_inst[p.get("idx")] = (role, i)
        instance_children[(role, i)] = set()

    for p in paragraphs:
        role = p.get("role", "")
        parent_idx = p.get("parent_idx")
        if not role or parent_idx is None:
            continue
        parent_inst = idx_to_inst.get(parent_idx)
        if parent_inst is None:
            continue
        instance_children[parent_inst].add(role)

    result = {}
    for role, inst_ids in role_instance_ids.items():
        if len(inst_ids) < 2:
            continue
        instances = [frozenset(instance_children[(role, iid)]) for iid in inst_ids]
        non_empty = [inst for inst in instances if inst]
        if not non_empty:
            continue
        all_children = set()
        for inst in non_empty:
            all_children |= inst
        if len(all_children) < 2:
            continue
        result[role] = instances
    return result


def canonicalize_by_data(paragraphs: list[dict],
                          ambiguous_threshold: float = 0.6) -> dict:
    """
    parent_first tree 위에서 signature 기반 클러스터링으로 structural_role 할당.

    Signature: (marker_family, parent_marker_family, level)
    - paraPrIDRef는 instance별 unique한 경우 많아 over-fragmentation 유발 → primary signature 제외
    - 대신 각 cluster 안의 paraPrIDRef 분포는 debug stat으로 보존
    - 같은 signature 인스턴스 = 같은 structural_role_id (role_cluster_<n>)
    - 각 cluster의 display_role = 가장 빈번한 1b semantic_role

    in-place 수정:
        - paragraph["structural_role_id"] = "role_cluster_N"
        - paragraph["display_role"] = 가장 빈번 semantic_role
        - paragraph["role"] = cluster_id (downstream 호환)
        - paragraph["structure_role"] = cluster_id

    Returns:
        role_registry: {cluster_id: {
            signature, display_role, instance_count,
            semantic_role_distribution,
            paraPrIDRef_distribution,         # debug only — 분포 균형 점검용
            ambiguous,                         # display_role 비율 < threshold 면 True
            instance_idxs,
        }}
    """
    from collections import Counter, defaultdict

    idx_to_p = {p.get("idx"): p for p in paragraphs}

    # 1) signature 계산 + 클러스터링 (paraPrIDRef 제외)
    sig_to_paras: dict = defaultdict(list)
    for p in paragraphs:
        family = p.get("marker_family", "") or ""
        parent_idx = p.get("parent_idx")
        parent = idx_to_p.get(parent_idx)
        parent_family = (parent.get("marker_family", "") if parent else "") or ""
        level = p.get("level")

        sig = (family, parent_family, level)
        sig_to_paras[sig].append(p)

    # 2) 안정적 cluster_id 할당 (signature 정렬 — 결정적)
    role_registry: dict = {}
    sorted_sigs = sorted(
        sig_to_paras.keys(),
        key=lambda s: (str(s[0]), str(s[1]), s[2] or 0)
    )

    for cluster_idx, sig in enumerate(sorted_sigs):
        paras_in_cluster = sig_to_paras[sig]
        cluster_id = f"role_cluster_{cluster_idx}"

        sem_roles = Counter(
            (p.get("semantic_role") or "unknown") for p in paras_in_cluster
        )
        para_prs = Counter(
            (p.get("paraPrIDRef") or "") for p in paras_in_cluster
        )

        if sem_roles:
            top_role, top_count = sem_roles.most_common(1)[0]
            display = top_role
            total = sum(sem_roles.values())
            top_ratio = top_count / total if total else 0.0
        else:
            display = "unknown"
            top_ratio = 0.0

        ambiguous = top_ratio < ambiguous_threshold

        role_registry[cluster_id] = {
            "signature": {
                "marker_family": sig[0],
                "parent_marker_family": sig[1],
                "level": sig[2],
            },
            "display_role": display,
            "instance_count": len(paras_in_cluster),
            "semantic_role_distribution": dict(sem_roles),
            "paraPrIDRef_distribution": dict(para_prs),
            "display_role_ratio": round(top_ratio, 3),
            "ambiguous": ambiguous,
            "instance_idxs": sorted(p.get("idx") for p in paras_in_cluster if p.get("idx") is not None),
        }

        for p in paras_in_cluster:
            p["structural_role_id"] = cluster_id
            p["display_role"] = display
            p["role"] = cluster_id
            p["structure_role"] = cluster_id

    return role_registry


CANONICAL_CLUSTERING_PROMPT = """당신은 양식 paragraph들에 structural cluster ID를 할당하는 전문가입니다 (1e).

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (분석 / 추론 / 자연어 설명 / cluster description) 은 반드시 한국어.
- 자체 표현에 한자 (`業務`, `行政`) / 일본어 가나 (`付き`) / 외국어 단어 (`cloud`) 사용 금지.
- 양식 sample 글자 인용은 그대로 옮김 — 자체 표현과 인용 구분.

## 핵심 목적

이 단계는 **grammar/rule extraction (1f) 용 structural node type clustering** 입니다.

## 임무

확정된 parent_first tree 위에서, 각 paragraph에 **cluster_id (numerical 0, 1, 2, ...)** 를 할당하라.

## 같은 cluster 기준 (강제 — hard constraint)

cluster 는 **structural role 단위로 나눈다.**

paragraph 두 개는 다음 조건을 **모두** 만족할 때만 같은 cluster:

1. **normalized marker** 가 같다 (마커 정규화 후).
2. **level** 이 같다.
3. **chapter_partition** 이 같다 (chapter_id).
4. **부모 paragraph 의 marker / level / structural role** 이 모두 같다.
5. **같은 부모 구조 안에서 같은 반복 위치 / 기능** 을 가진다.

## 다른 cluster 강제 (예외 없음 — hard)

다음 중 **하나라도 다르면 반드시 다른 cluster. 절대 통합 금지**:

1. **normalized marker** 가 다름 (정규화 후) — 같은 패밀리만 통합 (*, **, *** 는 같은 마커. ➊ ➋ ➌ 도 같은 마커). **`*` 와 `ㅇ` 는 절대 다른 cluster. `*` 와 `①` 도 절대 다른 cluster. `*` 와 `1)` 도 절대 다른 cluster. `▪` 와 `①` 도 절대 다른 cluster.** 마커 family 가 다르면 무조건 분리.
2. **level** 이 다름
3. **chapter_partition** 이 다름 (chapter_id)
4. **부모 paragraph 의 marker / level / structural role** 중 하나가 다름

위 4가지는 **제안 X. 강제 X. hard constraint**. 보조 신호 (paraPrIDRef / description / 자식 수) 가 일치해도 위 4가지 중 하나 다르면 무조건 분리.

⚠️ **자주 발생하는 wrong (절대 X)**:
- `*` + `**` 통합 = OK (같은 마커 family 정규화)
- `*` + `ㅇ` 통합 = **wrong** (다른 마커 family — 절대 다른 cluster)
- `*` + `①` 통합 = **wrong** (다른 마커 family)
- `▪` + `1)` + `①` 통합 = **wrong** (3개 다 다른 마커 family — 각각 다른 cluster)
- `➊` + `▪` 통합 = **wrong** (다른 마커 family)

## 처리 순서 — level 낮은 것부터 (parent_first)

cluster 결정은 **양식 트리의 root 부터 자식 순서로** 진행하세요. 즉 level 낮은 paragraph 부터 cluster 결정.

이유: 부모 cluster 가 같은 paragraph 들의 자식들은 같은 부모 구조 — cluster 결정 힌트.

순서:
1. level 0 paragraph 들의 cluster 결정 (대분류 — chapter title 등).
2. 다음 level 로 내려가면서 동일하게 결정.

**자기 cluster 결정 시 부모 paragraph 의 marker / level / role 비교** — 부모 marker / level / role 모두 같으면 자기는 같은 cluster 후보. 하나라도 다르면 자기도 다른 cluster.

(※ 1e 단계에서는 부모의 cluster 자체는 아직 결정 안 된 상태로 입력에 없음. parent_marker / parent_level / parent_role 만 비교.)

## 보조 신호 (단독으로 split X)

다음 신호들은 **단독으로 분리 X**. 위 4가지 (marker / level / chapter / 부모 구조) 와 함께 안정적 결합 시만 다른 structural role 신호:

- **paraPrIDRef / charPrIDRef** — 단순 ID 차이 ≠ 구조 차이.
  - 차이가 같은 sibling 위치 / 같은 기능 슬롯에서 반복 결합 → 구조 슬롯 신호 (분리 후보)
  - 한두 instance 에만 우연히 나타남 → 작성 실수 / 미세 조정 (통합 가능)
- **description (의미)** — 형식 신호 같으면 통합. description 만 다른 슬롯 (시퀀스 본체 vs trailing summary 등) 이면 다른 신호와 함께 분리 후보.
- **자식 구성** — 같은 부모 / 마커 / chapter 면 자식 수·종류 차이 무시 (optional / repeatable 정상). 자식 구성 차이 + 다른 신호 결합 시만 분리 후보.

## 절대 금지 (위반 시 cluster 결과 전체 무효)

- ❌ **marker family 다른데 같은 cluster** (`*` vs `ㅇ` / `*` vs `①` / `▪` vs `1)` / `➊` vs `*` 등) — **무조건 분리**. 같은 부모 / 같은 level / 같은 chapter 라도 마커 family 다르면 다른 cluster.
- ❌ **level 다른데 같은 cluster**.
- ❌ **chapter_partition 다른데 같은 cluster** (chapter root 예외만).
- ❌ **부모 marker / level / role 다른데 같은 cluster**.
- ❌ 보조 신호 단독으로 분리.
- ❌ 1b/1c 가 준 role 이름을 cluster 정답으로 가정.
- ❌ 외부 convention (한국 문서 등) 정답으로 가정.

## 마지막 자기 점검 (출력 직전 필수)

JSON 출력 만든 직후 자기 출력을 다시 훑어서:

1. 각 cluster 안 paragraph 들의 marker 가 정규화 후 **모두 같은 family** 인가? 다른 family 가 섞여있으면 즉시 분리.
2. 각 cluster 안 level 이 **모두 같은가**? 다르면 분리.
3. 각 cluster 안 chapter_id 가 **모두 같은가** (chapter root 예외 제외)? 다르면 분리.
4. 각 cluster 안 parent marker / level / role 이 **모두 같은가**? 다르면 분리.

위 4 가지 점검 거치지 않은 출력은 wrong. 한 cluster 안에 다른 marker family 가 한 글자라도 섞여있으면 batch 전체 실패로 처리됩니다.

## 마커 정규화 규칙 (마커 비교 시 반드시 적용)

- *, **, *** → 같은 마커 "*"
- ➊, ➋, ➌ → 같은 마커 "➊"
- ①, ②, ③ → 같은 마커 "①"
- 1), 2), 3) → 같은 마커 "1)"
- 󰊱, 󰊲, 󰊳 → 같은 마커 "󰊱"
- 종류가 같으면 같은 마커. 번호/반복횟수 차이는 무시.

## 표지 / header 특수 슬롯 (보조 신호 단독 분리 룰의 예외)

- 표지의 제목/날짜/기관명 같은 **고정 슬롯**: 마커 없음 + level 0 + 자식 없음 + 그룹 내 paraPrIDRef 가 서로 모두 다른 경우 — 각자 고유 서식의 고정 슬롯이므로 **반드시 별도 cluster 분리**.
- 이건 일반 paraPrIDRef 단독 분리 X 룰의 예외 (표지 슬롯은 같은 marker 없음 + 같은 level + 같은 chapter 라 위 4가지 룰만으로는 분리 불가).

## chapter_id 분리 (자세히)

각 paragraph entry에 `ch=N` 표시:
- `ch=0, 1, 2, ...`: 양식의 N번째 chapter 안의 paragraph
- `ch=-1`: chapter 밖 (표지/header/footer/TOC/container/preserve)

각 paragraph entry 에 `is_chapter_root` flag 도 있음 (chapter title paragraph 면 true).

### 기본 — chapter 내부 body paragraph
- 다른 chapter_id 면 **다른 cluster** (hard, singleton 생겨도 무조건 분리).
- chapter 안 body paragraph 와 ch=-1 (표지/header) 끼리도 같은 marker 라도 다른 cluster.

### 예외 — chapter root (is_chapter_root=true)
각 chapter 의 chapter title paragraph 만 예외. 다음 모두 충족 시 chapter_id 무관 통합 가능:
- normalized marker 같음
- 부모 paragraph 의 marker / level / role 같음 (TOC / 표지 container 같은 부모)
- level 같음

이유: 모든 chapter title 이 같은 marker 정책 / 같은 grammar 노드로 처리되어야 일관 작동.

**chapter root 예외는 is_chapter_root=true 인 paragraph 에만 적용**. 일반 body paragraph 는 chapter_id 분리 룰 그대로.

## 입력

각 paragraph 에 대해 다음 정보가 주어집니다:
- idx, level, marker, marker_family, description
- parent_idx, children_idxs, sibling_idxs (tree 구조)
- **ch** (chapter_id): 0, 1, 2, ... 또는 -1 (chapter 밖)
- **is_chapter_root** (bool): chapter title paragraph 면 true. chapter root 예외 적용 대상.
- **parent_marker, parent_level, parent_role**: 부모 paragraph 의 정보 (부모 구조 비교용)
- 1b role_candidates (참고용, 정답 X)
- 1c selected_role, parent_hint_idx (참고용)
- paraPrIDRef, charPrIDRef (보조 신호 — 단독 분리 X)

※ **자기 paragraph 의 cluster 는 출력이라 입력에 없음**. 부모의 cluster 도 결정 중. parent_role / marker / level 만 비교.

## 출력 형식 (JSON 만)

```json
{
  "clusters": [
    {
      "cluster_id": 0,
      "paragraph_idxs": [0, 5, 9],
      "rationale": "최상위 단독 무마커, 표지 위치, 자식 없음"
    },
    {
      "cluster_id": 1,
      "paragraph_idxs": [1, 6, 10],
      "rationale": "level 1 무마커, 자식 다수 가짐, chapter 시작점"
    }
  ]
}
```

- cluster_id 는 **numerical** (의미 이름 X). 0부터 시작, 연속 정수.
- paragraph_idxs: 그 cluster 에 속한 모든 idx 나열
- **모든 paragraph 가 정확히 한 cluster 에 속해야 함** (누락/중복 없이)
- rationale: debug 용 짧게 (다운스트림 사용 X)

## 중요

- 반드시 JSON 만 출력
- 모든 paragraph idx 빠짐없이 분류
- 같은 idx 가 여러 cluster 에 들어가지 않도록
- semantic taxonomy 가 아니라 **structural node type clustering** 이라는 점 잊지 말 것
"""


def build_canonical_clustering_prompt(
    paragraphs: list[dict],
    role_candidates: dict = None,
    decisions: dict = None,
) -> list[dict]:
    """
    1e prompt 구성. paragraph 데이터 + tree 구조 + 1b/1c 참고 정보를 표 형식으로.

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    from collections import defaultdict

    role_candidates = role_candidates or {}
    decisions = decisions or {}

    # children/siblings 그래프 계산
    parent_to_kids: dict = defaultdict(list)
    for p in paragraphs:
        parent_to_kids[p.get("parent_idx")].append(p.get("idx"))

    # idx → paragraph lookup (부모 정보 + chapter root 식별용)
    idx_to_p = {p.get("idx"): p for p in paragraphs}

    # chapter root paragraphs 식별 — 각 chapter_id 의 첫 paragraph
    # (level 1 + 부모가 ch=-1 container or root)
    chapter_id_to_root: dict = {}
    for p in paragraphs:
        cid = p.get("chapter_id", -1)
        if cid is None or cid < 0:
            continue
        parent_p = idx_to_p.get(p.get("parent_idx"))
        parent_cid = parent_p.get("chapter_id", -1) if parent_p else -1
        if parent_cid == -1 or parent_p is None:
            if cid not in chapter_id_to_root:
                chapter_id_to_root[cid] = p.get("idx")

    table_lines = []
    table_lines.append(
        "# Paragraph table — idx | L | ch | root | marker | family | parent | "
        "parent_marker | parent_L | parent_role | kids | sibs | "
        "1b_top | 1c_sel | hint | description | paraPr | charPr"
    )
    table_lines.append(
        "# ch = chapter_id (0-based, -1=chapter 밖). root = is_chapter_root (true 면 chapter title — chapter root 예외 적용 대상)."
    )
    table_lines.append(
        "# parent_marker / parent_L / parent_role = 부모 paragraph 정보. 같은 cluster 기준 (마커+부모구조+chapter+level) 비교에 사용."
    )
    for p in paragraphs:
        idx = p.get("idx")
        level = p.get("level")
        chapter_id = p.get("chapter_id", -1)
        marker = p.get("marker", "") or ""
        family = p.get("marker_family", "") or ""
        parent = p.get("parent_idx")
        is_root = idx == chapter_id_to_root.get(chapter_id) if chapter_id is not None and chapter_id >= 0 else False
        kids = parent_to_kids.get(idx, [])
        all_sibs = parent_to_kids.get(parent, [])
        sibs = [s for s in all_sibs if s != idx]

        # 부모 paragraph 정보
        parent_p = idx_to_p.get(parent) if parent is not None else None
        parent_marker = (parent_p.get("marker") or "") if parent_p else ""
        parent_level = parent_p.get("level") if parent_p else None
        parent_role = (parent_p.get("role") or "") if parent_p else ""

        # 1b candidates (top 2)
        cands = role_candidates.get(idx) or role_candidates.get(str(idx)) or []
        if isinstance(cands, list) and cands:
            top_cands = ", ".join(
                f"{c.get('role','?')}({c.get('score','?')})" for c in cands[:2]
            )
        else:
            top_cands = ""

        # 1c decision
        d = decisions.get(idx) or decisions.get(str(idx)) or {}
        sel_idx = d.get("selected_index", 0)
        if isinstance(cands, list) and cands and 0 <= sel_idx < len(cands):
            selected_role = cands[sel_idx].get("role", "?")
        else:
            selected_role = ""
        hint_idx = d.get("parent_hint_idx")

        desc = p.get("description") or ""
        if len(desc) > 80:
            desc = desc[:80] + "…"

        paraPr = p.get("paraPrIDRef") or ""
        charPr = p.get("charPrIDRef") or ""

        kids_str = str(kids[:6]) if len(kids) <= 6 else f"{kids[:6]}+{len(kids)-6}"
        sibs_str = str(sibs[:6]) if len(sibs) <= 6 else f"{sibs[:6]}+{len(sibs)-6}"

        line = (
            f"{idx} | L{level} | ch={chapter_id} | root={str(is_root).lower()} | "
            f"{marker!r} | {family} | parent={parent} | "
            f"pmarker={parent_marker!r} | pL={parent_level} | prole={parent_role} | "
            f"kids={kids_str} | sibs={sibs_str} | "
            f"{top_cands} | sel={selected_role} | hint={hint_idx} | "
            f"{desc!r} | pp={paraPr} | cp={charPr}"
        )
        table_lines.append(line)

    table_text = "\n".join(table_lines)
    user_msg = (
        "## 양식 paragraph 데이터 (tree 구조 + 참고 정보)\n\n"
        f"```\n{table_text}\n```\n\n"
        f"전체 {len(paragraphs)}개 paragraph 모두 cluster에 할당. JSON만 출력."
    )

    return [
        {"role": "system", "content": CANONICAL_CLUSTERING_PROMPT},
        {"role": "user", "content": user_msg},
    ]


CANONICAL_CLUSTERING_REPAIR_PROMPT = """당신은 이전 1e structural clustering 결과를 **의미 검증 + 정정** 하는 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 sample 글자 인용은 그대로 — 자체 표현과 인용 구분.

## 핵심 목적

**이전 1e 의 모든 cluster 를 다시 의미 검증하고, 위반 발견 시 즉시 분리하라.**

- 이전 1e 가 의미 룰 (marker family / parent / chapter / level) 위반한 cluster 만들었을 가능성 매우 높음. 당신은 그걸 찾아서 분리하는 책임.
- **issues 가 비어있어도 무조건 모든 cluster 검토**. "고칠 게 없네" 하지 말 것 — 의미 위반은 issues 에 안 잡혀있음.
- idx 누락 / 중복 / extra 오류도 동시 정정 (secondary).

## 다른 cluster — hard constraint (강제. 제안 X. 예외 없음.)

다음 중 **하나라도 다르면 반드시 다른 cluster. 절대 통합 금지**:

1. **normalized marker family 다름** — `*` 와 `ㅇ` / `*` 와 `①` / `▪` 와 `1)` / `➊` 와 `*` / `□` 와 `ㅇ` **절대 다른 cluster**. 같은 family 만 통합 가능 (예: `*`/`**`/`***` 는 같은 family. `1`/`2`/`3` 같은 sequence 도 같은 family. `Ⅰ`/`Ⅱ`/`Ⅲ` 도 같은 family).
2. **level 다름** — level 4 와 level 5 절대 다른 cluster.
3. **chapter_partition 다름** — chapter_id 다른 paragraph 절대 다른 cluster (chapter root paragraph 자체 예외 외).
4. **부모 paragraph 의 cluster 다름** — 가장 강한 기준. 같은 marker `ㅇ` 라도 부모가 `cluster_6 (Ⅰ장 □)` 와 `cluster_11 (Ⅱ장 □)` 면 절대 다른 cluster. 같은 marker `▪` 라도 부모가 `cluster_19 (➊)` 와 `cluster_21 (*)` 면 절대 다른 cluster.
5. **부모 paragraph 의 marker / level / structural role 중 하나 다름**.

**위 5가지는 hard constraint. 보조 신호 (paraPrIDRef / description / 자식 수) 일치해도 위 5가지 중 하나 다르면 무조건 분리. 제안 X. 강제.**

## 같은 cluster 조건 (모두 충족 시만 통합 가능)

1. normalized marker family 같다.
2. level 같다.
3. chapter_partition 같다 (chapter root 예외).
4. 부모 paragraph 의 cluster 같다.
5. 부모 paragraph 의 marker / level / role 모두 같다.
6. 같은 부모 구조 안에서 같은 반복 위치 / 기능.

## 요구사항

1. **이전 1e 의 모든 cluster 다시 검증**. issues 유무 무관. 의미 룰 위반 발견 시 분리.
2. **누락 / 중복 / extra idx 오류** 동시 정정. 누락 idx 는 적절한 cluster 에 배정 (singleton 남발 X). 중복 idx 는 가장 적절한 한 cluster 만 남김. extra idx 는 제거.
3. **모든 input paragraph idx 가 정확히 한 번씩** 등장 (100%).
4. **이전 cluster 가 의미 룰 만족 + idx 형식 OK 면 그대로 유지**. 룰 위반된 cluster 만 분리.

## 자주 발생하는 1e wrong (반드시 분리)

이전 1e 가 만들 가능성이 높은 wrong 패턴 — 발견 시 즉시 분리:

- **같은 marker 인데 부모 cluster 가 둘 이상** → 부모 cluster 별로 분리.
  예: `cluster_X (marker=ㅇ)` 의 paragraph 중 부모 cluster 가 `cluster_A` / `cluster_B` 두 가지 → `cluster_X_a (부모=A)`, `cluster_X_b (부모=B)` 로 분리.
- **같은 marker 인데 chapter_id 가 둘 이상** (chapter root 예외 외) → chapter 별로 분리.
- **같은 marker 인데 level 이 둘 이상** → level 별로 분리.
- **marker family 가 한 cluster 안에 두 개 이상** → family 별로 분리.

## 자기 점검 (필수 — 출력 직전)

JSON 출력 만든 직후 자기 출력을 훑어서 **각 cluster 안**:

1. marker family 모두 같은가? (정규화 후) — 다르면 즉시 분리.
2. level 모두 같은가? — 다르면 즉시 분리.
3. chapter_id 모두 같은가? (chapter root 예외) — 다르면 즉시 분리.
4. **부모 cluster 모두 같은가?** (이전 1e 결과 기준 — 가장 중요) — 다르면 즉시 분리.
5. 부모 marker / level / role 모두 같은가? — 다르면 즉시 분리.

위 5가지 한 가지라도 위반된 cluster 한 개라도 남아있으면 wrong. **출력 직전 한 번 더 자기 검토하라.**

## 입력

- 전체 paragraph 데이터 (1e 와 동일 형식 — parent_marker / parent_level / parent_role 포함)
- **이전 1e cluster 출력** (cluster_id 별 paragraph_idxs) — 검증 대상. 각 paragraph 의 부모 cluster 는 이 출력의 parent_idx 의 cluster_id 로 확인.
- 발견된 idx issues (missing/duplicate/extra) — secondary (의미 위반은 여기 안 들어있음. 당신이 직접 찾아라).

## 보조 신호 (단독 분리 X)

다음은 보조 신호 — 단독으로는 분리 근거 X:
- paraPrIDRef / charPrIDRef
- child 개수
- description 의미 차이

보조 신호는 위 hard constraint (marker / level / chapter / parent) 와 같이 일치할 때만 통합 근거. 단독으로 다르다고 분리하지 마라.

## 금지

- ❌ 위 5가지 hard constraint 중 하나라도 다른데 같은 cluster 묶기 — 절대 X.
- ❌ "issues 가 비어있으니 그대로 유지" — 의미 위반은 issues 에 없음. 직접 검토하라.
- ❌ 1b/1c role 이름 / 외부 convention 정답 가정.
- ❌ 보조 신호 단독으로 분리.

## 출력 형식 (JSON 만)

```json
{
  "clusters": [
    {
      "cluster_id": 0,
      "paragraph_idxs": [...],
      "rationale": "..."
    },
    ...
  ]
}
```

- 수정 내용 별도 설명 금지 — corrected 결과 JSON 만 출력
- 모든 cluster_id 와 paragraph_idxs 다시 작성 (변경 없는 cluster 도 포함)
- 반드시 JSON 만
"""


def build_canonical_clustering_repair_prompt(
    paragraphs: list[dict],
    previous_clusters: list[dict],
    issues: list,
    role_candidates: dict = None,
    decisions: dict = None,
) -> list[dict]:
    """
    1e repair prompt — validation 오류 수정용.

    이전 1e 결과에 누락/중복/extra idx 발생 시 LLM 재호출.
    기존 cluster 구조 유지하면서 오류만 수정.
    """
    # 기본 1e prompt 의 paragraph table 재사용
    base_prompt = build_canonical_clustering_prompt(paragraphs, role_candidates, decisions)
    user_table_msg = base_prompt[1]["content"]

    # 이전 cluster 결과
    prev_clusters_text = "## 이전 1e cluster 출력\n\n```json\n"
    import json as _json
    prev_clusters_text += _json.dumps({"clusters": previous_clusters}, ensure_ascii=False, indent=2)
    prev_clusters_text += "\n```\n"

    # issues
    issues_text = "## 발견된 validation 오류\n\n"
    for issue in issues:
        issues_text += f"- {issue}\n"

    user_msg = (
        f"{user_table_msg}\n\n"
        f"{prev_clusters_text}\n\n"
        f"{issues_text}\n\n"
        "위 오류를 수정한 corrected cluster 출력을 JSON 으로 작성하라. "
        "**모든 idx 가 정확히 한 번씩** 등장해야 함."
    )

    return [
        {"role": "system", "content": CANONICAL_CLUSTERING_REPAIR_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_canonical_clustering_from_llm(
    llm_response: str,
    expected_idxs: set[int],
) -> dict:
    """
    1e LLM 응답 파싱 + 검증 + cluster_id normalization.

    Validation:
        - 모든 expected_idxs가 정확히 한 cluster에 속해야 (누락/중복 X)
        - cluster_id를 0부터 연속 정수로 normalize

    Returns:
        {
            "cluster_map": {paragraph_idx: cluster_id (int)},
            "clusters": [{cluster_id, paragraph_idxs, rationale}],
            "issues": [validation 문제 리스트],
            "raw_clusters_count": int,  # LLM이 처음 준 cluster 수
        }

    Raises:
        ValueError: JSON 파싱 실패 또는 critical validation 실패
    """
    import json as _json

    # JSON 추출
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', llm_response)
    if json_match:
        raw_json = json_match.group(1)
    else:
        json_match = re.search(r'(\{[\s\S]*\})', llm_response)
        if not json_match:
            raise ValueError("1e: JSON not found in LLM response")
        raw_json = json_match.group(0)

    try:
        parsed = _json.loads(raw_json)
    except _json.JSONDecodeError as e:
        repaired = _repair_json(raw_json)
        try:
            parsed = _json.loads(repaired)
        except _json.JSONDecodeError:
            raise ValueError(f"1e: JSON parsing failed: {e}")

    raw_clusters = parsed.get("clusters", [])
    if not raw_clusters:
        raise ValueError("1e: empty clusters list")

    # 검증 — 누락/중복 idx
    issues = []
    seen: dict = {}
    for cluster in raw_clusters:
        cid = cluster.get("cluster_id")
        idxs = cluster.get("paragraph_idxs", [])
        for pidx in idxs:
            if pidx in seen:
                issues.append(
                    f"duplicate idx {pidx} in clusters {seen[pidx]} and {cid}"
                )
            seen[pidx] = cid

    missing = expected_idxs - set(seen.keys())
    if missing:
        issues.append(f"missing paragraph idxs: {sorted(missing)[:20]}")

    extra = set(seen.keys()) - expected_idxs
    if extra:
        issues.append(f"unknown paragraph idxs: {sorted(extra)[:20]}")

    # cluster_id를 0부터 연속 정수로 normalize
    # 정렬 기준: 각 cluster의 minimum paragraph_idx
    # 빈 paragraph_idxs를 가진 cluster 제거 (AI가 빈 배열 반환 시)
    raw_clusters = [c for c in raw_clusters if c.get("paragraph_idxs")]
    clusters_sorted = sorted(
        raw_clusters,
        key=lambda c: min(c.get("paragraph_idxs", [10**9]))
    )

    old_to_new = {}
    for new_id, c in enumerate(clusters_sorted):
        old_to_new[c.get("cluster_id")] = new_id

    cluster_map = {pidx: old_to_new[old_cid] for pidx, old_cid in seen.items()}

    normalized_clusters = []
    for c in clusters_sorted:
        old_cid = c.get("cluster_id")
        normalized_clusters.append({
            "cluster_id": old_to_new[old_cid],
            "paragraph_idxs": sorted(
                pidx for pidx in c.get("paragraph_idxs", []) if pidx in expected_idxs
            ),
            "rationale": c.get("rationale", ""),
            "original_cluster_id": old_cid,
        })

    return {
        "cluster_map": cluster_map,
        "clusters": normalized_clusters,
        "issues": issues,
        "raw_clusters_count": len(raw_clusters),
    }


def apply_structural_clustering(
    paragraphs: list[dict],
    cluster_map: dict,
    clusters: list[dict],
) -> dict:
    """
    1e 결과 (cluster_map + clusters)를 paragraph에 적용.

    paragraph["structural_role_id"] = "role_cluster_N"
    paragraph["display_role"] = cluster 내 가장 빈번한 1b semantic_role
    paragraph["role"] = cluster_id (downstream 호환)
    paragraph["structure_role"] = cluster_id

    Returns:
        role_registry: {cluster_id_str: {display_role, instance_count,
                                          semantic_role_distribution,
                                          paraPrIDRef_distribution,
                                          display_role_ratio, ambiguous,
                                          rationale, instance_idxs}}
    """
    from collections import Counter

    role_registry: dict = {}
    idx_set_per_cluster: dict = {}
    for c in clusters:
        idx_set_per_cluster[c["cluster_id"]] = set(c["paragraph_idxs"])

    paras_by_cluster: dict = {cid: [] for cid in idx_set_per_cluster}
    for p in paragraphs:
        cid = cluster_map.get(p.get("idx"))
        if cid is not None and cid in paras_by_cluster:
            paras_by_cluster[cid].append(p)

    for cluster_int_id in sorted(paras_by_cluster.keys()):
        paras_in_cluster = paras_by_cluster[cluster_int_id]
        cluster_id_str = f"role_cluster_{cluster_int_id}"

        sem_roles = Counter(
            (p.get("semantic_role") or "unknown") for p in paras_in_cluster
        )
        para_prs = Counter(
            (p.get("paraPrIDRef") or "") for p in paras_in_cluster
        )

        if sem_roles:
            top_role, top_count = sem_roles.most_common(1)[0]
            display = top_role
            top_ratio = top_count / sum(sem_roles.values())
        else:
            display = "unknown"
            top_ratio = 0.0

        # rationale 찾기
        rationale = ""
        for c in clusters:
            if c["cluster_id"] == cluster_int_id:
                rationale = c.get("rationale", "")
                break

        role_registry[cluster_id_str] = {
            "cluster_id_int": cluster_int_id,
            "display_role": display,
            "instance_count": len(paras_in_cluster),
            "semantic_role_distribution": dict(sem_roles),
            "paraPrIDRef_distribution": dict(para_prs),
            "display_role_ratio": round(top_ratio, 3),
            "ambiguous": top_ratio < 0.6,
            "rationale": rationale,
            "instance_idxs": sorted(
                p.get("idx") for p in paras_in_cluster if p.get("idx") is not None
            ),
        }

        for p in paras_in_cluster:
            p["structural_role_id"] = cluster_id_str
            p["display_role"] = display
            p["role"] = cluster_id_str
            p["structure_role"] = cluster_id_str

    return role_registry


# ═══════════════════════════════════════════════════════════════
# Tree Rebuild (1g) — cluster 확정 후 트리 재구성 (별도 LLM)
# ═══════════════════════════════════════════════════════════════

TREE_REBUILD_PROMPT = """당신은 양식 paragraph 의 **tree (parent_idx + level)** 를 재구성하는 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 sample 글자 인용은 그대로.

## 핵심 목적

이전 1c 단계가 paragraph 단위로 level + parent 를 추론했으나 wrong 가능성 큼. 1c 결과는 wrong 트리.
당신의 역할은 **트리를 고치는 것** — 텍스트 내용 + cluster 정보 + paragraph 위치 모두 보고 부모-자식 관계를 다시 잘 만든다.

## 부모-자식 관계 정의 (양방향)

- **자식**: 부모의 내용을 **설명 / 부연 / 구체화 / 예시 / 근거 제시** 하는 paragraph.
- **부모**: 자식들의 내용을 **포괄 / 도입 / 요약 / 묶는 헤딩** 역할의 paragraph.

→ A 가 B 의 자식이라면 **B 는 A 를 포괄**, **A 는 B 를 설명**. 양방향 다 성립해야.

## parent_idx 판단 — local_anchor 최우선

**parent_idx 는 local_anchor 를 최우선으로 한다.**
marker / 번호 체계, cluster_id, 1c hint 는 모두 local_anchor 판단의 **보조 신호**다.

### local_anchor 정의

- 같은 chapter 안에서 현재 paragraph 보다 앞에 있고,
- 현재 paragraph 를 **의미상 직접 포괄**하는 paragraph,
- 중간에 더 가까운 직접 부모 후보가 없는 paragraph.

직접 부모 후보가 여러 개면:
- 가장 가까운 이전 paragraph 우선.
- 넓게 포괄하는 오래된 heading 보다, 바로 앞의 구체 heading / 번호 항목 우선.

**예외**: **같은 local enumeration block 안의 같은 series 직전 항목은 local_anchor 후보에서 제외**한다.
- 예: 같은 block 안에서 ➋ 의 local_anchor 는 직전 ➊ 이 **아니라**, ➊ 과 ➋ 를 함께 묶는 상위 paragraph.
- 즉 같은 block 안 ➊ 의 parent 가 󰊳 이면, ➋ 의 local_anchor 도 󰊳.
- **local enumeration block** = 같은 series 항목들이 **중간에 상위 heading 없이** 같은 상위 부모 아래 함께 등장하는 영역. 중간에 더 상위 heading 또는 다른 묶음 heading 나오면 새 block.

## cluster_id 의 의미 (중요)

`cluster_id` 는 **시각적 / 서식상 역할** 을 나타내는 참고 정보다.

- **`cluster_id` 는 parent_idx 를 강제하지 않는다**.
- 같은 cluster_id paragraph 는 같은 시각적 역할일 가능성이 높지만, **같은 부모를 가져야 한다는 뜻은 아니다**.
- 같은 cluster_id 라도 서로 다른 local_anchor 아래에서 반복될 수 있다.
- parent_idx 는 cluster_id 일관성보다 **local_anchor 와 텍스트 의미** 를 우선한다.

예: `* (국내)` 와 `* 공급망관리` 가 같은 cluster_id 라도, local_anchor 가 다르면 parent_idx 가 다르다.

## input

각 paragraph 마다:
- `idx`: paragraph 위치
- `chapter_id`: 속한 chapter
- `cluster_id`: 1e + repair 가 확정한 시각적 cluster (참고 정보)
- `marker`: paragraph 앞 마커
- `text`: paragraph 본문

각 paragraph 의 1c hint:
- `1c_hint_parent_idx`: 1c 가 추론한 부모 idx (wrong 가능 — 마지막 참고)
- `1c_hint_level`: 1c 가 추론한 level (wrong 가능)

## 임무

각 paragraph 에 대해 **parent_idx + level** 결정.

- `parent_idx`: 의미상 부모 paragraph 의 idx. 부모 없으면 null.
- `level`: 단일 룰.
  - **parent_idx = null → level = 0**.
  - **parent_idx 있음 → level = 부모 paragraph 의 level + 1**.

## hard constraint (강제. 위반 시 wrong)

1. **parent_idx 는 자기 idx 보다 작은 정수 or null**. self-loop / forward reference 금지.
2. **level 단일 룰**: parent_idx=null → level=0. parent_idx 있음 → level=parent.level+1.
3. **모든 paragraph 의 parent_idx + level 출력**. 누락 X.
4. **chapter_id 가 다른 paragraph 를 parent 로 잡지 마라** (chapter root 예외).
   - 단 chapter root (chapter 의 최상위) 의 parent 는 다른 chapter 또는 null 가능.
5. **cycle 금지**.
6. **같은 local enumeration block 안의 같은 series 항목은 서로 parent-child 가 될 수 없다**.
   - 적용 대상 (명확한 순번 묶음): `➊/➋/➌/➍`, `1)/2)/3)`, `가)/나)/다)`, `(1)/(2)/(3)`, `①/②/③`, `1./2./3.`, `ⅰ/ⅱ/ⅲ`.
   - 특수 기호형 순번 (`󰊱/󰊲/󰊳` 등): 같은 local enumeration block 안에서 순번으로 쓰인 게 명확할 때만 적용.
   - 적용 제외: `*`/`**`/`***`, `□/◇/◈`, `ㅇ/▪` 같은 비순번 마커.
   - **local enumeration block** = 같은 series 항목들이 중간에 더 상위 heading / 다른 묶음 heading 없이 같은 상위 부모 아래 함께 등장하는 영역.
   - 같은 block 안 현재 paragraph 의 `parent_idx` 는 직전 같은 series 항목의 `parent_idx` 와 **같다**.
   - 같은 block 안 현재 paragraph 의 `level` 은 직전 같은 series 항목의 `level` 과 **같다**.
   - 예: 같은 block 안 `➊` 의 parent 가 `󰊳` 이면, `➋` 의 parent 도 `󰊳`. `➋` 는 `➊` 의 자식이 **아니다**.
   - **block 이 다르면** (예: 문서 멀리 떨어진 다른 묶음의 ➋) 이 룰 적용 X. 새 block 의 첫 항목은 자기 local_anchor 따로 결정.

## 자식 판단 — 구체 패턴

다음 경우 A 는 B 의 자식:

1. **B 가 헤딩 / 번호 제목** ("1 업무추진", "Ⅱ . ...", "[전략 1]" 등) 이고 A 가 그 본문 / 부연 / 설명 / 예시.
2. **B 가 박스 / 요약 paragraph** 이고 A 가 그 박스의 세부 내용.
3. **B 가 도입 / 개요** 이고 A 가 그 안에서 다루는 항목.
4. **A 가 B 직후 enumeration item** (1, 2, 3 / ➊, ➋, ➌ / * 등) 인데 B 가 그 enumeration 을 묶는 헤딩.

→ 모든 경우 공통: **B 가 A 를 포괄, A 가 B 를 설명**.

## 형제 판단

A 와 B 가 형제 (같은 parent) 인 조건:

- **같은 local_anchor 아래에서 병렬 나열**.

같은 cluster_id 거나 같은 marker family (`*` 와 `**` 등) 라도, **local_anchor 가 다르면 다른 부모**. marker 만 보고 형제 확정 X.

## 1c hint 활용

1c hint 는 **마지막 참고**.

- hint 가 local_anchor + 텍스트 의미 판단에 부합하면 따른다.
- hint 가 cluster 일관성만 근거로 **더 가까운 직접 부모를 건너뛰면 무시한다**.
- hint 의 level 이 같은 cluster 안에서 일관되더라도, **local_anchor 가 다르면 따르지 않는다**.

## 자기 점검 (출력 직전 필수)

1. 모든 paragraph idx 가 정확히 한 번씩 등장.
2. parent_idx 가 자기보다 작은 정수 or null.
3. level 룰 만족: parent_idx=null → level=0. parent_idx 있음 → level=parent.level+1.
4. 각 paragraph 의 parent_idx 가 **가장 가까운 의미상 직접 부모** 인가? 더 가까운 후보 건너뛰고 오래된 heading 에 붙이지 않았는가?
5. 같은 cluster_id paragraph 인데 parent 가 다른 경우 — **local_anchor 가 다른 반복 구조** 인지 확인 (다르면 OK, 같은데 parent 다르면 재검토).
6. cycle 없음.
7. chapter_id 다른 paragraph 를 parent 로 잡지 않음 (chapter root 예외).
8. **같은 local enumeration block 안 같은 series 항목끼리 parent-child 로 연결되지 않음**. 예: 같은 block 안 `➋` 의 parent 가 `➊` 이면 wrong → `➊` 의 parent 로 정정.

위 8 가지 한 가지라도 위반 시 wrong. 재검토 후 출력.

## 출력 형식 (JSON 만)

```json
{
  "paragraphs": [
    {"idx": 0, "parent_idx": null, "level": 0},
    {"idx": 1, "parent_idx": null, "level": 0},
    {"idx": 3, "parent_idx": null, "level": 0},
    {"idx": 4, "parent_idx": 3, "level": 1},
    {"idx": 5, "parent_idx": 4, "level": 2},
    ...
  ]
}
```

(예시: idx=0/1/3 = 표지 / 차례 — parent null, level 0. idx=4 = 차례 자식, level 1. idx=5 = idx=4 자식, level 2. **level = parent.level + 1 일관**.)

- 모든 idx 등장
- 별도 설명 금지 — JSON 만
- 반드시 JSON 만
"""


def build_tree_rebuild_prompt(
    paragraphs: list[dict],
    decisions: dict,
    idx_texts: dict,
) -> list[dict]:
    """
    Tree rebuild (1g) prompt 구성.

    Args:
        paragraphs: 1e+repair 가 확정한 paragraph list (cluster_id 포함)
        decisions: 1c decisions (parent_hint_idx + level)
        idx_texts: idx (str 또는 int) → 본문 텍스트 매핑. **필수**.

    Returns:
        [{"role": "system", "content": TREE_REBUILD_PROMPT},
         {"role": "user", "content": "..."}]

    Raises:
        ValueError: paragraph 의 idx / cluster_id / text 매칭 wrong 발견 시.
    """
    if not idx_texts:
        raise ValueError("build_tree_rebuild_prompt: idx_texts is required (text source).")

    # idx 순서 sort (paragraph list 가 idx 순서 안 일치할 가능성 방어)
    paras_sorted = sorted(
        (p for p in paragraphs if p.get("idx") is not None),
        key=lambda x: x["idx"],
    )

    # idx → text 매핑. str / int 양쪽 시도. 빈 텍스트 "" 정상 (간격 paragraph).
    # idx 자체가 없으면 wrong → 빈 문자열로 대체 (silent skip 안전).
    def _text_of(idx: int) -> str:
        t = idx_texts.get(str(idx))
        if t is None:
            t = idx_texts.get(idx)
        if t is None:
            t = ""
        return str(t).replace("\n", " ").replace("|", "/").strip()

    # cluster_id 검증 (None 인 paragraph 없어야)
    for p in paras_sorted:
        cid = p.get("structural_role_id")
        if not cid:
            raise ValueError(
                f"build_tree_rebuild_prompt: paragraph idx={p.get('idx')} has no structural_role_id"
            )

    lines = []
    lines.append("# 양식 paragraph 정보")
    lines.append("")
    lines.append("형식: idx | chapter_id | cluster_id | marker | text")
    lines.append("")

    for p in paras_sorted:
        idx = p["idx"]
        ch = p.get("chapter_id")
        if ch is None:
            ch = ""
        cid = p["structural_role_id"]
        mk = (p.get("marker") or "").replace("|", "/")
        text = _text_of(idx)
        lines.append(f"{idx} | {ch} | {cid} | {mk} | {text}")

    lines.append("")
    lines.append("# 1c 가 추론한 힌트 (wrong 가능 — 참고만)")
    lines.append("")
    lines.append("형식: idx | 1c_hint_parent_idx | 1c_hint_level")
    lines.append("")

    for p in paras_sorted:
        idx = p["idx"]
        d = decisions.get(idx) or decisions.get(str(idx)) or {}
        hint_parent = d.get("parent_hint_idx")
        if hint_parent is None:
            hint_parent = d.get("parent_idx")
        hint_level = d.get("level")
        # paragraph 에 직접 있는 경우도 fallback (decisions 안 들어간 케이스 방어)
        if hint_parent is None:
            hint_parent = p.get("parent_idx")
        if hint_level is None:
            hint_level = p.get("level")
        lines.append(f"{idx} | {hint_parent} | {hint_level}")

    lines.append("")
    lines.append("위 정보를 보고 hard constraint 와 의미 가이드 따라 트리를 재구성하세요.")
    lines.append("**JSON 만 출력**.")

    return [
        {"role": "system", "content": TREE_REBUILD_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_tree_rebuild_from_llm(
    llm_response: str,
    expected_idxs: set,
) -> dict:
    """
    Tree rebuild LLM 응답 파싱 + validation.

    Validation:
        - 모든 expected_idxs 등장 (한 번씩)
        - parent_idx 가 자기 idx 보다 작은 정수 or null
        - level 정수 >= 0
        - cycle 없음 (parent chain backward only 라서 by construction 없음)

    Returns:
        {
            "tree": {idx: {"parent_idx": int|None, "level": int}},
            "issues": list[str]  # 비어있어야 정상
        }

    Raises:
        ValueError: JSON 파싱 실패 또는 critical validation 실패.
    """
    import json as _json

    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', llm_response)
    if json_match:
        raw_json = json_match.group(1)
    else:
        json_match = re.search(r'(\{[\s\S]*\})', llm_response)
        if not json_match:
            raise ValueError("tree_rebuild: JSON not found in LLM response")
        raw_json = json_match.group(0)

    try:
        parsed = _json.loads(raw_json)
    except _json.JSONDecodeError as e:
        repaired = _repair_json(raw_json)
        try:
            parsed = _json.loads(repaired)
        except _json.JSONDecodeError:
            raise ValueError(f"tree_rebuild: JSON parsing failed: {e}")

    raw_rows = parsed.get("paragraphs", [])
    if not raw_rows:
        raise ValueError("tree_rebuild: empty paragraphs list")

    issues = []
    tree = {}
    seen = set()
    for row in raw_rows:
        idx = row.get("idx")
        if idx is None:
            issues.append(f"row missing idx: {row}")
            continue
        if idx in seen:
            issues.append(f"duplicate idx {idx}")
            continue
        seen.add(idx)

        parent = row.get("parent_idx")
        if parent is not None:
            if not isinstance(parent, int):
                try:
                    parent = int(parent)
                except (TypeError, ValueError):
                    issues.append(f"idx {idx}: invalid parent_idx {parent!r}")
                    parent = None
            if parent is not None and parent >= idx:
                issues.append(f"idx {idx}: forward/self ref parent={parent}")
                parent = None

        level = row.get("level")
        if not isinstance(level, int):
            try:
                level = int(level)
            except (TypeError, ValueError):
                issues.append(f"idx {idx}: invalid level {level!r}")
                level = 0
        if level < 0:
            issues.append(f"idx {idx}: negative level {level}")
            level = 0

        tree[idx] = {"parent_idx": parent, "level": level}

    missing = expected_idxs - seen
    if missing:
        issues.append(f"missing paragraph idxs: {sorted(missing)[:30]}")

    extra = seen - expected_idxs
    if extra:
        issues.append(f"unknown paragraph idxs: {sorted(extra)[:30]}")

    # level 무결성 검증: parent_idx=null → level=0. parent_idx 있음 → level=parent.level+1.
    for idx, node in tree.items():
        parent = node["parent_idx"]
        level = node["level"]
        if parent is None:
            if level != 0:
                issues.append(f"idx {idx}: parent_idx=null but level={level} (expected 0)")
        else:
            if parent not in tree:
                issues.append(f"idx {idx}: parent_idx={parent} not in tree")
            else:
                expected_level = tree[parent]["level"] + 1
                if level != expected_level:
                    issues.append(
                        f"idx {idx}: level={level}, expected={expected_level} "
                        f"(parent={parent} level={tree[parent]['level']})"
                    )

    return {"tree": tree, "issues": issues}


def apply_tree_rebuild_to_paragraphs(
    paragraphs: list[dict],
    tree: dict,
) -> list[dict]:
    """
    Tree rebuild 결과 (parent_idx + level) 를 paragraph 에 적용.

    paragraph["parent_idx"] = tree[idx]["parent_idx"]
    paragraph["level"] = tree[idx]["level"]
    paragraph["sibling_group_id"] = "roots" or "children_of_{pid}"

    Returns:
        paragraphs (in-place mutation + return).

    Raises:
        ValueError: idx 매칭 wrong (paragraph idx 가 tree 에 없거나 반대).
    """
    para_idx_set = {p.get("idx") for p in paragraphs if p.get("idx") is not None}
    tree_idx_set = set(tree.keys())

    missing_in_tree = para_idx_set - tree_idx_set
    if missing_in_tree:
        raise ValueError(
            f"apply_tree_rebuild: paragraph idxs missing from tree: "
            f"{sorted(missing_in_tree)[:30]}"
        )
    extra_in_tree = tree_idx_set - para_idx_set
    if extra_in_tree:
        raise ValueError(
            f"apply_tree_rebuild: tree has unknown idxs: "
            f"{sorted(extra_in_tree)[:30]}"
        )

    for p in paragraphs:
        idx = p.get("idx")
        if idx is None:
            continue
        t = tree[idx]
        p["parent_idx"] = t["parent_idx"]
        p["level"] = t["level"]
        p["sibling_group_id"] = (
            "roots" if t["parent_idx"] is None
            else f"children_of_{t['parent_idx']}"
        )
    return paragraphs


def measure_tree_inconsistency(paragraphs: list[dict]) -> dict:
    """
    트리 내적 일관성 측정 — parent_idx와 level이 정합한가.

    각 paragraph p에 대해 p.level == parent.level + 1 (root이면 level==0)이
    성립하는지 검사. 어긋나면 inconsistency 1건.

    stack tree에선 "container만 push" 정책 때문에 leaf-only 노드를 건너뛰고
    더 위 ancestor와 parent_idx 연결됨 → level 갭 ≥ 2 발생 가능 (불일치).
    parent_first tree는 BFS로 level 재계산이라 by construction 일관.

    Returns:
      {
        "level_mismatch_count": int,
        "root_level_mismatch_count": int,    # parent_idx None인데 level != 0
        "details": [{idx, role, level, parent_idx, parent_level,
                     expected_level, gap}, ...],
      }
    """
    idx_to_p = {p.get("idx"): p for p in paragraphs}
    details = []
    root_mismatch = 0
    for p in paragraphs:
        parent_idx = p.get("parent_idx")
        level = p.get("level")
        if parent_idx is None:
            if level not in (0, None):
                root_mismatch += 1
                details.append({
                    "idx": p.get("idx"),
                    "role": p.get("role"),
                    "level": level,
                    "parent_idx": None,
                    "parent_level": None,
                    "expected_level": 0,
                    "gap": (level or 0) - 0,
                })
            continue
        parent = idx_to_p.get(parent_idx)
        if parent is None:
            continue
        plevel = parent.get("level")
        if plevel is None or level is None:
            continue
        expected = plevel + 1
        if level != expected:
            details.append({
                "idx": p.get("idx"),
                "role": p.get("role"),
                "level": level,
                "parent_idx": parent_idx,
                "parent_level": plevel,
                "expected_level": expected,
                "gap": level - expected,
            })
    return {
        "level_mismatch_count": len(details),
        "root_level_mismatch_count": root_mismatch,
        "details": details,
    }


def compute_tree_diff(stack_paragraphs: list[dict],
                       hint_paragraphs: list[dict],
                       core_idxs: set = None) -> dict:
    """
    stack_tree vs hint_tree edge difference + 분포 비교.

    Returns:
      {
        "total_paragraphs": int,
        "edge_change_count": int,
        "changed_edges": [{idx, role, stack_parent, hint_parent,
                           stack_level, hint_level, is_core}, ...],
        "stack_root_count": int,
        "hint_root_count": int,
        "level_dist_stack": {level: count},
        "level_dist_hint": {level: count},
      }
    """
    from collections import Counter

    stack_by_idx = {p.get("idx"): p for p in stack_paragraphs}
    hint_by_idx = {p.get("idx"): p for p in hint_paragraphs}
    core_set = core_idxs or set()

    changed = []
    for idx, sp in stack_by_idx.items():
        hp = hint_by_idx.get(idx, {})
        sparent = sp.get("parent_idx")
        hparent = hp.get("parent_idx")
        if sparent != hparent:
            changed.append({
                "idx": idx,
                "role": sp.get("role"),
                "stack_parent": sparent,
                "hint_parent": hparent,
                "stack_level": sp.get("level"),
                "hint_level": hp.get("level"),
                "is_core": idx in core_set,
            })

    stack_levels = Counter(p.get("level") for p in stack_paragraphs if p.get("level") is not None)
    hint_levels = Counter(p.get("level") for p in hint_paragraphs if p.get("level") is not None)
    stack_roots = sum(1 for p in stack_paragraphs if p.get("parent_idx") is None)
    hint_roots = sum(1 for p in hint_paragraphs if p.get("parent_idx") is None)

    return {
        "total_paragraphs": len(stack_paragraphs),
        "edge_change_count": len(changed),
        "changed_edges": changed,
        "stack_root_count": stack_roots,
        "hint_root_count": hint_roots,
        "level_dist_stack": dict(sorted(stack_levels.items())),
        "level_dist_hint": dict(sorted(hint_levels.items())),
    }


def reparent_leaf_prone_children(paragraphs: list[dict], container_scores: dict) -> tuple:
    """
    Weak parent (non-strong container)의 자식들을 strong container인 grandparent로 승격.

    조건:
      - parent role이 _is_strong_container False
      - grandparent role이 _is_strong_container True

    효과:
      - 자식의 parent_idx를 grandparent로 변경
      - level을 grandparent.level + 1로 조정
      - sibling_group_id 재계산

    한 단만 처리 (재귀 X). 입력 paragraphs in-place 수정. log 반환.
    """
    para_by_idx = {p.get("idx"): p for p in paragraphs}
    log = []
    for p in paragraphs:
        parent_idx = p.get("parent_idx")
        if parent_idx is None:
            continue
        parent = para_by_idx.get(parent_idx)
        if not parent:
            continue
        parent_role = parent.get("role", "")
        if _is_strong_container(parent_role, container_scores):
            continue
        gp_idx = parent.get("parent_idx")
        if gp_idx is None:
            continue
        gp = para_by_idx.get(gp_idx)
        if not gp:
            continue
        gp_role = gp.get("role", "")
        if not _is_strong_container(gp_role, container_scores):
            continue
        log.append({
            "child_idx": p.get("idx"),
            "child_role": p.get("role"),
            "old_parent_idx": parent_idx,
            "old_parent_role": parent_role,
            "new_parent_idx": gp_idx,
            "new_parent_role": gp_role,
        })
        p["parent_idx"] = gp_idx
        p["level"] = (gp.get("level", 0) or 0) + 1
        p["sibling_group_id"] = f"children_of_{gp_idx}"
    return paragraphs, log


def compute_parent_and_sibling_from_levels(paragraphs: list[dict]) -> list[dict]:
    """
    level 시퀀스로부터 parent_idx + sibling_group_id를 stack 알고리즘으로 자동 계산.

    알고리즘:
    - 각 문단의 parent = 직전에 등장한 더 낮은 level 중 _can_be_parent True인 가장 가까운 문단
    - non-container role(summary_box/supplement 등)은 parent 후보에서 skip → 그 위 level로 올라감
    - sibling_group_id = `children_of_<parent_idx>` (root는 `roots`)
    - level 별 stack 유지: 현재 level보다 깊은 entry는 scope 종료

    원본 paragraphs를 in-place 수정.
    """
    # stack 기반 parent 계산
    # 모든 role을 stack에 push — level이 정확하면 parent-child 관계가 자동으로 맞음
    # (이전의 container 필터링은 level이 잘못된 경우를 보정하려 했으나,
    #  올바른 parent-child까지 망가뜨리는 부작용이 있어 제거)
    level_stack = {}

    for p in paragraphs:
        level = p.get("level")
        if level is None:
            p["parent_idx"] = None
            p["sibling_group_id"] = "roots"
            continue
        try:
            level = int(level)
        except Exception:
            p["parent_idx"] = None
            p["sibling_group_id"] = "roots"
            continue

        # 부모 찾기: 직전에 나온 level-1 이하의 가장 가까운 문단
        parent = None
        for l in range(level - 1, -1, -1):
            if l in level_stack:
                parent = level_stack[l]
                break

        p["parent_idx"] = parent.get("idx") if parent else None
        if p["parent_idx"] is None:
            p["sibling_group_id"] = "roots"
        else:
            p["sibling_group_id"] = f"children_of_{p['parent_idx']}"

        # 현재 level보다 깊은 stack 정리
        for deeper in [k for k in level_stack if k > level]:
            del level_stack[deeper]

        level_stack[level] = p

    return paragraphs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1f: Marker policy induction (role-level, post-clustering)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MARKER_POLICY_PROMPT = """당신은 양식의 role별 **마커(marker) 정책**과 **표 종류(table_kind)**를 판별하는 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (분석·추론·자연어 설명) 은 반드시 한국어.
- 자체 표현에 한자·일본어 가나·외국어 단어 사용 금지.
- 양식 sample 의 마커 글자 (예: `Ⅰ`, `□`, `①` 등) 와 sample 글자 인용은 그대로 옮김.

## 임무 1: marker_policy 판별

각 role에 대해, 해당 role의 text samples를 보고 **일관된 leading marker가 있는지** 판별하세요.

1. **각 sample의 텍스트 앞부분**에서 marker 후보를 찾으세요.
   - marker: 텍스트 시작 부분의 기호·번호 (□, ◈, Ⅰ, ➊, 1., 가., *, (1) 등)
   - marker 뒤에는 보통 공백이나 구분자(`. `, ` `)가 옴
   - marker가 없는 sample도 있을 수 있음 (no_marker)

2. **role 전체에서 일관성 확인**:
   - 모든 sample이 같은 marker → `fixed_char` (예: □, ◈)
   - 순차적 marker 시퀀스 → sequence 타입 (예: Ⅰ→Ⅱ→Ⅲ, 1→2→3, ➊→➋→➌)
   - marker 없음 → `no_marker`
   - 일부만 있거나 일관성 없음 → `ambiguous`

3. **separator**: marker와 content 사이의 구분자 (공백, `. `, `) ` 등)

## 임무 2: table_kind 판별 — 1f 가 최종 책임

각 role에 대해, 해당 role의 sample 중 `tbl` 필드가 있는 sample을 보고 표가 **장식 박스**인지 **진짜 데이터 표**인지 판별하세요. `tbl` 필드는 해당 paragraph가 자기 안에 `<hp:tbl>` element를 자식으로 포함한다는 뜻입니다.

※ 1a 는 표의 셀 위치만 기록. **table_kind (decorative_box / real_table / not_applicable) 의 최종 판단은 1f 의 임무**.

판별 기준:
- **`decorative_box`**: 표를 텍스트 강조·박스·배너 목적으로 사용. cell 안 텍스트가 paragraph 본문 텍스트와 일치 또는 부분 분할일 뿐, 데이터 구조 X.
  - 예: paragraph_text="[전략1] 활력이 넘치는 역동적 조달시장", cell_texts=["[전략1] 활력이 넘치는 역동적 조달시장"] (1×1 박스)
  - 예: paragraph_text="1 업무추진 여건", cell_texts=["1", "", "업무추진 여건"] (1×3 번호+제목 분할)
  - 예: paragraph_text="2024년 주요업무 추진계획", cell_texts=["", "2024년 주요업무 추진계획", ""] (3×1 상하 padding)
  - 공통: cell 텍스트 합치면 paragraph 본문과 (공백 제외) 거의 동일. 표가 paragraph의 외피 역할만 함.
- **`real_table`**: 행/열에 독립적 데이터 (매출표, 일정표, 비교표 등). cell 데이터가 paragraph 본문에 종속 X. 표 구조 자체가 정보의 본질.
- **`not_applicable`**: 해당 role의 어떤 sample에도 tbl 없음 (`tbl` 필드 자체가 없음). table_kind 판단 불필요.

판별 시 cell 수가 1, 3, 또는 그 이상인 것은 단독 기준 아님 — **cell 안 내용이 paragraph 본문과 의미적으로 일치하는지** 가 핵심.

## 출력 형식 (JSON만)

```json
{
  "roles": [
    {
      "role": "role_cluster_4",
      "marker_policy_status": "explicit_marker_detected",
      "policy_type": "roman_sequence",
      "marker_family": "roman",
      "separator": " . ",
      "confidence": 0.95,
      "uncertainty_reason": null,
      "evidence": [
        {"sample_idx": 4, "detected_marker": "Ⅰ", "remaining_text": "추진성과 및 평가"},
        {"sample_idx": 21, "detected_marker": "Ⅱ", "remaining_text": "2024년 업무추진 여건 및 방향"}
      ],
      "table_kind": "decorative_box",
      "table_kind_reason": "cell 텍스트가 paragraph 본문과 일치 — 박스 강조용"
    }
  ]
}
```

## policy_type 목록 (이 중에서만 선택)

- `fixed_char`: 모든 sample이 같은 기호 (□, ◈, ◇, ▪, ㅇ 등)
- `arabic_sequence`: 1, 2, 3, ...
- `roman_sequence`: Ⅰ, Ⅱ, Ⅲ, ...
- `circled_sequence`: ➊, ➋, ➌, ...
- `circled_num_sequence`: ①, ②, ③, ...
- `circled_pua_sequence`: 󰊱, 󰊲, 󰊳, ... (PUA 영역)
- `num_paren_sequence`: 1), 2), 3), ...
- `star_depth`: *, **, *** (반복 깊이)
- `korean_sequence`: 가., 나., 다., ...
- `unknown_sequence`: 위에 해당 안 되는 순차 패턴
- `no_marker`: marker 없음

## table_kind 목록 (이 중에서만 선택)

- `decorative_box`
- `real_table`
- `not_applicable`

## 규칙

- **모든 role에 대해 빠짐없이 출력** (marker_policy + table_kind 둘 다 필수)
- marker가 확실하지 않으면 `ambiguous`로 표시하세요. 억지로 분류하지 마세요.
- confidence는 0~1. sample 수가 적으면 낮게 (1개: 0.5 이하, 2개: 0.6~0.7, 3개+: 0.7~0.95)
- evidence에 각 sample별로 detected_marker를 남기세요 (없으면 null)
- table_kind가 `not_applicable`이면 table_kind_reason은 null 또는 생략 가능
- 반드시 JSON만 출력
"""


def _extract_paragraph_tbl_info_from_xml(xml_str: str) -> dict:
    """light_xml에서 각 top-level paragraph(`_idx` 속성 있는 것)의 자식 tbl 정보 추출.

    1f table_kind 판별용. paragraph가 자기 안에 tbl element를 자식으로 가질 때
    그 tbl이 장식 박스인지 진짜 표인지 AI가 판단할 근거 데이터 제공.

    Returns:
        {idx (int): {"tbl_count": int, "paragraph_text_preview": str,
                     "tbl_summaries": [{"row_count", "col_count_max",
                                        "cell_count", "cell_texts": [str]}]}}
        tbl 없는 paragraph는 dict에 포함 X.
    """
    if not xml_str:
        return {}
    try:
        root = etree.fromstring(xml_str.encode("utf-8")) if isinstance(xml_str, str) else etree.fromstring(xml_str)
    except Exception as e:
        log.warning(f"_extract_paragraph_tbl_info_from_xml parse fail: {e}")
        return {}

    info: dict = {}
    for p in root.iter(f"{NS_HP}p"):
        idx_attr = p.get("_idx")
        if idx_attr is None:
            continue
        try:
            idx = int(idx_attr)
        except (TypeError, ValueError):
            continue
        # 자식 tbl 수집 (paragraph 본인 안의 tbl만; section 다른 paragraph 자식 X)
        tbls = [c for c in p.iter() if c.tag == f"{NS_HP}tbl"]
        if not tbls:
            continue
        # paragraph 전체 텍스트 (자기 + 자식 tbl 안 t 모두 포함)
        para_text = "".join(t.text or "" for t in p.iter(f"{NS_HP}t")).strip()
        tbl_summaries = []
        for tbl in tbls:
            rows = list(tbl.iter(f"{NS_HP}tr"))
            cells = list(tbl.iter(f"{NS_HP}tc"))
            cell_texts = []
            for c in cells:
                ct = "".join(t.text or "" for t in c.iter(f"{NS_HP}t")).strip()
                cell_texts.append(ct[:80])
            tbl_summaries.append({
                "row_count": len(rows),
                "col_count_max": max(
                    (len(list(r.iter(f"{NS_HP}tc"))) for r in rows),
                    default=0,
                ),
                "cell_count": len(cells),
                "cell_texts": cell_texts,
            })
        info[idx] = {
            "tbl_count": len(tbls),
            "paragraph_text_preview": para_text[:120],
            "tbl_summaries": tbl_summaries,
        }
    return info


def build_marker_policy_prompt(
    paragraphs: list[dict],
    idx_texts: dict,
    light_xml: str = "",
    max_samples_per_role: int = 5,
) -> list[dict]:
    """
    1f: role별 sample text preview → marker policy + table_kind induction prompt 생성.

    light_xml이 제공되면 각 sample의 tbl 자식 정보를 prompt에 첨부 → AI가
    table_kind 판별 (decorative_box vs real_table). light_xml 없으면 marker policy만.
    """
    from collections import defaultdict

    # light_xml 있으면 idx별 tbl 정보 추출 (없으면 빈 dict — 모든 sample tbl 없음 처리)
    paragraph_tbl_info = _extract_paragraph_tbl_info_from_xml(light_xml) if light_xml else {}

    # role → sample indices 수집
    role_samples = defaultdict(list)
    for p in paragraphs:
        role = p.get("role", "")
        if role:
            role_samples[role].append(p.get("idx"))

    # role별 text preview + tbl 정보 구성
    role_entries = []
    for role, idxs in sorted(role_samples.items()):
        samples = []
        for idx in idxs[:max_samples_per_role]:
            text = idx_texts.get(str(idx), idx_texts.get(idx, ""))
            sample_entry: dict = {
                "idx": idx,
                "text_preview": text[:80] if text else "(빈 문단)",
            }
            # tbl 정보가 있으면 첨부 (없으면 필드 자체 생략 → AI가 not_applicable로 판단)
            tbl_info = paragraph_tbl_info.get(idx)
            if tbl_info is None and isinstance(idx, int):
                tbl_info = paragraph_tbl_info.get(str(idx))
            if tbl_info:
                sample_entry["tbl"] = tbl_info
            samples.append(sample_entry)
        role_entries.append({
            "role": role,
            "sample_count": len(idxs),
            "samples": samples,
        })

    user_msg = (
        "## role별 text samples\n\n"
        "(`tbl` 필드가 있는 sample은 해당 paragraph가 자기 안에 `<hp:tbl>` element를 자식으로 포함.\n"
        " cell_texts와 paragraph_text_preview를 비교해서 table_kind 판별.)\n\n"
        + json.dumps(role_entries, ensure_ascii=False, indent=2)
        + "\n\n위 role들의 marker_policy + table_kind를 판별하세요. 반드시 JSON만 출력."
    )

    return [
        {"role": "system", "content": MARKER_POLICY_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_marker_policy_from_llm(llm_response: str) -> dict:
    """1f LLM 응답 파싱."""
    text = llm_response.strip()

    # JSON 추출
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(text))
        except Exception as e:
            log.warning(f"1f marker policy JSON 파싱 실패: {e}")
            return {"roles": [], "parse_error": str(e)}

    roles = parsed.get("roles", [])
    return {"roles": roles}


def verify_marker_policy_evidence(
    policy_result: dict,
    idx_texts: dict,
) -> dict:
    """
    1f AI 결과의 evidence를 idx_texts와 교차검증.

    각 role entry에 verification 필드 추가:
    - "consistent": claimed marker가 실제 text에 존재
    - "marker_not_found": claimed marker가 text에 없음
    - "no_evidence": evidence가 비어있음
    """
    for role_entry in policy_result.get("roles", []):
        status = role_entry.get("marker_policy_status", "")
        evidence = role_entry.get("evidence", [])

        if status == "no_marker" or status == "ambiguous":
            role_entry["verification"] = "not_applicable"
            continue

        if not evidence:
            role_entry["verification"] = "no_evidence"
            continue

        all_consistent = True
        for ev in evidence:
            idx = ev.get("sample_idx")
            claimed = ev.get("detected_marker", "")
            actual = idx_texts.get(str(idx), idx_texts.get(idx, ""))

            if claimed and actual:
                ev["_actual_starts_with"] = actual.lstrip().startswith(claimed)
                if not ev["_actual_starts_with"]:
                    all_consistent = False
            elif claimed and not actual:
                ev["_actual_starts_with"] = False
                all_consistent = False

        role_entry["verification"] = "consistent" if all_consistent else "marker_not_found"

    return policy_result


def _validate_selected_index(p: dict) -> dict:
    """
    1c가 정한 selected_index 검증. 다음 조건 위반 시 index 0으로 fallback:
    - 선택된 후보의 score >= 0.50
    - 1순위와의 score 차이 <= 0.20
    - reason_code 비어있지 않음

    반환: {"valid": bool, "fallback": bool, "issue": str}
    """
    sel_idx = p.get("selected_role_candidate_index", 0)
    if not sel_idx or sel_idx == 0:
        return {"valid": True, "fallback": False, "issue": ""}

    cands = p.get("role_candidates", [])
    if not cands or sel_idx >= len(cands):
        return {"valid": False, "fallback": True, "issue": "candidate index out of range"}

    selected_score = cands[sel_idx].get("score", 0.0)
    top_score = cands[0].get("score", 0.0)
    reason = p.get("selection_reason_code", "")

    issues = []
    if selected_score < 0.50:
        issues.append(f"selected score {selected_score:.2f} < 0.50")
    if (top_score - selected_score) > 0.20:
        issues.append(f"score diff {top_score - selected_score:.2f} > 0.20")
    if not reason:
        issues.append("reason_code empty")

    if issues:
        return {"valid": False, "fallback": True, "issue": "; ".join(issues)}
    return {"valid": True, "fallback": False, "issue": ""}


def _validate_and_split(structure: dict) -> dict:
    """
    Code validator — AI가 놓친 구조 충돌 자동 보정.

    적용 룰:
    R1. 같은 structure_role인데 marker_family 다르면 split (실은 합성에서 자동 처리됨, 검증만)
    R2. 같은 sibling_group 안에 marker_family 섞이면 경고 로그
    R3. 같은 structure_role이 너무 넓은 level_band에 퍼지면 경고 로그
    R4. selected_index != 0인데 reason_code 없으면 경고 로그
    """
    from collections import defaultdict
    paragraphs = structure.get("paragraphs", [])

    # R1: structure_role → marker_family set 점검
    role_families = defaultdict(set)
    for p in paragraphs:
        sr = p.get("structure_role", "")
        mf = p.get("marker_family", "")
        if sr:
            role_families[sr].add(mf)
    r1_issues = [(sr, fams) for sr, fams in role_families.items() if len(fams) > 1]
    for sr, fams in r1_issues:
        log.warning(f"[VALIDATOR R1] structure_role={sr} 가 여러 marker_family에 걸침: {fams}")

    # R2: sibling_group 안 marker_family 섞임 점검
    sibling_families = defaultdict(set)
    for p in paragraphs:
        sg = p.get("sibling_group_id", "")
        mf = p.get("marker_family", "")
        if sg and mf:
            sibling_families[sg].add(mf)
    for sg, fams in sibling_families.items():
        if len(fams) > 1:
            log.info(f"[VALIDATOR R2] sibling_group={sg} 에 마커 family 섞임: {fams} (정상일 수도)")

    # R3: structure_role이 너무 넓은 level에 퍼짐 점검
    role_levels = defaultdict(set)
    for p in paragraphs:
        sr = p.get("structure_role", "")
        lv = p.get("level", -1)
        if sr and lv >= 0:
            role_levels[sr].add(lv)
    for sr, levels in role_levels.items():
        if len(levels) >= 3:
            log.warning(
                f"[VALIDATOR R3] structure_role={sr} 가 너무 넓은 level에 분포: {sorted(levels)}"
            )

    # R4: selected_index != 0인데 reason_code 없으면
    for p in paragraphs:
        sel_idx = p.get("selected_role_candidate_index", 0)
        if sel_idx and sel_idx != 0 and not p.get("selection_reason_code"):
            log.info(
                f"[VALIDATOR R4] idx={p.get('idx')}: selected_index={sel_idx}인데 reason_code 없음"
            )

    structure["validator_issues"] = {
        "r1_role_family_conflict": [{"structure_role": sr, "families": list(fams)} for sr, fams in r1_issues],
        "r2_sibling_mixed_count": len([s for s, fs in sibling_families.items() if len(fs) > 1]),
        "r3_role_level_spread_count": len([sr for sr, lvs in role_levels.items() if len(lvs) >= 3]),
    }
    return structure


# ──────────────────────────────────────────────────────────────────────
# 1c: Role 분류 (level·marker·description 기반)
# ──────────────────────────────────────────────────────────────────────

ROLE_CLASSIFICATION_PROMPT = """당신은 양식 문단의 **role 분석** 전문가입니다 (1b).
각 문단을 독립적으로 보고 가능한 **semantic_role 후보들**을 점수화합니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (role 이름 / description / reason) 은 반드시 한국어.
- 자체 표현에 한자 (`業務`, `行政`) / 일본어 가나 (`付き`) / 외국어 단어 (`cloud`) 사용 금지.
- 양식 sample 글자 인용은 그대로 옮김 — 자체 표현과 인용 구분.

## 역할 분담
- **1b (이 단계)**: semantic_role 후보 + 점수 (level·hierarchy 결정 안 함)
- 1c (다음 단계): 전체 시퀀스 + 후보 → level + 후보 index 선택

## 후보 갯수 — 동적 (2026-05-25)

명확도에 따라 후보 갯수 조절:

- **명확한 문단** (마커 분명, role 흐름 안정) → **후보 1 개** 허용. 가짜 차선책 만들지 X.
- **애매한 문단** (마커 모호, role 충돌, 자리 다중 해석 가능) → 후보 2~3 개. 1c 가 선택할 여지 남김.

⚠️ **억지 다중 후보 금지**. 차선책이 진짜 가능할 때만 추가. 1순위가 명백하면 1 개로.

⚠️ **reason 짧게**. 길게 풀어쓰지 말고 핵심 신호 한 줄. confidence 만 남기는 것도 OK.

## 핵심 개념 분리
당신은 **semantic_role(의미)**만 다룬다. 다음은 별도 시스템이 처리:
- `marker_family` (표면 패턴): 코드가 자동 추출 → 입력에 포함됨
- `level/depth` (구조 깊이): AI 2가 결정
- `structure_role` (signature용): 코드가 `marker_family + semantic_role`로 합성

→ **다른 marker_family를 가진 문단을 같은 semantic_role로 묶어도 됨** (구조 기능이 같으면 marker_family는 별도 신호로 보존됨). 코드가 marker_family + semantic_role을 따로 트래킹.

## 입력 features (코드 계산)
- marker, marker_family, description
- prev/next marker(family), same_paraPr_run, paraPrIDRef

## 임무 (규칙)

각 문단에 대해 **1~3개 후보**를 출력 (명확도에 따라 동적):

### 규칙 R1: 후보 갯수 동적
- **명확한 문단** (마커 분명, role 흐름 안정, 표지·날짜처럼 unique role) → **후보 1 개** OK.
- **애매한 문단** (마커 모호, role 충돌, 다중 해석 가능) → 2~3 개. 1c 에게 선택 여지.
- **억지 차선책 금지** — 진짜 가능한 차선만. 가짜 후보 X.

### 규칙 R2: 점수 범위 0.55~0.85 주로 사용
- 0.9+ 거의 안 씀 (over-confident 금지)
- 1순위 0.65~0.80, 2순위 0.50~0.65 정도가 자연스러움
- 점수 낮은 후보(< 0.4)는 제외

### 규칙 R3: marker_family 보존 후보 강제 포함
- marker가 있으면, **그 marker_family에 자연스러운 semantic_role 후보를 반드시 1개 이상 포함**

### 규칙 R3.5: marker_family는 힌트일 뿐, 의미는 데이터에서 관찰

- marker family는 role 후보 판단의 **보조 신호**일 뿐이다. 특정 기호 = 특정 role이라고 가정하지 말 것.
- 같은 양식 안에서 같은 marker family가 반복적으로 수행하는 기능을 paragraph 시퀀스 전체에서 관찰할 것.
- 주변 문단과의 관계, 들여쓰기, 반복 패턴, 내용상 역할을 함께 보고 role 후보 제안.
- 특정 기호 → 특정 role 1순위라는 사전 룰을 적용하지 말 것. 같은 기호도 양식·문맥에 따라 다른 의미 가능.

**무마커(텍스트 박스 등) — 애매 case 면 후보 여러 개**:
- 무마커 제목 박스가 양식 안에서 여러 위계로 등장 가능 → 위치 / 위계 모호하면 후보 2~3 개 (1c 가 위치로 고를 수 있게).
- 같은 description ("제목" · "항목 제목") 인데 다른 위계 가능 → 후보 다양화.
- 단 명백한 단일 위계 (예: 표지 단독 제목) → 후보 1 개 OK.

### 규칙 R4: 후보 다양성 — 의미적으로 다른 가능성 제시
- 차선책은 **의미적으로 구별되는** 후보로 제시 (예: `bullet_item` vs `detail_item`, `note` vs `supplement_note`)
- ❌ marker_family를 박은 이름 금지 (`square_marker_item`, `dingbat_numbered` 등) — R5 위반

### 규칙 R5: semantic_role 이름 — pure 의미만
- ✓ `bullet_item`, `numbered_item`, `note`, `summary_box`, `header`, `footnote`
- ❌ `square_bullet_item` (marker family 박힘 — 코드가 합성), `note_l5` (level 박힘)

### 규칙 R6: reason은 짧게
- 어떤 신호로 그 후보 줬는지 한 줄

## 출력 형식 (JSON만)

```json
{
  "paragraphs": [
    {
      "idx": 0,
      "candidates": [
        {"role": "cover_title_box", "score": 0.78, "reason": "최상위 단독, 표지 description"},
        {"role": "document_title", "score": 0.62, "reason": "큰 글자 단독 헤더"}
      ]
    },
    {
      "idx": 5,
      "candidates": [
        {"role": "section_header", "score": 0.74, "reason": "독립 헤더 description, 같은 패턴 인스턴스 다수가 자식 가짐"},
        {"role": "task_title", "score": 0.61, "reason": "장 단위 제목 위치"}
      ]
    },
    {
      "idx": 12,
      "candidates": [
        {"role": "detail_item", "score": 0.71, "reason": "직전 항목보다 깊은 들여쓰기 + 본문성 description"},
        {"role": "supplement_note", "score": 0.62, "reason": "직전 항목 보충 의미"}
      ]
    }
  ]
}
```

## 중요
- **모든 idx 출력** (빠뜨리지 마세요)
- 각 문단 후보 갯수 **명확하면 1 개, 애매하면 2~3 개** (R1)
- 점수 0.55~0.85 범위 (R2)
- semantic_role 이름엔 marker_family·level 박지 마라 (R5)
- reason 짧게, 또는 생략 가능 (R6)
- 반드시 JSON만 출력
"""


def build_role_classification_prompt(
    structure: dict, signals: dict = None
) -> list[dict]:
    """
    1c 호출 (AI 1, local): 각 문단에 role 후보 + 점수 부여.

    Args:
        structure: paragraphs는 compute_paragraph_features로 enrichment 권장
                   (marker_family, prev/next marker, same_paraPr_run 등)
        signals: compute_role_context_signals 결과 (선택, text preview 용도)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    paragraphs = structure.get("paragraphs", [])

    text_by_idx = {}
    if signals:
        for pt in signals.get("paragraph_texts", []):
            text_by_idx[pt.get("idx")] = pt.get("text", "")

    para_lines = []
    for p in paragraphs:
        idx = p.get("idx", -1)
        marker = p.get("marker", "")
        desc = p.get("description", "")
        marker_family = p.get("marker_family", "")
        prev_marker = p.get("prev_marker", "")
        next_marker = p.get("next_marker", "")
        prev_family = p.get("prev_marker_family", "")
        next_family = p.get("next_marker_family", "")
        same_paraPr = p.get("same_paraPr_run", False)
        para_pr = p.get("paraPrIDRef", "")

        marker_str = f'"{marker}"' if marker else '""'
        text_preview = text_by_idx.get(idx, "")[:60]

        feature_parts = [
            f'"idx": {idx}',
            f'"marker": {marker_str}',
            f'"marker_family": "{marker_family}"',
            f'"description": {json.dumps(desc, ensure_ascii=False)}',
            f'"paraPrIDRef": "{para_pr}"',
            f'"prev_marker": "{prev_marker}"',
            f'"prev_marker_family": "{prev_family}"',
            f'"next_marker": "{next_marker}"',
            f'"next_marker_family": "{next_family}"',
            f'"same_paraPr_run": {str(same_paraPr).lower()}',
        ]
        if text_preview:
            feature_parts.append(
                f'"text": {json.dumps(text_preview, ensure_ascii=False)}'
            )
        para_lines.append("{" + ", ".join(feature_parts) + "}")

    para_text = "[\n  " + ",\n  ".join(para_lines) + "\n]"

    user_msg = (
        "아래 문단 목록 각각에 대해 role 후보 + 점수를 출력하세요.\n"
        "- description의 의미 + marker_family + features 조합으로 판단\n"
        "- 위계(level) 결정 금지 — AI 2가 처리\n"
        "- 1~3개 후보, 점수 낮은 것(< 0.2) 제외\n\n"
        f"## 문단 목록\n```json\n{para_text}\n```\n\n"
        "반드시 JSON만 출력하세요."
    )

    return [
        {"role": "system", "content": ROLE_CLASSIFICATION_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_role_classification_from_llm(llm_response: str) -> dict:
    """
    1c (AI 1) LLM 응답에서 role 후보를 파싱.

    Returns:
        {idx: [{role, score, reason}, ...]} dict — 점수 내림차순 정렬
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("role 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"role JSON 파싱 실패: {e}")

    paras_list = data.get("paragraphs", []) if isinstance(data, dict) else data
    # 하위 호환: 옛 "roles" 키도 처리 (단일 role per idx)
    if not paras_list and isinstance(data, dict) and "roles" in data:
        legacy = data.get("roles", [])
        result = {}
        for e in legacy:
            if isinstance(e, dict) and e.get("idx") is not None and e.get("role"):
                result[int(e["idx"])] = [{"role": str(e["role"]), "score": 1.0, "reason": "legacy"}]
        log.info(f"role 후보 파싱 (legacy 형식): {len(result)}개 문단")
        return result

    if not isinstance(paras_list, list):
        raise ValueError(f"paragraphs가 배열이 아닙니다: {type(paras_list)}")

    result = {}
    for entry in paras_list:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        candidates = entry.get("candidates", [])
        if idx is None or not isinstance(candidates, list):
            continue
        norm_cands = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            role = c.get("role")
            score = c.get("score", 0.0)
            reason = c.get("reason", "")
            if role:
                try:
                    score = float(score)
                except Exception:
                    score = 0.0
                norm_cands.append({"role": str(role), "score": score, "reason": str(reason)})
        # 점수 내림차순
        norm_cands.sort(key=lambda x: -x["score"])
        if norm_cands:
            result[int(idx)] = norm_cands

    log.info(f"role 후보 파싱: {len(result)}개 문단, 평균 후보 {sum(len(v) for v in result.values())/max(len(result),1):.1f}개")
    return result


def merge_roles_into_structure(structure: dict, role_candidates: dict) -> dict:
    """
    structure.paragraphs에 role 후보 필드 병합.

    Args:
        role_candidates: parse_role_classification_from_llm 결과
                        {idx: [{role, score, reason}, ...]}

    각 문단에 추가:
    - role_candidates: 후보 리스트
    - role: 1순위 후보 (placeholder, AI 2가 final_role로 확정)
    """
    paragraphs = structure.get("paragraphs", [])
    for p in paragraphs:
        idx = p.get("idx", -1)
        cands = role_candidates.get(idx, [])
        if cands:
            p["role_candidates"] = cands
            # 1순위를 임시 role로 (AI 2가 final_role 결정)
            p["role"] = cands[0]["role"]
        else:
            p.setdefault("role", "")
    return structure


def compute_parent_instance_children(structure: dict) -> dict:
    """
    level이 배정된 structure에서 각 부모 role의 인스턴스별 직계 자식 집합을 추출.

    Returns:
        {parent_role: [frozenset(children)×N]}
        - 직계 자식이 2종 이상인 부모만 포함 (배타 판단 대상)
        - 부모 인스턴스가 2개 미만인 부모는 제외
    """
    from collections import defaultdict

    paragraphs = structure.get("paragraphs", [])
    if not paragraphs:
        return {}

    # 스택 기반으로 부모 인스턴스 추적
    # 각 인스턴스 키: (role, instance_id)
    instance_children = defaultdict(set)  # (role, inst_id) → set(직계 자식 role)
    role_instance_ids = defaultdict(list)  # role → [inst_id, ...]
    stack = []  # [(level, role, inst_id), ...]
    inst_counter = 0

    for p in paragraphs:
        role = p.get("role", "")
        level = p.get("level")
        if not role or level is None:
            continue

        # 상위 스택 정리
        while stack and stack[-1][0] >= level:
            stack.pop()

        # 직계 부모 있으면 자식으로 기록
        if stack:
            parent_level, parent_role, parent_inst = stack[-1]
            if level == parent_level + 1:
                instance_children[(parent_role, parent_inst)].add(role)

        # 이 문단을 스택에 추가 (부모가 될 수 있음)
        my_inst = inst_counter
        inst_counter += 1
        role_instance_ids[role].append(my_inst)
        instance_children[(role, my_inst)]  # 빈 세트라도 만들어둠
        stack.append((level, role, my_inst))

    # role별 자식 인스턴스 집합 수집
    result = {}
    for role, inst_ids in role_instance_ids.items():
        if len(inst_ids) < 2:
            continue  # 인스턴스 1개뿐이면 배타 판단 불가
        instances = [frozenset(instance_children[(role, iid)]) for iid in inst_ids]
        # 자식이 하나라도 있는 인스턴스만 고려 (빈 인스턴스는 무시 가능)
        non_empty = [inst for inst in instances if inst]
        if not non_empty:
            continue
        # 관측된 자식 종류 2종 이상인 경우만
        all_children = set()
        for inst in non_empty:
            all_children |= inst
        if len(all_children) < 2:
            continue
        result[role] = instances  # 빈 인스턴스 포함 (부모 수 정보 보존)
    return result


def _extract_indent_and_marker_data(para_elem) -> dict:
    """
    HWPX paragraph element에서 indent/marker 관련 원시 데이터 추출.

    Returns:
        {
          "indent_parts": [{"type": "tab"}, {"type": "space", "count": 2}, ...],
          "first_text_after_indent": "ㅇ 내용",  # 첫 비공백부터의 텍스트
          "is_blank": bool,  # 공백만 있으면 True
          "paraPrIDRef": str,
        }
    """
    result = {
        "indent_parts": [],
        "first_text_after_indent": "",
        "is_blank": True,
        "paraPrIDRef": para_elem.get("paraPrIDRef", "0"),
    }

    found_visible = False
    first_text = ""

    # run들을 문서 순서대로 순회하며 tab/text 수집
    for run in para_elem.findall(f"{NS_HP}run"):
        for child in run:
            tag = etree.QName(child).localname
            if tag == "tab":
                if not found_visible:
                    result["indent_parts"].append({"type": "tab"})
            elif tag == "t":
                text = child.text or ""
                if not found_visible:
                    stripped = text.lstrip(" ")
                    leading_spaces = len(text) - len(stripped)
                    if leading_spaces > 0:
                        result["indent_parts"].append({
                            "type": "space", "count": leading_spaces
                        })
                    if stripped:
                        found_visible = True
                        result["is_blank"] = False
                        first_text += stripped
                else:
                    first_text += text
        if found_visible:
            # 첫 run에서 text 찾았으면 더 이상 indent 수집 안 함
            pass

    # 표 배치 문단: 직접 run에 텍스트가 없으면 표 셀 내부 첫 텍스트를 fallback
    if not found_visible:
        for tbl in para_elem.iter(f"{NS_HP}tbl"):
            for t in tbl.iter(f"{NS_HP}t"):
                text = (t.text or "").strip()
                if text:
                    first_text = text
                    found_visible = True
                    result["is_blank"] = False
                    result["is_table_text"] = True
                    break
            if found_visible:
                break

    result["first_text_after_indent"] = first_text
    return result


def compute_format_observations(
    structure: dict, light_xml: str, idx_map: dict = None
) -> dict:
    """
    light_xml을 직접 파싱해서 1.5c 입력용 원시 관측 데이터를 만듦.

    - 각 role의 indent/marker/separator 샘플 (직계 XML 관측)
    - 연속 문단 쌍의 blank 존재 여부 + paraPrIDRef
      (light_xml은 blank 문단 포함 — truncate_xml에서 제거된 것까지 보임)

    Args:
        structure: 1.5a 이후 structure (paragraphs에 idx, role, level)
        light_xml: 경량화 전체 XML (blank 포함)
        idx_map: {ai_idx: real_idx} — AI가 본 truncated idx → light_xml _idx

    Returns:
        {
          "role_formats": {role: {indent_parts_samples, first_text_samples,
                                  marker_samples_from_ai}},
          "transitions": [{from, to, relation, has_blank, blank_paraPrIDRef}, ...]
        }
    """
    paragraphs = structure.get("paragraphs", [])
    if not paragraphs or not light_xml:
        return {"role_formats": {}, "transitions": []}

    # ai_idx → real_idx (light_xml의 원본 _idx)
    def _translate(ai_idx):
        if idx_map:
            return idx_map.get(ai_idx, ai_idx)
        return ai_idx

    # real_idx → structure paragraph
    real_to_struct = {}
    for p in paragraphs:
        raw = p.get("idx")
        if raw is None:
            continue
        try:
            ai_idx = int(raw)
        except (TypeError, ValueError):
            continue
        real_idx = _translate(ai_idx)
        try:
            real_to_struct[int(real_idx)] = p
        except (TypeError, ValueError):
            continue

    # light_xml의 hp:p들을 _idx 기반으로 수집
    try:
        root = etree.fromstring(light_xml.encode("utf-8"))
    except Exception as e:
        log.warning(f"format 관측: XML 파싱 실패 {e}")
        return {"role_formats": {}, "transitions": []}

    # _idx → xml elem (lighten_xml이 _idx 부여)
    xml_by_real_idx = {}
    # fallback: _idx 없으면 document order로 번호 부여
    fallback_counter = 0
    sections = [root] if root.tag == f"{NS_HP}sec" else root.findall(f".//{NS_HP}sec")
    if not sections:
        sections = [root]
    for section in sections:
        for p in section.findall(f"{NS_HP}p"):
            ridx_str = p.get("_idx")
            if ridx_str is not None:
                try:
                    xml_by_real_idx[int(ridx_str)] = p
                except (TypeError, ValueError):
                    xml_by_real_idx[fallback_counter] = p
            else:
                xml_by_real_idx[fallback_counter] = p
            fallback_counter += 1

    # role별 format 샘플 수집
    role_formats = {}
    for real_idx, struct_p in real_to_struct.items():
        elem = xml_by_real_idx.get(real_idx)
        if elem is None:
            continue
        role = struct_p.get("role", "")
        if not role:
            continue

        data = _extract_indent_and_marker_data(elem)
        if data["is_blank"]:
            continue

        if role not in role_formats:
            role_formats[role] = {
                "indent_parts_samples": [],
                "first_text_samples": [],
                "marker_samples_from_ai": [],
            }
        rf = role_formats[role]
        if len(rf["indent_parts_samples"]) < 6:
            rf["indent_parts_samples"].append(data["indent_parts"])
        if len(rf["first_text_samples"]) < 6:
            rf["first_text_samples"].append(data["first_text_after_indent"][:50])
        raw_marker = struct_p.get("marker", "")
        if raw_marker and raw_marker not in rf["marker_samples_from_ai"]:
            rf["marker_samples_from_ai"].append(raw_marker)

    # 전환(transition) 관측: structure paragraph들의 real_idx를 정렬
    transitions = []
    real_sorted = sorted(real_to_struct.keys())
    for i in range(len(real_sorted) - 1):
        a_real = real_sorted[i]
        b_real = real_sorted[i + 1]
        a = real_to_struct[a_real]
        b = real_to_struct[b_real]
        from_role = a.get("role", "")
        to_role = b.get("role", "")
        a_level = a.get("level")
        b_level = b.get("level")
        if not from_role or not to_role or a_level is None or b_level is None:
            continue

        # relation 판정
        if b_level == a_level:
            relation = "sibling"
        elif b_level > a_level:
            relation = "descent"
        else:
            relation = "ascent"

        # a_real과 b_real 사이의 light_xml 문단 중 blank인 것 확인
        has_blank = False
        blank_paraPrIDRef = None
        for k in range(a_real + 1, b_real):
            elem = xml_by_real_idx.get(k)
            if elem is None:
                continue
            data = _extract_indent_and_marker_data(elem)
            if data["is_blank"]:
                has_blank = True
                blank_paraPrIDRef = data["paraPrIDRef"]
                break

        transitions.append({
            "from": from_role,
            "to": to_role,
            "relation": relation,
            "has_blank": has_blank,
            "blank_paraPrIDRef": blank_paraPrIDRef,
        })

    return {
        "role_formats": role_formats,
        "transitions": transitions,
    }


FORMAT_ANALYSIS_PROMPT = """당신은 양식의 빈 줄·들여쓰기·마커 규칙을 추출하는 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 sample 글자 인용은 그대로 — 자체 표현과 인용 구분.

코드가 양식을 파싱해 **원시 관측 데이터**를 제공합니다. 이 데이터를 보고 규칙을 판정하세요.

## 임무 1: format_rules (role별 포맷 규칙)

각 role에 대해:
- **indent_parts**: 들여쓰기 구성 (탭·공백 순서). 여러 샘플 중 **가장 흔한 패턴** 선택.
  - 예: 모든 샘플이 `[{type:"tab"}]`이면 그걸 채택
  - 예: 공백 2개가 일관되면 `[{type:"space", count:2}]`
- **marker_style**: `fixed` 또는 `enumerate`
  - `fixed`: 모든 샘플이 동일 마커
  - `enumerate`: 마커가 순차 변화 (다음 패턴 중 하나)
    - 같은 base 글자의 반복 횟수만 다름
    - 같은 wrapper/형태에 counter(숫자/글자)만 변함
    - enumeration 시리즈에 속한 글리프 시퀀스
- **markers_sample**: 관측된 마커들을 **등장 순서대로** 배열 (2b가 순번 확장에 사용)
- **separator**: 마커와 내용 사이 공백 (`" "`, `""`, `"  "` 등)

## 임무 2: blank_rules (전환별 빈 줄 규칙)

각 `(from_role, to_role, relation)` 전환에 대해:
- 관측 데이터의 `has_blank`를 그대로 반영 (OX)
- 빈 줄이 있으면 `paraPrIDRef` 포함 (빈 줄의 글자 크기 결정)

## 핵심 원칙

- **관측을 그대로 믿기** — 샘플이 2개뿐이고 둘 다 같으면 그게 규칙
- outlier 1건 무시 — 4건 동일·1건 다르면 다수 쪽 채택
- enumerate 판정: 샘플 마커들이 위 enumerate 패턴 중 하나에 해당하면 enumerate, 아니면 fixed

## 출력 형식 (JSON만)

```json
{
  "format_rules": {
    "detail_item": {
      "indent_parts": [{"type": "space", "count": 2}],
      "marker_style": "fixed",
      "markers_sample": ["ㅇ"],
      "separator": " "
    },
    "note": {
      "indent_parts": [{"type": "tab"}],
      "marker_style": "enumerate",
      "markers_sample": ["*", "**", "***"],
      "separator": " "
    },
    "body_text": {
      "indent_parts": [{"type": "space", "count": 8}],
      "marker_style": "fixed",
      "markers_sample": [""],
      "separator": ""
    }
  },
  "blank_rules": [
    {
      "from": "section_header",
      "to": "section_header",
      "relation": "sibling",
      "has_blank": true,
      "paraPrIDRef": "140"
    },
    {
      "from": "section_header",
      "to": "detail_item",
      "relation": "descent",
      "has_blank": false
    }
  ]
}
```

## 중요
- role 이름은 입력 데이터에 있는 그대로 사용 (절대 수정 금지)
- `markers_sample`은 빈 문자열 `[""]`도 허용 (마커 없는 role)
- 판단 여지 없음 — 관측 카운트대로
- 반드시 JSON만 출력. 다른 설명 금지
"""


def build_format_analysis_prompt(observations: dict) -> list[dict]:
    """
    1.5c 호출: compute_format_observations 결과 → format_rules + blank_rules
    """
    role_formats = observations.get("role_formats", {})
    transitions = observations.get("transitions", [])

    lines = ["## role별 포맷 관측 샘플\n"]
    for role, info in role_formats.items():
        lines.append(f"\n### `{role}`")
        samples_indent = info.get("indent_parts_samples", [])
        samples_text = info.get("first_text_samples", [])
        markers_ai = info.get("marker_samples_from_ai", [])
        lines.append(f"- 관측된 indent_parts 샘플 ({len(samples_indent)}개):")
        for s in samples_indent:
            lines.append(f"  - {s}")
        lines.append(f"- 관측된 마커 (1차 AI 추출): {markers_ai}")
        lines.append(f"- 첫 텍스트 샘플 (indent 제외):")
        for s in samples_text:
            lines.append(f"  - {repr(s)}")

    lines.append("\n## 전환(transition) 관측 데이터\n")
    for t in transitions:
        paraPr = t.get("blank_paraPrIDRef") or "-"
        lines.append(
            f"- `{t['from']}` → `{t['to']}` ({t['relation']}): "
            f"has_blank={t['has_blank']}, blank_paraPrIDRef={paraPr}"
        )

    lines.append(
        "\n위 관측 데이터로 format_rules + blank_rules를 JSON 출력하세요.\n"
        "반드시 JSON만 출력."
    )

    return [
        {"role": "system", "content": FORMAT_ANALYSIS_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def parse_format_rules_from_llm(llm_response: str) -> dict:
    """
    1.5c LLM 응답에서 format_rules + blank_rules 파싱.

    Returns:
        {
          "format_rules": {role: {...}},
          "blank_rules": [{from, to, relation, has_blank, paraPrIDRef}, ...]
        }
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("format 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"format JSON 파싱 실패: {e}")

    result = {"format_rules": {}, "blank_rules": []}

    fr_raw = data.get("format_rules", {}) if isinstance(data, dict) else {}
    if isinstance(fr_raw, dict):
        for role, info in fr_raw.items():
            if not isinstance(info, dict):
                continue
            result["format_rules"][role] = {
                "indent_parts": info.get("indent_parts", []),
                "marker_style": info.get("marker_style", "fixed"),
                "markers_sample": info.get("markers_sample", []),
                "separator": info.get("separator", ""),
            }

    br_raw = data.get("blank_rules", []) if isinstance(data, dict) else []
    if isinstance(br_raw, list):
        for r in br_raw:
            if not isinstance(r, dict):
                continue
            result["blank_rules"].append({
                "from": r.get("from", ""),
                "to": r.get("to", ""),
                "relation": r.get("relation", ""),
                "has_blank": bool(r.get("has_blank", False)),
                "paraPrIDRef": r.get("paraPrIDRef") or r.get("blank_paraPrIDRef"),
            })

    log.info(
        f"format 파싱: format_rules {len(result['format_rules'])}개, "
        f"blank_rules {len(result['blank_rules'])}개"
    )
    return result


EXCLUSIVITY_ANALYSIS_PROMPT = """당신은 계층 구조의 형제 배타 관계를 판정하는 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 role 이름은 그대로 인용.

아래 **각 부모 role의 인스턴스별 직계 자식 집합**을 보고,
**한 번이라도 같은 인스턴스에서 공존한 자식 쌍**을 찾아 공존 규칙을 출력하세요.
공존한 적 없는 쌍은 자동으로 배타 처리됩니다.

## 규칙 (기계적 적용)

각 부모 role의 인스턴스들을 훑어서:
- 자식 쌍 (A, B) 공존 횟수 ≥ 1 → **공존 OK** (리스트에 포함)
- 공존 횟수 = 0 → **배타** (리스트에 미포함 → 자동 배타)

OX의 이분법입니다. 판단 여지 없음.

## 절차

1. 각 부모 role에 대해 인스턴스들을 순회하며 자식 쌍 공존 카운트
2. 공존 ≥1회 쌍을 `pairs_cooccurred`에 기록
3. variant = 공존 그래프의 maximal clique (서로 공존 OK인 자식들의 묶음)
4. 모든 쌍이 공존 → 배타 없음 → 그 부모는 스킵 (규칙 출력 X)

## 예시

입력:
```
section_header (6 인스턴스):
- inst 0: {detail_item}
- inst 1: {detail_item}
- inst 2: {detail_item}
- inst 3: {detail_item, note}
- inst 4: {key_point, note}
- inst 5: {key_point}
```

쌍별 공존:
- (detail_item, note): 1 → **공존 OK**
- (key_point, note): 1 → **공존 OK**
- (detail_item, key_point): 0 → 배타 (리스트에 미포함)

출력:
- variant A = {detail_item, note}
- variant B = {key_point, note}
(공통 자식 note는 양쪽 포함)

## 출력 형식 (JSON만)

```json
{
  "exclusive_rules": [
    {
      "parent": "section_header",
      "variants": [
        ["detail_item", "note"],
        ["key_point", "note"]
      ],
      "pairs_cooccurred": [["detail_item", "note"], ["key_point", "note"]]
    }
  ]
}
```

- `exclusive_rules`: 배타 쌍이 존재하는 **모든** 부모를 포함. 없으면 빈 배열.
- `pairs_cooccurred`: 한 번이라도 공존한 쌍만 기록. 여기 없는 쌍은 배타.
- 판단 여지 없음. 카운트 결과만.
- 반드시 JSON만 출력. 다른 설명 금지.
"""


def build_exclusivity_analysis_prompt(
    parent_instances: dict,
    role_markers: dict = None,
) -> list[dict]:
    """
    1.5b 호출: 부모 role별 자식 인스턴스 데이터 → 배타 규칙

    Args:
        parent_instances: {parent_role: [frozenset(children), ...]}
                          compute_parent_instance_children()의 결과
        role_markers: {role: marker} (선택, 표기용)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    if role_markers is None:
        role_markers = {}

    # role 이름과 마커를 섞지 않기 — AI가 role 이름에 마커를 포함시키는 버그 방지
    used_roles = set()
    for parent_role, instances in parent_instances.items():
        used_roles.add(parent_role)
        for inst in instances:
            used_roles.update(inst)

    lines = []
    if role_markers:
        lines.append("## role 목록 (참고용 마커)")
        lines.append("role 이름과 마커는 **별개**입니다. 출력에는 role 이름만 쓰고 마커는 쓰지 마세요.\n")
        for r in sorted(used_roles):
            m = role_markers.get(r, "")
            lines.append(f"- `{r}`: 마커 \"{m}\"" if m else f"- `{r}`: (마커 없음)")
        lines.append("")

    lines.append("## 각 부모 role의 직계 자식 인스턴스")
    lines.append("(아래 표의 role 이름을 그대로 출력에 사용하세요 — 마커 붙이지 말 것)\n")
    for parent_role, instances in parent_instances.items():
        non_empty_count = sum(1 for inst in instances if inst)
        lines.append(
            f"\n### 부모: `{parent_role}` — 총 {len(instances)}개 인스턴스 "
            f"({non_empty_count}개는 자식 있음)"
        )
        for i, inst in enumerate(instances):
            if inst:
                children_str = ", ".join(f"`{r}`" for r in sorted(inst))
                lines.append(f"- inst {i}: {{{children_str}}}")
            else:
                lines.append(f"- inst {i}: {{}}")
    lines.append(
        "\n위 데이터를 기반으로 exclusive_rules를 JSON으로 출력하세요.\n"
        "**공존한 쌍만 `pairs_cooccurred`에 기록. 공존 안 한 쌍은 기록하지 마세요 (자동 배타).**\n"
        "**role 이름에 마커(괄호 포함) 붙이지 말고 위 표의 이름 그대로 사용.**\n"
        "반드시 JSON만 출력."
    )
    user_msg = "\n".join(lines)

    return [
        {"role": "system", "content": EXCLUSIVITY_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_exclusivity_from_llm(llm_response: str) -> list:
    """
    1.5b LLM 응답에서 exclusive_rules 리스트를 파싱합니다.

    Returns:
        [{"parent": str, "variants": [[role,...], ...], "pairs_cooccurred": [...]}, ...]
    """
    json_match = re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', llm_response)
    if json_match:
        raw = json_match.group(1)
    else:
        brace_match = re.search(r'\{[\s\S]*\}', llm_response)
        if brace_match:
            raw = brace_match.group(0)
        else:
            raise ValueError("exclusivity 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"exclusivity JSON 파싱 실패: {e}")

    raw_rules = data.get("exclusive_rules", []) if isinstance(data, dict) else []
    if not isinstance(raw_rules, list):
        return []

    result = []
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        parent = r.get("parent", "")
        variants = r.get("variants", [])
        if not parent or not isinstance(variants, list) or len(variants) < 2:
            continue
        norm_variants = []
        for v in variants:
            if isinstance(v, list):
                roles = [str(x) for x in v if isinstance(x, str)]
                if roles:
                    norm_variants.append(roles)
        if len(norm_variants) >= 2:
            result.append({
                "parent": parent,
                "variants": norm_variants,
                "pairs_cooccurred": r.get("pairs_cooccurred", []),
            })

    log.info(f"배타 규칙 파싱: {len(result)}개")
    return result


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


CACHE_SCHEMA_VERSION = 13  # tree rebuild (1g) — 1e+repair 후 cluster + 의미로 트리 재구성. paragraph.parent_idx + level 새로.


def compute_template_hash(template_path: str) -> str:
    """양식 파일 바이트의 SHA256 해시 앞 16자리 (캐시 키용).

    file_id와 달리 내용이 같으면 같은 해시 → 재업로드해도 캐시 hit.
    """
    import hashlib
    h = hashlib.sha256()
    with open(template_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def get_template_cache_path(cache_key: str, namespace: str = 'full') -> str:
    """템플릿 분석 캐시 경로.

    namespace:
      - 'full': 1a~1e+chapter_types 통째 (기존 호환, suffix 없음)
      - 'step1ab': 1a/1b 결과만 (1c 격리 실험용)
      - 그 외: <key>_<namespace>.json
    """
    import os
    safe_key = cache_key.replace("/", "_").replace("..", "_")
    if namespace == 'full':
        return os.path.join(TEMPLATE_CACHE_DIR, f"{safe_key}.json")
    return os.path.join(TEMPLATE_CACHE_DIR, f"{safe_key}_{namespace}.json")


def save_template_cache(cache_key: str, data: dict, namespace: str = 'full') -> bool:
    """양식 분석 결과를 캐시에 저장. cache_schema_version 자동 삽입."""
    import os
    path = get_template_cache_path(cache_key, namespace)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data["cache_schema_version"] = CACHE_SCHEMA_VERSION
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        log.info(f"[CACHE/{namespace}] 저장: {path} ({os.path.getsize(path):,}B)")
        return True
    except Exception as e:
        log.warning(f"[CACHE/{namespace}] 저장 실패: {e}")
        return False


def load_template_cache(cache_key: str, namespace: str = 'full') -> dict | None:
    """캐시에서 양식 분석 결과 로드. 없거나 버전 불일치 시 None."""
    import os
    path = get_template_cache_path(cache_key, namespace)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached_version = data.get("cache_schema_version", 1)
        if cached_version < CACHE_SCHEMA_VERSION:
            log.info(
                f"[CACHE/{namespace}] version mismatch "
                f"(found={cached_version}, required={CACHE_SCHEMA_VERSION}), "
                f"treating as miss: {path}"
            )
            return None
        log.info(f"[CACHE/{namespace}] 로드: {path} ({os.path.getsize(path):,}B)")
        return data
    except Exception as e:
        log.warning(f"[CACHE/{namespace}] 로드 실패 ({path}): {e}")
        return None


def clear_template_cache(cache_key: str, namespace: str = 'full') -> bool:
    """캐시 파일 삭제"""
    import os
    path = get_template_cache_path(cache_key, namespace)
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info(f"[CACHE/{namespace}] 삭제: {path}")
            return True
    except Exception as e:
        log.warning(f"[CACHE/{namespace}] 삭제 실패: {e}")
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cache-before-save validation (7단계 gate)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def validate_structure_for_cache(
    structure: dict,
    chapter_types: dict,
) -> dict:
    """
    full 캐시 저장 전 구조 무결성 검증. 순수 함수 — IO 없음.

    Returns:
        {can_cache, should_abort, blocker_count, watch_count, checks: [...]}
    """
    paragraphs = structure.get("paragraphs", [])
    grammar = structure.get("template_grammar", {})
    per_type = grammar.get("per_type", {})

    # chapter title role 수집
    title_roles = set()
    for ct in chapter_types.values():
        tr = ct.get("title_role", "")
        if tr:
            title_roles.add(tr)

    # 본문 시작 idx (첫 title role 등장)
    first_ch_idx = len(paragraphs)
    for p in paragraphs:
        if p.get("role") in title_roles:
            first_ch_idx = p.get("idx", 0)
            break

    valid_idxs = {p.get("idx") for p in paragraphs}

    checks = []

    # ── SC1: chapter_types 0개 ──
    checks.append({
        "check_id": "SC1",
        "name": "no_chapter_types",
        "severity": "blocker",
        "triggered": len(chapter_types) == 0,
        "detail": f"chapter_types={len(chapter_types)}" if len(chapter_types) == 0 else "",
        "evidence": [],
    })

    # ── SC2: root_roles가 grammar에 없음 ──
    sc2_missing = []
    for tn, tg in per_type.items():
        roots = tg.get("root_roles", [])
        gram_keys = set(tg.get("grammar", {}).keys())
        for r in roots:
            if r not in gram_keys:
                sc2_missing.append({"type": tn, "root_role": r, "grammar_keys": sorted(gram_keys)})
    checks.append({
        "check_id": "SC2",
        "name": "root_roles_not_in_grammar",
        "severity": "blocker",
        "triggered": len(sc2_missing) > 0,
        "detail": f"{len(sc2_missing)}개 root_role이 grammar에 없음" if sc2_missing else "",
        "evidence": sc2_missing,
    })

    # ── SC3: parent_idx self-loop ──
    sc3 = [{"idx": p.get("idx"), "role": p.get("role")}
           for p in paragraphs if p.get("parent_idx") is not None and p.get("parent_idx") == p.get("idx")]
    checks.append({
        "check_id": "SC3",
        "name": "parent_self_loop",
        "severity": "blocker",
        "triggered": len(sc3) > 0,
        "detail": f"{len(sc3)}개 self-loop" if sc3 else "",
        "evidence": sc3[:10],
    })

    # ── SC4: parent_idx out_of_range ──
    sc4 = [{"idx": p.get("idx"), "parent_idx": p.get("parent_idx"), "role": p.get("role")}
           for p in paragraphs
           if p.get("parent_idx") is not None and p.get("parent_idx") not in valid_idxs]
    checks.append({
        "check_id": "SC4",
        "name": "parent_out_of_range",
        "severity": "blocker",
        "triggered": len(sc4) > 0,
        "detail": f"{len(sc4)}개 out-of-range parent" if sc4 else "",
        "evidence": sc4[:10],
    })

    # ── SC5: 본문 paragraph인데 role 없음 ──
    sc5 = []
    for p in paragraphs:
        if p.get("idx", 0) < first_ch_idx:
            continue
        if p.get("level") is None:
            continue
        if not p.get("role"):
            sc5.append({
                "idx": p.get("idx"),
                "level": p.get("level"),
                "marker": p.get("marker", ""),
                "description": p.get("description", ""),
                "role_candidates": p.get("role_candidates", [])[:3],
            })
    checks.append({
        "check_id": "SC5",
        "name": "body_paragraph_no_role",
        "severity": "blocker",
        "triggered": len(sc5) > 0,
        "detail": f"{len(sc5)}개 본문 paragraph에 role 없음" if sc5 else "",
        "evidence": sc5[:10],
    })

    # ── SC6: parent forward_ref (watch) ──
    sc6 = [{"idx": p.get("idx"), "parent_idx": p.get("parent_idx"), "role": p.get("role")}
           for p in paragraphs
           if p.get("parent_idx") is not None and p.get("parent_idx") > p.get("idx", 0)]
    checks.append({
        "check_id": "SC6",
        "name": "parent_forward_ref",
        "severity": "watch",
        "triggered": len(sc6) > 0,
        "detail": f"{len(sc6)}개 forward reference" if sc6 else "",
        "evidence": sc6[:10],
    })

    # ── SC7: grammar 자기참조 (watch) ──
    sc7 = []
    for tn, tg in per_type.items():
        for role, g in tg.get("grammar", {}).items():
            if role in g.get("allowed_children", []):
                sc7.append({"type": tn, "role": role})
    checks.append({
        "check_id": "SC7",
        "name": "grammar_self_ref",
        "severity": "watch",
        "triggered": len(sc7) > 0,
        "detail": f"{len(sc7)}개 자기참조" if sc7 else "",
        "evidence": sc7,
    })

    # ── SC8: level gap >= 2 (watch) ──
    idx_to_p = {p.get("idx"): p for p in paragraphs}
    sc8 = []
    for p in paragraphs:
        pi = p.get("parent_idx")
        if pi is None:
            continue
        parent = idx_to_p.get(pi)
        if not parent:
            continue
        pl = parent.get("level") or 0
        cl = p.get("level") or 0
        gap = abs(cl - pl)
        if gap >= 2:
            sc8.append({"idx": p.get("idx"), "level": cl, "parent_level": pl, "gap": gap})
    checks.append({
        "check_id": "SC8",
        "name": "level_gap",
        "severity": "watch",
        "triggered": len(sc8) > 0,
        "detail": f"{len(sc8)}개 level gap >= 2" if sc8 else "",
        "evidence": sc8[:10],
    })

    # ── SC9: singleton 불일치 (watch) ──
    sc9 = []
    for tn, tg in per_type.items():
        for role, g in tg.get("grammar", {}).items():
            if g.get("singleton"):
                count = sum(1 for p in paragraphs if p.get("role") == role)
                if count > 1:
                    sc9.append({"type": tn, "role": role, "observed_count": count})
    checks.append({
        "check_id": "SC9",
        "name": "singleton_mismatch",
        "severity": "watch",
        "triggered": len(sc9) > 0,
        "detail": f"{len(sc9)}개 singleton 초과" if sc9 else "",
        "evidence": sc9,
    })

    # ── 집계 ──
    blockers = [c for c in checks if c["severity"] == "blocker" and c["triggered"]]
    watches = [c for c in checks if c["severity"] == "watch" and c["triggered"]]

    return {
        "can_cache": len(blockers) == 0,
        "should_abort": len(blockers) > 0,
        "blocker_count": len(blockers),
        "watch_count": len(watches),
        "checks": checks,
    }


def write_cache_validation_debug(result: dict, debug_dir: str) -> None:
    """05b_cache_validation.json을 debug_dir에 저장."""
    import os
    from datetime import datetime
    os.makedirs(debug_dir, exist_ok=True)
    path = os.path.join(debug_dir, "05b_cache_validation.json")
    try:
        output = {
            "generated_at": datetime.now().isoformat(),
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            **result,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.warning(f"[DEBUG] 05b_cache_validation.json 저장 실패: {e}")


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
    structure["template_grammar"] = extract_template_grammar(
        structure.get("paragraphs", []),
        structure.get("chapter_types", {}),
    )
    structure["role_text_types"] = classify_role_text_types(
        structure.get("paragraphs", []),
        structure.get("template_grammar"),
    )
    structure["per_type_role_semantics"] = build_per_type_role_semantics(
        structure.get("paragraphs", []),
        structure.get("chapter_types", {}),
        structure.get("template_grammar"),
    )
    return structure


def extract_template_grammar(
    paragraphs: list[dict],
    chapter_types: dict,
) -> dict:
    """
    Template의 observed parent→child 전이에서 grammar를 추출합니다.

    Returns:
        {
            "global": {
                role: {
                    "allowed_children": [child_roles],
                    "allowed_parents": [parent_roles],
                    "repeatable": bool,
                    "singleton": bool,       # 부모 인스턴스당 1회만
                    "optional": bool,
                    "observed_counts": {parent_role: [counts_per_instance]},
                },
                ...
            },
            "per_type": {
                type_name: {
                    "root_roles": [roles],   # chapter title 직속 자식
                    "grammar": {role: {...}}, # type별 grammar subset
                },
                ...
            },
            "observed_transitions": [(parent, child), ...],
        }
    """
    # ── 1. Global observed transitions ──
    # parent_idx → parent role, child role 매핑
    idx_to_role = {}
    idx_to_parent = {}
    for p in paragraphs:
        idx = p.get("idx")
        role = p.get("role", "")
        parent_idx = p.get("parent_idx")
        if role:
            idx_to_role[idx] = role
            idx_to_parent[idx] = parent_idx

    # parent_role → child_role 전이 수집
    transitions = set()
    parent_children = {}      # parent_role → set(child_roles)
    child_parents = {}        # child_role → set(parent_roles)
    role_counts = {}          # role → total count
    parent_instance_children = {}  # (parent_role, parent_idx) → {child_role: count}

    for p in paragraphs:
        idx = p.get("idx")
        role = p.get("role", "")
        parent_idx = p.get("parent_idx")
        if not role:
            continue

        role_counts[role] = role_counts.get(role, 0) + 1

        if parent_idx is not None:
            parent_role = idx_to_role.get(parent_idx)
            if parent_role:
                transitions.add((parent_role, role))
                parent_children.setdefault(parent_role, set()).add(role)
                child_parents.setdefault(role, set()).add(parent_role)
                # per-instance count
                key = (parent_role, parent_idx)
                if key not in parent_instance_children:
                    parent_instance_children[key] = {}
                parent_instance_children[key][role] = (
                    parent_instance_children[key].get(role, 0) + 1
                )

    # ── 2. Role별 singleton/repeatable/optional 계산 ──
    # parent_role별 인스턴스들의 idx 수집
    parent_instances = {}
    for p in paragraphs:
        role = p.get("role", "")
        idx = p.get("idx")
        if role:
            parent_instances.setdefault(role, []).append(idx)

    global_grammar = {}
    for role in set(list(parent_children.keys()) + list(child_parents.keys()) +
                    list(role_counts.keys())):
        allowed_ch = sorted(parent_children.get(role, set()))
        allowed_pa = sorted(child_parents.get(role, set()))

        # per-parent observed counts → singleton/repeatable/optional
        observed = {}  # parent_role → [count_per_instance]
        for pr in allowed_pa:
            pr_idxs = parent_instances.get(pr, [])
            counts = []
            for pr_idx in pr_idxs:
                key = (pr, pr_idx)
                c = parent_instance_children.get(key, {}).get(role, 0)
                counts.append(c)
            observed[pr] = counts

        # Aggregate: singleton if max count across all parent instances <= 1
        all_counts = [c for clist in observed.values() for c in clist]
        max_count = max(all_counts) if all_counts else 0
        has_zero = any(c == 0 for c in all_counts) if all_counts else False

        global_grammar[role] = {
            "allowed_children": allowed_ch,
            "allowed_parents": allowed_pa,
            "repeatable": max_count >= 2,
            "singleton": max_count <= 1 and not has_zero,
            "optional": has_zero,
            "total_count": role_counts.get(role, 0),
            "observed_counts": observed,
        }

    # ── 3. Per-type grammar subset ──
    per_type = {}
    for type_name, type_info in chapter_types.items():
        pattern = type_info.get("pattern", {})
        title_role = type_info.get("title_role", "")

        # pattern tree에서 사용되는 role 수집
        def _collect_pattern_roles(pat, acc):
            for r, info in pat.items():
                acc.add(r)
                ch = info.get("children", {})
                if ch:
                    _collect_pattern_roles(ch, acc)

        type_roles = set()
        _collect_pattern_roles(pattern, type_roles)

        # root_roles = pattern의 top-level keys
        root_roles = sorted(pattern.keys())

        # type에 속하는 role만 추린 grammar subset
        type_grammar = {}
        for role in type_roles:
            if role in global_grammar:
                g = global_grammar[role]
                type_grammar[role] = {
                    "allowed_children": [
                        c for c in g["allowed_children"] if c in type_roles
                    ],
                    "allowed_parents": [
                        p for p in g["allowed_parents"]
                        if p in type_roles or p == title_role
                    ],
                    "repeatable": g["repeatable"],
                    "singleton": g["singleton"],
                    "optional": g["optional"],
                }

        per_type[type_name] = {
            "title_role": title_role,
            "root_roles": root_roles,
            "grammar": type_grammar,
        }

    return {
        "global": global_grammar,
        "per_type": per_type,
        "observed_transitions": sorted(transitions),
    }


def build_per_type_role_semantics(
    paragraphs: list[dict],
    chapter_types: dict,
    template_grammar: dict | None = None,
) -> dict:
    """
    1a description을 chapter→type별로 그룹핑하여 role별 per_type semantics 생성.

    같은 role_cluster라도 type/context에 따라 다른 의미를 가질 수 있음.
    AI가 이미 만든 paragraph-level description을 type-level로 집계.

    Returns:
        {role: {"default": {...}, "per_type": {type_name: {...}}}}
    """
    from collections import defaultdict

    global_grammar = (template_grammar or {}).get("global", {})
    per_type_grammar = (template_grammar or {}).get("per_type", {})

    # ── 1. chapter 경계 결정 (same logic as _build_chapter_types) ──
    l0_with_ch = sum(
        1 for i, p in enumerate(paragraphs)
        if p.get("level", 0) == 0
        and i + 1 < len(paragraphs)
        and paragraphs[i + 1].get("level", 0) > 0
    )
    ch_title_level = 0 if l0_with_ch >= 2 else 1

    chapters = []  # [(title_para, body_paras)]
    cur_title = None
    cur_body = []
    for p in paragraphs:
        lv = p.get("level", 0)
        if lv < ch_title_level:
            continue
        if lv == ch_title_level:
            if ch_title_level == 0:
                idx = p.get("idx", 0)
                has_child = any(
                    pp.get("level", 0) > 0
                    for pp in paragraphs[idx + 1: idx + 5]
                )
                if not has_child:
                    continue
            if cur_title is not None:
                chapters.append((cur_title, cur_body))
            cur_title = p
            cur_body = []
        elif cur_title is not None:
            cur_body.append(p)
    if cur_title is not None:
        chapters.append((cur_title, cur_body))

    # ── 2. chapter→type 매핑 (role set overlap) ──
    def _collect_pattern_roles(pat: dict) -> set:
        roles = set()
        for r, info in pat.items():
            roles.add(r)
            ch = info.get("children", {})
            if ch:
                roles |= _collect_pattern_roles(ch)
        return roles

    type_role_sets = {}
    for tn, ti in chapter_types.items():
        type_role_sets[tn] = _collect_pattern_roles(ti.get("pattern", {}))

    ch_type_map = []  # [(type_name, body_paras)]
    for title, body in chapters:
        ch_roles = {p.get("role", "") for p in body if p.get("role")}
        # best match: highest Jaccard similarity
        best_type = None
        best_score = -1.0
        for tn, tr in type_role_sets.items():
            if not tr:
                continue
            intersection = len(ch_roles & tr)
            union = len(ch_roles | tr)
            score = intersection / union if union else 0
            if score > best_score:
                best_score = score
                best_type = tn
        ch_type_map.append((best_type, body))

    # ── 3. (type, role) 별로 description + parent + evidence 수집 ──
    # type_role_data[type_name][role] = {descriptions, parent_roles, evidence_idx}
    type_role_data = defaultdict(lambda: defaultdict(lambda: {
        "descriptions": [], "parent_roles": set(), "evidence_idx": [],
        "levels": [],
    }))
    idx_role = {p.get("idx"): p.get("role", "") for p in paragraphs}

    for type_name, body in ch_type_map:
        if not type_name:
            continue
        for p in body:
            role = p.get("role", "")
            if not role:
                continue
            desc = p.get("description", "")
            pidx = p.get("parent_idx")
            parent_role = idx_role.get(pidx, "")

            entry = type_role_data[type_name][role]
            if desc and desc not in entry["descriptions"]:
                entry["descriptions"].append(desc)
            if parent_role:
                entry["parent_roles"].add(parent_role)
            entry["evidence_idx"].append(p.get("idx"))
            entry["levels"].append(p.get("level", 0))

    # ── 4. 결과 구성 ──
    # text_type 추론 keywords
    _summary_kw = {"요약", "박스", "마무리", "전환", "기대효과"}
    _supporting_kw = {"보충", "예시", "나열", "각주", "보충문", "근거", "수치"}
    _body_kw = {"설명", "본문", "서술", "실행 내용", "성과 설명", "내용 제시", "진단"}
    _heading_kw = {"제목", "표지", "단원", "분류", "장 시작", "전략", "과제", "항목 제목"}

    def _infer_text_type(desc: str, has_ch: bool) -> str:
        is_summary = any(k in desc for k in _summary_kw)
        is_supporting = any(k in desc for k in _supporting_kw)
        is_body = any(k in desc for k in _body_kw)
        is_heading = any(k in desc for k in _heading_kw)
        if has_ch:
            # children 있어도 description이 명확하면 그쪽 우선
            if is_summary:
                return "summary"
            if is_supporting:
                return "supporting"
            if is_body and not is_heading:
                return "body"
            return "heading"
        # leaf
        if is_summary:
            return "summary"
        if is_supporting:
            return "supporting"
        if is_heading:
            return "heading"
        return "body"

    result = {}
    all_roles = set()
    for trd in type_role_data.values():
        all_roles |= trd.keys()

    for role in sorted(all_roles):
        has_ch_global = bool(global_grammar.get(role, {}).get("allowed_children"))
        all_descs = []
        all_levels = []
        all_parents = set()
        for trd in type_role_data.values():
            rd = trd.get(role, {})
            all_descs.extend(rd.get("descriptions", []))
            all_levels.extend(rd.get("levels", []))
            all_parents |= rd.get("parent_roles", set())
        default_desc = all_descs[0] if all_descs else ""

        per_type = {}
        for type_name in sorted(type_role_data.keys()):
            entry = type_role_data[type_name].get(role)
            if not entry or not entry["descriptions"]:
                continue
            rep_desc = entry["descriptions"][0]
            # per_type grammar로 has_children 판단 (type context별로 다를 수 있음)
            type_g = per_type_grammar.get(type_name, {}).get("grammar", {})
            has_ch_in_type = bool(type_g.get(role, {}).get("allowed_children"))
            _rep_level = entry["levels"][0] if entry["levels"] else 0
            _sorted_parents = sorted(entry["parent_roles"])
            _rep_parent = _sorted_parents[0] if _sorted_parents else ""
            _sem = infer_semantic_tag(
                rep_desc, has_ch_in_type, _rep_level, _rep_parent, "grammar",
            )
            per_type[type_name] = {
                "representative_description": rep_desc,
                "description_examples": entry["descriptions"][:3],
                "parent_roles": _sorted_parents,
                "evidence_idx": entry["evidence_idx"][:10],
                "has_children_in_type": has_ch_in_type,
                "inferred_text_type": _infer_text_type(rep_desc, has_ch_in_type),
                "semantic_tag": _sem["semantic_tag"],
                "semantic_inference": {
                    "mode": _sem["inference_mode"],
                    "source": "description_keyword",
                    "matched_keywords": _sem["matched_keywords"],
                    "representative_level": _rep_level,
                    "representative_parent_role": _rep_parent,
                    "parent_role_count": len(_sorted_parents),
                    "children_signal_source": "grammar",
                },
            }

        _def_level = all_levels[0] if all_levels else 0
        _sorted_all_parents = sorted(all_parents)
        _def_parent = _sorted_all_parents[0] if _sorted_all_parents else ""
        _def_sem = infer_semantic_tag(
            default_desc, has_ch_global, _def_level, _def_parent, "grammar",
        )
        result[role] = {
            "default": {
                "representative_description": default_desc,
                "has_children_global": has_ch_global,
                "inferred_text_type": _infer_text_type(default_desc, has_ch_global),
                "semantic_tag": _def_sem["semantic_tag"],
                "semantic_inference": {
                    "mode": _def_sem["inference_mode"],
                    "source": "description_keyword",
                    "matched_keywords": _def_sem["matched_keywords"],
                    "representative_level": _def_level,
                    "representative_parent_role": _def_parent,
                    "parent_roles": _sorted_all_parents,
                    "parent_role_count": len(_sorted_all_parents),
                    "children_signal_source": "grammar",
                },
            },
            "per_type": per_type,
        }

    return result


def classify_role_text_types(
    paragraphs: list[dict],
    template_grammar: dict | None = None,
) -> dict[str, dict]:
    """
    role별 text_type을 자동 분류합니다.

    분류 기준:
    1. grammar의 has_children → heading 후보
    2. description keyword로 보정
    3. 불확실하면 grammar fallback

    Returns:
        {role: {"text_type": "heading"|"body"|"supporting"|"summary"|"unknown",
                "length_hint": str, "reason": str}}
    """
    global_grammar = (template_grammar or {}).get("global", {})

    # role → description, markers 수집
    role_meta: dict[str, dict] = {}
    for p in paragraphs:
        role = p.get("role", "")
        if not role or role in role_meta:
            continue
        role_meta[role] = {
            "desc": p.get("description", ""),
            "marker": p.get("marker", "").strip(),
        }

    # has_children 판단: global grammar의 allowed_children 비어있지 않으면
    role_has_children: dict[str, bool] = {}
    for role in role_meta:
        g = global_grammar.get(role, {})
        role_has_children[role] = bool(g.get("allowed_children"))

    # keyword sets
    _heading_kw = {"제목", "표지", "단원", "장 시작", "항목 제목", "구분 제목"}
    _summary_kw = {"요약", "박스", "마무리", "전환", "기대효과"}
    _supporting_kw = {"보충", "예시", "나열", "각주", "보충문", "근거", "수치"}
    _body_kw = {"설명", "본문", "서술", "실행 내용", "성과 설명", "내용 제시", "진단"}

    result = {}
    for role, meta in role_meta.items():
        desc = meta["desc"]
        has_ch = role_has_children.get(role, False)

        is_summary = any(kw in desc for kw in _summary_kw)
        is_supporting = any(kw in desc for kw in _supporting_kw)
        is_body = any(kw in desc for kw in _body_kw)
        is_heading = any(kw in desc for kw in _heading_kw)

        if has_ch:
            if is_summary:
                text_type = "summary"
                reason = "has_children + keyword: summary"
            elif is_supporting:
                text_type = "supporting"
                reason = "has_children + keyword: supporting"
            elif is_body and not is_heading:
                text_type = "body"
                reason = "has_children + keyword: body (no heading kw)"
            else:
                text_type = "heading"
                reason = "has_children" + (" + keyword: heading" if is_heading else "")
        else:
            if is_summary:
                text_type = "summary"
                reason = "leaf + keyword: summary"
            elif is_supporting:
                text_type = "supporting"
                reason = "leaf + keyword: supporting"
            elif is_heading:
                text_type = "heading"
                reason = "leaf + keyword: heading"
            else:
                text_type = "body"
                reason = "grammar: leaf node"

        # 3. length_hint
        if text_type == "heading":
            length_hint = "짧은 한 줄 (20~40자)"
        elif text_type == "summary":
            length_hint = "1~2문장 (40~80자)"
        elif text_type == "supporting":
            length_hint = "짧은 보충문 (20~60자)"
        else:  # body
            length_hint = "한 문장 (30~100자)"

        result[role] = {
            "text_type": text_type,
            "length_hint": length_hint,
            "reason": reason,
            "has_children": has_ch,
            "description": desc[:60],
        }

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 11: Structural Intent — semantic_tag heuristic (관측용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def infer_semantic_tag(
    description: str,
    has_children: bool,
    level: int,
    parent_role: str = "",
    children_signal_source: str = "grammar",
) -> dict:
    """
    description keyword 기반으로 semantic_tag를 heuristic 추론합니다.

    11단계 관측용. pipeline decision (2b prompt, role selection, validation,
    marker rewrite, assemble)에는 사용하지 않습니다. cached analysis 및
    debug observation에 optional metadata로만 기록됩니다.

    6종 initial taxonomy (관측용 가설, 12단계에서 확정/변경):
      section_title, subsection_title, body_paragraph,
      supporting_note, caution_note, summary_conclusion

    Args:
        has_children: children 보유 여부 signal.
        children_signal_source: has_children 값의 출처
            ("grammar" = allowed_children 기반, "actual" = template 실제 자식 존재).

    Returns:
        {"semantic_tag": str, "inference_mode": "heuristic",
         "matched_keywords": list[str],
         "evidence": {"source": ..., "has_children": ...,
                      "children_signal_source": ..., "level": ...,
                      "parent_role": ...}}
    """
    desc = description or ""

    _caution_kw = {"유의", "주의", "경고", "금지", "제한", "예외"}
    _summary_kw = {"요약", "기대효과", "마무리", "방향", "결론", "전환"}
    _supporting_kw = {
        "보충", "예시", "나열", "각주", "보충문",
        "근거", "수치", "참고", "참조",
    }
    _heading_kw = {
        "제목", "표지", "단원", "장 시작", "구분 제목",
        "항목 제목", "소제목", "분류", "과제", "전략",
    }
    _body_kw = {
        "설명", "본문", "서술", "실행 내용",
        "성과 설명", "내용 제시", "진단",
    }

    def _find(kw_set):
        return [kw for kw in kw_set if kw in desc]

    m_caution = _find(_caution_kw)
    m_summary = _find(_summary_kw)
    m_supporting = _find(_supporting_kw)
    m_heading = _find(_heading_kw)
    m_body = _find(_body_kw)

    # Priority: caution > summary > supporting > heading > body > default
    if m_caution:
        tag, matched = "caution_note", m_caution
    elif m_summary:
        tag, matched = "summary_conclusion", m_summary
    elif m_supporting:
        tag, matched = "supporting_note", m_supporting
    elif m_heading or has_children:
        tag = "section_title" if level <= 1 else "subsection_title"
        matched = m_heading
    elif m_body:
        tag, matched = "body_paragraph", m_body
    else:
        tag = "subsection_title" if has_children else "body_paragraph"
        matched = []

    return {
        "semantic_tag": tag,
        "inference_mode": "heuristic",
        "matched_keywords": matched,
        "evidence": {
            "source": "description_keyword",
            "has_children": has_children,
            "children_signal_source": children_signal_source,
            "level": level,
            "parent_role": parent_role,
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 11.2: Style Profile Observation (관측용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STYLE_PROFILE_PROMPT = """당신은 한국 행정문서의 문체 분석 전문가입니다.

# ⚠️ 응답 언어 — 한국어 전용
- rule 문장 (자체 표현) 은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 sample 인용은 그대로 — 자체 표현과 인용 구분.

아래에 여러 role_cluster의 양식 paragraph sample이 주어집니다.
각 cluster의 말투/문장 특징 패턴을 분석해 자연어 rule로 추출하세요.

## 핵심 원칙

1. **제공된 sample 텍스트에서 직접 관찰 가능한 패턴만**. 추측 X, 일반 행정문서 규칙 X.
2. **각 cluster만의 고유 특징**에 집중. 모든 role 공통점 X.
3. **마커/번호/서식 자체는 다루지 X** — 별 stage가 처리. 여기는 **말투, 문장 구조, 어휘, 어미, 어조** 등 content 패턴만.
4. **문장 글자 수·길이 수치는 직접 묘사 X** — 양식 sample 전부 보고 있으므로 굳이 수치 적지 마세요. 단 "짧고 명사구 종결" 같이 길이가 만드는 패턴은 OK.
5. **rule 문장 안에 반드시 다음 3가지 포함**:
   - **적용 조건** (언제 적용)
   - **비적용 조건** (필요 시; 예외 case가 있으면 명시)
   - **근거 sample id** `[s2, s5, s8]` 형식 inline 인용 (필수)
6. **각 cluster의 sample_id는 해당 cluster 안에서만 유효**. 다른 cluster sample_id 인용하지 마세요.

## 좋은 rule 예시

> "해당 cluster 본문은 명사구 또는 '~한다' / '~함' 종결로 작성. 서술형 '~다' 는 사용 X. 근거: [s0, s2, s5]"

(※ 11.2 는 **문체 / 종결 / 분할 패턴만** 분석. 마커 / 번호 / 서식은 별 stage 가 처리 — rule 에 마커 언급 X.)

## 나쁜 rule 예시

> "괄호 안 핵심어를 강조한다." ← 적용/비적용 조건 모호 + 근거 없음
> "공식적이고 간결한 톤." ← 모든 role에 해당하는 무의미 진술

## rule이 너무 길어지거나 case별 분포가 다르면

rule을 split (`case A는 X`, `case B는 Y`) 으로 분리.

## 출력 형식

반드시 아래 JSON만 출력하세요. **profiles 배열에 input cluster 수만큼 entry**.

핵심 rule 3개 이하 — 가장 중요한 말투 패턴만 추출. 자유 자연어 / 추가 관찰 / 부수 분포 출력 금지.

```json
{
  "profiles": [
    {
      "role": "role_cluster_N",
      "content_style_rules_for_generation": ["rule 1", "rule 2", "rule 3"]
    }
  ]
}
```

각 cluster 의 rules 는 0~3 개. sample 부족 등으로 rule 없으면 빈 list. 다른 필드 추가 금지.
"""


def _collect_style_samples(
    paragraphs: list[dict],
    idx_full_texts: dict,
    semantic_tags: list[dict] | None = None,
    sample_text_char_budget: int = 80000,
) -> list[dict]:
    """
    role_cluster별 style analysis용 샘플을 수집합니다.

    원칙 (2026-05-20 redesign):
    - 전수 sample을 default로 보냄 (양식 분석은 cache 전제 — token 비용 적음)
    - 중복 paragraph는 정규화 후 제거
    - text 합이 sample_text_char_budget(80K) 초과 시 stratified fallback:
        forced (shortest + longest + semantic_tag별 1) + 나머지 stratum 중간점
    - min_samples 없음 — cluster paragraph 1개라도 분석. 0인 경우만 skip.

    raw_measurements는 code 결정적 추출 — AI input 아닌 downstream용.

    Returns:
        [{role, marker, description, level,
          raw_count, dedup_count, selected_count, sampling_method,
          char_budget_used, char_budget_cap,
          samples: [{sample_id, idx, text}],
          raw_measurements: {...}}, ...]
    """
    import re
    from collections import defaultdict, Counter

    role_entries = defaultdict(list)
    role_meta = {}
    _ws_re = re.compile(r"\s+")

    for p in paragraphs:
        role = p.get("role", "")
        if not role:
            continue
        pidx = p.get("idx")
        raw_text = idx_full_texts.get(str(pidx), idx_full_texts.get(pidx, ""))
        if not raw_text.strip():
            continue
        normalized = _ws_re.sub(" ", raw_text).strip()
        role_entries[role].append((pidx, raw_text, normalized))
        if role not in role_meta:
            role_meta[role] = {
                "marker": p.get("marker", ""),
                "description": p.get("description", "")[:80],
                "level": p.get("level", 0),
            }

    idx_to_tag: dict = {}
    tag_dist: dict = defaultdict(lambda: defaultdict(int))
    if semantic_tags:
        for entry in semantic_tags:
            r = entry.get("role", "")
            tag = entry.get("semantic_tag", "")
            idx_val = entry.get("idx")
            if r and tag:
                tag_dist[r][tag] += 1
                if idx_val is not None:
                    idx_to_tag[idx_val] = tag

    _ending_re = re.compile(r"([\uAC00-\uD7A3]{1,4})[.!?\s]*$")

    def _percentile(sorted_vals: list, q: float) -> int:
        if not sorted_vals:
            return 0
        pos = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
        return sorted_vals[pos]

    result = []
    for role in sorted(role_entries.keys()):
        entries = role_entries[role]
        raw_count = len(entries)

        seen_norm: set = set()
        uniq: list = []
        for entry in entries:
            _, _, norm = entry
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            uniq.append(entry)
        dedup_count = len(uniq)

        if dedup_count == 0:
            continue

        all_texts = [raw for _, raw, _ in uniq]
        char_lengths = sorted(len(t) for t in all_texts)
        ending_counter: Counter = Counter()
        for t in all_texts:
            m = _ending_re.search(t.rstrip())
            if m:
                ending_counter[m.group(1)] += 1

        total_chars = sum(len(raw) for _, raw, _ in uniq)
        if total_chars <= sample_text_char_budget:
            selected = list(uniq)
            sampling_method = "all"
        else:
            forced_idxs: set = set()
            by_len = sorted(range(len(uniq)), key=lambda i: len(uniq[i][1]))
            forced_idxs.add(by_len[0])
            forced_idxs.add(by_len[-1])
            tag_groups: dict = defaultdict(list)
            for ei, (pidx, _, _) in enumerate(uniq):
                tag = idx_to_tag.get(pidx, "")
                if tag:
                    tag_groups[tag].append(ei)
            for _, group_indices in tag_groups.items():
                if not any(gi in forced_idxs for gi in group_indices):
                    forced_idxs.add(group_indices[0])

            selected = [uniq[i] for i in forced_idxs]
            used_chars = sum(len(e[1]) for e in selected)
            remaining_budget = max(0, sample_text_char_budget - used_chars)
            avg_len = total_chars / max(1, len(uniq))
            target_extra = int(remaining_budget / max(1, avg_len))

            non_forced = [i for i in range(len(uniq)) if i not in forced_idxs]
            if target_extra >= len(non_forced):
                selected.extend(uniq[i] for i in non_forced)
            elif target_extra > 0:
                step = len(non_forced) / target_extra
                for i in range(target_extra):
                    pos = int(i * step + step / 2)
                    if pos < len(non_forced):
                        selected.append(uniq[non_forced[pos]])
            sampling_method = "stratified"

        selected.sort(key=lambda e: e[0])

        samples = [
            {"sample_id": f"s{si}", "idx": pidx, "text": raw}
            for si, (pidx, raw, _) in enumerate(selected)
        ]
        char_budget_used = sum(len(s["text"]) for s in samples)

        meta = role_meta.get(role, {})
        result.append({
            "role": role,
            "marker": meta.get("marker", ""),
            "description": meta.get("description", ""),
            "level": meta.get("level", 0),
            "raw_count": raw_count,
            "dedup_count": dedup_count,
            "selected_count": len(samples),
            "sampling_method": sampling_method,
            "char_budget_used": char_budget_used,
            "char_budget_cap": sample_text_char_budget,
            "samples": samples,
            "raw_measurements": {
                "char_lengths_all": char_lengths,
                "char_length_min": char_lengths[0] if char_lengths else 0,
                "char_length_max": char_lengths[-1] if char_lengths else 0,
                "char_length_mean": (sum(char_lengths) / len(char_lengths)) if char_lengths else 0,
                "char_length_p25": _percentile(char_lengths, 0.25),
                "char_length_p50": _percentile(char_lengths, 0.50),
                "char_length_p75": _percentile(char_lengths, 0.75),
                "text_endings_counter": dict(ending_counter),
                "semantic_tag_distribution": dict(tag_dist.get(role, {})),
            },
        })

    return result


def build_style_profile_prompt(
    cluster_entries: list[dict],
) -> list[dict]:
    """
    여러 cluster의 style profile AI prompt를 생성 (batch).

    Args:
        cluster_entries: _collect_style_samples 결과 list의 subset (예: 10개씩 chunk)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    user_parts = []
    for entry in cluster_entries:
        role = entry.get("role", "")
        marker = entry.get("marker", "")
        desc = entry.get("description", "")
        level = entry.get("level", 0)
        raw_count = entry.get("raw_count", 0)
        dedup_count = entry.get("dedup_count", 0)
        selected_count = entry.get("selected_count", 0)
        sampling_method = entry.get("sampling_method", "all")
        tag_dist = (entry.get("raw_measurements") or {}).get("semantic_tag_distribution", {})

        header = f"## {role}"
        if marker:
            header += f"  (양식 marker: {marker})"
        header += f"  — level {level}"
        if desc:
            header += f"\n설명: {desc}"
        header += (
            f"\nparagraph 수: {raw_count} (dedup 후 {dedup_count})"
            f", sample 전달: {selected_count} ({sampling_method})"
        )
        if tag_dist:
            tag_str = ", ".join(f"{t}:{n}" for t, n in sorted(tag_dist.items()))
            header += f"\nsemantic_tag 분포: {tag_str}"

        samples_text = "\n".join(
            f"  [{s['sample_id']}] {s['text']}"
            for s in entry.get("samples", [])
        )
        user_parts.append(f"{header}\n\nsample paragraph:\n{samples_text}")

    user_content = (
        f"아래 {len(cluster_entries)}개 cluster를 batch로 분석. "
        f"profiles 배열에 cluster 수만큼 entry 출력.\n\n"
        + "\n\n".join(user_parts)
        + f"\n\n위 {len(cluster_entries)}개 cluster를 각각 분석해서 profiles 배열로 JSON 출력. "
        "각 rule에 적용 조건 + (필요 시) 비적용 조건 + 근거 [sN, sN] inline 인용 필수. "
        "sample_id는 해당 cluster 안에서만 유효 — 다른 cluster sample_id 인용 X."
    )

    return [
        {"role": "system", "content": STYLE_PROFILE_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_style_profile_from_llm(
    llm_response: str,
    cluster_entries: list[dict],
) -> dict:
    """
    batch AI 응답에서 cluster별 style profile 파싱.

    cluster_entries를 base truth로 사용 — AI가 cluster 빠뜨려도 빈 entry 보존.

    Returns:
        {
            cluster_id: {
                "role": str,
                "content_style_rules_for_generation": list[str],
                "additional_observations": str,
                "_parse_status": "ok" | "parse_failed" | "schema_violation",
                "_evidence_missing_rule_count": int,
            },
            ...
        }
    """
    import re as _re

    expected_roles = [e.get("role", "") for e in cluster_entries if e.get("role")]

    def _empty_for_role(role: str, status: str, raw_preview: str = "") -> dict:
        return {
            "role": role,
            "content_style_rules_for_generation": [],
            "additional_observations": "",
            "_parse_status": status,
            "_evidence_missing_rule_count": 0,
            "_raw_response_preview": raw_preview,
        }

    text = llm_response.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning(f"[STYLE-PROFILE batch] JSON 파싱 실패: {e}")
        return {r: _empty_for_role(r, "parse_failed", llm_response[:1000]) for r in expected_roles}

    if not isinstance(data, dict):
        return {r: _empty_for_role(r, "schema_violation", llm_response[:1000]) for r in expected_roles}

    ai_profiles = data.get("profiles") or data.get("data") or []
    if not isinstance(ai_profiles, list):
        return {r: _empty_for_role(r, "schema_violation", llm_response[:1000]) for r in expected_roles}

    _evidence_re = _re.compile(r"\[s\d+(?:\s*,\s*s\d+)*\]")

    result: dict = {}
    # AI 출력 우선 매핑
    for ai_p in ai_profiles:
        if not isinstance(ai_p, dict):
            continue
        role = ai_p.get("role", "") or ""
        if not role or role not in expected_roles:
            continue
        rules_raw = ai_p.get("content_style_rules_for_generation", []) or []
        rules = [str(r).strip() for r in rules_raw if str(r).strip()][:3]
        result[role] = {
            "role": role,
            "content_style_rules_for_generation": rules,
            "additional_observations": "",
            "_parse_status": "ok",
            "_evidence_missing_rule_count": 0,
        }

    # AI가 빠뜨린 cluster 보전
    for r in expected_roles:
        if r not in result:
            result[r] = _empty_for_role(r, "missing_in_ai_response")

    return result


def extract_paragraph_emphasis_map(
    hwpx_source,
    paragraphs: list[dict],
    idx_full_texts: dict | None = None,
    debug_trace: dict | None = None,
) -> dict:
    """
    Stage 11.2b 보조 (code only — AI 호출 X):
    원본 양식 zipfile을 직접 열어 paragraph별 run segment를 추출 + cluster별 layer 부여.

    원칙:
    - 코드는 글꼴 ID 차이만 식별 (base vs 강조 단정 X)
    - paragraph 안 글꼴 ID 종류가 2개 이상이면 그 paragraph는 markup 후보
    - cluster 안 글꼴 ID 종류에 em1/em2/... 일관 layer 부여 (빈도 내림차순 + ID 안정)
    - layer/segment 통계를 함께 제공 — AI가 base 판정 시 참고

    1a paragraph.idx ↔ raw zip top-level p index 매핑은 _build_1a_to_xml_p_idx_mapping
    (13.7b 매핑 함수) 활용. idx_full_texts 미제공 시 sequential fallback (정확도 ↓).

    Args:
        hwpx_source: 양식 경로 또는 bytes/file-like (HwpxDocument.open 가능한 형태)
        paragraphs: structure["paragraphs"] (각 paragraph의 cluster_id는 "role" 필드)
        idx_full_texts: section_results[N].idx_full_texts (1a paragraph idx → text).
                        제공 시 정확한 idx 매핑 가능.

    Returns:
        {
            cluster_id: {
                "charpr_to_layer": {charpr_id: "emN"},
                "layer_stats": [
                    {"layer_id": "em1", "charpr_id": str,
                     "segment_count": int, "char_count": int,
                     "paragraph_count": int},
                    ...
                ],
                "total_paragraphs_in_cluster": int,
                "multi_charpr_paragraph_count": int,
                "sample_paragraphs": [
                    {
                        "paragraph_idx": int,
                        "segments": [{"charpr_id": str, "layer_id": "emN", "text": str}, ...],
                        "annotated_text": str,  # "[[em1]]...[[/em1]][[em2]]...[[/em2]]"
                    },
                    ...
                ]
            }
        }
        cluster 안 글꼴 ID가 1종뿐이면 entry 생략 (강조 없음).
    """
    from collections import Counter, defaultdict
    from hwpx.document import HwpxDocument
    import io as _io

    _dbg = debug_trace if isinstance(debug_trace, dict) else {}
    _dbg["input_paragraphs_count"] = len(paragraphs) if paragraphs else 0
    _dbg["input_idx_full_texts_count"] = len(idx_full_texts) if idx_full_texts else 0
    _dbg["hwpx_source_type"] = type(hwpx_source).__name__

    try:
        if isinstance(hwpx_source, str):
            doc = HwpxDocument.open(hwpx_source)
        elif isinstance(hwpx_source, (bytes, bytearray)):
            doc = HwpxDocument.open(_io.BytesIO(hwpx_source))
        else:
            doc = HwpxDocument.open(hwpx_source)
        _dbg["open_ok"] = True
    except Exception as _open_e:
        _dbg["open_ok"] = False
        _dbg["open_error"] = f"{type(_open_e).__name__}: {_open_e}"
        return {}

    # paragraph idx → cluster_id (structure 기준; 1a paragraph.idx — 재할당된 0~N sequential)
    idx_to_cluster = {p.get("idx"): p.get("role", "") for p in paragraphs if p.get("role")}
    # paragraph idx → parent_idx — sample에 부모 정보 명시 (LLM의 마커 reset 인식)
    idx_to_parent = {p.get("idx"): p.get("parent_idx") for p in paragraphs if p.get("idx") is not None}
    _dbg["idx_to_cluster_count"] = len(idx_to_cluster)
    _dbg["distinct_cluster_count"] = len(set(idx_to_cluster.values()))
    _dbg["paragraphs_sample"] = [
        {"idx": p.get("idx"), "role": p.get("role", "")}
        for p in (paragraphs or [])[:3]
    ]

    # 양식 zip top-level p 수집 (raw — charPr 보존)
    xml_p_elements: list = []
    xml_p_texts: list = []
    _sec_count = 0
    _sec_attr_err = 0
    for section in doc.sections:
        _sec_count += 1
        try:
            sec_elem = section.element
        except AttributeError:
            _sec_attr_err += 1
            continue
        for p_elem in sec_elem.findall(f"{NS_HP}p"):
            xml_p_elements.append(p_elem)
            text = "".join(t.text or "" for t in p_elem.iter(f"{NS_HP}t"))
            xml_p_texts.append(text)
    _dbg["section_count"] = _sec_count
    _dbg["section_attr_error_count"] = _sec_attr_err
    _dbg["xml_top_p_count"] = len(xml_p_elements)
    _dbg["xml_top_p_text_samples"] = [t[:60] for t in xml_p_texts[:5]]

    # 1a paragraph.idx → xml top-level p index 매핑 (13.7b 검증 helper 활용)
    if idx_full_texts:
        ai_to_xml = _build_1a_to_xml_p_idx_mapping(idx_full_texts, xml_p_texts)
        _dbg["mapping_path"] = "by_idx_full_texts"
    else:
        # idx_full_texts 미제공 fallback — sequential 가정 (정확도 떨어짐)
        log.warning("[11.2b] idx_full_texts not provided — using sequential fallback (idx mismatch 위험)")
        ai_to_xml = {i: i for i in range(min(len(paragraphs), len(xml_p_elements)))}
        _dbg["mapping_path"] = "sequential_fallback"
    # 역방향 매핑 (xml idx → 1a idx)
    xml_to_ai = {xml_idx: ai_idx for ai_idx, xml_idx in ai_to_xml.items()}
    _dbg["ai_to_xml_count"] = len(ai_to_xml)
    _dbg["ai_to_xml_samples"] = dict(list(ai_to_xml.items())[:5])

    # cluster별 통계 + per-paragraph segment list
    cluster_charpr_seg_count: dict = defaultdict(Counter)   # cluster → cp → seg_count
    cluster_charpr_char_count: dict = defaultdict(Counter)  # cluster → cp → char_count
    cluster_charpr_para: dict = defaultdict(lambda: defaultdict(set))  # cluster → cp → 1a idx set
    cluster_total_para = Counter()
    cluster_multi_para_count = Counter()
    cluster_paragraph_segments: dict = defaultdict(list)  # cluster → list of (ai_idx, [(cp, text), ...])

    _n_xml_visited = 0
    _n_no_ai_idx = 0
    _n_no_cluster = 0
    _n_no_segments = 0
    _n_kept = 0
    _n_multi_para = 0
    _n_single_para = 0
    for xml_idx, p_elem in enumerate(xml_p_elements):
        _n_xml_visited += 1
        ai_idx = xml_to_ai.get(xml_idx)
        if ai_idx is None:
            _n_no_ai_idx += 1
            continue  # 1a 분석 제외된 paragraph
        cluster_id = idx_to_cluster.get(ai_idx, "")
        if not cluster_id:
            _n_no_cluster += 1
            continue
        cluster_total_para[cluster_id] += 1

        # run segments — 텍스트 박스(tbl) 안 cell paragraph의 run까지 포함.
        # p_elem.iter("run")이 모든 descendant run 반환. cell 안 다른 글꼴 인식.
        # 단 각 run의 direct t만 사용 (recursive iter면 cell run의 t를 outer가
        # 또 가져오는 중복 발생).
        segments: list = []
        for run in p_elem.iter(f"{NS_HP}run"):
            cp = run.get("charPrIDRef", "0")
            text_parts = [t.text or "" for t in run.findall(f"{NS_HP}t")]
            text = "".join(text_parts)
            if not text:
                continue
            segments.append((cp, text))

        if not segments:
            _n_no_segments += 1
            continue
        _n_kept += 1

        distinct_cps = set(cp for cp, _ in segments)
        if len(distinct_cps) >= 2:
            _n_multi_para += 1
        else:
            _n_single_para += 1

        # 통계 (모든 paragraph 반영 — 단일 cp paragraph도 base 판정 hint)
        for cp, text in segments:
            cluster_charpr_seg_count[cluster_id][cp] += 1
            cluster_charpr_char_count[cluster_id][cp] += len(text)
            cluster_charpr_para[cluster_id][cp].add(ai_idx)

        if len(distinct_cps) >= 2:
            cluster_multi_para_count[cluster_id] += 1
            # multi-charpr paragraph만 sample_paragraphs에 보존
            cluster_paragraph_segments[cluster_id].append(
                (ai_idx, segments)
            )

    _dbg["xml_visited_count"] = _n_xml_visited
    _dbg["skipped_no_ai_idx"] = _n_no_ai_idx
    _dbg["skipped_no_cluster"] = _n_no_cluster
    _dbg["skipped_no_segments"] = _n_no_segments
    _dbg["kept_paragraph_count"] = _n_kept
    _dbg["multi_charpr_paragraph_count"] = _n_multi_para
    _dbg["single_charpr_paragraph_count"] = _n_single_para
    _dbg["cluster_charpr_distribution"] = {
        cid: {"distinct_charpr_count": len(cnt), "total_segments": sum(cnt.values())}
        for cid, cnt in cluster_charpr_seg_count.items()
    }
    _n_gate_pass = sum(1 for cnt in cluster_charpr_seg_count.values() if len(cnt) >= 2)
    _n_gate_skip = sum(1 for cnt in cluster_charpr_seg_count.values() if len(cnt) < 2)
    _dbg["gate_pass_cluster_count"] = _n_gate_pass
    _dbg["gate_skip_cluster_count"] = _n_gate_skip

    # 글꼴 종류 1종 cluster도 entry 생성 — base layer 정보(em1)만 채움.
    # 2c가 base에도 markup 박는 기본 동작으로 변경되어 모든 cluster에 base 정보 필요.
    # AI batch는 multi_charpr_paragraph_count=0 cluster를 skip (호출자에서 처리).
    result: dict = {}
    for cluster_id, seg_counter in cluster_charpr_seg_count.items():
        if not seg_counter:
            continue

        # layer 부여 (빈도 내림차순, 동률 시 charpr id 사전순 — 결정적)
        sorted_cps = sorted(
            seg_counter.keys(),
            key=lambda cp: (-seg_counter[cp], cp),
        )
        charpr_to_layer = {cp: f"em{i + 1}" for i, cp in enumerate(sorted_cps)}

        layer_stats = []
        for cp in sorted_cps:
            layer_stats.append({
                "layer_id": charpr_to_layer[cp],
                "charpr_id": cp,
                "segment_count": seg_counter[cp],
                "char_count": cluster_charpr_char_count[cluster_id][cp],
                "paragraph_count": len(cluster_charpr_para[cluster_id][cp]),
            })

        # multi-charpr paragraph → annotated text
        sample_paragraphs = []
        for pidx, segs in cluster_paragraph_segments[cluster_id]:
            seg_list = []
            annotated_parts = []
            for cp, text in segs:
                layer = charpr_to_layer[cp]
                seg_list.append({
                    "charpr_id": cp,
                    "layer_id": layer,
                    "text": text,
                })
                annotated_parts.append(f"[[{layer}]]{text}[[/{layer}]]")
            sample_paragraphs.append({
                "paragraph_idx": pidx,
                "parent_idx": idx_to_parent.get(pidx),
                "segments": seg_list,
                "annotated_text": "".join(annotated_parts),
            })

        result[cluster_id] = {
            "charpr_to_layer": charpr_to_layer,
            "layer_stats": layer_stats,
            "total_paragraphs_in_cluster": cluster_total_para[cluster_id],
            "multi_charpr_paragraph_count": cluster_multi_para_count[cluster_id],
            "sample_paragraphs": sample_paragraphs,
        }

    _dbg["result_cluster_count"] = len(result)
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage 11.2b: Inline Emphasis Layer Analysis (AI)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EMPHASIS_LAYER_PROMPT = """당신은 한국 행정문서의 inline 강조 패턴 분석 전문가입니다.

# ⚠️ 절대 규칙 1 — 양식 원문 글자 그대로 옮기기

양식 sample 안의 인용부호는 **한국 문서 표준 유니코드 좌우 단일인용부호 (`‘ ’`)** 입니다. rule 문장에서 양식 표현을 인용할 때 **`‘ ’` 그대로 옮기세요**.

- 맞는 예: `"분야 (‘교육·보건·의료’) 나 재고 (‘재고’) 에 적용."`
- **틀린 예** (절대 X): `"분야 ('교육·보건·의료') 나 재고 ('재고') 에 적용."` — ASCII `'` 로 변환 금지.

ASCII 작은따옴표 (`'`) 를 출력에 쓰지 마세요. 양식 원문 글자 그대로 옮기는 것이 일관성 + JSON 안전 양쪽에서 정답입니다.

# ⚠️ 절대 규칙 2 — backslash (\\) 사용 금지

**JSON 안에서 backslash 는 큰따옴표 (") 앞에만 붙입니다. 다른 어떤 글자 앞에도 절대 붙이지 마세요.**

- 한글, 숫자, 괄호, 점, 쉼표, 유니코드 인용부호 (`‘ ’`) 앞 → backslash 금지.
- 큰따옴표 (`"`) 앞에만 `\\"` 로 적습니다. 이게 backslash 쓰는 유일한 경우.

규칙 1 + 2 한 글자라도 어기면 **batch 전체 분석 결과가 통째로 사라집니다**. 다른 모든 규칙보다 우선합니다.

---

여러 role_cluster의 양식 paragraph sample이 주어집니다. 각 sample은 글꼴 ID
종류에 따라 [[em1]]...[[/em1]] / [[em2]]...[[/em2]] 등 markup으로 표시되어 있습니다.

**중요**: 코드는 글꼴 ID 종류만 분리해 layer를 부여했을 뿐, **어느 layer가
base(일반 텍스트)이고 어느 layer가 강조인지는 판정하지 않았습니다**.
당신이 cluster마다 sample을 보고 base vs 강조를 결정하세요.

## 결정 원칙

1. **base 판정 — 의미가 결정한다. 통계는 보지 마세요**:
   - sample paragraph를 읽고, **일반 본문 텍스트 (문장 골격: 주어·서술어·조사·연결어 등 본문 흐름을 잇는 부분)** 를 담당하는 layer 가 base.
   - **강조가 base 보다 segment/char 통계가 많을 수 있습니다.** 양식이 핵심 명사구 위주로 화려하면 강조 layer 가 본문보다 길어집니다. **통계 최다 = base 가 절대 아닙니다.**
   - 예시: sample 이 "[[em1]] ㅇ 정책목표 [[/em1]][[em2]]금융지원 확대[[/em2]][[em1]]를 통한 [[/em1]][[em2]]중소기업 경쟁력 강화[[/em2]]" 라면, em2 의 글자/segment 가 더 많아도 base 는 em1 (본문 골격: " ㅇ 정책목표 ", "를 통한 ").
   - **판정 순서**: (1) sample 읽기 → (2) 본문 골격 담당 layer 식별 → (3) 그 layer = base.
   - 통계는 보지도 마세요. sample 의미만 보세요.
2. **강조 layer**: base 외 모든 layer. 각 강조 layer 마다 적용 패턴 분석.
3. **양식 디자이너의 의도와 무관하게 다른 글꼴이면 강조로 본다** — 미적 이유든
   의미 강조든 상관 없이, 양식 원본의 시각적 차이를 그대로 재현.
4. **layer_id 는 input 과 동일하게 유지**. 합치거나 분리 X.

## 각 강조 layer rule 작성 원칙

1. **반드시 한국어로 작성** — 영어 문장 금지. 영어 단어 한 개도 X.
2. **양식 sample 에서 직접 관찰 가능한 패턴만**. 일반 행정문서 규칙 X.
3. **rule 문장 안에 반드시 포함**:
   - **적용 조건** (이 layer 가 양식 sample 의 어디에 등장하는지 — 구체적 위치/형태)
   - **적용 범위** (전체 paragraph / 일부 segment / 괄호 안 / 마커 직후 등 — 정확히 명시)
   - **비적용 조건** (필요 시; 예외)
4. **rule 안에 다음 셋 중 하나로 위치 명시 (필수)**:
   - "전체 paragraph 통째" — paragraph 전체가 이 layer 인 경우
   - "괄호 안 만" / "마커 직후 N 글자 만" / "특정 키워드 만" 같이 **paragraph 안 일부 segment 만** 인 경우
   - "양식 sample 의 한 paragraph 단일 layer — 새 instance 도 통째 한 layer" — 단일 layer paragraph 인 경우
5. **의미 기반 패턴 OK** ("paragraph 의 핵심 결론 명사구" 등). 다만 위치 정보 (괄호 안인지 본문 전체인지) 는 반드시 명시.

## 좋은 rule 예시 (한국어 + 위치 명시)

> "em2: 괄호 안의 한국어 부제 만 (예: `(부제 텍스트)` 형태). 본문 전체 X, 괄호 밖 X."
> "em5: 영어 괄호 부제 만 (예: `(English subtitle)` 형태). 한국어 본문 X."
> "em1: 전체 paragraph 통째 단일 layer (양식 sample 에서 한 paragraph 가 한 layer 로만 구성)."

## 나쁜 rule 예시 (절대 X)

> "Subtitle for Strategy 1" — 영어 + 위치 모호. 2c 가 본문 전체에 박을 위험.
> "강조 표시" — 위치 / 조건 명시 X. 모호함.

## 출력 형식

반드시 아래 JSON 만 출력. **clusters 배열에 input cluster 수만큼 entry**.

```json
{
  "clusters": [
    {
      "role": "role_cluster_N",
      "base_layer_id": "input layer_id 중 하나",
      "emphasis_layers": [
        {
          "layer_id": "base 외 layer_id",
          "rules_for_generation": ["rule 1", "rule 2", ...]
        }
      ]
    }
  ]
}
```

- base 외 layer 없으면 (sample 이 모두 한 layer 만) `emphasis_layers` 는 빈 list.
- 다른 필드 추가 금지 — JSON 짧게 유지하세요. 출력 길이가 길어지면 응답이 끊겨 전체 batch 가 실패합니다.

## 출력 마지막 검사 (반드시)

JSON 출력 후, 자기 출력을 다시 훑어서 두 가지 확인:

1. **ASCII 작은따옴표 (`'`) 가 있으면 양식 원문대로 유니코드 (`‘` 또는 `’`) 로 교체**. 양식에는 ASCII `'` 가 한 글자도 없으니, 응답에 ASCII `'` 가 나타나면 자기 멋대로 변환한 것입니다.
2. **backslash (\\) 가 큰따옴표 앞 외에 있으면 모두 제거**. 한 개라도 남아있으면 batch 전체가 사라집니다.

맞는 예: `"분야 (‘교육·보건·의료’) 나 재고 (‘재고’) 에 적용."`
틀린 예: `"분야 ('교육·보건·의료') 나 재고 ('재고') 에 적용."` / `"분야 (\\'교육·보건·의료\\') 에 적용."`
"""


def build_emphasis_layer_prompt(
    cluster_entries: list[tuple],
) -> list[dict]:
    """
    여러 cluster의 emphasis layer 분석 batch AI prompt 생성.

    Args:
        cluster_entries: [(cluster_id, cluster_emphasis_entry), ...]
            cluster_emphasis_entry는 extract_paragraph_emphasis_map의 한 entry

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    user_parts = []
    for cluster_id, em_entry in cluster_entries:
        layer_stats = em_entry.get("layer_stats", [])
        sample_paragraphs = em_entry.get("sample_paragraphs", [])
        total_para = em_entry.get("total_paragraphs_in_cluster", 0)
        multi_para = em_entry.get("multi_charpr_paragraph_count", 0)

        # 통계는 layer_id 식별용으로만 노출. base 판정은 의미 우선.
        layer_id_lines = [f"  - {ls['layer_id']}" for ls in layer_stats]

        sample_lines = []
        for si, sp in enumerate(sample_paragraphs):
            sample_lines.append(f"  [s{si}] {sp.get('annotated_text', '')}")

        header = (
            f"## {cluster_id}\n"
            f"layer 목록 (이 cluster 안에 존재하는 layer_id — base 판정용 후보):\n"
            + "\n".join(layer_id_lines)
        )
        body = (
            f"{header}\n\n"
            f"양식 sample paragraph (글꼴 layer markup):\n"
            + "\n".join(sample_lines)
        )
        user_parts.append(body)

    user_content = (
        f"아래 {len(cluster_entries)}개 cluster를 batch 로 분석. "
        f"clusters 배열에 cluster 수만큼 entry 출력.\n\n"
        + "\n\n".join(user_parts)
        + f"\n\n위 {len(cluster_entries)}개 cluster 각각에 대해 base_layer 판정 (sample 의미 보고 본문 골격 담당 layer 선택) + "
        "강조 layer rules 를 JSON 으로 출력. 각 rule 에 적용 조건 + (필요 시) 비적용 조건 포함."
    )

    return [
        {"role": "system", "content": EMPHASIS_LAYER_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_emphasis_layer_from_llm(
    llm_response: str,
    cluster_entries: list[tuple],
) -> dict:
    """
    batch AI 응답에서 cluster별 emphasis layer rule 파싱.

    cluster_entries를 base truth로 사용 — AI가 cluster 빠뜨려도 빈 entry 보존.

    Returns:
        {
            cluster_id: {
                "role": str,
                "base_layer_id": str,
                "base_charpr_id": str,
                "base_judgement_reason": str,
                "emphasis_layers": [{"layer_id", "charpr_id", "segment_count", "rules_for_generation"}, ...],
                "additional_observations": str,
                "_parse_status": "ok" | "parse_failed" | ...,
                "_evidence_missing_rule_count": int,
            },
            ...
        }
    """
    import re as _re

    def _fallback_for_cluster(cluster_id: str, em_entry: dict, status: str, raw: str = "") -> dict:
        stats = em_entry.get("layer_stats", []) or []
        if stats:
            base = stats[0]
            non_base = stats[1:]
        else:
            base = {"layer_id": "", "charpr_id": ""}
            non_base = []
        return {
            "role": cluster_id,
            "base_layer_id": base.get("layer_id", ""),
            "base_charpr_id": base.get("charpr_id", ""),
            "base_judgement_reason": f"fallback ({status}) — segment 수 최다 layer를 base로 가정",
            "emphasis_layers": [
                {
                    "layer_id": ls["layer_id"],
                    "charpr_id": ls["charpr_id"],
                    "segment_count": ls.get("segment_count", 0),
                    "rules_for_generation": [],
                }
                for ls in non_base
            ],
            "additional_observations": "",
            "_parse_status": status,
            "_evidence_missing_rule_count": 0,
            "_raw_response_preview": raw[:50000] if raw else "",
            "_raw_response_full_len": len(raw) if raw else 0,
        }

    entry_by_id = {cid: em for cid, em in cluster_entries}
    expected_ids = [cid for cid, _ in cluster_entries]

    text = llm_response.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning(f"[EMPHASIS-LAYER batch] JSON 파싱 실패: {e}")
        return {cid: _fallback_for_cluster(cid, entry_by_id[cid], "parse_failed", llm_response) for cid in expected_ids}

    if not isinstance(data, dict):
        return {cid: _fallback_for_cluster(cid, entry_by_id[cid], "schema_violation", llm_response) for cid in expected_ids}

    ai_clusters = data.get("clusters") or data.get("data") or []
    if not isinstance(ai_clusters, list):
        return {cid: _fallback_for_cluster(cid, entry_by_id[cid], "schema_violation", llm_response) for cid in expected_ids}

    result: dict = {}

    for ai_c in ai_clusters:
        if not isinstance(ai_c, dict):
            continue
        cluster_id = ai_c.get("role", "") or ""
        if cluster_id not in entry_by_id:
            continue
        em_entry = entry_by_id[cluster_id]
        layer_stats = em_entry.get("layer_stats", []) or []
        layer_lookup = {ls["layer_id"]: ls for ls in layer_stats}

        base_layer_id = ai_c.get("base_layer_id", "") or ""
        base_entry = layer_lookup.get(base_layer_id)
        if base_entry is None and layer_stats:
            # AI가 무효한 layer_id 출력 — 사용자 정책: 코드는 base 결정 안 함.
            # 다만 base가 없으면 downstream 깨지므로 첫 layer를 임시 채움 + reason 명시.
            base_entry = layer_stats[0]
            base_layer_id = base_entry["layer_id"]
            base_reason = "AI base_layer_id invalid"
        else:
            base_reason = ""
        base_charpr_id = (base_entry or {}).get("charpr_id", "")

        ai_layers = ai_c.get("emphasis_layers", []) or []
        emphasis_out = []
        seen = set()
        for al in ai_layers:
            if not isinstance(al, dict):
                continue
            lid = al.get("layer_id", "") or ""
            if lid == base_layer_id or lid not in layer_lookup:
                continue
            base_l = layer_lookup[lid]
            rules_raw = al.get("rules_for_generation", []) or []
            rules = [str(r).strip() for r in rules_raw if str(r).strip()]
            emphasis_out.append({
                "layer_id": lid,
                "charpr_id": base_l["charpr_id"],
                "segment_count": base_l.get("segment_count", 0),
                "rules_for_generation": rules,
            })
            seen.add(lid)

        # AI가 빠뜨린 non-base layer 보존
        for ls in layer_stats:
            if ls["layer_id"] == base_layer_id or ls["layer_id"] in seen:
                continue
            emphasis_out.append({
                "layer_id": ls["layer_id"],
                "charpr_id": ls["charpr_id"],
                "segment_count": ls.get("segment_count", 0),
                "rules_for_generation": [],
            })

        result[cluster_id] = {
            "role": cluster_id,
            "base_layer_id": base_layer_id,
            "base_charpr_id": base_charpr_id,
            "base_judgement_reason": base_reason,
            "emphasis_layers": emphasis_out,
            "additional_observations": "",
            "_parse_status": "ok",
            "_evidence_missing_rule_count": 0,
        }

    # AI 누락 cluster fallback
    for cid in expected_ids:
        if cid not in result:
            result[cid] = _fallback_for_cluster(cid, entry_by_id[cid], "missing_in_ai_response")

    return result


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

    # 1단계: 챕터 경계 나누기
    # chapter title = "뒤에 더 깊은 level의 자식을 가진 최상위 문단"
    # cover/TOC처럼 자식 없는 level 0 문단은 자동 제외됨

    # 먼저 chapter title level 결정: level 0 중 자식을 가진 것이 2개 이상이면 0,
    # 1개뿐이면 컨테이너(목차 등)이므로 level 1을 chapter title로 사용
    l0_with_children = 0
    for i, p in enumerate(paragraphs):
        if p.get("level", 0) == 0:
            if i + 1 < len(paragraphs) and paragraphs[i + 1].get("level", 0) > 0:
                l0_with_children += 1

    if l0_with_children >= 2:
        chapter_title_level = 0
    else:
        chapter_title_level = 1

    body_min_level = chapter_title_level + 1

    chapters = []  # [(title_para, [body_paras])]
    current_title = None
    current_body = []

    for p in paragraphs:
        level = p.get("level", 0)
        if level < chapter_title_level:
            continue
        if level == chapter_title_level:
            # level 0이 chapter_title_level인 경우, 자식 없는 cover 문단은 skip
            if chapter_title_level == 0:
                idx = p.get("idx", 0)
                has_child = any(
                    pp.get("level", 0) > 0
                    for pp in paragraphs[idx + 1: idx + 5]
                )
                if not has_child:
                    continue
            if current_title is not None:
                chapters.append((current_title, current_body))
            current_title = p
            current_body = []
        elif current_title is not None:
            current_body.append(p)

    if current_title is not None:
        chapters.append((current_title, current_body))

    if not chapters:
        log.warning("chapter_types 생성 실패: chapter title 문단이 없습니다")
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
                "repeat": info.get("per_parent", "single") == "multiple" or info["count"] >= 2,
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

    # ── chapter type 그룹화 전략 ────────────────────────────────────
    # 고정 depth는 양식 종속적이라 폐기. 대신 coarse grouping + path presence_ratio.
    #
    # 1. coarse_key = (title_role, sorted top-level children roles)
    #    → 같은 coarse_key 챕터들은 같은 chapter_type
    # 2. 그룹 내 union으로 pattern 병합 (모든 variant가 한 type 안에 optional로 보존)
    # 3. path presence_ratio 계산 → variant 마킹 (info 용도, dedup 영향 없음)
    #
    # 이러면 양식별로 chapter type 깊이가 달라도 자동 적응:
    # - 본 사업: 같은 strategy_header + (summary_box, task_title) → 한 type, 깊은
    #   variant들(▪/*/1)/① 등)은 union으로 모두 포함
    # - 진단형: 다른 top-level children → 별도 type
    #
    # pathological case (같은 top-level이지만 deep이 완전 다른 두 챕터) 발견되면
    # presence_ratio 기반 sub-dedup 추가 검토.

    def _collect_paths(pattern: dict, prefix: tuple = ()) -> set:
        """Pattern 트리의 root-to-node 모든 path를 tuple로 수집."""
        paths = set()
        for role, info in pattern.items():
            path = prefix + (role,)
            paths.add(path)
            children = info.get("children", {})
            if children:
                paths |= _collect_paths(children, path)
        return paths

    def _annotate_presence_ratio(pattern: dict, path_counts: dict, total: int,
                                 prefix: tuple = (), threshold: float = 0.7) -> None:
        """각 노드에 presence_ratio + is_variant 플래그 추가 (info 용도)."""
        for role, info in pattern.items():
            path = prefix + (role,)
            count = path_counts.get(path, 0)
            ratio = count / total if total else 0.0
            info["presence_ratio"] = round(ratio, 2)
            info["is_variant"] = ratio < threshold
            children = info.get("children", {})
            if children:
                _annotate_presence_ratio(children, path_counts, total, path, threshold)

    def _merge_patterns(existing: dict, new_pattern: dict) -> None:
        """
        new_pattern을 existing pattern에 union 병합. in-place 수정.

        병합 규칙:
        - 새 role: 그대로 추가, optional=True (다른 chapter엔 없었으므로)
        - 기존 role: optional 플래그 OR (한 chapter라도 optional이면 optional),
          per_parent 'multiple' 우세, observed_counts 누적, children 재귀 union
        """
        for role, new_info in new_pattern.items():
            if role not in existing:
                # 다른 chapter엔 없던 새 role → optional로 추가
                merged_info = dict(new_info)
                merged_info["optional"] = True
                existing[role] = merged_info
            else:
                ex = existing[role]
                if new_info.get("optional"):
                    ex["optional"] = True
                if new_info.get("per_parent") == "multiple":
                    ex["per_parent"] = "multiple"
                ex["observed_counts"] = (
                    ex.get("observed_counts", []) + new_info.get("observed_counts", [])
                )
                # children 재귀
                new_children = new_info.get("children", {})
                if new_children:
                    ex_children = ex.setdefault("children", {})
                    _merge_patterns(ex_children, new_children)
        # 새 pattern에 없는 기존 role은 optional로 표시 (이번 chapter엔 없었으므로)
        for role, ex in existing.items():
            if role not in new_pattern:
                ex["optional"] = True

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

    # ── 1단계: 모든 chapter의 (title_role, role_info, body_paras, pattern) 수집 ──
    # 배타 감지를 위해 role_info, body_paras도 보존
    chapters_data = []  # [(title_role, pattern, body_paras, role_info)]
    for title_para, body_paras in chapters:
        title_role = title_para.get("role", "chapter_title")
        role_info = _build_role_info(body_paras)
        if not role_info:
            continue

        # top-level 배타 감지: parent=None인 역할들만 대상
        top_level_roles = {r for r, info in role_info.items() if info["parent"] is None}
        exclusive = _detect_exclusive_children(body_paras, role_info)

        # top-level parent에서의 배타만 유지 (깊은 배타는 무시)
        top_exclusive = {
            pr: variants for pr, variants in exclusive.items()
            if pr in top_level_roles
        }

        if top_exclusive:
            # 배타적 자식 → 변형별로 별도 pattern 생성
            exclusive_items = list(top_exclusive.items())
            variant_combos = list(product(
                *[variants for _, variants in exclusive_items]
            ))
            variant_combos = variant_combos[:8]  # 변형 수 제한

            for combo in variant_combos:
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
                marker_info = " / ".join(marker_descs)
                chapters_data.append((title_role, variant_pattern, body_paras, role_info))

            log.info(
                f"top-level 배타 감지 → {len(variant_combos)}개 변형: "
                + ", ".join(
                    f"{pr}={[set(v) for v in vs]}"
                    for pr, vs in exclusive_items
                )
            )
        else:
            pattern = _build_pattern(role_info)
            chapters_data.append((title_role, pattern, body_paras, role_info))

    # ── 2단계: pattern signature 기반 그룹화 ───────────────���────────
    # coarse_key = (title_role, pattern_signature) — top-level + 1단계 children까지
    def _shallow_signature(pattern: dict, max_depth: int = 2, depth: int = 0) -> str:
        """top-level + immediate children까지만 signature (깊은 variant 차이는 무시)"""
        if depth >= max_depth:
            return ""
        parts = []
        for role in sorted(pattern.keys()):
            info = pattern[role]
            children = info.get("children", {})
            children_sig = _shallow_signature(children, max_depth, depth + 1)
            parts.append(f"{role}({children_sig})")
        return "|".join(parts)

    sig_groups = {}  # (title_role, sig) → [chapter index list]
    for i, (tr, pat, _, _) in enumerate(chapters_data):
        sig = _shallow_signature(pat)
        key = (tr, sig)
        sig_groups.setdefault(key, []).append(i)

    # ── 3단계: 그룹별로 union pattern 만들고 type 부여 ────────────
    chapter_types = {}
    type_counter = 0
    for group_key, indices in sig_groups.items():
        type_counter += 1
        type_name = f"type_{type_counter}"
        title_role = chapters_data[indices[0]][0]

        # union 병합 (단일 챕터면 그 패턴 그대로)
        merged = {}
        for i in indices:
            _merge_patterns(merged, chapters_data[i][1])

        # presence_ratio 계산 (info 용도, dedup엔 영향 없음)
        n = len(indices)
        path_counts = {}
        for i in indices:
            for path in _collect_paths(chapters_data[i][1]):
                path_counts[path] = path_counts.get(path, 0) + 1
        _annotate_presence_ratio(merged, path_counts, n)

        chapter_types[type_name] = {
            "title_role": title_role,
            "description": _pattern_summary(merged),
            "pattern": merged,
            "merged_chapter_count": n,
        }

    log.info(
        f"chapter_types 그룹화: {len(chapters_data)}개 챕터 항목 → "
        f"{len(chapter_types)}개 type ({list(chapter_types.keys())})"
    )
    for type_name, info in chapter_types.items():
        log.info(
            f"  {type_name}: title_role={info['title_role']}, "
            f"merged={info.get('merged_chapter_count', 1)} chapters"
        )

    return chapter_types


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Grammar-based tree reconstruction & validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class GrammarViolation:
    """단일 grammar 위반 사항."""

    def __init__(self, violation_type: str, item_index: int, role: str,
                 detail: str, expected: str = "", actual: str = ""):
        self.violation_type = violation_type  # no_valid_parent, ambiguous_parent, etc.
        self.item_index = item_index
        self.role = role
        self.detail = detail
        self.expected = expected
        self.actual = actual

    def to_dict(self) -> dict:
        return {
            "type": self.violation_type,
            "item_index": self.item_index,
            "role": self.role,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
        }

    def __repr__(self):
        return f"GrammarViolation({self.violation_type}, idx={self.item_index}, {self.role}: {self.detail})"


class ReconstructionResult:
    """Tree reconstruction 결과."""

    def __init__(self):
        self.nodes: list[dict] = []        # [{id, parent_id, role, text}, ...]
        self.violations: list[GrammarViolation] = []
        self.failure_type: str | None = None  # None=성공, generation_failure 등

    @property
    def success(self) -> bool:
        return len(self.violations) == 0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "failure_type": self.failure_type,
            "node_count": len(self.nodes),
            "violation_count": len(self.violations),
            "violations": [v.to_dict() for v in self.violations],
            "nodes": self.nodes,
        }


def reconstruct_tree_from_flat(
    flat_items: list[dict],
    type_grammar: dict,
    root_roles: list[str],
    title_role: str = "",
) -> ReconstructionResult:
    """
    2b flat list를 grammar 기반으로 strict tree reconstruction.

    자동 보정이 아니라 검증: flat list가 grammar상 유일한 tree로
    복원 가능한지 확인합니다. 불가능하면 violation을 기록합니다.

    violation이 있어도 노드는 추가하여 후속 분석이 가능하게 합니다.
    (violation이 있으면 assemble 전에 차단됨)

    Args:
        flat_items: [{"role": ..., "text": ...}, ...]
        type_grammar: {role: {"allowed_children": [...], ...}}
        root_roles: chapter title 직속 자식으로 허용되는 role 목록
        title_role: chapter title role (부모의 부모)

    Returns:
        ReconstructionResult
    """
    result = ReconstructionResult()

    if not flat_items:
        return result

    # singleton 추적: role → count (이 chapter 내에서)
    singleton_counts = {}

    # planning_failure 감지: 첫 item이 root_roles에 없으면
    first_role = flat_items[0].get("role", "")
    if first_role and first_role not in root_roles:
        result.violations.append(GrammarViolation(
            "wrong_type_assignment", 0, first_role,
            f"첫 item이 root_roles에 없음 — 2a type 선택 오류 가능성",
            expected=f"one of {root_roles}",
            actual=first_role,
        ))

    # stack: [(node_id, role)] — 현재 열려있는 조상 경로
    stack = []  # (node_id, role)

    for i, item in enumerate(flat_items):
        role = item.get("role", "")
        text = item.get("text", "")

        if not role:
            result.violations.append(GrammarViolation(
                "empty_role", i, "", "role이 비어있음",
            ))
            continue

        # role이 이 type의 grammar에 있는지
        if role not in type_grammar and role != title_role:
            result.violations.append(GrammarViolation(
                "unknown_role", i, role,
                f"type grammar에 없는 role",
                expected=f"one of {sorted(type_grammar.keys())}",
                actual=role,
            ))
            # violation이어도 노드 추가 (orphan)
            node = {"id": i, "parent_id": None, "role": role, "text": text,
                    "violation": "unknown_role"}
            result.nodes.append(node)
            continue

        # singleton 체크
        grammar_entry = type_grammar.get(role, {})
        singleton_counts[role] = singleton_counts.get(role, 0) + 1
        if grammar_entry.get("singleton") and singleton_counts[role] > 1:
            result.violations.append(GrammarViolation(
                "singleton_duplicate", i, role,
                f"singleton role이 {singleton_counts[role]}번째 등장",
                expected="1", actual=str(singleton_counts[role]),
            ))

        # parent 찾기
        parent_id = None
        violation_on_parent = None

        # Case 1: root role → parent는 chapter title
        if role in root_roles:
            parent_id = None
            stack.clear()

        # Case 2: stack에서 이 role을 자식으로 허용하는 부모 찾기
        else:
            candidates = []
            for idx in range(len(stack) - 1, -1, -1):
                ancestor_id, ancestor_role = stack[idx]
                ancestor_grammar = type_grammar.get(ancestor_role, {})
                if role in ancestor_grammar.get("allowed_children", []):
                    candidates.append((idx, ancestor_id, ancestor_role))

            if len(candidates) == 0:
                violation_on_parent = GrammarViolation(
                    "no_valid_parent", i, role,
                    f"grammar상 유효한 부모가 없음. stack: {[r for _, r in stack]}",
                    expected=f"parent with {role} in allowed_children",
                    actual="none found",
                )
                result.violations.append(violation_on_parent)
                # best-effort: ROOT에 붙이되 violation 기록
                parent_id = None

            elif len(candidates) == 1:
                pop_to_idx, parent_id, parent_role = candidates[0]
                stack = stack[:pop_to_idx + 1]

            else:
                # 가장 가까운(깊은) 조상 선택 (proximity rule)
                # Grammar가 여러 부모를 허용하는 건 자연스러운 현상
                # (예: *보충노트가 ➊ 아래에도, ▪ 아래에도 올 수 있음)
                # proximity로 결정 가능하면 ambiguous가 아님
                closest = candidates[0]
                pop_to_idx, parent_id, parent_role = closest
                stack = stack[:pop_to_idx + 1]

        # 노드 추가 (violation이 있어도 항상 추가)
        node_id = i
        node = {"id": node_id, "parent_id": parent_id, "role": role, "text": text}
        if violation_on_parent:
            node["violation"] = violation_on_parent.violation_type
        result.nodes.append(node)
        stack.append((node_id, role))

    # failure type 결정
    if result.violations:
        vtypes = {v.violation_type for v in result.violations}
        # planning_failure: 2a가 type을 잘못 골랐을 가능성
        #   - 첫 item이 root에 없음 (wrong_type_assignment)
        #   - ROOT에 붙은 non-root role이 전체 violations의 과반 (invalid_root_child)
        invalid_root_count = sum(
            1 for v in result.violations if v.violation_type == "invalid_root_child"
        )
        if "wrong_type_assignment" in vtypes:
            result.failure_type = "planning_failure"
        elif invalid_root_count >= len(result.violations) // 2 and invalid_root_count >= 2:
            result.failure_type = "planning_failure"
        else:
            result.failure_type = "generation_failure"

    return result


def validate_reconstruction(
    recon: ReconstructionResult,
    type_grammar: dict,
    root_roles: list[str],
) -> list[GrammarViolation]:
    """
    Reconstruction 결과에 대한 추가 validation.
    - required (non-optional) role 누락
    - root 이외의 role이 ROOT에 직접 붙어있는지
    - 전체적 구조 일관성

    Returns:
        추가 violation 목록 (recon.violations에 append됨)
    """
    extra = []

    # 사용된 role 집합
    used_roles = {n["role"] for n in recon.nodes}

    # required role 누락 체크 (optional=False인 role)
    for role, g in type_grammar.items():
        if not g.get("optional", True) and role not in used_roles:
            extra.append(GrammarViolation(
                "missing_required_role", -1, role,
                f"required role이 생성되지 않음",
                expected=role, actual="(absent)",
            ))

    # ROOT에 붙은 role이 root_roles에 있는지
    for node in recon.nodes:
        if node["parent_id"] is None and node["role"] not in root_roles:
            extra.append(GrammarViolation(
                "invalid_root_child", node["id"], node["role"],
                f"ROOT 직속 자식으로 허용되지 않는 role",
                expected=f"one of {root_roles}",
                actual=node["role"],
            ))

    recon.violations.extend(extra)
    if extra and not recon.failure_type:
        recon.failure_type = "generation_failure"

    return extra


def validate_text_quality(
    flat_items: list[dict],
    role_text_types: dict | None = None,
    role_markers: dict | None = None,
    expected_item_range: tuple[int, int] | None = None,
) -> list[dict]:
    """
    6-lite: text 품질 검사 (warning only, assemble 차단 안 함).

    검사 항목:
    - heading role 텍스트 길이 과다 (>80자)
    - marker contamination (text가 expected marker로 시작)
    - item count expected range 이탈

    Returns:
        [{"type": "heading_too_long"|"marker_contamination"|"item_count_mismatch",
          "item_index": N, "role": str, "detail": str, "severity": "warning"}]
    """
    warnings = []
    rtt = role_text_types or {}
    markers = role_markers or {}

    for i, item in enumerate(flat_items):
        role = item.get("role", "")
        text = item.get("text", "")
        tt = rtt.get(role, {})
        text_type = tt.get("text_type", "")

        # heading length check
        if text_type == "heading" and len(text) > 80:
            warnings.append({
                "type": "heading_too_long",
                "item_index": i,
                "role": role,
                "detail": f"heading role에 {len(text)}자 (>80). text: {text[:40]}...",
                "severity": "warning",
            })

        # marker contamination: text가 role의 known marker로 시작하는지
        expected_marker = markers.get(role, "")
        if expected_marker and text.lstrip().startswith(expected_marker):
            warnings.append({
                "type": "marker_contamination",
                "item_index": i,
                "role": role,
                "detail": f"text가 마커 '{expected_marker}'로 시작: {text[:30]}",
                "severity": "info",
            })

    # item count range check
    if expected_item_range:
        lo, hi = expected_item_range
        actual = len(flat_items)
        if actual < lo or actual > hi:
            warnings.append({
                "type": "item_count_mismatch",
                "item_index": -1,
                "role": "",
                "detail": f"item count {actual}, expected range [{lo}, {hi}]",
                "severity": "warning",
            })

    return warnings


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Validation contract — 11_validation_summary builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_VALIDATION_CHECKS = [
    # --- blocker (gate_ready=True, gate_enabled=False for contract phase) ---
    {
        "check_id": "A1", "name": "wrong_type_assignment",
        "source_file": "09", "owner_stage": "2a_type_selection",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "low",
        "violation_type": "wrong_type_assignment",
        "suggested_action": "inspect_type_selection",
        "notes": "첫 item ∉ root_roles → type 선택 오류",
    },
    {
        "check_id": "A2", "name": "empty_role",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "none",
        "violation_type": "empty_role",
        "suggested_action": "fix_generation",
        "notes": "role 필드 누락 → 구조적 불가 상태",
    },
    {
        "check_id": "A3", "name": "unknown_role",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "low",
        "violation_type": "unknown_role",
        "suggested_action": "fix_generation",
        "notes": "grammar에 없는 role 생성",
    },
    {
        "check_id": "A5", "name": "no_valid_parent",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "low",
        "violation_type": "no_valid_parent",
        "suggested_action": "fix_generation",
        "notes": "grammar상 부모 없음 → tree 깨짐",
    },
    {
        "check_id": "A7", "name": "invalid_root_child",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "low",
        "violation_type": "invalid_root_child",
        "suggested_action": "fix_generation",
        "notes": "ROOT 직속에 부적절한 role",
    },
    # --- blocker (assemble) ---
    {
        "check_id": "C1", "name": "assemble_command_fail",
        "source_file": "10", "owner_stage": "assemble",
        "severity": "blocker", "gate_candidate": True, "gate_ready": True,
        "gate_enabled": False,
        "false_positive_risk": "none",
        "violation_type": None,
        "suggested_action": "assemble_fix",
        "notes": "assemble 명령 실행 실패 → 출력 손상 가능",
    },
    # --- warning ---
    {
        "check_id": "A4", "name": "singleton_duplicate",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "warning", "gate_candidate": True, "gate_ready": False,
        "gate_enabled": False,
        "false_positive_risk": "medium",
        "violation_type": "singleton_duplicate",
        "suggested_action": "inspect_grammar",
        "notes": "grammar singleton 플래그 정확도 미검증",
    },
    {
        "check_id": "A6", "name": "missing_required_role",
        "source_file": "09", "owner_stage": "2b_generation",
        "severity": "warning", "gate_candidate": True, "gate_ready": False,
        "gate_enabled": False,
        "false_positive_risk": "high",
        "violation_type": "missing_required_role",
        "suggested_action": "inspect_grammar",
        "notes": "optional 플래그 대부분 true → 거의 트리거 안 됨",
    },
]

# Check definitions that are NOT in _VALIDATION_CHECKS (different collection logic)
_CHECK_E1 = {
    "check_id": "E1", "name": "heading_too_long",
    "source_file": "09", "owner_stage": "2b_generation",
    "severity": "warning", "gate_candidate": False, "gate_ready": False,
    "gate_enabled": False,
    "false_positive_risk": "high",
    "suggested_action": "inspect_text_type_classification",
    "notes": "heading text_type 분류 role 중 장문 statement 성격 가능성 — role semantics 기반 재검토 필요",
}
_CHECK_B1 = {
    "check_id": "B1", "name": "marker_wrong_sequence_pre",
    "source_file": "09b", "owner_stage": "2b_generation",
    "severity": "watch", "gate_candidate": False, "gate_ready": False,
    "gate_enabled": False,
    "false_positive_risk": "high",
    "suggested_action": "observe",
    "notes": "rewrite 전 분석. 대량 발생이 정상. B3 구현 후 비교 기준",
}
_CHECK_C2 = {
    "check_id": "C2", "name": "chapter_count_mismatch",
    "source_file": "10", "owner_stage": "assemble",
    "severity": "watch", "gate_candidate": False, "gate_ready": False,
    "gate_enabled": False,
    "false_positive_risk": "low",
    "suggested_action": "observe",
    "notes": "body_split vs tree chapter count 불일치",
}
_CHECK_C3 = {
    "check_id": "C3", "name": "node_count_mismatch",
    "source_file": "10", "owner_stage": "assemble",
    "severity": "watch", "gate_candidate": False, "gate_ready": False,
    "gate_enabled": False,
    "false_positive_risk": "low",
    "suggested_action": "observe",
    "notes": "chapter 내 body vs tree node count 불일치",
}
_CHECK_B3 = {
    "check_id": "B3", "name": "marker_post_rewrite_mismatch",
    "source_file": "(미구현)", "owner_stage": "marker_rewrite",
    "severity": "later", "gate_candidate": True, "gate_ready": False,
    "gate_enabled": False,
    "false_positive_risk": "low",
    "suggested_action": "implement",
    "notes": "placeholder — rewrite 후 marker 검증 후보. 구현 후 false positive 평가 필요",
}


def build_validation_summary(
    grammar_result: dict | None,
    marker_analysis: dict | None,
    assemble_result: dict | None,
    *,
    template_hash: str = "",
    model: str = "",
    total_chapters: int = 0,
) -> dict:
    """
    09, 09b, 10 데이터를 기반으로 validation contract summary를 생성.

    Returns:
        11_validation_summary.json에 쓸 dict
    """
    from datetime import datetime

    checks = []

    # ── A-group + C1: grammar violations (09) + assemble fail (10) ──
    all_violations = []
    chapters_checked = 0
    total_items_checked = 0
    if grammar_result:
        chapters_checked = len(grammar_result.get("chapters", []))
        for ch in grammar_result.get("chapters", []):
            nodes = ch.get("reconstructed_tree", [])
            total_items_checked += len(nodes)
            for v in ch.get("violations", []):
                v["_chapter_idx"] = ch.get("idx")
                all_violations.append(v)

    for check_def in _VALIDATION_CHECKS:
        vtype = check_def["violation_type"]

        # C1 (assemble_command_fail) — violation_type=None, 별도 수집
        if vtype is None and check_def["check_id"] == "C1":
            c1_fail = 0
            c1_checked = 0
            if assemble_result:
                c1_fail = assemble_result.get("fail_count", 0)
                c1_checked = assemble_result.get("success_count", 0) + c1_fail
            checks.append({
                **{k: v for k, v in check_def.items() if k != "violation_type"},
                "observed_count": c1_fail,
                "checked_count": c1_checked,
                "affected_chapters": [],
                "evidence_fields": ["fail_count", "errors[]"],
            })
            continue

        matched = [v for v in all_violations if v.get("type") == vtype]
        affected = sorted({v["_chapter_idx"] for v in matched if v.get("_chapter_idx") is not None})
        is_item_level = vtype not in ("wrong_type_assignment", "missing_required_role")
        checks.append({
            **{k: v for k, v in check_def.items() if k != "violation_type"},
            "observed_count": len(matched),
            "checked_count": total_items_checked if is_item_level else chapters_checked,
            "affected_chapters": affected,
            "evidence_fields": [f"chapters[].violations[?type=='{vtype}']"],
        })

    # ── E1: heading_too_long (09) ──
    e1_count = 0
    e1_chapters = set()
    if grammar_result:
        for ch in grammar_result.get("chapters", []):
            for w in ch.get("text_quality_warnings", []):
                if w.get("type") == "heading_too_long":
                    e1_count += 1
                    e1_chapters.add(ch.get("idx"))
    checks.append({
        **_CHECK_E1,
        "observed_count": e1_count,
        "checked_count": total_items_checked,
        "affected_chapters": sorted(e1_chapters),
        "evidence_fields": ["chapters[].text_quality_warnings[?type=='heading_too_long']"],
    })

    # ── B1: marker wrong_sequence pre-rewrite (09b) ──
    b1_count = 0
    b1_checked = 0
    b1_chapters = set()
    if marker_analysis:
        for ch in marker_analysis.get("chapters", []):
            b1_checked += ch.get("total_items", 0)
            for a in ch.get("analysis", []):
                if a.get("issue") == "wrong_sequence":
                    b1_count += 1
                    b1_chapters.add(ch.get("idx"))
    checks.append({
        **_CHECK_B1,
        "observed_count": b1_count,
        "checked_count": b1_checked,
        "affected_chapters": sorted(b1_chapters),
        "evidence_fields": ["chapters[].analysis[?issue=='wrong_sequence']"],
    })

    # ── C2/C3: rewrite alignment (10) ──
    alignment = {}
    has_alignment_data = False
    if assemble_result:
        alignment = assemble_result.get("rewrite_alignment", {})
        has_alignment_data = bool(alignment)

    c2_observed = 0 if alignment.get("chapter_count_match", True) else 1
    checks.append({
        **_CHECK_C2,
        "check_status": "checked" if has_alignment_data else "skipped_no_data",
        "observed_count": c2_observed if has_alignment_data else None,
        "checked_count": 1 if has_alignment_data else 0,
        "affected_chapters": [],
        "evidence_fields": ["rewrite_alignment.chapter_count_match",
                            "rewrite_alignment.body_split_count",
                            "rewrite_alignment.tree_chapter_count"],
    })

    per_chapter = alignment.get("per_chapter", [])
    c3_mismatched = [pc for pc in per_chapter if not pc.get("aligned", True)]
    checks.append({
        **_CHECK_C3,
        "check_status": "checked" if per_chapter else "skipped_no_data",
        "observed_count": len(c3_mismatched) if per_chapter else None,
        "checked_count": len(per_chapter) if per_chapter else 0,
        "affected_chapters": [pc["chapter_idx"] for pc in c3_mismatched],
        "evidence_fields": ["rewrite_alignment.per_chapter[?aligned==false]"],
    })

    # ── B3: placeholder ──
    checks.append({
        **_CHECK_B3,
        "check_status": "not_implemented",
        "observed_count": None,
        "checked_count": None,
        "affected_chapters": None,
        "evidence_fields": [],
    })

    # ── summary 집계 ──
    severity_summary = {}
    for c in checks:
        sev = c["severity"]
        if sev not in severity_summary:
            severity_summary[sev] = {"defined": 0, "triggered": 0}
        severity_summary[sev]["defined"] += 1
        if c["observed_count"] and c["observed_count"] > 0:
            severity_summary[sev]["triggered"] += 1

    return {
        "schema_version": "0.1",
        "generated_at": datetime.now().isoformat(),
        "template_hash": template_hash,
        "model": model,
        "total_chapters": total_chapters,
        "summary": severity_summary,
        "checks": checks,
    }


def extract_marker_policies(
    paragraphs: list[dict],
    marker_policy_1f: dict | None = None,
) -> dict:
    """
    role별 marker_policy를 추출.

    우선순위:
    1. marker_policy_1f (1f AI 결과, verified) — explicit + consistent인 것만
    2. 기존 1a marker field 기반 (fallback)

    Returns:
        {role: {"markers": [...], "family": str, "policy_type": str,
                "style": "fixed"|"sequence", "separator": str,
                "source": "1f"|"1a"}}
    """
    from collections import defaultdict

    role_markers_ordered = defaultdict(list)
    role_separators = {}
    for p in paragraphs:
        role = p.get("role", "")
        marker = p.get("marker", "").strip()
        if not role or not marker:
            continue
        if marker not in role_markers_ordered[role]:
            role_markers_ordered[role].append(marker)
        # separator 추출: marker 뒤 첫 문자 (공백/탭/없음)
        text = p.get("text", "") or ""
        if marker and text.startswith(marker):
            after = text[len(marker):]
            if after and after[0] in (" ", "\t"):
                role_separators.setdefault(role, after[0])

    result = {}
    for role, markers in role_markers_ordered.items():
        family = _normalize_marker_type(markers[0]) if markers else ""

        # style: sequence(순서형) vs fixed(고정형)
        if len(markers) >= 2:
            style = "sequence"
        else:
            style = "fixed"

        # family 기반 더 구체적인 분류
        family_map = {
            "roman": "roman_sequence",
            "dingbat_neg_circle": "circled_sequence",
            "circle_num_pua": "circled_pua_sequence",
            "circle_num": "circled_num_sequence",
            "num_paren": "num_paren_sequence",
        }
        if family in family_map:
            policy_type = family_map[family]
        elif style == "sequence" and markers[0].isdigit():
            policy_type = "arabic_sequence"
        elif family.startswith("char_"):
            char = family[5:]
            if char == "*" and len(markers) >= 2:
                policy_type = "star_depth"
            else:
                policy_type = "fixed_char"
        else:
            policy_type = "fixed" if style == "fixed" else "sequence"

        result[role] = {
            "markers": markers,
            "family": family,
            "policy_type": policy_type,
            "style": style,
            "separator": role_separators.get(role, " "),
            "source": "1a",
            "table_kind": "not_applicable",  # 1a fallback 기본값 — 1f 결과 있으면 아래서 override
        }

    # 1f 결과 병합: verified + explicit인 것만 우선 사용 + table_kind는 항상 보존
    if marker_policy_1f:
        for role_entry in marker_policy_1f.get("roles", []):
            role = role_entry.get("role", "")

            # table_kind는 1f가 판단한 것 항상 우선 (verification 무관 — tbl 구조 사실 기반)
            _tk_1f = role_entry.get("table_kind") or "not_applicable"
            if role in result:
                result[role]["table_kind"] = _tk_1f

            status = role_entry.get("marker_policy_status", "")
            verification = role_entry.get("verification", "")

            if status == "explicit_marker_detected" and verification == "consistent":
                observed = role_entry.get("evidence", [])
                markers_1f = [
                    e["detected_marker"] for e in observed
                    if e.get("detected_marker")
                ]
                # 중복 제거하면서 순서 보존
                seen = set()
                unique_markers = []
                for m in markers_1f:
                    if m not in seen:
                        seen.add(m)
                        unique_markers.append(m)

                if not unique_markers:
                    continue

                policy_type_1f = role_entry.get("policy_type", "")
                family_1f = role_entry.get("marker_family", "")
                separator_1f = role_entry.get("separator", " ")
                style_1f = "fixed" if len(unique_markers) == 1 else "sequence"

                # 기존 1a 결과와 충돌 감지
                if role in result and result[role]["source"] == "1a":
                    old = result[role]
                    if old["policy_type"] != policy_type_1f or old["markers"] != unique_markers:
                        log.info(
                            f"[MARKER-POLICY] 1f overrides 1a for {role}: "
                            f"1a={old['policy_type']}:{old['markers']} → "
                            f"1f={policy_type_1f}:{unique_markers}"
                        )

                result[role] = {
                    "markers": unique_markers,
                    "family": family_1f,
                    "policy_type": policy_type_1f,
                    "style": style_1f,
                    "separator": separator_1f,
                    "source": "1f",
                    "table_kind": _tk_1f,  # 1f가 결정한 table_kind 보존
                }

            elif status == "no_marker" and verification == "not_applicable":
                # 1f가 no_marker로 판정 + 1a에도 없음 → 확정
                if role not in result:
                    pass  # 이미 없으므로 추가할 것 없음
                # 1a에는 있지만 1f가 no_marker → conflict
                elif role in result:
                    log.warning(
                        f"[MARKER-POLICY] conflict: 1a has markers for {role} "
                        f"but 1f says no_marker"
                    )

    return result


def analyze_marker_in_text(
    flat_items: list[dict],
    marker_policies: dict,
) -> list[dict]:
    """
    2b output의 각 item에서 marker를 감지하고 content를 분리.

    Returns:
        [{role, raw_text, detected_marker, content, expected_policy_type,
          marker_match, issue}]
    """
    results = []
    # role별 sibling counter (같은 parent 아래 같은 role 카운트)
    sibling_counts: dict[str, int] = {}

    for item in flat_items:
        role = item.get("role", "")
        text = item.get("text", "")
        policy = marker_policies.get(role, {})
        markers = policy.get("markers", [])
        policy_type = policy.get("policy_type", "unknown")
        sep = policy.get("separator", " ")

        # sibling index
        sibling_counts[role] = sibling_counts.get(role, 0) + 1
        sibling_idx = sibling_counts[role]

        # marker 감지: text 앞부분이 known markers 중 하나와 일치하는지
        detected_marker = ""
        content = text
        for m in sorted(markers, key=len, reverse=True):
            stripped = text.lstrip()
            if stripped.startswith(m):
                detected_marker = m
                after = stripped[len(m):]
                # separator 제거
                if after and after[0] in (" ", "\t"):
                    content = after[1:]
                else:
                    content = after
                break

        # expected marker (sequence면 sibling_idx 기반)
        expected_marker = ""
        if markers:
            if policy.get("style") == "sequence" and sibling_idx <= len(markers):
                expected_marker = markers[sibling_idx - 1]
            elif policy.get("style") == "fixed":
                expected_marker = markers[0]

        # match 판정
        if not detected_marker:
            issue = "no_marker_in_text"
            marker_match = None
        elif detected_marker == expected_marker:
            issue = ""
            marker_match = True
        elif policy.get("style") == "sequence":
            issue = "wrong_sequence"
            marker_match = False
        else:
            issue = ""
            marker_match = True

        results.append({
            "role": role,
            "raw_text": text[:60],
            "detected_marker": detected_marker,
            "content": content[:60],
            "expected_marker": expected_marker,
            "expected_policy_type": policy_type,
            "sibling_index": sibling_idx,
            "marker_match": marker_match,
            "issue": issue,
        })

    return results


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


def compute_sibling_cooccurrence_rules(
    paragraphs: list[dict],
    idx_texts: dict | None = None,
) -> list[dict]:
    """양식 paragraph 전수 분석 — parent별 instance-aware variant + sample 추출.

    2b prompt가 "기본 배타 + 양식 관찰 variant만 허용" 룰 적용하기 위한 데이터.
    variant 단위로 분리해서 각 variant에 representative instance sample (marker + text)
    명시 — AI가 양식 instance와 variant 매핑 직관적으로 보도록.

    Returns:
        [{
          "parent": str,
          "all_children_clusters": [str, ...],
          "cooccurred_pairs": [[a, b], ...],
          "instance_count": int,
          "variants": [
            {
              "variant_id": "v1",
              "child_set": [str, ...],     # 정렬된 cluster list
              "samples": [
                {"marker": str, "text_preview": str, "first_idx": int},
                ...                          # variant 안 instance들의 sample (최대 3개)
              ],
              "instance_count": int,
            },
            ...
          ],
        }, ...]

    variant 단위 분리 기준: 같은 child set (frozenset) 가진 instance끼리 같은 variant.
    """
    from collections import defaultdict

    paragraphs = paragraphs or []
    idx_texts = idx_texts or {}

    # 각 paragraph idx → paragraph dict 매핑 (parent 조회용)
    idx_to_p = {p.get("idx"): p for p in paragraphs if p.get("idx") is not None}

    # parent paragraph idx → set of child cluster ids
    parent_to_children: dict = defaultdict(set)
    for p in paragraphs:
        role = p.get("role", "")
        parent_idx = p.get("parent_idx")
        if not role or parent_idx is None:
            continue
        parent_to_children[parent_idx].add(role)

    # role 별 자식 있는 instance 수 — heading role 식별용
    # (자식 있는 instance 가 1개 이상 있으면 그 role 은 heading. 자식 없는 instance 도 등록 대상)
    role_has_children_count: dict = defaultdict(int)
    for parent_idx in parent_to_children:
        parent_p = idx_to_p.get(parent_idx)
        if parent_p:
            parent_role = parent_p.get("role", "")
            if parent_role:
                role_has_children_count[parent_role] += 1

    # role → list of (paragraph idx, child_set)
    # heading role (같은 role 중 자식 있는 instance 가 있는 role) 은 자식 없는 instance 도 등록.
    # 이로써 cluster_8 같이 첫 번째 instance 는 자식 풍부 + 두 번째 instance 는 빈 heading 인 경우
    # 두 번째 instance 가 variant `child_set: []` 로 살아남음 (2026-05-25 fix).
    role_instances: dict = defaultdict(list)
    for p in paragraphs:
        pidx = p.get("idx")
        role = p.get("role", "")
        if pidx is None or not role:
            continue
        if role_has_children_count.get(role, 0) > 0:
            children = parent_to_children.get(pidx, set())
            role_instances[role].append((pidx, frozenset(children)))

    rules = []
    for parent_role, instances in role_instances.items():
        if not parent_role or not instances:
            continue

        # 자식 cluster union
        all_children: set = set()
        for _, cs in instances:
            all_children.update(cs)
        all_children_list = sorted(all_children)

        # cooccurred pairs (legacy, debug)
        cooccurred: set = set()
        for _, cs in instances:
            cs_sorted = sorted(cs)
            for i in range(len(cs_sorted)):
                for j in range(i + 1, len(cs_sorted)):
                    cooccurred.add((cs_sorted[i], cs_sorted[j]))
        cooccurred_pairs = [list(p) for p in sorted(cooccurred)]

        # variant 단위 instance grouping (같은 child_set frozenset)
        # 빈 child_set 도 별도 variant 로 등록 — 자식 없는 heading instance 누락 방지 (2026-05-25 fix).
        variant_groups: dict = defaultdict(list)  # child_set frozenset → list of parent_idx
        for parent_idx, cs in instances:
            variant_groups[cs].append(parent_idx)

        variants = []
        for vi, (child_set_fs, parent_idxs) in enumerate(
            sorted(variant_groups.items(), key=lambda x: sorted(x[0]))
        ):
            # 각 variant 안 instance의 sample (parent paragraph의 marker + text)
            samples = []
            for pidx in parent_idxs[:3]:  # 최대 3개 sample
                parent_p = idx_to_p.get(pidx) or {}
                marker = parent_p.get("marker", "")
                # text는 idx_texts에서. lookup string + int 둘 다 시도
                text = (
                    idx_texts.get(str(pidx))
                    or idx_texts.get(pidx, "")
                    or ""
                )
                samples.append({
                    "marker": marker,
                    "text_preview": text[:80] if text else "",
                    "first_idx": pidx,
                })
            variants.append({
                "variant_id": f"v{vi + 1}",
                "child_set": sorted(child_set_fs),
                "samples": samples,
                "instance_count": len(parent_idxs),
            })

        rules.append({
            "parent": parent_role,
            "all_children_clusters": all_children_list,
            "cooccurred_pairs": cooccurred_pairs,
            "instance_count": len(instances),
            "variants": variants,
        })
    return rules


def compute_exclusivity_rules_code(parent_instances: dict) -> list[dict]:
    """
    1d 코드 구현 — 자식 쌍 공존 카운트 → 배타 variant 묶음.

    AI 호출 대체. 결정적·고속·무토큰.

    Args:
        parent_instances: {parent_role: [{children_set}, ...]}
                          compute_parent_instance_children() 결과

    Returns:
        [{"parent": str, "variants": [[role, ...], ...],
          "pairs_cooccurred": [[a, b], ...]}, ...]
    """
    from itertools import combinations

    rules = []
    for parent, instances in parent_instances.items():
        if not instances or len(instances) < 2:
            continue

        # 모든 자식 role 수집
        all_children = set()
        for inst in instances:
            all_children |= set(inst)
        if len(all_children) < 2:
            continue

        # 쌍별 co-occurrence count
        pair_cooc = {}
        for inst in instances:
            inst_set = set(inst)
            for a, b in combinations(sorted(inst_set), 2):
                pair_cooc[(a, b)] = pair_cooc.get((a, b), 0) + 1

        # 공존한 쌍 기록 (공존 안 한 쌍은 자동 배타)
        cooccur_pairs = []
        has_never = False
        for a, b in combinations(sorted(all_children), 2):
            if pair_cooc.get((a, b), 0) > 0:
                cooccur_pairs.append([a, b])
            else:
                has_never = True

        if not has_never:
            # 모든 쌍이 한 번 이상 공존 → 배타 없음
            continue

        # variants = co-occurrence 그래프의 maximal cliques
        # 그래프: 같이 등장한 적 있는 두 자식 사이에 edge (self-loop 금지)
        adj = {c: set() for c in all_children}
        for (a, b), cnt in pair_cooc.items():
            if cnt > 0:
                adj[a].add(b)
                adj[b].add(a)

        # Bron-Kerbosch maximal clique (singleton도 자동으로 잡힘)
        cliques = []
        def _bk(R, P, X):
            if not P and not X:
                if R:
                    cliques.append(frozenset(R))
                return
            for v in list(P):
                _bk(R | {v}, P & adj[v], X & adj[v])
                P = P - {v}
                X = X | {v}
        _bk(set(), set(all_children), set())
        # 부분집합 제거 (maximal만)
        maximal = []
        for c in cliques:
            if not any(c < other for other in cliques):
                maximal.append(c)
        # 중복 제거
        unique_maximal = []
        seen = set()
        for c in maximal:
            if c not in seen:
                seen.add(c)
                unique_maximal.append(c)

        rules.append({
            "parent": parent,
            "variants": [sorted(list(v)) for v in unique_maximal],
            "pairs_cooccurred": cooccur_pairs,
        })

    return rules


def compute_format_rules_code(observations: dict) -> dict:
    """
    1e 코드 구현 — 관측 카운트 기반 format_rules + blank_rules.

    AI 호출 대체. 결정적·고속·무토큰.

    Args:
        observations: compute_format_observations() 결과
                      {role_formats: {role: {indent_parts_samples, first_text_samples,
                                              marker_samples_from_ai}},
                       transitions: [...]}

    Returns:
        {format_rules: {role: {indent_parts, marker_style, markers_sample, separator}},
         blank_rules: [{from, to, relation, has_blank, paraPrIDRef?}]}
    """
    from collections import Counter

    role_formats_obs = observations.get("role_formats", {})
    transitions = observations.get("transitions", [])

    format_rules = {}
    for role, samples in role_formats_obs.items():
        # indent_parts: 가장 흔한 패턴
        indent_samples = samples.get("indent_parts_samples", [])
        if indent_samples:
            tup_samples = []
            for s in indent_samples:
                if isinstance(s, list):
                    tup_samples.append(tuple(
                        (d.get("type"), d.get("count")) for d in s if isinstance(d, dict)
                    ))
                else:
                    tup_samples.append(())
            most_common_tup = Counter(tup_samples).most_common(1)[0][0]
            indent_parts = []
            for t, c in most_common_tup:
                d = {"type": t}
                if c is not None:
                    d["count"] = c
                indent_parts.append(d)
        else:
            indent_parts = []

        # markers
        marker_samples = samples.get("marker_samples_from_ai", []) or []
        markers_clean = [m for m in marker_samples if m]
        unique_markers = list(dict.fromkeys(markers_clean))  # preserve order, dedupe

        if not unique_markers:
            marker_style = "fixed"
            markers_sample = [""]
        elif len(unique_markers) == 1:
            marker_style = "fixed"
            markers_sample = unique_markers
        else:
            # 같은 family면 enumerate, 다르면 fixed (fallback)
            families = set(_normalize_marker_type(m) for m in unique_markers)
            if len(families) <= 1:
                marker_style = "enumerate"
            else:
                marker_style = "fixed"
            markers_sample = unique_markers

        # separator: first_text_samples에서 marker 다음 공백 추출
        first_texts = samples.get("first_text_samples", [])
        sep_candidates = []
        for ft in first_texts:
            if not isinstance(ft, str) or not ft:
                continue
            for mk in unique_markers:
                if mk and ft.startswith(mk):
                    rest = ft[len(mk):]
                    # 첫 비공백 전까지의 공백을 separator로
                    sep = ""
                    for ch in rest:
                        if ch in (" ", "\t", " "):
                            sep += ch
                        else:
                            break
                    sep_candidates.append(sep)
                    break
        separator = " "
        if sep_candidates:
            separator = Counter(sep_candidates).most_common(1)[0][0]

        format_rules[role] = {
            "indent_parts": indent_parts,
            "marker_style": marker_style,
            "markers_sample": markers_sample,
            "separator": separator,
        }

    # blank_rules
    blank_rules = []
    for t in transitions:
        rule = {
            "from": t.get("from"),
            "to": t.get("to"),
            "relation": t.get("relation"),
            "has_blank": bool(t.get("has_blank")),
        }
        ppr = t.get("blank_paraPrIDRef") or t.get("paraPrIDRef")
        if ppr:
            rule["paraPrIDRef"] = ppr
        blank_rules.append(rule)

    return {"format_rules": format_rules, "blank_rules": blank_rules}


def compute_paragraph_features(paragraphs: list[dict]) -> list[dict]:
    """
    각 문단에 local feature를 추가 (AI 1·AI 2 입력용).

    추가되는 필드:
    - marker_family: _normalize_marker_type 결과
    - prev_marker, prev_marker_family
    - next_marker, next_marker_family
    - same_paraPr_run: 직전 문단과 같은 paraPrIDRef를 공유하는지 (양식 작성자가 같은 위계로 묶었다는 신호)

    원본 paragraphs는 변경하지 않고 새 list 반환.
    """
    n = len(paragraphs)
    enriched = []
    for i, p in enumerate(paragraphs):
        new_p = dict(p)
        marker = p.get("marker", "")
        new_p["marker_family"] = _normalize_marker_type(marker)

        prev_marker = paragraphs[i-1].get("marker", "") if i > 0 else ""
        next_marker = paragraphs[i+1].get("marker", "") if i < n - 1 else ""
        new_p["prev_marker"] = prev_marker
        new_p["next_marker"] = next_marker
        new_p["prev_marker_family"] = _normalize_marker_type(prev_marker)
        new_p["next_marker_family"] = _normalize_marker_type(next_marker)

        prev_para_pr = paragraphs[i-1].get("paraPrIDRef", "") if i > 0 else ""
        new_p["same_paraPr_run"] = bool(
            prev_para_pr and prev_para_pr == p.get("paraPrIDRef", "")
        )

        # 본문 첫 글자 형식(body_first_charpr) 일치 신호 — paraPrIDRef는 paragraph
        # 외부 형식(들여쓰기·줄간격)만 표현하므로 글자 크기·폰트는 못 잡음.
        # body_first_charpr는 paragraph 안 실제 첫 글자가 박힌 run의 charPr ID로,
        # 사람 눈에 보이는 글자 형식 자체. 같으면 같은 시각 형식, 다르면 다름.
        prev_body_cp = paragraphs[i-1].get("body_first_charpr", "") if i > 0 else ""
        new_p["same_body_charpr_run"] = bool(
            prev_body_cp and prev_body_cp == p.get("body_first_charpr", "")
        )

        enriched.append(new_p)
    return enriched


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

CHAPTER_CLASSIFY_PROMPT = """당신은 양식에 내용을 담는 **편집자/기획자**입니다.
소스 문서의 내용을 깊이 이해한 뒤, 그 내용을 **가장 잘 표현할 수 있는 방식**으로 양식 구성을 설계합니다.

## 핵심 관점: "구조 매칭"이 아닌 "표현 최적화"

당신의 임무는 소스 구조를 양식 구조와 1:1로 맞추는 게 아닙니다.
**"이 내용을 독자에게 어떻게 잘 전달할까?"** 관점에서 양식 type을 선택합니다.

- 소스가 **비정형**(뉴스, 메모, 회의록, 에세이 등) — 원래 chapter가 없어도 OK. 당신이 내용 기반으로 나누세요.
- 소스가 **구조화**(보고서, 규정) — 기존 구조 활용 가능하지만 꼭 따를 필요는 없음.
- 핵심은: **이 내용을 보여주기에 어떤 type이 최적인가?**

## 작업 순서

### Step 1: 소스 내용 이해 (구조 무관)

소스가 무엇을 전달하려 하는지 파악:
- 핵심 메시지는 무엇?
- 주요 정보 (사실/주장/데이터)는?
- 보조 정보 (배경·예시·수치·인용·반응)는?
- 결론/시사점/전망은?

### Step 2: 양식 구성 설계 (편집적 창의성)

이 소스를 어떻게 "보여줄까" 결정:
- 소스 한 덩어리를 여러 chapter로 나누는 게 나은가? 하나로 충분한가?
- 소스가 뉴스라면: "개요 + 배경 + 상세 + 영향" 같이 chapter를 창작할 수 있음
- 소스에 명시된 주제 구분이 있으면 활용 가능 (강제 X)
- chapter 개수는 **내용 표현을 가장 잘 하는 개수** (1개든 N개든)

### Step 2.5: 각 chapter의 **최적 구조 예측** ⭐ (핵심)

type을 고르기 전에, **각 chapter의 내용을 객관적으로 분석**하여 **그 내용을 가장 잘 담을 최적 구조**를 예측합니다.
**자유로운 상상이 아니라 소스 근거 기반 분석**입니다.

**분석 절차**:
1. 이 chapter에 해당하는 **소스 내용을 실제로 정독** (단순 제목 추측 금지)
2. 내용의 **의미 단위**를 식별: 섹션/주제/세부항목/보충/참고 등
3. 의미 단위들을 **어떻게 그룹핑/계층화**하면 가장 잘 전달되나 판단
4. 결과: 최적 구조 트리 (top-level items, sub-items, depth, total)

**기록해야 할 항목**:
- **rationale** (소스 근거): 이 chapter 내용이 구체적으로 어떤 성격(요약성/분석적/나열적 등)이고 왜 이런 구조가 최적인지
- **hierarchy**: top-level 항목 수, 각 top 아래 자식 수, 전체 깊이, 총 항목 수
- **content_nature**: 요약적 / 분석적 / 나열적 / 서술적 / 조항·규정 / 설명적 / 기타

### 예시

- "정부 신규 정책 발표 뉴스" chapter
  - 소스 내용: 정책 발표 3가지 핵심 + 반응 2건 + 시행 일정
  - rationale: 사실 나열+반응 참고 위주, 분석 단계 없음 → 2단 구조로 충분
  - hierarchy: top_level=3 (정책 3개), sub_items_per_top=1-2 (부연/예시), depth=2, total=7-10
  - content_nature: 나열적+보충

- "정책 평가 보고서" chapter
  - 소스 내용: 검토배경 + 운영평가 (여러 관점) + 보완 조치계획
  - rationale: 논리 전개 3단계(배경→평가→조치), 각 단계가 여러 세부 섹션 포함 → 깊은 계층 필요
  - hierarchy: top_level=3, sub_items_per_top=3-5, depth=3-4, total=20+
  - content_nature: 분석적+논리전개

### Step 3: 최적 구조와 가장 닮은 type 선택

**Step 2.5에서 상상한 내용을 기준으로** 양식 type 중 가장 잘 담을 것을 선택합니다.
단순히 구조 유사성만 보지 말고, **상상한 모습이 실제로 이 type에 담기는 그림**을 머릿속에 그려보세요.

판단 기준 (우선순위):

**(1) Role 구조 적합성 — 가장 중요**
상상한 구성 요소가 이 type의 role 조합에 자연스럽게 매핑되나?
- 상상: "요약 박스 + 여러 세부 항목 + 참고 박스" → role에 summary/detail/reference 있는 type
- 상상: "전략 + 과제 + 세부계획" → `strategy > task > subtask` 깊은 계층 type

**(2) Pattern 흐름 적합성**
이 type의 반복·옵션·계층 구조가 상상한 내용 전개를 지원하나?
- 상상: "주제 → 사례 반복" → `section > detail(multiple)` pattern
- 상상: "목록 나열" → 반복 가능한 단순 list pattern

**(3) 용기(capacity) 적합성 — 매우 중요**
type의 깊이·role 수 vs 상상한 항목 수·계층:
- 상상한 항목이 **3-5개, 1-2단** → **단순 type** (role 2-3개, 1-2단)
- 상상한 항목이 **10-15개, 2-3단** → **중간 type** (role 4-6개, 2-3단)
- 상상한 항목이 **20+개, 3단 이상** → **깊은 type** (role 6+개, 4단+)
- ⚠️ 상상한 내용이 단순한데 깊은 type 선택하면 → **빈 슬롯 많아지거나 AI가 허구 생성**

**(4) Top-level role 이름의 기능 힌트**
type의 최상위 role 이름에서 성격 유추:
- `strategy_*` — 전략·방향성 내용
- `numbered_section_*` — 번호 매긴 논리 전개
- `summary_box` 중심 — 요약성 내용
- `regulation_clause` — 조·항 구조
- 상상한 chapter의 기능과 매칭

## 핵심 원칙

- **같은 type 여러 chapter에 반복 사용 OK** — 소스에 비슷한 성격 주제 여럿이면
- **사용 안 하는 type이 있어도 OK** — 소스에 그런 성격 내용 없으면
- **chapter 개수 ≠ type 개수** — 소스 표현에 필요한 만큼
- 소스에 명확한 대제목 있으면 **title에 순수 제목 텍스트만 작성** (번호·장번호·마커·기호·불릿 제외)
- 없으면 chapter의 핵심을 한 줄로 요약한 title 작성

## 출력 형식

```json
{
  "chapters": [
    {
      "type": "type_X",
      "title": "이 chapter에 배치할 내용의 순수 제목 텍스트 (번호·마커 제외)",
      "optimal_structure": {
        "rationale": "이 chapter 내용이 OOO한 성격이라 XXX 구조가 최적 (소스 근거 언급)",
        "hierarchy": {
          "top_level_items": <숫자 or 범위>,
          "sub_items_per_top": <숫자 or 범위>,
          "depth": <1-6 정수>,
          "total_items": <총 항목 수 추정>
        },
        "content_nature": "요약적 / 분석적 / 나열적 / 서술적 / 조항·규정 / 설명적 / 기타"
      },
      "type_match_reason": "위 최적 구조가 type_X의 pattern과 일치하는 이유 (role 조합, depth, capacity 관점)",
      "rejected_types": [
        {"type": "type_Y", "reason": "이 type을 선택하지 않은 구체적 이유"}
      ],
      "confidence": "high"
    },
    ...
  ],
  "header": {
    "<양식 header role 이름>": "소스에서 추출한 값",
    ...
  }
}
```

⭐ **`optimal_structure`, `type_match_reason`, `rejected_types` 필드는 필수**.

규칙:
- `rationale`: **반드시 소스 내용 근거** — "이 chapter가 OOO이기 때문에" 형태로 구체적 근거
- `hierarchy`: **숫자로 명시** — "많음/적음" 같은 모호한 표현 금지
- `type_match_reason`: **최적 구조와 type pattern의 대응 관계** — role 조합, depth, suggested_count 중 무엇이 맞는지

`confidence`:
- `high`: 최적 구조가 선택한 type의 pattern과 잘 맞음 (role 조합, depth, capacity)
- `medium`: 약간 어긋나지만 제공된 type 중 최선
- `low`: 적합한 type 없어 불가피한 선택

## 예시 상황

**상황 A — 뉴스 기사 1편**
→ chapters 1-3개: "사건 개요"(단순 type) + "배경 분석"(중간 type) + "영향/시사점"(단순 type)
→ type 개수와 무관하게 내용 표현 중심으로 결정

**상황 B — 보고서 (대제목 5개)**
→ 기존 대제목 활용해도 OK, 내용상 합치거나 나눠도 OK
→ 각 chapter에 표현 최적 type 선택 (같은 type 반복 사용 OK)

**상황 C — 회의록 (안건 3개)**
→ 안건마다 chapter. 각 안건의 내용 성격 보고 type 선택
→ 짧은 안건은 단순 type, 심층 논의된 안건은 깊은 type

**상황 D — 짧은 메모 (1쪽)**
→ chapters 1개만: 내용 담기에 충분한 단순 type 하나

## 금지사항

- ❌ 양식에 없는 새 type 이름 만들기
- ❌ 소스에 없는 내용 창작하기
- ❌ 양식 type 개수에 맞춰 억지로 chapter 수 맞추기
- ❌ 구조 유사성만 보고 기능·표현 적합성 무시하기
- ❌ chapter title에 번호·장번호·마커·기호·불릿 포함 (예: "1.", "Ⅰ.", "□", "*", "(1)", "가.")

## 중요
- 반드시 JSON만 출력. 다른 설명 포함 금지
- chapters의 type은 user 메시지의 "양식 대제목 타입 목록"에 있는 이름만 사용
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage-separated debug file writer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def write_stage_debug_files(
    debug_payload: dict,
    debug_dir: str = "/tmp/hwpx_debug",
) -> dict:
    """
    debug_payload를 단계별 파일로 분리 저장.

    Returns:
        {filename: "ok" | "skip" | "error: ..."} status dict
    """
    import os
    from datetime import datetime

    # 이전 실행 잔재: 현재 payload에 없는 파일만 남는 문제 방지
    # → 매 호출 시 기존 파일 전부 삭제 후 현재 payload 기준으로 재생성
    import glob as _glob_mod
    os.makedirs(debug_dir, exist_ok=True)
    for old in _glob_mod.glob(os.path.join(debug_dir, "*.json")):
        os.remove(old)
    results = {}

    def _write(filename: str, data: dict) -> None:
        path = os.path.join(debug_dir, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            results[filename] = "ok"
        except Exception as e:
            results[filename] = f"error: {e}"

    def _skip(filename: str) -> None:
        results[filename] = "skip"

    # ── shortcuts ──
    struct_after = debug_payload.get("structure_after_split", {})
    struct_before = debug_payload.get("structure_before_split", {})
    paras_after = struct_after.get("paragraphs", [])
    paras_before = struct_before.get("paragraphs", [])
    chapter_types = struct_after.get("chapter_types", {})
    template_grammar = struct_after.get("template_grammar", {})
    role_cands = debug_payload.get("1b_role_candidates", {})
    level_data = debug_payload.get("1c_structure_global", {})
    parent_corr = debug_payload.get("parent_correction", {})
    clustering = debug_payload.get("1e_canonical_clustering", {})
    classify = debug_payload.get("chapter_classify", {})
    section_fill = debug_payload.get("section_fill", [])
    assembly = debug_payload.get("assembly", {})

    # ═══════════════════════════════════════════════════════════════
    # 01. Template paragraph analysis (1a/1b)
    # ═══════════════════════════════════════════════════════════════
    if paras_before or role_cands:
        rc_list = role_cands.get("role_candidates", [])
        rc_by_idx = {}
        if isinstance(rc_list, list):
            for rc in rc_list:
                if isinstance(rc, dict):
                    rc_by_idx[rc.get("idx", rc.get("paragraph_idx"))] = rc

        rows = []
        for p in (paras_before or paras_after):
            idx = p.get("idx")
            rc = rc_by_idx.get(idx, {})
            rows.append({
                "idx": idx,
                "marker": p.get("marker", ""),
                "description": p.get("description", ""),
                "paraPrIDRef": p.get("paraPrIDRef", p.get("paraStyleId", "")),
                "charPrIDRef": p.get("charPrIDRef", p.get("charStyleId", "")),
                "text_preview": p.get("text", "")[:80],
                "role_candidates": rc.get("candidates", rc.get("role_candidates", [])),
            })
        _write("01_template_paragraph_analysis.json", {
            "paragraph_count": len(rows),
            "paragraphs": rows,
        })
    else:
        _skip("01_template_paragraph_analysis.json")

    # ═══════════════════════════════════════════════════════════════
    # 02. Level + parent tree (1c + parent correction)
    # ═══════════════════════════════════════════════════════════════
    if paras_after:
        tree_rows = []
        for p in paras_after:
            tree_rows.append({
                "idx": p.get("idx"),
                "level": p.get("level"),
                "parent_idx": p.get("parent_idx"),
                "sibling_group_id": p.get("sibling_group_id"),
                "role": p.get("role", ""),
                "marker": p.get("marker", ""),
            })

        # parent correction diff
        before_paras = parent_corr.get("before_paragraphs", [])
        after_paras = parent_corr.get("after_paragraphs", [])
        correction_diff = []
        if before_paras and after_paras:
            before_map = {p.get("idx"): p for p in before_paras}
            for ap in after_paras:
                idx = ap.get("idx")
                bp = before_map.get(idx, {})
                if bp.get("parent_idx") != ap.get("parent_idx"):
                    correction_diff.append({
                        "idx": idx,
                        "role": ap.get("role", ""),
                        "parent_before": bp.get("parent_idx"),
                        "parent_after": ap.get("parent_idx"),
                    })

        _write("02_level_parent_tree.json", {
            "paragraph_count": len(tree_rows),
            "paragraphs": tree_rows,
            "parent_correction": {
                "diff_count": len(correction_diff),
                "diff": correction_diff,
                "reattach_log": parent_corr.get("reattach_log", []),
                "reparent_log": parent_corr.get("reparent_log", []),
            },
            "level_decisions": level_data.get("decisions", {}),
        })
    else:
        _skip("02_level_parent_tree.json")

    # ═══════════════════════════════════════════════════════════════
    # 03. Role clustering (1e)
    # ═══════════════════════════════════════════════════════════════
    if paras_after:
        clusters: dict[str, dict] = {}
        for p in paras_after:
            role = p.get("role", "")
            if not role:
                continue
            if role not in clusters:
                clusters[role] = {
                    "idx_list": [],
                    "markers": [],
                    "descriptions": [],
                    "parent_roles": set(),
                    "child_roles": set(),
                }
            c = clusters[role]
            c["idx_list"].append(p.get("idx"))
            m = p.get("marker", "").strip()
            if m and m not in c["markers"]:
                c["markers"].append(m)
            d = p.get("description", "")
            if d and d not in c["descriptions"]:
                c["descriptions"].append(d)

        # parent/child relationships
        idx_role = {p.get("idx"): p.get("role", "") for p in paras_after}
        for p in paras_after:
            role = p.get("role", "")
            pidx = p.get("parent_idx")
            if role and pidx is not None and pidx in idx_role:
                pr = idx_role[pidx]
                if pr:
                    clusters[role]["parent_roles"].add(pr)
                    if pr in clusters:
                        clusters[pr]["child_roles"].add(role)

        # convert sets to sorted lists
        for c in clusters.values():
            c["parent_roles"] = sorted(c["parent_roles"])
            c["child_roles"] = sorted(c["child_roles"])
            c["count"] = len(c["idx_list"])

        _write("03_role_clustering.json", {
            "cluster_count": len(clusters),
            "clusters": clusters,
            "role_registry": clustering.get("role_registry", {}),
            "per_type_role_semantics": struct_after.get("per_type_role_semantics", {}),
        })
    else:
        _skip("03_role_clustering.json")

    # ═══════════════════════════════════════════════════════════════
    # 04. Chapter types
    # ═══════════════════════════════════════════════════════════════
    if chapter_types:
        def _ct_depth(pat):
            if not pat:
                return 0
            return max(
                (1 + _ct_depth(v.get("children", {}))) if v.get("children") else 1
                for v in pat.values()
            )

        def _ct_roles(pat, acc=None):
            if acc is None:
                acc = set()
            for r, v in pat.items():
                acc.add(r)
                if v.get("children"):
                    _ct_roles(v["children"], acc)
            return acc

        per_type = template_grammar.get("per_type", {})
        types_out = {}
        for tn, ti in chapter_types.items():
            pat = ti.get("pattern", {})
            tg = per_type.get(tn, {})
            roles = sorted(_ct_roles(pat))
            # evidence: paragraphs with these roles
            evidence = [
                p.get("idx") for p in paras_after
                if p.get("role") in roles
            ]
            types_out[tn] = {
                "title_role": ti.get("title_role", ""),
                "description": ti.get("description", ""),
                "root_roles": tg.get("root_roles", sorted(pat.keys())),
                "max_depth": _ct_depth(pat),
                "included_roles": roles,
                "role_count": len(roles),
                "evidence_idx": evidence[:50],
                "pattern": pat,
            }

        _write("04_chapter_types.json", {
            "type_count": len(types_out),
            "types": types_out,
        })
    else:
        _skip("04_chapter_types.json")

    # ═══════════════════════════════════════════════════════════════
    # 05. Template grammar
    # ═══════════════════════════════════════════════════════════════
    if template_grammar:
        per_type_out = {}
        for tn, tg in template_grammar.get("per_type", {}).items():
            grammar = tg.get("grammar", {})
            per_type_out[tn] = {
                "root_roles": tg.get("root_roles", []),
                "title_role": tg.get("title_role", ""),
                "grammar": grammar,
            }

        _write("05_template_grammar.json", {
            "type_count": len(per_type_out),
            "per_type": per_type_out,
            "global": template_grammar.get("global", {}),
            "observed_transitions": template_grammar.get("observed_transitions", []),
        })
    else:
        _skip("05_template_grammar.json")

    # ═══════════════════════════════════════════════════════════════
    # 05b. Cache validation (from debug_payload, re-written here
    #      because debug_dir cleanup at start deletes the early copy)
    # ═══════════════════════════════════════════════════════════════
    _cv_data = debug_payload.get("cache_validation")
    if _cv_data:
        from datetime import datetime as _dt2
        _write("05b_cache_validation.json", {
            "generated_at": _dt2.now().isoformat(),
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            **_cv_data,
        })
    else:
        _skip("05b_cache_validation.json")

    # ═══════════════════════════════════════════════════════════════
    # 05c. Marker policy induction (1f)
    # ═══════════════════════════════════════════════════════════════
    _mp1f = debug_payload.get("marker_policy_1f")
    if _mp1f:
        _write("05c_marker_policy_induction.json", _mp1f)
    else:
        _skip("05c_marker_policy_induction.json")

    # ═══════════════════════════════════════════════════════════════
    # 06. Type catalog for 2a prompt
    # ═══════════════════════════════════════════════════════════════
    if chapter_types and paras_after:
        catalog_text = _build_rich_type_catalog(
            chapter_types, template_grammar or None, paras_after,
        )
        # per-type structured summary
        per_type_grammar = (template_grammar or {}).get("per_type", {})
        type_summaries = {}
        for tn, ti in chapter_types.items():
            pat = ti.get("pattern", {})
            tg = per_type_grammar.get(tn, {})
            type_summaries[tn] = {
                "root_roles": tg.get("root_roles", sorted(pat.keys())),
                "depth": _ct_depth(pat) if chapter_types else 0,
                "role_count": len(_ct_roles(pat)) if chapter_types else 0,
            }

        _write("06_type_catalog_for_2a_prompt.json", {
            "catalog_text": catalog_text,
            "type_summaries": type_summaries,
        })
    else:
        _skip("06_type_catalog_for_2a_prompt.json")

    # ═══════════════════════════════════════════════════════════════
    # 07. 2a type selection result
    # ═══════════════════════════════════════════════════════════════
    if classify:
        chapters_out = []
        for ch in classify.get("chapters", []):
            chapters_out.append({
                "title": ch.get("title", ""),
                "selected_type": ch.get("type", ""),
                "optimal_structure": ch.get("optimal_structure", {}),
                "type_match_reason": ch.get("type_match_reason", ""),
                "rejected_types": ch.get("rejected_types", []),
                "confidence": ch.get("confidence", ""),
            })

        _write("07_2a_type_selection_result.json", {
            "chapter_count": len(chapters_out),
            "chapters": chapters_out,
            "header": classify.get("header_data", classify.get("header", {})),
            "header_roles": classify.get("header_roles", []),
        })
    else:
        _skip("07_2a_type_selection_result.json")

    # ═══════════════════════════════════════════════════════════════
    # 07b. Source split decision log
    # ═══════════════════════════════════════════════════════════════
    _source_split = debug_payload.get("source_split_decision")
    if _source_split:
        # underfill/overfill candidate 집계 (section_fill과 결합)
        _ss_per_ch = _source_split.get("per_chapter", [])
        _ss_src_len = _source_split.get("source_length", 0)
        _underfill = []
        for _si, _ch_d in enumerate(_ss_per_ch):
            _sf_entry = section_fill[_si] if _si < len(section_fill) else {}
            _gen_items = _sf_entry.get("items_count", 0)
            _ch_d["generated_items"] = _gen_items
            _chunk = _ch_d.get("chunk_length", 0)
            if _chunk < 500 and _gen_items == 0:
                _ch_d["allocation_status"] = "underfill_candidate"
                _underfill.append(_si)
            elif _ss_src_len > 0 and _chunk / _ss_src_len > 0.8:
                _ch_d["allocation_status"] = "overfill_candidate"
            else:
                _ch_d["allocation_status"] = "normal"
        _source_split["underfill_chapters"] = _underfill
        _write("07b_source_split_decision.json", _source_split)
    else:
        _skip("07b_source_split_decision.json")

    # ═══════════════════════════════════════════════════════════════
    # 08. 2b generation by chapter
    # ═══════════════════════════════════════════════════════════════
    if section_fill:
        chapters_gen = []
        for sf in section_fill:
            items = sf.get("items", [])
            role_seq = [it.get("role", "") for it in items if isinstance(it, dict)]
            ch_entry = {
                "idx": sf.get("idx"),
                "chapter_title": sf.get("chapter_title", ""),
                "selected_type": sf.get("chapter_type", ""),
                "items_count": len(items),
                "items": items,
                "role_sequence": role_seq,
                "pattern_roles": sf.get("pattern_roles", []),
            }
            # 8.0a: normalize/validate 지표
            if sf.get("normalize_diff"):
                ch_entry["normalize_diff"] = sf["normalize_diff"]
            if sf.get("raw_items"):
                ch_entry["raw_items"] = sf["raw_items"]
            if sf.get("parent_id_stats"):
                ch_entry["parent_id_stats"] = sf["parent_id_stats"]
            if sf.get("chapter_context"):
                ch_entry["chapter_context"] = sf["chapter_context"]
            # 13.6-B: grammar override debug
            for _gf in ("validation_grammar_source", "override_root_roles", "override_grammar_role_count"):
                if sf.get(_gf) is not None:
                    ch_entry[_gf] = sf[_gf]
            chapters_gen.append(ch_entry)

        _write("08_2b_generation_by_chapter.json", {
            "chapter_count": len(chapters_gen),
            "chapters": chapters_gen,
        })
    else:
        _skip("08_2b_generation_by_chapter.json")

    # ═══════════════════════════════════════════════════════════════
    # 08b. Shallow generation result (13.3)
    # ═══════════════════════════════════════════════════════════════
    _shallow = debug_payload.get("shallow_generation")
    if _shallow:
        _write("08b_shallow_generation.json", _shallow)
    else:
        _skip("08b_shallow_generation.json")

    # ═══════════════════════════════════════════════════════════════
    # 09. Grammar validation result
    # ═══════════════════════════════════════════════════════════════
    if section_fill:
        val_chapters = []
        total_pass = 0
        total_fail = 0
        for sf in section_fill:
            gv = sf.get("grammar_validation")
            if gv:
                passed = gv.get("success", False)
                if passed:
                    total_pass += 1
                else:
                    total_fail += 1
                ch_val = {
                    "idx": sf.get("idx"),
                    "chapter_title": sf.get("chapter_title", ""),
                    "selected_type": sf.get("chapter_type", ""),
                    "success": passed,
                    "failure_type": gv.get("failure_type"),
                    "violation_count": gv.get("violation_count", 0),
                    "violations": gv.get("violations", []),
                    "reconstructed_tree": gv.get("nodes", []),
                    "text_quality_warnings": sf.get("text_quality_warnings", []),
                }
                # 8.0a: parent_id 검증 지표
                if sf.get("parent_id_stats"):
                    ch_val["parent_id_stats"] = sf["parent_id_stats"]
                if sf.get("chapter_context"):
                    ch_val["chapter_context"] = sf["chapter_context"]
                val_chapters.append(ch_val)
            else:
                val_chapters.append({
                    "idx": sf.get("idx"),
                    "chapter_title": sf.get("chapter_title", ""),
                    "selected_type": sf.get("chapter_type", ""),
                    "success": None,
                    "note": "grammar_validation not available",
                })

        _write("09_grammar_validation_result.json", {
            "total_pass": total_pass,
            "total_fail": total_fail,
            "chapters": val_chapters,
        })
    else:
        _skip("09_grammar_validation_result.json")

    # ═══════════════════════════════════════════════════════════════
    # 09b. Marker analysis
    # ═══════════════════════════════════════════════════════════════
    _marker_chapters_for_11 = None  # 11번에서 재사용
    if paras_after and section_fill:
        policies = extract_marker_policies(paras_after)
        marker_chapters = []
        for sf in section_fill:
            items = sf.get("items", [])
            analysis = analyze_marker_in_text(items, policies)
            issues = [a for a in analysis if a.get("issue")]
            marker_chapters.append({
                "idx": sf.get("idx"),
                "chapter_type": sf.get("chapter_type", ""),
                "total_items": len(items),
                "marker_issues": len(issues),
                "analysis": analysis,
            })
        _write("09b_marker_analysis.json", {
            "marker_policies": policies,
            "chapters": marker_chapters,
        })
        _marker_chapters_for_11 = marker_chapters
    else:
        _skip("09b_marker_analysis.json")

    # ═══════════════════════════════════════════════════════════════
    # 10. Assemble result
    # ═══════════════════════════════════════════════════════════════
    if assembly:
        _write("10_assemble_result.json", {
            "success_count": assembly.get("success_count", 0),
            "fail_count": assembly.get("fail_count", 0),
            "errors": assembly.get("errors", []),
            "output_size": assembly.get("output_size", 0),
            "marker_rewrite_log": assembly.get("marker_rewrite_log", []),
            "rewrite_alignment": assembly.get("rewrite_alignment", {}),
            "section_info": assembly.get("section_info"),
        })
    else:
        _skip("10_assemble_result.json")

    # ═══════════════════════════════════════════════════════════════
    # 11. Validation summary (contract)
    # ═══════════════════════════════════════════════════════════════
    try:
        # 09 grammar result — section_fill에서 직접 추출
        grammar_result_data = None
        if section_fill:
            _gv_chapters = []
            for sf in section_fill:
                gv = sf.get("grammar_validation")
                if gv:
                    _gv_chapters.append({
                        "idx": sf.get("idx"),
                        "violations": gv.get("violations", []),
                        "reconstructed_tree": gv.get("nodes", []),
                        "text_quality_warnings": sf.get("text_quality_warnings", []),
                    })
            grammar_result_data = {"chapters": _gv_chapters}

        # 09b marker analysis — 위에서 이미 계산한 _marker_chapters_for_11 재사용
        marker_analysis_data = (
            {"chapters": _marker_chapters_for_11}
            if _marker_chapters_for_11 else None
        )

        # 10 assemble result
        assemble_data = None
        if assembly:
            assemble_data = {
                "success_count": assembly.get("success_count", 0),
                "fail_count": assembly.get("fail_count", 0),
                "rewrite_alignment": assembly.get("rewrite_alignment", {}),
            }

        summary = build_validation_summary(
            grammar_result=grammar_result_data,
            marker_analysis=marker_analysis_data,
            assemble_result=assemble_data,
            template_hash=debug_payload.get("template_hash", ""),
            model=debug_payload.get("model", ""),
            total_chapters=len(classify.get("chapters", [])),
        )
        _write("11_validation_summary.json", summary)
    except Exception as e:
        log.warning(f"[DEBUG-HWPX] 11_validation_summary 생성 실패: {e}")
        results["11_validation_summary.json"] = f"error: {e}"

    # ═══════════════════════════════════════════════════════════════
    # 12. Structural intent observation (Stage 11)
    # ═══════════════════════════════════════════════════════════════
    if paras_after:
        _si_global_grammar = template_grammar.get("global", {}) if template_grammar else {}
        _si_idx_to_role = {p.get("idx"): p.get("role", "") for p in paras_after}

        # actual children: idx가 다른 문단의 parent_idx로 참조되는지
        _si_actual_parent_idxs = set()
        for p in paras_after:
            _pidx = p.get("parent_idx")
            if _pidx is not None:
                _si_actual_parent_idxs.add(_pidx)

        _si_per_para = []
        for p in paras_after:
            _si_role = p.get("role", "")
            if not _si_role:
                continue
            _si_desc = p.get("description", "")
            _si_level = p.get("level", 0)
            _si_has_ch_grammar = bool(
                _si_global_grammar.get(_si_role, {}).get("allowed_children")
            )
            _si_has_ch_actual = p.get("idx") in _si_actual_parent_idxs
            _si_pidx = p.get("parent_idx")
            _si_prole = _si_idx_to_role.get(_si_pidx, "") if _si_pidx is not None else ""

            _si_tag = infer_semantic_tag(
                _si_desc, _si_has_ch_grammar, _si_level, _si_prole, "grammar",
            )
            _si_per_para.append({
                "idx": p.get("idx"),
                "role": _si_role,
                "description": _si_desc[:80],
                "level": _si_level,
                "has_children_by_grammar": _si_has_ch_grammar,
                "has_actual_children": _si_has_ch_actual,
                "parent_role": _si_prole,
                "semantic_tag": _si_tag["semantic_tag"],
                "inference_mode": _si_tag["inference_mode"],
                "matched_keywords": _si_tag["matched_keywords"],
                "children_signal_source": "grammar",
            })

        # cluster distribution
        from collections import defaultdict as _ddict
        _si_ctags = _ddict(lambda: _ddict(int))
        _si_ctotals = _ddict(int)
        for _e in _si_per_para:
            _si_ctags[_e["role"]][_e["semantic_tag"]] += 1
            _si_ctotals[_e["role"]] += 1

        _si_dist = {}
        _si_poly = []
        _si_mono = []
        for _r in sorted(_si_ctags.keys()):
            _tags = dict(_si_ctags[_r])
            _total = _si_ctotals[_r]
            _is_poly = len(_tags) >= 2
            _dom = max(_tags, key=_tags.get) if _tags else ""
            _dom_ratio = round(_tags[_dom] / _total, 3) if _total else 0
            _si_dist[_r] = {
                "total": _total,
                "tags": _tags,
                "is_polysemous": _is_poly,
                "dominant_tag": _dom,
                "dominant_ratio": _dom_ratio,
            }
            (_si_poly if _is_poly else _si_mono).append(_r)

        _write("12_structural_intent.json", {
            "template_semantics": {
                "per_paragraph": _si_per_para,
                "cluster_semantic_distribution": _si_dist,
                "polysemous_clusters": _si_poly,
                "monomorphic_clusters": _si_mono,
                "total_clusters": len(_si_dist),
                "polysemous_count": len(_si_poly),
                "monomorphic_count": len(_si_mono),
            },
        })
    else:
        _skip("12_structural_intent.json")

    # ═══════════════════════════════════════════════════════════════
    # 12b. Style profile (Stage 11.2) — v2 key "style_profile" (singular)
    # ═══════════════════════════════════════════════════════════════
    _sp_data = debug_payload.get("style_profile") or debug_payload.get("style_profiles")
    if _sp_data:
        _write("12b_style_profile.json", _sp_data)
    else:
        _skip("12b_style_profile.json")

    # ═══════════════════════════════════════════════════════════════
    # 12d. Emphasis layers (Stage 11.2b — markup 기반)
    # ═══════════════════════════════════════════════════════════════
    _em_data = debug_payload.get("emphasis_layer") or {}
    _el_data = _em_data.get("emphasis_layers")
    if _el_data:
        _write("12d_emphasis_layers.json", {
            "from_cache": _em_data.get("from_cache", False),
            "emphasis_cluster_count": _em_data.get("emphasis_cluster_count", 0),
            "emphasis_layers": _el_data,
            "paragraph_emphasis_map_summary": _em_data.get("paragraph_emphasis_map_summary", {}),
        })
    else:
        _skip("12d_emphasis_layers.json")

    # ═══════════════════════════════════════════════════════════════
    # 13. Template unit observation (12.0)
    # ═══════════════════════════════════════════════════════════════
    _tuo = debug_payload.get("template_unit_observation")
    if _tuo:
        _write("13_template_unit_observation.json", _tuo)
    else:
        _skip("13_template_unit_observation.json")

    # ═══════════════════════════════════════════════════════════════
    # 14. Marker roundtrip readiness (12.1 Phase 1)
    # ═══════════════════════════════════════════════════════════════
    _mrt = debug_payload.get("marker_roundtrip_readiness")
    if _mrt:
        _write("14_marker_roundtrip_readiness.json", _mrt)
    else:
        _skip("14_marker_roundtrip_readiness.json")

    # ═══════════════════════════════════════════════════════════════
    # 15. Target unit planning (12.2)
    # ═══════════════════════════════════════════════════════════════
    _tup = debug_payload.get("target_unit_planning")
    if _tup:
        _write("15_target_unit_planning.json", _tup)
    else:
        _skip("15_target_unit_planning.json")

    # ═══════════════════════════════════════════════════════════════
    # 16. Source blocks (13.0 debug-only adapter output)
    # ═══════════════════════════════════════════════════════════════
    _sb = debug_payload.get("source_blocks")
    if _sb:
        _write("16_source_blocks.json", _sb)
    else:
        _skip("16_source_blocks.json")

    # ═══════════════════════════════════════════════════════════════
    # 17. Section role proposals (13.7b B2.2, AI sub-step, debug-only)
    # ═══════════════════════════════════════════════════════════════
    _srp_dbg = debug_payload.get("section_role_proposals")
    if _srp_dbg:
        _write("17_section_role_proposals.json", _srp_dbg)
    else:
        _skip("17_section_role_proposals.json")

    # ═══════════════════════════════════════════════════════════════
    # 18. Merge feasibility (13.7b B0b, debug-only)
    # ═══════════════════════════════════════════════════════════════
    _mf_dbg = debug_payload.get("merge_feasibility")
    if _mf_dbg:
        _write("18_merge_feasibility.json", _mf_dbg)
    else:
        _skip("18_merge_feasibility.json")

    # ═══════════════════════════════════════════════════════════════
    # 13_7b_b0b_observation.json — review artifact (사용자+claude review 후 채움)
    # ═══════════════════════════════════════════════════════════════
    _b0b_artifact = debug_payload.get("b0b_observation_artifact")
    if _b0b_artifact:
        _write("13_7b_b0b_observation.json", _b0b_artifact)
    else:
        _skip("13_7b_b0b_observation.json")

    # ═══════════════════════════════════════════════════════════════
    # 19. Section-local decisions (13.7b section-local generation-lite, debug-only)
    # ═══════════════════════════════════════════════════════════════
    _sld = debug_payload.get("section_local_decisions")
    if _sld:
        _write("19_section_local_decisions.json", _sld)
    else:
        _skip("19_section_local_decisions.json")

    _scl_dbg = debug_payload.get("section_local_chapter_lists")
    if _scl_dbg:
        _write("20_section_local_chapter_lists.json", _scl_dbg)
    else:
        _skip("20_section_local_chapter_lists.json")

    # ═══════════════════════════════════════════════════════════════
    # 99. Debug summary
    # ═══════════════════════════════════════════════════════════════
    sf_pass = sum(
        1 for sf in section_fill
        if (sf.get("grammar_validation") or {}).get("success")
    )
    sf_fail = sum(
        1 for sf in section_fill
        if sf.get("grammar_validation") and not sf["grammar_validation"].get("success")
    )

    # cache_validation 요약 (정상 완료 시에만 기록, abort 시에는 05b가 증거)
    _cv = debug_payload.get("cache_validation")
    _cv_summary = {}
    if _cv:
        _cv_summary = {
            "cache_validation_present": True,
            "cache_validation_can_cache": _cv.get("can_cache"),
            "cache_validation_should_abort": _cv.get("should_abort"),
            "cache_validation_blocker_count": _cv.get("blocker_count", 0),
            "cache_validation_watch_count": _cv.get("watch_count", 0),
        }
    else:
        _cv_summary = {"cache_validation_present": False}

    # section_info 요약
    _si = assembly.get("section_info") if assembly else None
    _si_summary = {}
    if _si:
        _si_summary = {
            "section_count": _si.get("section_count", 0),
            "append_target_section": _si.get("append_target_section", 0),
            "secpr_carrier_warning_count": len(_si.get("secpr_carrier_warnings", [])),
            "secpr_conflict_warning_count": len(_si.get("secpr_conflict_warnings", [])),
            "residual_candidate_count": len(_si.get("residual_candidates", [])),
        }

    _write("99_debug_summary.json", {
        "timestamp": datetime.now().isoformat(),
        "model": debug_payload.get("model", ""),
        "from_cache": debug_payload.get("from_cache", False),
        "stage_status": results.copy(),
        "paragraph_count": len(paras_after),
        "table_count": len(struct_after.get("tables", [])),
        "chapter_types": sorted(chapter_types.keys()),
        "source_chapters": len(classify.get("chapters", [])),
        "grammar_validation_pass": sf_pass,
        "grammar_validation_fail": sf_fail,
        "assembly_success": assembly.get("success_count", 0),
        "assembly_fail": assembly.get("fail_count", 0),
        **_cv_summary,
        **_si_summary,
    })

    log.info(
        f"[DEBUG-HWPX] stage files written to {debug_dir}: "
        + ", ".join(f"{k}={v}" for k, v in results.items() if v != "skip")
    )
    return results


def _build_rich_type_catalog(
    chapter_types: dict,
    template_grammar: dict | None = None,
    paragraphs: list[dict] | None = None,
) -> str:
    """
    chapter_types + grammar + paragraph descriptions → 2a 프롬프트용 type catalog.

    각 type에 대해:
    - 구조 트리 (marker + semantic description)
    - depth, role count
    - 적합/부적합 소스 구조 힌트
    - 예상 항목 수 범위
    """
    # ── 1. role → (markers, description) 매핑 ──
    role_meta: dict[str, dict] = {}
    for p in (paragraphs or []):
        role = p.get("role", "")
        if not role:
            continue
        marker = p.get("marker", "").strip()
        desc = p.get("description", "")
        if role not in role_meta:
            role_meta[role] = {"markers": [], "desc": desc}
        if marker and marker not in role_meta[role]["markers"]:
            role_meta[role]["markers"].append(marker)

    per_type_grammar = (template_grammar or {}).get("per_type", {})

    # ── helpers ──
    def _pdepth(pat: dict) -> int:
        if not pat:
            return 0
        return max(
            1 + _pdepth(info.get("children", {})) if info.get("children") else 1
            for info in pat.values()
        )

    def _proles(pat: dict) -> int:
        return sum(
            1 + _proles(info.get("children", {}))
            for info in pat.values()
        )

    def _role_label(role: str) -> str:
        meta = role_meta.get(role, {})
        markers = meta.get("markers", [])
        short = meta.get("desc", role).split("(")[0].strip()
        m = markers[0] if markers else ""
        return f"{m} {short}".strip() if m else short

    def _deepest_chain(role: str, grammar: dict, visited: set | None = None) -> list[str]:
        if visited is None:
            visited = set()
        if role in visited:
            return []
        visited.add(role)
        children = grammar.get(role, {}).get("allowed_children", [])
        if not children:
            return [role]
        best = [role]
        for ch in children:
            cand = [role] + _deepest_chain(ch, grammar, visited.copy())
            if len(cand) > len(best):
                best = cand
        return best

    def _tree_lines(role: str, grammar: dict, indent: int = 0,
                    visited: set | None = None) -> list[str]:
        if visited is None:
            visited = set()
        if role in visited:
            return []
        visited.add(role)

        meta = role_meta.get(role, {})
        markers = meta.get("markers", [])
        desc_short = meta.get("desc", role).split("(")[0].strip()
        marker_str = ",".join(markers[:3]) if markers else "(없음)"

        g = grammar.get(role, {})
        tags = []
        if g.get("repeatable"):
            tags.append("반복")
        if g.get("optional"):
            tags.append("선택")
        tag_str = f"  [{','.join(tags)}]" if tags else ""

        prefix = "  " * indent
        lines = [f"{prefix}{marker_str} {desc_short}{tag_str}"]

        for ch in g.get("allowed_children", []):
            lines.extend(_tree_lines(ch, grammar, indent + 1, visited.copy()))
        return lines

    # ── 2. 각 type의 rich description 생성 ──
    sections = []
    for type_name, type_info in chapter_types.items():
        pattern = type_info.get("pattern", {})
        title_role = type_info.get("title_role", "")

        tg = per_type_grammar.get(type_name, {})
        root_roles = tg.get("root_roles", sorted(pattern.keys()))
        type_grammar = tg.get("grammar", {})

        depth = _pdepth(pattern)
        total = _proles(pattern)

        # one-line summary via deepest chain
        chains = [_deepest_chain(rr, type_grammar) for rr in root_roles]
        main_chain = max(chains, key=len) if chains else []
        chain_str = " → ".join(_role_label(r) for r in main_chain)

        if len(root_roles) > 1:
            other = [_role_label(r) for r in root_roles if r != main_chain[0]]
            summary_line = f"대표 경로: {chain_str}" + (f" + {', '.join(other)}" if other else "")
        else:
            summary_line = chain_str

        # tree visualization
        tree = []
        for rr in root_roles:
            tree.extend(_tree_lines(rr, type_grammar))
        tree_str = "\n".join(tree)

        # suitability hints (depth-based + role description keywords)
        all_descs = " ".join(role_meta.get(r, {}).get("desc", "") for r in type_grammar)
        has_strategy = any(k in all_descs for k in ("전략", "과제", "추진"))
        has_summary = any(k in all_descs for k in ("요약", "박스"))
        has_numbered = any(k in all_descs for k in ("번호형", "중분류"))

        if depth <= 2:
            suitable = "단순 나열, 요약, 현황 보고, 배경+항목+결론"
            unsuitable = "전략/과제 계층, 다단계 분석, 깊은 정책 계획"
            item_range = "5~15"
        elif depth <= 3:
            if len(root_roles) >= 3:
                suitable = "요약+항목 나열+결론 복합 구조, 현황 보고, 성과 나열"
            elif has_numbered:
                suitable = "번호형 논점 전개, 분석 보고, 여러 관점의 세부 분석"
            else:
                suitable = "중간 깊이 분석, 세부 항목이 있는 보고"
            unsuitable = "전략→과제→세부계획 다단계 구조, 단순 1단 나열"
            item_range = "10~30"
        else:
            if has_strategy:
                suitable = "전략/과제/세부추진항목 다단계 계획, 체계적 정책 문서"
            else:
                suitable = "깊은 계층 구조, 다단계 세부 분석"
            unsuitable = "단순 나열, 짧은 요약, 배경 설명 위주"
            item_range = "20~80"

        section = (
            f"### {type_name} — depth={depth}, {total}개 role\n"
            f"**요약**: {summary_line}\n\n"
            f"**구조 트리** (들여쓰기 = 부모→자식):\n"
            f"```\n{tree_str}\n```\n\n"
            f"**적합한 소스**: {suitable}\n"
            f"**부적합**: {unsuitable}\n"
            f"**예상 항목 수**: {item_range}개"
        )
        sections.append(section)

    return "\n\n---\n\n".join(sections)


def extract_header_roles(structure: dict) -> list[dict]:
    """structure에서 header role 목록을 description 포함하여 추출.

    level-0이고 chapter title_role이 아닌 role 중,
    첫 번째 chapter 시작 idx 이전에 위치한 것만 수집.
    """
    chapter_types = structure.get("chapter_types", {})
    paragraphs = structure.get("paragraphs", [])

    title_roles = set()
    for ct in chapter_types.values():
        tr = ct.get("title_role", "")
        if tr:
            title_roles.add(tr)

    # 첫 번째 chapter 시작 idx
    first_ch_idx = None
    for p in paragraphs:
        role = p.get("role", "")
        if role in title_roles:
            first_ch_idx = p.get("idx", 0)
            break
    if first_ch_idx is None:
        first_ch_idx = len(paragraphs)

    # level-0, title_role 아닌, first_ch_idx 이전
    seen = set()
    result = []
    for p in paragraphs:
        role = p.get("role", "")
        if not role or role in seen:
            continue
        if role in title_roles:
            continue
        if p.get("level", 0) != 0:
            continue
        if p.get("idx", 0) >= first_ch_idx:
            continue
        seen.add(role)
        result.append({
            "role": role,
            "description": p.get("description", ""),
        })
    return result


def build_chapter_classify_prompt(
    chapter_types: dict,
    header_roles: list[str] | list[dict],
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
    template_grammar: dict | None = None,
    paragraphs: list[dict] | None = None,
) -> list[dict]:
    """
    2a 호출: 소스 PDF → 대제목 추출 + 양식 타입 분류

    Args:
        chapter_types: 1차 AI가 출력한 chapter_types dict
        header_roles: 양식의 header role 목록.
            list[str] (하위 호환) 또는 list[dict] ({"role": ..., "description": ...})
        content_text: 직접 입력 텍스트
        content_images: PDF 페이지 base64 JPEG 이미지 리스트
        pdf_text: PDF에서 추출한 텍스트
        template_grammar: extract_template_grammar() 결과 (per_type grammar 포함)
        paragraphs: structure["paragraphs"] (role descriptions/markers 포함)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # 양식 타입 카탈로그 구성 (rich catalog with grammar + descriptions)
    valid_type_names = list(chapter_types.keys())
    types_text = _build_rich_type_catalog(
        chapter_types, template_grammar, paragraphs,
    )
    type_count = len(valid_type_names)
    type_names_str = ", ".join(valid_type_names) if valid_type_names else "(없음)"

    # header role 목록 — list[str] 또는 list[dict] 모두 지원
    # list[str]이면 paragraphs에서 description을 자동 조회
    _role_desc_lookup: dict[str, str] = {}
    if paragraphs:
        for p in paragraphs:
            r = p.get("role", "")
            if r and r not in _role_desc_lookup and p.get("description"):
                _role_desc_lookup[r] = p["description"]

    _header_entries: list[dict] = []
    if header_roles:
        for item in header_roles:
            if isinstance(item, dict):
                _header_entries.append(item)
            else:
                _header_entries.append({
                    "role": item,
                    "description": _role_desc_lookup.get(item, ""),
                })

    if _header_entries:
        _has_desc = any(e.get("description") for e in _header_entries)
        if _has_desc:
            header_lines = []
            for e in _header_entries:
                desc = e.get("description", "")
                header_lines.append(f"- {e['role']}: {desc}" if desc else f"- {e['role']}")
            header_text = "\n".join(header_lines)
        else:
            header_text = ", ".join(e["role"] for e in _header_entries)
        header_keys = ", ".join(e["role"] for e in _header_entries)
        header_rule = (
            f"**header에는 다음 key만 사용 가능**: {header_keys}\n"
            f"- 각 role의 description을 읽고, 소스에서 해당 의미에 맞는 값만 넣으세요\n"
            f"- 보안등급·분류표시(예: 대외비, 대외주의 등)는 제목·날짜·기관 슬롯에 넣지 마세요\n"
            f"- 소스에서 해당 슬롯에 맞는 값이 없으면 빈 문자열 \"\"을 출력하세요\n"
            f"- 목차·구성 안내처럼 소스에서 직접 추출할 값이 애매하면 빈 문자열로 두세요 (양식 원본이 보존됩니다)\n"
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
# 13.3 Shallow Generator: flat region → content-only items
# ──────────────────────────────────────────────────────────────────────

SHALLOW_FILL_PROMPT = """당신은 한국 행정문서 작성 전문가입니다.
하나의 **shallow region** (얕은 나열형 영역)에 대해, 주어진 role 목록에 따라 소스 내용을 배치합니다.

## 핵심 규칙

1. **주어진 role만 사용하세요** — 새 role 생성 금지
2. **마커/번호를 텍스트에 넣지 마세요** — content만 출력. 마커는 자동 부착됩니다.
3. **들여쓰기 넣지 마세요** — 앞 공백/탭 금지.
4. **얕은 구조**: 이 영역은 깊은 계층이 아니라 항목 나열형입니다. 대분류 제목 아래 핵심 항목을 짧게 나열하세요.
5. **소스 내용을 이 양식 구조에 맞게 재구성/요약하세요** — 소스의 주제가 양식과 달라도 양식 구조에 맞춰 배치합니다.

## 출력 형식

JSON 배열만 출력하세요:
```json
[
  {"role": "role_cluster_X", "text": "내용"},
  {"role": "role_cluster_Y", "text": "내용"},
  ...
]
```

- `role`: 아래 role 목록에 있는 것만 사용
- `text`: content만 (마커/번호/들여쓰기 없이)
"""


def build_shallow_fill_prompt(
    region_description: str,
    region_roles: list[dict],
    region_paragraph_count: int,
    source_text: str,
    content_only_mode: bool = True,
) -> list[dict]:
    """
    13.3 shallow generator prompt.

    Args:
        region_description: region 설명 (target_unit_plan에서)
        region_roles: [{role, description, marker_hint}, ...] per role in region
        region_paragraph_count: template의 paragraph 수 (참고값)
        source_text: broad source text
        content_only_mode: always True

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # role list 텍스트
    role_lines = []
    for r in region_roles:
        role_name = r.get("role", "")
        desc = r.get("description", "")
        marker = r.get("marker_hint", "")
        marker_note = f" (마커: {marker} — 텍스트에 넣지 마세요)" if marker else ""
        role_lines.append(f"- **{role_name}**: {desc}{marker_note}")
    role_text = "\n".join(role_lines)

    user_text = f"""## 영역 설명
{region_description}

## 사용 가능한 role 목록
{role_text}

## 참고: 양식의 이 영역에는 약 {region_paragraph_count}개 항목이 있습니다.
정확한 개수는 자유이지만, 이 수를 참고하여 적절한 분량으로 생성하세요.

## 소스 내용
{source_text}

위 소스 내용을 이 영역의 role 구조에 맞게 재구성하여 JSON 배열로 출력하세요.
반드시 JSON만 출력하세요.
"""

    return [
        {"role": "system", "content": SHALLOW_FILL_PROMPT},
        {"role": "user", "content": user_text},
    ]


def parse_shallow_fill_from_llm(raw: str) -> list[dict]:
    """Parse shallow generator AI output. Returns [{role, text}, ...]."""
    text = raw.strip()
    # Strip markdown code fence
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else 3
        text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in text
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            items = json.loads(text[start:end + 1])
        else:
            raise ValueError(f"shallow output is not valid JSON: {text[:200]}")
    if not isinstance(items, list):
        raise ValueError(f"shallow output is not a list: {type(items)}")
    # Normalize field names
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        # Accept both "text" and "content" field names
        txt = item.get("text", item.get("content", ""))
        if role:
            result.append({"role": role, "text": str(txt)})
    return result


def validate_shallow_output(
    items: list[dict],
    allowed_roles: set[str],
) -> dict:
    """Validate shallow generator output. Returns validation dict."""
    invalid_roles = []
    empty_items = 0
    for item in items:
        if item.get("role", "") not in allowed_roles:
            invalid_roles.append(item.get("role", ""))
        if not (item.get("text", "") or "").strip():
            empty_items += 1
    return {
        "role_membership_pass": len(invalid_roles) == 0,
        "invalid_roles": invalid_roles,
        "empty_items": empty_items,
        "total_items": len(items),
        "pass": len(invalid_roles) == 0 and len(items) > 0,
    }


def should_use_shallow_route(target_unit_plan: dict) -> tuple[bool, dict]:
    """
    target_unit_plan 기반 shallow route 판단.
    조건: chapter region 없음 + shallow_block region 있음.

    Returns:
        (use_shallow, route_debug)
    """
    regions = target_unit_plan.get("regions", [])
    if not regions:
        regions = target_unit_plan.get("ai_plan", {}).get("regions", [])

    has_chapter = any(r.get("unit_type") == "chapter" for r in regions)
    has_shallow = any(r.get("unit_type") == "shallow_block" for r in regions)

    shallow_para_count = sum(
        len(r.get("paragraph_indices", []))
        for r in regions if r.get("unit_type") == "shallow_block"
    )
    total_body_count = sum(
        len(r.get("paragraph_indices", []))
        for r in regions if r.get("unit_type") not in ("slot", "attachment")
    )

    shallow_is_primary = (
        shallow_para_count > 0
        and total_body_count > 0
        and shallow_para_count / total_body_count > 0.5
    )
    use_shallow = not has_chapter and has_shallow and shallow_is_primary

    debug = {
        "has_chapter_regions": has_chapter,
        "has_shallow_regions": has_shallow,
        "shallow_is_primary_body": shallow_is_primary,
        "shallow_para_count": shallow_para_count,
        "total_body_para_count": total_body_count,
        "route_reason": (
            "shallow: no chapter regions, shallow_block is primary body"
            if use_shallow
            else f"chapter: has_chapter={has_chapter}, has_shallow={has_shallow}"
        ),
    }
    return use_shallow, debug


# ──────────────────────────────────────────────────────────────────────
# 13.4b: Chapter Template Plan Seed
# ──────────────────────────────────────────────────────────────────────


def extract_chapter_template_plan_seed(
    target_unit_plan: dict,
    structure: dict,
    idx_full_texts: dict,
) -> dict | None:
    """
    target_unit_plan에서 chapter region을 추출하여 template chapter plan seed를 반환.

    template-driven chapter loop의 구동 데이터.
    seed가 있으면 chapter loop를 template chapter 기준으로 돌리고,
    None이면 기존 2a-driven loop로 fallback.

    Returns:
        dict with {chapters, total_chapters, confidence, evidence, loop_driver}
        or None if extraction fails.
    """
    regions = target_unit_plan.get("regions", [])
    if not regions:
        regions = target_unit_plan.get("ai_plan", {}).get("regions", [])

    chapter_regions = [r for r in regions if r.get("unit_type") == "chapter"]
    if not chapter_regions:
        return None

    # paragraph lookup for descriptions
    para_by_idx = {}
    for p in structure.get("paragraphs", []):
        pidx = p.get("idx")
        if pidx is not None:
            para_by_idx[pidx] = p

    chapters = []
    for position, region in enumerate(chapter_regions):
        pi = region.get("paragraph_indices", [])
        if not pi:
            continue

        first_idx = pi[0]

        # template_title: first paragraph text from idx_full_texts
        raw_title = ""
        for key in (first_idx, str(first_idx)):
            if key in idx_full_texts:
                raw_title = str(idx_full_texts[key])
                break

        # description: from target_unit_plan region (AI-generated during 12.2)
        region_desc = region.get("description", "")

        # fallback description: from 1a paragraph description
        if not region_desc:
            first_para = para_by_idx.get(first_idx, {})
            region_desc = first_para.get("description", "")

        # --- 13.6-B: per-chapter local pattern/catalog ---
        ch_pattern_result = extract_per_chapter_pattern(pi, structure, idx_full_texts)
        use_local = (
            ch_pattern_result.get("extraction_confidence") != "low"
            and ch_pattern_result.get("local_pattern")
            and not ch_pattern_result.get("fallback_to_dominant")
        )

        ch_entry = {
            "template_title": raw_title.strip() if raw_title else f"Chapter {position + 1}",
            "description": region_desc,
            "position": position,
            "total_chapters": len(chapter_regions),
            "paragraph_count": len(pi),
            "region_id": region.get("region_id"),
            "first_paragraph_idx": first_idx,
        }

        # local_title_role을 항상 chapter 첫 paragraph의 role로 기본 채움.
        # extract_per_chapter_pattern이 _empty_chapter_pattern으로 fallback할 때
        # (sub-tree 비어있는 chapter 등) local_title_role가 비어버려서, 호출부가
        # _seed_title_role 등 다른 fallback을 쓰며 잘못된 라벨(예: "section_header")이
        # downstream으로 흘러가는 문제 방지. 첫 paragraph가 chapter title이라는
        # invariant은 sub-tree 유무와 무관하게 성립.
        ch_entry["local_title_role"] = (
            para_by_idx.get(first_idx, {}).get("role", "") or ""
        )

        if use_local:
            ch_entry["local_pattern"] = ch_pattern_result["local_pattern"]
            ch_entry["local_catalog"] = ch_pattern_result["local_catalog"]
            # ch_pattern_result에 유효한 local_title_role이 있으면 덮어쓰기 (보통 동일 값)
            if ch_pattern_result.get("local_title_role"):
                ch_entry["local_title_role"] = ch_pattern_result["local_title_role"]
            ch_entry["pattern_source"] = "per_chapter_subtree"
        else:
            ch_entry["pattern_source"] = "dominant_type_fallback"

        ch_entry["_pattern_extraction"] = {
            "confidence": ch_pattern_result.get("extraction_confidence"),
            "stats": ch_pattern_result.get("stats"),
            "extraction_detail": ch_pattern_result.get("extraction_detail"),
            "repeatable_detail": ch_pattern_result.get("repeatable_detail"),
        }

        chapters.append(ch_entry)

    if not chapters:
        return None

    # chapter_type determination: find the dominant body chapter type
    chapter_types = structure.get("chapter_types", {})
    dominant_ch_type = _find_dominant_chapter_type(chapter_regions, structure)

    # confidence: based on evidence quality
    has_titles = sum(1 for c in chapters if c["template_title"] and c["template_title"] != f"Chapter {c['position'] + 1}")
    has_descriptions = sum(1 for c in chapters if c["description"])
    title_ratio = has_titles / len(chapters) if chapters else 0
    desc_ratio = has_descriptions / len(chapters) if chapters else 0

    if title_ratio >= 0.8 and desc_ratio >= 0.5:
        confidence = "high"
    elif title_ratio >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"

    # dual-use role warning: check if title_role is also in slot region
    dual_use_warnings = []
    if dominant_ch_type:
        type_info = chapter_types.get(dominant_ch_type, {})
        title_role = type_info.get("title_role", "")
        if title_role:
            slot_regions = [r for r in regions if r.get("unit_type") == "slot"]
            slot_indices = set()
            for sr in slot_regions:
                slot_indices.update(sr.get("paragraph_indices", []))
            for sidx in slot_indices:
                slot_para = para_by_idx.get(sidx, {})
                if slot_para.get("role") == title_role:
                    dual_use_warnings.append({
                        "warning": "dual_use_role",
                        "role": title_role,
                        "slot_idx": sidx,
                        "detail": "title_role is also used in slot region — may cause text concatenation in assembly",
                    })

    return {
        "chapters": chapters,
        "total_chapters": len(chapters),
        "dominant_chapter_type": dominant_ch_type,
        "confidence": confidence,
        "loop_driver": "template_plan",
        "evidence": {
            "chapter_region_count": len(chapter_regions),
            "titles_found": has_titles,
            "descriptions_found": has_descriptions,
            "title_ratio": round(title_ratio, 2),
            "desc_ratio": round(desc_ratio, 2),
        },
        "dual_use_warnings": dual_use_warnings,
    }


def strip_chapter_title_marker(
    text: str,
    title_role: str,
    marker_policy_1f: dict | None,
) -> str:
    """chapter title text에서 1f marker_policy의 marker prefix를 제거.

    13.7c AI input 정제용. AI가 marker 답습/형식 변형 위험 없이 chapter
    의미만 판단하도록 marker 떼고 보냄. 양식 marker는 assembly 단계에서
    code가 다시 자동 부착 (책임 분리).

    감지 기준: 양식 1f가 그 role에 대해 evidence로 기록한 detected_marker
    중 하나가 text의 prefix (lstrip 후)면 그 marker + 그 뒤 separator 문자
    (' ', '.', '\\t', '·') 떼기. 양식 1f가 marker 없다고 보면 (or role 매칭
    안 되면) text 원본 반환 (graceful).
    """
    if not text or not title_role:
        return text
    role_entry = next(
        (
            r for r in (marker_policy_1f or {}).get("roles", [])
            if r.get("role") == title_role
        ),
        None,
    )
    if not role_entry:
        return text
    detected = {
        e.get("detected_marker") for e in role_entry.get("evidence", []) or []
        if e.get("detected_marker")
    }
    if not detected:
        return text
    # 긴 marker 우선 (prefix 중복 방지)
    sorted_markers = sorted(detected, key=len, reverse=True)
    stripped = text.lstrip()
    for m in sorted_markers:
        if stripped.startswith(m):
            rest = stripped[len(m):]
            # 양식 separator 문자 (공백/마침표/탭/중점 등) 제거 — marker 직후 일관 형식
            return rest.lstrip(" \t.·:|)　")
    return text


def _find_dominant_chapter_type(
    chapter_regions: list[dict],
    structure: dict,
) -> str | None:
    """
    chapter region에서 dominant chapter_type을 결정.

    chapter_types의 각 type에 대해, type의 title_role이
    chapter region 첫 paragraph의 role과 일치하는지 확인.
    일치하는 type 중 가장 많이 매칭된 것을 dominant로 선택.
    """
    chapter_types = structure.get("chapter_types", {})
    if not chapter_types:
        return None

    para_by_idx = {}
    for p in structure.get("paragraphs", []):
        pidx = p.get("idx")
        if pidx is not None:
            para_by_idx[pidx] = p

    # Collect roles of first paragraphs in each chapter region
    first_roles = []
    for region in chapter_regions:
        pi = region.get("paragraph_indices", [])
        if pi:
            first_para = para_by_idx.get(pi[0], {})
            first_roles.append(first_para.get("role", ""))

    # Match against chapter_type patterns
    type_counts = {}
    for type_name, type_info in chapter_types.items():
        pattern = type_info.get("pattern", {})
        pattern_roles = set(pattern.keys())
        # Check how many chapter regions have their first role in this pattern
        match_count = sum(1 for role in first_roles if role in pattern_roles)
        if match_count > 0:
            type_counts[type_name] = match_count

    if not type_counts:
        # fallback: return first chapter_type
        return next(iter(chapter_types), None)

    return max(type_counts, key=type_counts.get)


def pattern_to_grammar(pattern: dict) -> tuple[dict, list[str]]:
    """
    local_pattern → (grammar, root_roles) 변환.

    local_pattern format:
        {role: {"repeatable": bool, "children": {child_role: {...}}}}

    grammar format:
        {role: {"allowed_children": [child_roles...]}}

    root_roles: pattern의 top-level keys (title 직속 자식).
    """
    grammar: dict[str, dict] = {}

    def _walk(pat: dict) -> None:
        for role, info in pat.items():
            children = info.get("children", {})
            child_keys = sorted(children.keys())
            if role not in grammar:
                grammar[role] = {"allowed_children": child_keys}
            else:
                # merge: union of allowed_children (cycle-safe)
                existing = set(grammar[role].get("allowed_children", []))
                existing.update(child_keys)
                grammar[role]["allowed_children"] = sorted(existing)
            if children:
                _walk(children)

    _walk(pattern)
    root_roles = sorted(pattern.keys())
    return grammar, root_roles


# ──────────────────────────────────────────────────────────────────────
# 13.6-B: Per-Chapter Subtree Extraction
# ──────────────────────────────────────────────────────────────────────


def extract_per_chapter_pattern(
    paragraph_indices: list[int],
    structure: dict,
    idx_full_texts: dict | None = None,
) -> dict:
    """
    chapter region의 paragraph에서 local pattern/catalog을 직접 추출.

    parent_idx 기반으로 tree를 먼저 구축하고, tree에서 role hierarchy와
    repeatable 여부를 파생한다.

    Returns:
        dict with local_pattern, local_catalog, local_title_role,
        stats, extraction_confidence, extraction_detail, repeatable_detail.
    """
    if not paragraph_indices:
        return _empty_chapter_pattern("no_paragraph_indices")

    idx_full_texts = idx_full_texts or {}

    # --- paragraph lookup ---
    para_by_idx: dict[int, dict] = {}
    for p in structure.get("paragraphs", []):
        pidx = p.get("idx")
        if pidx is not None:
            para_by_idx[pidx] = p

    pi_set = set(paragraph_indices)
    first_idx = paragraph_indices[0]
    title_para = para_by_idx.get(first_idx, {})
    title_role = title_para.get("role", "")

    # --- body paragraphs (title 제외) ---
    body_indices = [i for i in paragraph_indices[1:] if i in para_by_idx]
    if not body_indices:
        return _empty_chapter_pattern("no_body_paragraphs")

    # --- exclusion tracking ---
    excluded = {"table": 0, "empty": 0}
    usable_indices = []
    for idx in body_indices:
        p = para_by_idx[idx]
        if p.get("is_tbl_box"):
            excluded["table"] += 1
            continue
        usable_indices.append(idx)

    # --- parent_idx analysis ---
    parent_outside_region = []
    parent_coverage = 0
    for idx in usable_indices:
        p = para_by_idx[idx]
        pi = p.get("parent_idx")
        if pi is not None:
            parent_coverage += 1
            if pi not in pi_set:
                parent_outside_region.append(idx)

    # --- build role-level parent-child relationships ---
    # For each paragraph, map its role to its parent's role
    from collections import defaultdict

    role_children: dict[str, set[str]] = defaultdict(set)
    # Track per-(parent_idx, child_role) → list of child indices (for repeatable)
    sibling_groups: dict[tuple[int, str], list[int]] = defaultdict(list)

    for idx in usable_indices:
        p = para_by_idx[idx]
        child_role = p.get("role", "")
        pi = p.get("parent_idx")
        if pi is not None and pi in para_by_idx:
            parent_role = para_by_idx[pi].get("role", "")
            if parent_role and child_role and parent_role != child_role:
                role_children[parent_role].add(child_role)
            sibling_groups[(pi, child_role)].append(idx)

    # --- repeatable: same role appears 2+ under same parent ---
    repeatable_roles: set[str] = set()
    repeatable_detail: dict[str, dict] = {}
    for (parent_idx, role), indices in sibling_groups.items():
        if len(indices) >= 2:
            repeatable_roles.add(role)
    # Build detail per repeatable role
    for role in repeatable_roles:
        groups = [(pi, idxs) for (pi, r), idxs in sibling_groups.items()
                  if r == role and len(idxs) >= 2]
        total_count = sum(len(p_list) for p_list in
                          [idxs for (pi, r), idxs in sibling_groups.items() if r == role])
        distinct_parents = len([(pi, r) for (pi, r), idxs in sibling_groups.items() if r == role])
        repeatable_detail[role] = {
            "source": "parent_sibling",
            "count": total_count,
            "distinct_parents": distinct_parents,
        }

    # --- identify root roles (direct children of title paragraph) ---
    title_child_roles: list[str] = []
    title_child_roles_seen: set[str] = set()
    for idx in usable_indices:
        p = para_by_idx[idx]
        if p.get("parent_idx") == first_idx:
            role = p.get("role", "")
            if role and role not in title_child_roles_seen:
                title_child_roles.append(role)
                title_child_roles_seen.add(role)

    # --- build pattern tree recursively ---
    def _build_subtree(role: str, visited: set[str] | None = None) -> dict:
        if visited is None:
            visited = set()
        if role in visited:
            return {"repeatable": role in repeatable_roles, "children": {}}
        visited = visited | {role}
        children: dict[str, dict] = {}
        for child_role in sorted(role_children.get(role, [])):
            children[child_role] = _build_subtree(child_role, visited)
        return {
            "repeatable": role in repeatable_roles,
            "children": children,
        }

    local_pattern: dict[str, dict] = {}
    for root_role in title_child_roles:
        local_pattern[root_role] = _build_subtree(root_role)

    # --- fallback: if no root roles found via parent_idx, use flat role set ---
    flat_count_fallback = False
    if not local_pattern and usable_indices:
        flat_count_fallback = True
        role_counts: dict[str, int] = defaultdict(int)
        for idx in usable_indices:
            role = para_by_idx[idx].get("role", "")
            if role and role != title_role:
                role_counts[role] += 1
        for role, count in sorted(role_counts.items()):
            local_pattern[role] = {
                "repeatable": count >= 2,
                "children": {},
            }
            if count >= 2:
                repeatable_detail[role] = {
                    "source": "flat_count_fallback",
                    "count": count,
                    "distinct_parents": 0,
                }

    # --- local catalog: first exemplar per role ---
    local_catalog: dict[str, dict] = {}
    role_first_seen: dict[str, bool] = {}
    role_count: dict[str, int] = defaultdict(int)

    for idx in body_indices:  # include tables in count, exclude from exemplar
        p = para_by_idx[idx]
        role = p.get("role", "")
        if not role or role == title_role:
            continue
        role_count[role] += 1
        if role in role_first_seen:
            continue
        role_first_seen[role] = True
        # exemplar: skip table, skip empty
        if p.get("is_tbl_box"):
            local_catalog.setdefault(role, {"exemplar": "(table)", "count": 0})
            continue
        text = ""
        for key in (idx, str(idx)):
            if key in idx_full_texts:
                text = str(idx_full_texts[key]).strip()
                break
        if not text:
            local_catalog.setdefault(role, {"exemplar": "(empty)", "count": 0})
            continue
        local_catalog[role] = {
            "exemplar": text[:200],
            "count": 0,  # filled below
        }
    # fill counts
    for role, count in role_count.items():
        if role in local_catalog:
            local_catalog[role]["count"] = count

    # --- stats ---
    all_roles = set()
    max_level = 0
    for idx in body_indices:
        p = para_by_idx[idx]
        role = p.get("role", "")
        if role:
            all_roles.add(role)
        lv = p.get("level", 0) or 0
        if lv > max_level:
            max_level = lv

    stats = {
        "role_count": len(all_roles),
        "max_depth": max_level,
        "paragraph_count": len(paragraph_indices),
        "body_paragraph_count": len(body_indices),
    }

    # --- confidence ---
    parent_pct = (parent_coverage / len(usable_indices) * 100) if usable_indices else 0
    confidence_reasons = []

    if parent_pct >= 80:
        confidence_reasons.append("parent_idx_coverage_high")
    elif parent_pct >= 50:
        confidence_reasons.append("parent_idx_coverage_medium")
    else:
        confidence_reasons.append("parent_idx_coverage_low")

    if len(all_roles) >= 3:
        confidence_reasons.append(f"role_count_{len(all_roles)}")
    elif len(all_roles) >= 1:
        confidence_reasons.append(f"role_count_low_{len(all_roles)}")

    if not flat_count_fallback and title_child_roles:
        confidence_reasons.append("tree_built_from_parent_idx")
    elif flat_count_fallback:
        confidence_reasons.append("flat_count_fallback_used")

    if parent_pct >= 80 and len(all_roles) >= 3 and not flat_count_fallback:
        confidence = "high"
    elif parent_pct >= 50 and len(all_roles) >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    extraction_detail = {
        "region_paragraph_count": len(paragraph_indices),
        "used_for_hierarchy": len(usable_indices),
        "excluded": excluded,
        "parent_outside_region_count": len(parent_outside_region),
        "parent_outside_region_indices": parent_outside_region[:10],
        "parent_coverage_pct": round(parent_pct, 1),
        "confidence_reasons": confidence_reasons,
        "flat_count_fallback": flat_count_fallback,
    }

    return {
        "local_pattern": local_pattern,
        "local_catalog": local_catalog,
        "local_title_role": title_role,
        "stats": stats,
        "extraction_confidence": confidence,
        "extraction_detail": extraction_detail,
        "repeatable_detail": repeatable_detail,
        "fallback_to_dominant": False,
    }


def _empty_chapter_pattern(reason: str) -> dict:
    """confidence=low empty pattern for fallback."""
    return {
        "local_pattern": {},
        "local_catalog": {},
        "local_title_role": "",
        "stats": {"role_count": 0, "max_depth": 0, "paragraph_count": 0, "body_paragraph_count": 0},
        "extraction_confidence": "low",
        "extraction_detail": {"fallback_reason": reason},
        "repeatable_detail": {},
        "fallback_to_dominant": True,
    }


# ──────────────────────────────────────────────────────────────────────
# 13.5: Region Action Plan
# ──────────────────────────────────────────────────────────────────────


_UNIT_TYPE_ACTION_MAP = {
    "slot": "fill_slot",
    "chapter": "generate",
    "attachment": "preserve_original",
    "shallow_block": "preserve_original",
}

_UNIT_TYPE_TABLE_POLICY = {
    "slot": "not_applicable",
    "chapter": "defer_table_filling",
    "attachment": "preserved_with_region",
    "shallow_block": "not_applicable",
}


def compute_region_action_plan(
    target_unit_plan: dict,
    structure: dict,
    idx_map: dict | None = None,
) -> dict | None:
    """
    target_unit_plan의 모든 region에 action을 부여하고 preserve_indices를 계산.

    chapter route 전용. shallow route는 기존 compute_preserve_indices 사용.

    Returns:
        dict with {actions, preserve_indices, summary, warnings} or None.
    """
    if not target_unit_plan:
        return None

    regions = target_unit_plan.get("regions", [])
    if not regions:
        regions = target_unit_plan.get("ai_plan", {}).get("regions", [])
    if not regions:
        return None

    # paragraph level lookup
    para_level = {}
    for p in structure.get("paragraphs", []):
        pidx = p.get("idx")
        if pidx is not None:
            para_level[pidx] = p.get("level")  # None if missing

    actions = []
    preserve_indices = []
    all_generate_real = set()
    all_preserve_real = set()
    warnings = []

    for region in regions:
        unit_type = region.get("unit_type", "")
        ai_indices = region.get("paragraph_indices", [])
        region_id = region.get("region_id")

        # idx_map: AI idx → real idx
        real_indices = []
        for idx in ai_indices:
            real_idx = idx_map.get(idx, idx) if idx_map else idx
            real_indices.append(real_idx)

        if not ai_indices:
            warnings.append({
                "type": "empty_region",
                "region_id": region_id,
                "unit_type": unit_type,
                "detail": "region has no paragraph_indices",
            })

        # --- action determination ---
        if unit_type in _UNIT_TYPE_ACTION_MAP:
            action = _UNIT_TYPE_ACTION_MAP[unit_type]
            table_policy = _UNIT_TYPE_TABLE_POLICY[unit_type]
        else:
            # unknown unit_type → 보수적 보존 + 강한 warning
            action = "preserve_original"
            table_policy = "preserved_with_region"
            warnings.append({
                "type": "unclassified_region_preserved",
                "region_id": region_id,
                "unit_type": unit_type,
                "detail": (
                    f"unknown unit_type '{unit_type}' — preserved conservatively. "
                    "Not safely classified as generate or preserve."
                ),
                "paragraph_indices": ai_indices,
                "real_paragraph_indices": real_indices,
            })

        # --- preserve_via_header / in_preserve_set ---
        in_preserve_set = False
        preserve_via_header = False
        level_warnings = []

        if unit_type == "slot":
            preserve_via_header = True
            # debug: check levels to validate assumption
            for idx in ai_indices:
                lv = para_level.get(idx)
                if lv is None:
                    level_warnings.append({"idx": idx, "level_missing": True})
                elif lv != 0:
                    level_warnings.append({"idx": idx, "level": lv, "expected": 0})
            if level_warnings:
                warnings.append({
                    "type": "slot_level_assumption_check",
                    "region_id": region_id,
                    "detail": "slot paragraphs with unexpected or missing level",
                    "entries": level_warnings,
                })

        elif unit_type == "shallow_block":
            # level-0 → header_indices에서 이미 보존
            all_level_0 = True
            for idx in ai_indices:
                lv = para_level.get(idx)
                if lv is None:
                    all_level_0 = False
                    level_warnings.append({"idx": idx, "level_missing": True})
                elif lv != 0:
                    all_level_0 = False
                    level_warnings.append({"idx": idx, "level": lv})
            if all_level_0 and ai_indices:
                preserve_via_header = True
            else:
                in_preserve_set = True
                if level_warnings:
                    warnings.append({
                        "type": "shallow_block_level_ambiguity",
                        "region_id": region_id,
                        "detail": "shallow_block with non-zero or missing level — added to preserve_indices",
                        "entries": level_warnings,
                    })

        elif unit_type == "attachment":
            in_preserve_set = True

        elif unit_type not in _UNIT_TYPE_ACTION_MAP:
            # unknown: already handled above as preserve_original
            in_preserve_set = True

        # --- collect preserve_indices ---
        if in_preserve_set:
            preserve_indices.extend(real_indices)
            all_preserve_real.update(real_indices)

        if action == "generate":
            all_generate_real.update(real_indices)

        # --- reason ---
        reason_parts = [f"{unit_type} — {action}"]
        if preserve_via_header:
            reason_parts.append("preserved via header_indices")
        if in_preserve_set:
            reason_parts.append("added to preserve_indices")
        reason = "; ".join(reason_parts)

        actions.append({
            "region_id": region_id,
            "unit_type": unit_type,
            "action": action,
            "paragraph_indices": ai_indices,
            "real_paragraph_indices": real_indices,
            "paragraph_count": len(ai_indices),
            "in_preserve_set": in_preserve_set,
            "preserve_via_header": preserve_via_header,
            "table_policy": table_policy,
            "reason": reason,
        })

    # --- overlap check ---
    overlap = all_generate_real & all_preserve_real
    if overlap:
        warnings.append({
            "type": "generate_preserve_overlap",
            "detail": "paragraphs in both generate and preserve sets",
            "overlapping_real_indices": sorted(overlap),
        })

    # --- summary ---
    action_summary = {}
    for a in actions:
        act = a["action"]
        if act not in action_summary:
            action_summary[act] = {"count": 0, "paragraphs": 0}
        action_summary[act]["count"] += 1
        action_summary[act]["paragraphs"] += a["paragraph_count"]

    visited_types = {a["unit_type"] for a in actions}
    coverage = "all_regions_visited" if len(actions) == len(regions) else "partial"

    return {
        "actions": actions,
        "preserve_indices": sorted(set(preserve_indices)),
        "summary": {
            "total_regions": len(regions),
            "actions": action_summary,
            "coverage": coverage,
            "overlap_warnings": sorted(overlap) if overlap else [],
        },
        "warnings": warnings,
    }


# ──────────────────────────────────────────────────────────────────────
# 13.6-A: Multi-Section Diagnostic
# ──────────────────────────────────────────────────────────────────────


def diagnose_multi_section(hwpx_path: str) -> dict:
    """
    HWPX의 모든 section을 관측하여 multi-section analysis/assembly 필요성을 진단.

    section role classification은 하지 않음.
    layout, content significance, preserve adequacy 관측값만 수집하고
    gate decision으로 13.7 blocker 여부를 판단.

    Returns:
        dict with sections, observations, gate_decision.
    """
    import zipfile
    import re
    from xml.etree import ElementTree as _ET

    try:
        zf = zipfile.ZipFile(hwpx_path, "r")
    except Exception as e:
        return {"error": f"cannot open hwpx: {e}", "section_count": 0}

    with zf:
        section_names = sorted(
            n for n in zf.namelist()
            if "section" in n.lower() and n.endswith(".xml")
        )

    if len(section_names) <= 1:
        return {
            "section_count": len(section_names),
            "skip_reason": "single_section",
            "gate_decision": None,
        }

    sections = []
    total_doc_paragraphs = 0

    with zipfile.ZipFile(hwpx_path, "r") as zf:
        for idx, sname in enumerate(section_names):
            raw_bytes = zf.read(sname)
            raw = raw_bytes.decode("utf-8", errors="replace")
            chars = len(raw)

            # Parse XML for accurate counting
            try:
                sec_root = _ET.fromstring(raw_bytes)
            except _ET.ParseError:
                sec_root = None

            if sec_root is not None:
                _local_tag = lambda el: el.tag.split("}")[-1] if "}" in el.tag else el.tag
                # body paragraphs: direct <p> children of root (section element)
                para_count = sum(
                    1 for child in sec_root if _local_tag(child) == "p"
                )
                # table count: all <tbl> elements (any nesting level)
                tbl_count = sum(
                    1 for el in sec_root.iter() if _local_tag(el) == "tbl"
                )
            else:
                # fallback to regex if XML parse fails
                para_count = len(re.findall(r"<[^>]*\bp\b[^/>]*(?<!/)>", raw))
                tbl_count = len(re.findall(r"<[^>]*\btbl\b[^/>]*(?<!/)>", raw))
            total_doc_paragraphs += para_count

            # text preview: extract text from first paragraphs
            text_preview = _extract_section_text_preview(raw, max_paragraphs=10)

            # layout from secPr > pagePr
            layout = _extract_section_layout(raw)

            sections.append({
                "name": sname,
                "index": idx,
                "chars": chars,
                "body_paragraph_count": para_count,
                "table_count": tbl_count,
                "text_preview": text_preview,
                "layout": layout,
            })

    # --- observations ---
    # 1. Layout heterogeneity
    layout_diffs = _compare_section_layouts(sections)
    layout_homogeneous = len(layout_diffs) == 0

    # 2. Content significance
    analyzed_sections = [0]  # currently only section0 is analyzed
    unanalyzed = [s for s in sections if s["index"] not in analyzed_sections]
    unanalyzed_paras = sum(s["body_paragraph_count"] for s in unanalyzed)
    unanalyzed_chars = sum(s["chars"] for s in unanalyzed)
    unanalyzed_pct = (
        round(unanalyzed_paras / total_doc_paragraphs, 3)
        if total_doc_paragraphs else 0
    )

    # 3. Preserve adequacy
    preserve_assessment = {
        "method": "13.5_unanalyzed_section_preserve",
        "preserved_paragraphs": unanalyzed_paras,
        "preserved_pct_of_document": unanalyzed_pct,
        "information_loss": "none",
        "note": (
            "preserve keeps all original content — "
            "question is whether some should be generated/modified"
        ),
    }

    # --- gate decision ---
    # Multi-section analysis needed if significant content in unanalyzed
    content_significant = unanalyzed_paras > 0
    assembly_needed = not layout_homogeneous or content_significant

    if not layout_homogeneous:
        priority = "blocker"
        reasoning = (
            f"layout differs across sections ({len(layout_diffs)} diffs); "
            f"{unanalyzed_paras} paragraphs in unanalyzed sections"
        )
    elif unanalyzed_pct > 0.3:
        priority = "blocker"
        reasoning = (
            f"{unanalyzed_pct:.0%} of document paragraphs in unanalyzed sections; "
            "significant content may need generation"
        )
    elif unanalyzed_paras > 0:
        priority = "watch"
        reasoning = (
            f"{unanalyzed_paras} paragraphs preserved in unanalyzed sections; "
            "preserve is safe but may miss generation targets"
        )
    else:
        priority = "later"
        reasoning = "no unanalyzed sections"

    return {
        "section_count": len(sections),
        "sections": sections,
        "observations": {
            "layout_heterogeneity": {
                "homogeneous": layout_homogeneous,
                "diffs": layout_diffs,
            },
            "content_significance": {
                "analyzed_sections": analyzed_sections,
                "unanalyzed_sections": [s["index"] for s in unanalyzed],
                "unanalyzed_total_paragraphs": unanalyzed_paras,
                "unanalyzed_total_chars": unanalyzed_chars,
                "unanalyzed_pct_of_document": unanalyzed_pct,
            },
            "preserve_adequacy": preserve_assessment,
        },
        "gate_decision": {
            "multi_section_analysis_needed": content_significant,
            "section_aware_assembly_needed": assembly_needed,
            "recommendation_priority": priority,
            "reasoning": reasoning,
        },
    }


def _extract_section_text_preview(
    raw_xml: str, max_paragraphs: int = 10
) -> list[str]:
    """section XML에서 첫 N 문단의 text를 추출."""
    import re

    previews: list[str] = []
    # Find <hp:t> or <t> content
    # Simple regex approach: find text runs
    texts = re.findall(r"<[^>]*\bt\b[^/>]*>([^<]*)</[^>]*\bt>", raw_xml)
    current_text = ""
    count = 0
    for t in texts:
        t = t.strip()
        if not t:
            if current_text:
                previews.append(current_text[:200])
                current_text = ""
                count += 1
                if count >= max_paragraphs:
                    break
            continue
        current_text = (current_text + " " + t).strip() if current_text else t

    if current_text and count < max_paragraphs:
        previews.append(current_text[:200])

    return previews


def _extract_section_layout(raw_xml: str) -> dict:
    """section XML의 secPr > pagePr에서 layout 정보 추출."""
    import re

    layout: dict = {"inherited": True}

    # Find pagePr element
    pagePr_m = re.search(
        r"<[^>]*\bpagePr\b([^>]*)>(.*?)</[^>]*\bpagePr>",
        raw_xml, re.DOTALL,
    )
    if not pagePr_m:
        return layout

    layout["inherited"] = False
    attrs_str = pagePr_m.group(1)
    inner = pagePr_m.group(2)

    # page size from pagePr attributes
    w_m = re.search(r'width="(\d+)"', attrs_str)
    h_m = re.search(r'height="(\d+)"', attrs_str)
    land_m = re.search(r'landscape="([^"]*)"', attrs_str)

    if w_m:
        layout["page_width"] = int(w_m.group(1))
    if h_m:
        layout["page_height"] = int(h_m.group(1))
    if land_m:
        layout["orientation"] = land_m.group(1)

    # margins from <margin> inside pagePr
    margin_m = re.search(r"<[^>]*\bmargin\b([^/]*)/>", inner)
    if margin_m:
        margin_str = margin_m.group(1)
        for field in ("left", "right", "top", "bottom", "header", "footer", "gutter"):
            fm = re.search(rf'{field}="(\d+)"', margin_str)
            if fm:
                layout[f"margin_{field}"] = int(fm.group(1))

    return layout


def _compare_section_layouts(sections: list[dict]) -> list[dict]:
    """section 간 layout 차이 목록 반환."""
    if len(sections) < 2:
        return []

    compare_fields = [
        "page_width", "page_height", "orientation",
        "margin_left", "margin_right", "margin_top", "margin_bottom",
    ]

    ref = sections[0].get("layout", {})
    diffs: list[dict] = []

    for s in sections[1:]:
        s_layout = s.get("layout", {})
        for field in compare_fields:
            ref_val = ref.get(field)
            s_val = s_layout.get(field)
            if ref_val is not None and s_val is not None and ref_val != s_val:
                diffs.append({
                    "sections": [0, s["index"]],
                    "field": field,
                    "values": [ref_val, s_val],
                })

    return diffs


# ──────────────────────────────────────────────────────────────────────
# 13.7b-B0a: Pre-1a Section Census (debug-only, AI 호출 없음)
# ──────────────────────────────────────────────────────────────────────


def extract_section_census(hwpx_path: str) -> dict:
    """
    13.7b B0a: pre-1a section census (debug-only, AI 호출 0).

    모든 section의 basic property를 측정. B2 (full 1a baseline) 진입 비용 추정
    + B3 설계 evidence + section_role_proposal AI sub-step input.

    docs/13_7b_plan.md §5.5 schema. review_decisions는 빈 상태로 두고
    B0a review point에서 사용자+claude가 채움.
    """
    import zipfile
    import re
    from xml.etree import ElementTree as _ET

    try:
        zf = zipfile.ZipFile(hwpx_path, "r")
    except Exception as e:
        return {"error": f"cannot open hwpx: {e}", "section_count": 0}

    with zf:
        section_names = sorted(
            n for n in zf.namelist()
            if "section" in n.lower() and n.endswith(".xml")
        )

    if not section_names:
        return {"error": "no section xml found", "section_count": 0}

    sections: list[dict] = []
    paragraph_offset = 0
    total_paragraphs = 0

    with zipfile.ZipFile(hwpx_path, "r") as zf:
        for idx, sname in enumerate(section_names):
            raw_bytes = zf.read(sname)
            raw_str = raw_bytes.decode("utf-8", errors="replace")
            section_xml_size = len(raw_bytes)

            try:
                sec_root = _ET.fromstring(raw_bytes)
            except _ET.ParseError:
                sec_root = None

            if sec_root is not None:
                _lt = lambda el: el.tag.split("}")[-1] if "}" in el.tag else el.tag
                para_count = sum(
                    1 for child in sec_root if _lt(child) == "p"
                )
                tbl_count = sum(
                    1 for el in sec_root.iter() if _lt(el) == "tbl"
                )
            else:
                para_count = len(re.findall(r"<[^>]*\bp\b[^/>]*(?<!/)>", raw_str))
                tbl_count = len(re.findall(r"<[^>]*\btbl\b[^/>]*(?<!/)>", raw_str))

            previews = _extract_section_text_preview(raw_str, max_paragraphs=10)
            first_preview = previews[0] if previews else ""
            last_preview = (
                previews[-1] if len(previews) > 1
                else (previews[0] if previews else "")
            )

            layout = _extract_section_layout(raw_str)

            # secPr element tag detection (namespace 무관)
            secpr_present = bool(re.search(r"<[^>:\s]*:?secPr\b", raw_str))

            if para_count > 0:
                idx_start = paragraph_offset
                idx_end = paragraph_offset + para_count - 1
                idx_range = [idx_start, idx_end]
            else:
                idx_range = [paragraph_offset, paragraph_offset - 1]

            paragraph_offset += para_count
            total_paragraphs += para_count

            sections.append({
                "section_id": idx,
                "name": sname,
                "paragraph_count": para_count,
                "table_count": tbl_count,
                "section_xml_size": section_xml_size,
                "layout": layout,
                "first_paragraph_preview": first_preview,
                "last_paragraph_preview": last_preview,
                "secpr_present": secpr_present,
                "idx_range": idx_range,
            })

    section_paragraph_share: dict[int, float] = {}
    estimated_b2_token_cost: dict[int, int] = {}
    for s in sections:
        sid = s["section_id"]
        section_paragraph_share[sid] = (
            round(s["paragraph_count"] / total_paragraphs, 3)
            if total_paragraphs else 0
        )
        # rough proxy: section_xml_size bytes / 4 ≈ input tokens.
        # reference metric only (§2.8) — B2 비용 추정용. 자동 결정 X.
        estimated_b2_token_cost[sid] = s["section_xml_size"] // 4

    return {
        "section_count": len(sections),
        "sections": sections,
        "reference_metrics": {
            "total_paragraphs": total_paragraphs,
            "section_paragraph_share": section_paragraph_share,
            "estimated_b2_token_cost_input": estimated_b2_token_cost,
        },
        "review_decisions": {
            "sections_to_analyze_in_b2": [],
            "sections_to_skip_in_b2": [],
            "skip_reason": {},
            "reviewer": "user+claude",
            "b2_entry_authorized": False,
            "reasoning": "",
        },
    }


# ──────────────────────────────────────────────────────────────────────
# 13.7a-0: Title Role Consistency Measurement (debug-only, A0-1)
# ──────────────────────────────────────────────────────────────────────


def measure_title_role_consistency(
    structure: dict,
    chapter_template_plan: dict | None,
) -> dict:
    """
    1d `chapter_types[*].title_role` vs chapter_template_plan
    `seed.chapters[*].local_title_role` 일관성 측정.

    13.7a-0 measurement. debug-only — 정책에 영향 X.
    13.6-B per-chapter local_pattern은 generation에서 우회했지만,
    assemble은 여전히 1d title_role에만 의존하므로 mismatch가
    body_split 실패로 이어진다. 양식별로 mismatch 발생 여부를
    수치화해 1d-fix stage 우선순위 판단 자료로 사용.

    Returns:
        {
            "chapter_types_title_roles": {type_id: role, ...},
            "chapter_types_title_roles_set": [sorted unique],
            "local_title_roles_per_chapter": [{idx, role}, ...],
            "local_title_roles_set": [sorted unique],
            "mismatch_summary": {
                "all_local_in_1d_set": bool,
                "missing_from_1d_set": [...],
                "extra_in_1d_set": [...],
            },
            "per_chapter": [
                {"idx": int, "local_title_role": str,
                 "in_1d_title_roles_set": bool},
                ...,
            ],
            "status": "ok" | "no_plan" | "no_chapter_types" | "empty_plan",
        }
    """
    chapter_types = (structure or {}).get("chapter_types", {}) or {}
    ct_title_roles_map = {}
    for type_id, ct in chapter_types.items():
        tr = (ct or {}).get("title_role", "")
        if tr:
            ct_title_roles_map[type_id] = tr
    ct_title_roles_set = sorted(set(ct_title_roles_map.values()))

    if not chapter_template_plan:
        return {
            "chapter_types_title_roles": ct_title_roles_map,
            "chapter_types_title_roles_set": ct_title_roles_set,
            "local_title_roles_per_chapter": [],
            "local_title_roles_set": [],
            "mismatch_summary": {
                "all_local_in_1d_set": None,
                "missing_from_1d_set": [],
                "extra_in_1d_set": [],
            },
            "per_chapter": [],
            "status": "no_plan",
        }

    if not chapter_types:
        # no 1d chapter_types — 비교 불가
        return {
            "chapter_types_title_roles": {},
            "chapter_types_title_roles_set": [],
            "local_title_roles_per_chapter": [],
            "local_title_roles_set": [],
            "mismatch_summary": {
                "all_local_in_1d_set": None,
                "missing_from_1d_set": [],
                "extra_in_1d_set": [],
            },
            "per_chapter": [],
            "status": "no_chapter_types",
        }

    seed = chapter_template_plan.get("seed") or {}
    plan_chapters = seed.get("chapters") or []

    local_per_chapter = []
    per_chapter = []
    local_roles_set_builder = set()
    ct_set = set(ct_title_roles_set)

    for i, ch in enumerate(plan_chapters):
        ltr = (ch or {}).get("local_title_role", "")
        local_per_chapter.append({"idx": i, "role": ltr})
        per_chapter.append({
            "idx": i,
            "local_title_role": ltr,
            "in_1d_title_roles_set": (ltr in ct_set) if ltr else False,
        })
        if ltr:
            local_roles_set_builder.add(ltr)

    local_title_roles_set = sorted(local_roles_set_builder)

    if not plan_chapters:
        return {
            "chapter_types_title_roles": ct_title_roles_map,
            "chapter_types_title_roles_set": ct_title_roles_set,
            "local_title_roles_per_chapter": [],
            "local_title_roles_set": [],
            "mismatch_summary": {
                "all_local_in_1d_set": None,
                "missing_from_1d_set": [],
                "extra_in_1d_set": ct_title_roles_set,
            },
            "per_chapter": [],
            "status": "empty_plan",
        }

    missing_from_1d = sorted(local_roles_set_builder - ct_set)
    extra_in_1d = sorted(ct_set - local_roles_set_builder)
    all_in = len(missing_from_1d) == 0

    return {
        "chapter_types_title_roles": ct_title_roles_map,
        "chapter_types_title_roles_set": ct_title_roles_set,
        "local_title_roles_per_chapter": local_per_chapter,
        "local_title_roles_set": local_title_roles_set,
        "mismatch_summary": {
            "all_local_in_1d_set": all_in,
            "missing_from_1d_set": missing_from_1d,
            "extra_in_1d_set": extra_in_1d,
        },
        "per_chapter": per_chapter,
        "status": "ok",
    }


def diagnose_chapter_empty_reason(section_fill_result: dict) -> dict:
    """
    chapter가 비어있다면 어느 단계에서 비었는지 진단 (debug-only, A0-2).

    process_section_fill_result가 노출하는 debug_entry 필드만 사용.
    process_section_fill_result 자체는 변경하지 않는다.

    stage:
        - "none"           : 비어있지 않음 (is_empty=False)
        - "llm_response"   : LLM 응답 길이 0
        - "parse"          : raw_items 0
        - "grammar_reject" : normalize 0 + grammar violations > 0
        - "normalize"      : normalize 0 (grammar 검증 정보 없음)
        - "shallow_skipped": shallow_mode (chapter 단위 진단 의미 약함)
        - "unknown"        : 위 어디에도 안 잡힘

    Returns:
        {
            "is_empty": bool,
            "stage": str,
            "evidence": {
                "llm_raw_response_len": int,
                "raw_items_count": int,
                "normalized_items_count": int,
                "grammar_violations_count": int,
                "grammar_failure_type": str | None,
                "section_pdf_text_len": int,
            },
        }
    """
    debug_entry = (section_fill_result or {}).get("debug_entry") or {}
    items_count = (section_fill_result or {}).get("items_count", 0) or 0
    shallow_mode = bool(debug_entry.get("shallow_mode", False))

    llm_raw = debug_entry.get("llm_raw_response", "") or ""
    llm_len = len(llm_raw)
    raw_items = debug_entry.get("raw_items") or []
    raw_count = len(raw_items) if isinstance(raw_items, list) else 0

    grammar_val = debug_entry.get("grammar_validation") or {}
    grammar_violations = grammar_val.get("violations") or []
    violations_count = len(grammar_violations) if isinstance(grammar_violations, list) else 0
    failure_type = grammar_val.get("failure_type") if isinstance(grammar_val, dict) else None

    source_len = debug_entry.get("section_pdf_text_len", 0) or 0
    is_empty = items_count == 0

    evidence = {
        "llm_raw_response_len": llm_len,
        "raw_items_count": raw_count,
        "normalized_items_count": items_count,
        "grammar_violations_count": violations_count,
        "grammar_failure_type": failure_type,
        "section_pdf_text_len": source_len,
    }

    if shallow_mode:
        stage = "shallow_skipped"
    elif not is_empty:
        stage = "none"
    elif llm_len == 0:
        stage = "llm_response"
    elif raw_count == 0:
        stage = "parse"
    elif violations_count > 0:
        stage = "grammar_reject"
    elif raw_count > 0:
        stage = "normalize"
    else:
        stage = "unknown"

    return {
        "is_empty": is_empty,
        "stage": stage,
        "evidence": evidence,
    }


# ──────────────────────────────────────────────────────────────────────
# 13.7a-A1: Chapter Object Helpers (chapter-grouped assembly)
# ──────────────────────────────────────────────────────────────────────


def build_chapter_object(
    source_chapter_idx: int,
    target_region: dict | None,
    section_fill_result: dict,
    empty_reason: dict | None = None,
    adaptation_decision: dict | None = None,
    reference_metrics: dict | None = None,
) -> dict:
    """
    13.7a-A1: chapter-grouped assembly용 chapter object 생성.
    13.7c: adaptation_decision + reference_metrics _debug attach.

    chapter는 generation unit이다. process_section_fill_result가 chapter
    단위로 들고 있는 nodes/items와 target_unit_plan region metadata를
    하나의 chapter object로 합쳐 assemble까지 보존한다.

    schema (확정):
        {
          "source_chapter_idx": int,
          "target_region_id": int | None,
          "section_id": int,              # 13.7a 기본값 0, 13.7b에서 실 값
          "first_paragraph_idx": int | None,
          "paragraph_indices": [int, ...],
          "title_item": {role, text} | None,
          "title_node": {id, parent_id, role, text} | None,
          "body_items": [{role, text}, ...],   # derived view of body_nodes
          "body_nodes": [{id, parent_id, role, text, ...}, ...],
          "status": "ok" | "empty" | "fail",
          "_debug": {
            "adaptation_decision": {...} | None,   # 13.7c
            "reference_metrics": {...} | None,      # 13.7c (debug-only)
            ...
          },
        }

    title_item과 title_node는 derived view 관계. invariant은
    assert_chapter_object_invariants()로 검증.

    shallow route는 chapter object를 만들지 않는다 (content["body"]
    flat path 유지). 호출부에서 분기.
    """
    region = target_region or {}
    region_id = region.get("region_id")
    paragraph_indices = list(region.get("paragraph_indices") or [])
    first_paragraph_idx = paragraph_indices[0] if paragraph_indices else None
    # 13.7a: section_id 기본값 0. 13.7b section-local generation-lite에서
    # synthetic region (section N != 0) 호출 시 region.section_id 실 값.
    section_id = region.get("section_id", 0)
    # 13.7b section-local anchor: section-local idx primary (assembly Priority 1)
    section_local_first_idx = region.get("section_local_first_idx")
    section_local_paragraph_indices = list(
        region.get("section_local_paragraph_indices") or []
    )

    body_items_full = (section_fill_result or {}).get("body_items") or []
    chapter_tree_nodes = (section_fill_result or {}).get("chapter_tree_nodes") or []

    # process_section_fill_result는 body_items / chapter_tree_nodes 둘 다 title 포함.
    # chapter object schema는 title/body 분리 (alignment 검증 명시화).
    title_item = None
    title_node = None
    body_items: list[dict] = []
    body_nodes: list[dict] = []

    if body_items_full:
        title_item = body_items_full[0]
        body_items = list(body_items_full[1:])
    if chapter_tree_nodes:
        title_node = chapter_tree_nodes[0]
        body_nodes = list(chapter_tree_nodes[1:])

    items_count = (section_fill_result or {}).get("items_count", 0) or 0
    # items_count는 process_section_fill_result의 debug_body_items count (title 제외).
    if items_count == 0:
        status = "empty"
    else:
        status = "ok"

    debug = {}
    if empty_reason:
        debug["empty_reason"] = empty_reason
    debug_entry = (section_fill_result or {}).get("debug_entry") or {}
    if debug_entry:
        debug["chapter_context"] = debug_entry.get("chapter_context")
        debug["validation_grammar_source"] = debug_entry.get("validation_grammar_source")
        debug["grammar_passed"] = (section_fill_result or {}).get("grammar_passed", False)
        if debug_entry.get("override_root_roles") is not None:
            debug["override_root_roles"] = debug_entry.get("override_root_roles")
        if debug_entry.get("override_grammar_role_count") is not None:
            debug["override_grammar_role_count"] = debug_entry.get("override_grammar_role_count")

    # 13.7c: adaptation_decision + reference_metrics (debug-only)
    if adaptation_decision is not None:
        debug["adaptation_decision"] = adaptation_decision
    if reference_metrics is not None:
        debug["reference_metrics"] = reference_metrics

    return {
        "source_chapter_idx": source_chapter_idx,
        "target_region_id": region_id,
        "section_id": section_id,
        "first_paragraph_idx": first_paragraph_idx,
        "paragraph_indices": paragraph_indices,
        # 13.7b section-local anchor primary: section_local idx (assembly Priority 1)
        "section_local_first_idx": section_local_first_idx,
        "section_local_paragraph_indices": section_local_paragraph_indices,
        "title_item": title_item,
        "title_node": title_node,
        "body_items": body_items,
        "body_nodes": body_nodes,
        "status": status,
        "_debug": debug,
    }


def assert_chapter_object_invariants(chapter_obj: dict) -> list[str]:
    """
    chapter object invariant check. 위반 리스트 반환 (빈 리스트=통과).

    검증 항목 (13.7a-A1 합의):
        - title_item ↔ title_node role/text 일치
        - len(body_items) == len(body_nodes)
        - body_items[i].role/text == body_nodes[i].role/text

    status="empty"는 alignment 검증 생략 (region 전체 preserve).
    status="fail"은 호출자가 별도 처리.

    raise하지 않는다. 호출자가 위반 리스트를 보고 status="fail"로
    설정하거나 assemble validation fail로 다룬다 (원칙 13 — hard gate
    전환은 evidence 축적 후).
    """
    violations: list[str] = []
    status = (chapter_obj or {}).get("status")

    if status == "empty":
        return violations

    title_item = chapter_obj.get("title_item") or {}
    title_node = chapter_obj.get("title_node") or {}
    body_items = chapter_obj.get("body_items") or []
    body_nodes = chapter_obj.get("body_nodes") or []

    if title_item.get("role") != title_node.get("role"):
        violations.append(
            f"title_role_mismatch: item={title_item.get('role')!r} "
            f"node={title_node.get('role')!r}"
        )
    if (title_item.get("text") or "") != (title_node.get("text") or ""):
        violations.append(
            f"title_text_mismatch: item_len="
            f"{len(title_item.get('text') or '')} "
            f"node_len={len(title_node.get('text') or '')}"
        )

    if len(body_items) != len(body_nodes):
        violations.append(
            f"body_length_mismatch: items={len(body_items)} "
            f"nodes={len(body_nodes)}"
        )
    else:
        for i, (it, nd) in enumerate(zip(body_items, body_nodes)):
            if it.get("role") != nd.get("role"):
                violations.append(
                    f"body_role_mismatch[{i}]: item={it.get('role')!r} "
                    f"node={nd.get('role')!r}"
                )
            if (it.get("text") or "") != (nd.get("text") or ""):
                violations.append(
                    f"body_text_mismatch[{i}]"
                )

    return violations


# ──────────────────────────────────────────────────────────────────────
# 13.7c: Source-to-Template Adaptation Planning
# ──────────────────────────────────────────────────────────────────────
#
# 원칙 (docs/13_7c_plan.md):
# - 의미 판단은 AI. code는 JSON/schema, 필수 필드, 명백한 계약 위반만 본다.
# - heuristic은 hard fail 금지. token overlap/length/substring/
#   reference_metrics/adaptation_degree는 정책에 영향 X (debug 참고값만).
# - preserve 강등은 AI 호출 실패, parse 실패, schema 위반, 필수 evidence
#   없음, action 모순 같은 명백 case만.
# - title adaptation 허용. evidence (preserved/adapted/supporting/counter)
#   명시 강제.
# - broad source 유지. source slice는 후속 stage.
# ──────────────────────────────────────────────────────────────────────


# action enum
# 13.7e: title/content 분리 + preserve 제거 schema
TITLE_ACTIONS = (
    "adapt_topic_terms",
    "adapt_role_equivalent_title",
    "adapt_from_source_block",
)
CONTENT_ACTIONS = (
    "generate_from_source",
    "generate_with_template_scaffold",
)
ROLE_HINTS = ("intro", "status", "issue", "action", "schedule", "summary", "other")
DEPTH_HINTS = ("shallow", "medium", "deep")
MATCH_STRENGTHS = ("exact", "close", "partial", "none")
# 13.7e v2: template-flow / source-surface 분리
TITLE_SOURCE_FITS = (
    "supported_as_is",
    "needs_marker_fill",
    "needs_topic_adaptation",
    "needs_role_equivalent_rewrite",
)
CHAPTER_TITLE_MODES = (
    "role_stage_title",
    "measure_topic_title",
    "schedule_title",
    "mixed_title",
)
TEMPLATE_TITLE_NATURES = ("generic_role_title", "topic_specific_title")
SOURCE_GENRE_MATCHES = ("same_genre", "different_genre", "unclear")
# 13.7c legacy (호환): generate/adapted_title_generate/preserve — 호환 처리만, 새 출력 X
ADAPTATION_ACTIONS = ("generate", "adapted_title_generate", "preserve")
PRESERVE_REASONS = (
    "source_gap", "low_confidence", "validation_failed",
    "adaptation_risk", "plan_unavailable", "other",
)
CONFIDENCE_LEVELS = ("high", "medium", "low")
# 13.7e: adaptation_degree에서 'none' 제거 (preserve 없음)
ADAPTATION_DEGREES = ("small", "medium", "large")
ADAPTATION_DEGREES_LEGACY = ("none", "small", "medium", "large")  # 호환


def build_source_inventory_prompt(
    broad_source: str,
    max_source_chars: int = 0,
) -> list[dict]:
    """13.7c: source inventory 추출 prompt 생성.

    원칙 (template-first):
    - source의 "주제"를 결정짓는 진술 X
    - source가 가진 "사용 가능한 evidence inventory"만 정리
    - 14단계 KB/RAG가 광범위한 source를 줄 수 있으므로, 이 단계는
      목록 inventory이지 frame이 아님
    - chapter mapping은 이 inventory를 도구로만 사용, 방향은 chapter need가 결정

    Returns: messages list (system + user)
    """
    # max_source_chars 0/None이면 자르지 않음 — source 전체 사용
    if max_source_chars and len(broad_source or "") > max_source_chars:
        _truncated = (broad_source or "")[:max_source_chars]
        _truncated_note = f"\n\n(주의: source가 길어 앞 {max_source_chars}자만 표시)"
    else:
        _truncated = broad_source or ""
        _truncated_note = ""

    system_msg = (
        "당신은 source 문서에서 사용 가능한 evidence inventory를 정리하는 도구입니다. "
        "source가 다루는 내용을 결정짓지 마세요. "
        "이 inventory는 다음 단계의 template chapter mapping에서 도구로만 사용됩니다. "
        "JSON 객체로만 응답하세요. 다른 텍스트 없이 JSON만 출력합니다."
    )

    user_msg = (
        "다음 source 문서에서 사용 가능한 evidence inventory를 정리하세요.\n\n"
        "source 문서:\n```\n"
        f"{_truncated}{_truncated_note}\n"
        "```\n\n"
        "다음 JSON schema로 응답하세요:\n"
        "{\n"
        '  "summary": "source가 다루는 영역의 간단한 description (1~2 문장, chapter intent를 결정짓지 않음)",\n'
        '  "available_topics": ["source가 다루는 영역의 영역/키워드 3~7개 (선택지로, 결정적 진술 X)"],\n'
        '  "main_headings": ["source의 주요 heading/section title 목록"],\n'
        '  "evidence_samples": ["source에서 짧은 인용 2~5개 (검색 도구로 사용됨)"]\n'
        "}\n\n"
        "원칙:\n"
        "- 양식 도메인을 가정하지 않습니다 (정부/계약/매뉴얼/논문 등 어떤 양식도 가능).\n"
        "- source의 \"주제는 X\"라는 결정적 진술을 피합니다.\n"
        "- 이 inventory는 chapter need에 매칭될 도구이지 chapter need의 frame이 아닙니다.\n"
        "- evidence_samples는 source 원문 그대로 인용.\n"
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def parse_source_inventory_from_llm(llm_raw_response: str) -> dict:
    """13.7c: source inventory LLM 응답 parse + schema validation.

    Returns:
        {
          "summary": str,                # source 영역 brief description (frame 아님)
          "available_topics": list[str],  # 선택지 (이전 key_themes의 의미 약화)
          "main_headings": list[str],
          "confidence": str (enum),
          "evidence_samples": list[str],
          "_validation": {"ok": bool, "errors": list[str], "raw_response_len": int},
        }
    """
    import json as _json
    import re as _re

    _raw = (llm_raw_response or "").strip()
    _result = {
        "summary": "",
        "available_topics": [],
        "main_headings": [],
        "confidence": "low",
        "evidence_samples": [],
        "_validation": {"ok": False, "errors": [], "raw_response_len": len(_raw)},
    }

    if not _raw:
        _result["_validation"]["errors"].append("empty_response")
        return _result

    # JSON parse — strip code fences if any
    _stripped = _raw
    _m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, _re.DOTALL)
    if _m:
        _stripped = _m.group(1)
    else:
        _m2 = _re.search(r"(\{.*\})", _raw, _re.DOTALL)
        if _m2:
            _stripped = _m2.group(1)

    try:
        _parsed = _json.loads(_stripped)
    except Exception as e:
        _result["_validation"]["errors"].append(f"json_parse_failed: {e}")
        return _result

    if not isinstance(_parsed, dict):
        _result["_validation"]["errors"].append("response_not_object")
        return _result

    # Schema validation — 명백한 형식만
    _summary = _parsed.get("summary", "")
    if not isinstance(_summary, str):
        _result["_validation"]["errors"].append("summary_not_string")
        _summary = ""
    _result["summary"] = _summary

    # available_topics (이전 key_themes — template-first 정정으로 이름 변경)
    # key_themes로 들어오는 경우 호환 처리 (혹시 LLM이 옛 이름 사용)
    _at = _parsed.get("available_topics", _parsed.get("key_themes", []))
    if not isinstance(_at, list) or not all(isinstance(x, str) for x in _at):
        _result["_validation"]["errors"].append("available_topics_not_string_list")
        _at = []
    _result["available_topics"] = _at

    _mh = _parsed.get("main_headings", [])
    if not isinstance(_mh, list) or not all(isinstance(x, str) for x in _mh):
        _result["_validation"]["errors"].append("main_headings_not_string_list")
        _mh = []
    _result["main_headings"] = _mh

    # confidence 출력 제거 (2026-05-24): downstream 활용 미미 — AI 출력 부담 ↓.
    # 호환성 위해 default "high" 만 채움 (downstream 코드가 기본값 의존 시 안전망).
    _result["confidence"] = _parsed.get("confidence", "high")

    _es = _parsed.get("evidence_samples", [])
    if not isinstance(_es, list) or not all(isinstance(x, str) for x in _es):
        _result["_validation"]["errors"].append("evidence_samples_not_string_list")
        _es = []
    _result["evidence_samples"] = _es

    _result["_validation"]["ok"] = (
        not _result["_validation"]["errors"] and bool(_summary)
    )
    return _result


def extract_toc_t_list(
    template_path: str,
    toc_paragraph_idx: int,
    idx_full_texts: dict,
) -> list[dict]:
    """양식 차례 영역 전체의 t element 리스트 추출 (multi-paragraph 확장 2026-05-25).

    AI가 양식 t element 단위로 차례 교체를 결정하도록, 양식 차례 영역의 모든
    paragraph 의 모든 t element 를 (p_idx, t_idx, text) 형태로 평면화하여 반환.

    1a 가 차례 영역 multi-paragraph 를 1 paragraph 로 통합 저장한 가정과
    양식 xml 이 차례를 multi-paragraph 로 두는 구조의 mismatch 를 해소하기 위해,
    양식 xml 차원에서 차례 영역을 직접 식별:
      1. 'tab + 페이지번호' 패턴 가진 paragraph (양식 표준 차례 행) 식별
      2. 그 paragraph 들의 연속 영역 (gap <= 2) 잡기
      3. 영역 직전의 짧은 라벨 paragraph ('순서' / '목차' / '차례' / 'Contents') 흡수

    Args:
        template_path: 양식 .hwpx 파일 경로
        toc_paragraph_idx: 1a 가 잡은 차례 paragraph idx (fallback 으로만 사용)
        idx_full_texts: 1a idx → text 매핑 (fallback mapping 용)

    Returns:
        [{"p_idx": int, "t_idx": int, "text": str}, ...] — 매칭 실패 시 빈 list
        p_idx: 양식 xml top-level p index. t_idx: 그 p 안에서 hp:t 순서 index.
    """
    import zipfile as _zip
    import re as _re_local

    if not template_path:
        return []
    try:
        with _zip.ZipFile(template_path) as _z:
            _section_names = sorted(
                n for n in _z.namelist()
                if _re_local.match(r'Contents/section\d+\.xml$', n)
            )
            if not _section_names:
                return []
            _xml = _z.read(_section_names[0]).decode("utf-8")
    except Exception:
        return []

    # ElementTree 로 파싱 — 직계 children 만. regex 매칭은 table cell 안
    # nested hp:p 까지 잡아서 doc.paragraphs idx 와 어긋남 (2026-05-25 fix).
    import xml.etree.ElementTree as _ET
    _NS_P = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    try:
        _root = _ET.fromstring(_xml)
    except Exception:
        return []

    # section element 의 직계 hp:p children — doc.paragraphs 와 idx 일치
    _xml_paras_elems = [c for c in _root if c.tag == f"{_NS_P}p"]

    def _extract_ts_from_elem(_p_elem):
        # paragraph 안 모든 hp:t 의 (text, has_tab) 추출.
        # iter() 사용 — paragraph 안 hp:run > hp:t 구조 처리.
        _result = []
        for _t in _p_elem.iter(f"{_NS_P}t"):
            # hp:t 의 text + child element (hp:tab 등) 후 text
            _full_text = (_t.text or "")
            # hp:t 안 child element (hp:tab 등) 의 tail 도 포함
            for _child in _t:
                _full_text += (_child.tail or "")
            _result.append(_full_text)
        return _result

    def _has_tab_in_p(_p_elem):
        # paragraph 안 hp:tab 존재 여부 (차례 행 식별용)
        for _el in _p_elem.iter():
            if _el.tag.endswith("}tab") or _el.tag == "tab":
                return True
        return False

    # 1. tab + 페이지번호 패턴 paragraph 식별 (양식 표준 차례 행)
    _toc_rows: list[int] = []
    for _i, _p_elem in enumerate(_xml_paras_elems):
        if not _has_tab_in_p(_p_elem):
            continue
        _ts = _extract_ts_from_elem(_p_elem)
        if not _ts:
            continue
        _combined = "".join(_ts).strip()
        _tokens = _combined.split()
        if not _tokens:
            continue
        if _re_local.match(r"^\d{1,3}$", _tokens[-1]):
            _toc_rows.append(_i)

    if not _toc_rows:
        return []

    # 2. 연속 영역 (gap <= 2 — 사이 빈 paragraph 흡수)
    _toc_start = _toc_rows[0]
    _toc_end = _toc_rows[0]
    for _c in _toc_rows[1:]:
        if _c - _toc_end <= 3:
            _toc_end = _c
        else:
            break

    # 3. 직전의 짧은 라벨 paragraph 흡수 (있으면)
    if _toc_start > 0:
        _prev_ts = _extract_ts_from_elem(_xml_paras_elems[_toc_start - 1])
        _prev_text = "".join(_prev_ts).strip()
        if 0 < len(_prev_text) <= 15:
            if any(_kw in _prev_text for _kw in ("순", "목", "차", "례", "Contents", "CONTENTS")):
                _toc_start = _toc_start - 1

    # 4. 영역 전체 paragraph 의 t element 모으기
    _result: list = []
    for _p_idx in range(_toc_start, _toc_end + 1):
        _p_elem = _xml_paras_elems[_p_idx]
        _ts = _extract_ts_from_elem(_p_elem)
        for _t_idx, _t_text in enumerate(_ts):
            _result.append({
                "p_idx": _p_idx,
                "t_idx": _t_idx,
                "text": _t_text,
            })
    return _result


def build_adaptation_plan_prompt(
    source_inventory: dict,
    chapter_inputs: list[dict],
    broad_source_preview: str = "",
    max_source_preview_chars: int = 0,
    header_roles: list[dict] | None = None,
    template_toc_text: str = "",
    template_toc_t_list: list[dict] | None = None,
) -> list[dict]:
    """13.7c (=신 2a) chapter mapping + header batch prompt.

    chapter route 전용. 양식 chapter set 전체에 대해 한 번에 결정:
    - overall_source_focus (chapter set 일관성)
    - chapter별 adapted_title
    - header 슬롯 (제목/날짜/기관 등) — 옛 2a 에서 흡수
    - chapter 의 역할/순서/깊이는 template 이 결정 (Roman numeral 위치로 고정 X)
    - source 는 broad_source_preview (전체 source) 직접 보고 선택
    - chapter set 은 동일한 overall_source_focus 공유

    source_inventory 인자: 호환성 위해 받음 (현재 빈 dict 전달). prompt 안 사용 X.
    (이전엔 source 정리 단계 결과 받았지만 source_inventory 제거됨 — 신 2a 가 source 직접 봄.)

    Args:
        header_roles: 양식 header role 목록 [{"role": ..., "description": ...}].
                      비어있으면 header 추출 skip.
    """
    import json as _json
    _ch_brief = []
    for ch in chapter_inputs:
        _ch_brief.append({
            "idx": ch.get("idx"),
            "original_title": ch.get("original_title", ""),
            "description": ch.get("description", ""),
            "local_catalog_summary": ch.get("local_catalog_summary", ""),
        })

    # max_source_preview_chars 0/None이면 자르지 않음 — source 전체 사용
    _src_preview = (
        (broad_source_preview or "")[:max_source_preview_chars]
        if max_source_preview_chars
        else (broad_source_preview or "")
    )

    system_msg = (
        "당신은 template chapter 구조에 source 내용을 배치하고, "
        "각 chapter의 제목과 본문 생성 계획을 결정하는 도구입니다.\n\n"
        "**가장 중요한 한 줄:**\n"
        "제목은 source를 반영하되, chapter의 역할어와 문서 흐름은 template을 따릅니다.\n\n"
        "Template-flow 우선 원칙:\n"
        "- chapter의 **역할(role), 순서, 깊이**는 template이 결정합니다. source가 이걸 바꾸지 않습니다.\n"
        "- chapter role은 **Roman numeral 위치로 고정되지 않습니다.** Ⅱ장이라고 status가 아니고, Ⅲ장이라고 action이 아닙니다.\n"
        "  original_title 과 local_catalog_summary 를 우선 봐서 role 을 판단합니다.\n"
        "- source의 heading/제목/표현이 좋아 보여도, template chapter role과 맞지 않으면 그대로 가져오지 않습니다.\n"
        "- chapter 제목 결정 원칙:\n"
        "  → 양식 제목의 모든 token은 sample. **chapter role 어휘만 양식 흐름 유지**.\n"
        "  → 그 외 token (연도, 기관, 도메인 명사 등)은 모두 source 기반.\n"
        "  → 양식 token이 source 값과 다르면 source 값으로 교체. 양식 값 보존 X.\n"
        "  → chapter role 어휘란 구조 어휘(여건 및 방향, 추진과제, 추진성과, 핵심 등). 어떤 chapter인지 식별하는 부분.\n\n"
        "Source focus 일관성:\n"
        "- 하나의 chapter set은 특별한 이유가 없는 한 동일한 source topic/thread를 중심으로 구성합니다.\n"
        "- source에 여러 안건/주제가 있더라도, chapter들을 서로 무관한 안건으로 나누지 마세요.\n"
        "- 먼저 chapter set 전체에 가장 적합한 overall_source_focus를 정하고,\n"
        "  각 chapter는 그 focus 안에서 role에 맞는 sub-evidence를 선택합니다.\n\n"
        "제목 구성 공식 (재구성이 필요한 경우):\n"
        "  adapted_title ≈ [source 도메인 명사] + [template chapter role 어휘]\n\n"
        "두 가지 실패 모드를 모두 피해야 합니다:\n"
        "- 실패 A — 양식 token 보존 함정: 양식의 연도/기관/도메인이 source와 달라도 양식 값 그대로 두기.\n"
        "  chapter role(여건 및 방향, 추진과제 등)만 양식 따르고, 나머지 token은 모두 source 값으로.\n"
        "- 실패 B — source 추종 함정: source의 heading/표현을 chapter role 무시하고 그대로 가져오기.\n\n"
        "그 외 규칙:\n"
        "- chapter adaptation 단계에서는 preserve action을 사용하지 않습니다.\n"
        "- 모든 chapter는 반드시 body를 생성합니다. body 안 만드는 옵션은 없습니다.\n"
        "- source 부족 시 사실을 지어내지 않고 source_gap_flags / missing_source_requirements에 명시합니다.\n"
        "- 새 chapter를 만들거나 삭제하지 않습니다. chapter_idx는 input과 정확히 일치해야 합니다.\n"
        "- JSON 객체로만 응답하세요. 다른 텍스트 없이 JSON만 출력합니다."
    )

    # header role 정보 — 옛 2a에서 흡수. 비어있으면 header 추출 skip.
    # template_sample을 같이 보내서 LLM이 양식 원 표현·형식을 볼 수 있게 함.
    # 제목 성격 role은 adapted_title처럼 양식 흐름 유지 + source 도메인 보정.
    _header_brief = []
    for _h in (header_roles or []):
        if isinstance(_h, dict):
            _entry = {
                "role": _h.get("role", ""),
                "description": _h.get("description", ""),
                "template_sample": _h.get("template_sample", ""),
            }
            # 양식 paragraph의 t별 charPr+text 분포 (있으면 LLM이 폰트 보존하며 text 결정)
            _tp = _h.get("template_parts")
            if isinstance(_tp, list) and _tp:
                _entry["template_parts"] = _tp
            _header_brief.append(_entry)
        elif isinstance(_h, str):
            _header_brief.append({"role": _h, "description": "", "template_sample": ""})
    _header_block = (
        "[header_roles]\n"
        f"{_json.dumps(_header_brief, ensure_ascii=False, indent=2)}\n\n"
        if _header_brief else ""
    )
    if template_toc_t_list:
        _toc_block = (
            "[template_toc_t_list]\n"
            "양식 차례 영역 전체의 t element 분포 (multi-paragraph).\n"
            "각 entry: {p_idx: 양식 paragraph index, t_idx: 그 paragraph 안 t index, text: 그 t 의 원문}.\n"
            "**같은 차례 줄은 같은 p_idx 를 공유** — 여러 t 로 나뉘어 있어도 한 줄.\n"
            f"```json\n{_json.dumps(template_toc_t_list, ensure_ascii=False, indent=2)}\n```\n\n"
        )
    elif template_toc_text and template_toc_text.strip():
        _toc_block = (
            "[template_toc_text]\n"
            f"```\n{template_toc_text}\n```\n\n"
        )
    else:
        _toc_block = ""

    user_msg = (
        "[chapters]\n"
        f"{_json.dumps(_ch_brief, ensure_ascii=False, indent=2)}\n\n"
        + _header_block
        + _toc_block
        + (f"[source_text]\n```\n{_src_preview}\n```\n\n" if _src_preview else "")
        + "Step 0 — overall_source_focus 결정 (먼저 결정)\n\n"
        "chapter별 결정을 시작하기 전에, 이 chapter set 전체가 사용할 source 중심 주제를 정합니다.\n"
        "- 위 [source_text] 전체를 직접 읽고 chapter set 흐름과 가장 잘 맞는 thread 를 선택합니다.\n"
        "- 어떤 주제 / heading 이 있는지, 어떤 자료 / 사실이 있는지 직접 파악하세요.\n\n"
        "focus의 granularity (매우 중요):\n"
        "- focus는 source 내용을 **최대한 많이 포괄할 수 있는 상위 주제**여야 합니다. 좁은 한 영역이 아님.\n"
        "- source가 여러 영역을 다루면 그것들을 **모두 묶는 상위 개념**을 focus로 선택.\n"
        "  한 영역만 선택하면 다른 영역이 본문에서 누락됩니다.\n"
        "- overall_source_focus는 반드시 하나의 세부 heading일 필요는 없습니다.\n"
        "- template chapter set이 여러 세부 대책/항목을 요구하는 경우, **하나의 정책 패키지/상위 주제를 focus로 잡고**\n"
        "  그 안의 세부 대책들을 chapter별로 배분합니다.\n"
        "- 버릴 대상은 **focus 밖의 무관한 안건**이지, **같은 정책 패키지 안의 세부 대책**이 아닙니다.\n"
        "  source가 명확히 서로 다른 정책 패키지를 다루면 그 중 하나만 focus로 선택.\n"
        "  다만 같은 흐름의 여러 영역은 묶어서 처리.\n\n"
        "추가 가이드:\n"
        "- source에 여러 안건이 동등하게 있으면, template chapter 흐름(intro→status→issue→action 등)을\n"
        "  가장 자연스럽게 채울 수 있는 단일 패키지를 선택합니다.\n"
        "- focus를 정할 수 없거나 서로 다른 정책 패키지를 섞어야 하면 ambiguity_flags에\n"
        "  'multi_topic_source_mixing_risk' 기록.\n"
        "- 결과를 overall_source_focus.topic에 기록합니다 (한 단어 / 한 줄).\n\n"
        "Step 1 — chapter별 adapted_title 결정\n\n"
        "각 chapter의 original_title과 source 도메인을 보고 적합한 제목을 결정합니다.\n\n"
        "원칙:\n"
        "- chapter role/순서/깊이는 template이 결정. source가 바꾸지 않음.\n"
        "- **chapter title은 source 내용을 최대한 포괄할 수 있는 상위 개념으로 결정**.\n"
        "  좁은 특정 분야로 한정하면 그 안에 안 들어가는 source 영역이 본문에서 누락됩니다.\n"
        "  source가 여러 영역을 다루면 그것들을 모두 묶는 상위 명사로.\n"
        "- source 도메인 명사가 양식 chapter title에 자연스럽게 맞도록 보정.\n"
        "- 양식 제목의 모든 token은 sample. source 값이 다르면 source 값으로 교체 (placeholder 형태 아니라도).\n"
        "  특히 연도, 기관명, 도메인 명사는 source 기준. 양식 값 보존 X.\n"
        "- chapter role 어휘(여건 및 방향, 추진과제, 추진성과, 핵심 등 구조 어휘)는 양식 흐름 유지.\n"
        "- 길이: original_title의 ±20% 정도 (양식 TOC/헤더 layout 보존). 부가 수식어 추가 X.\n"
        "- 길이 핑계로 source 보정 생략 금지. 길이 우선시한다고 결과가 부정확하면 안 됨.\n"
        "- 새 대책/범위/하위항목 추가 금지. 어절 수 크게 늘리거나 새 명사 추가 시 token swap이 아닌 재구성.\n"
        "- source heading을 chapter role 무시하고 그대로 복사 금지.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "overall_source_focus": {\n'
        '    "topic": "이 chapter set이 사용하는 중심 source 주제 (한 줄)."\n'
        "  },\n"
        '  "chapter_decisions": [\n'
        "    {\n"
        '      "chapter_idx": int,\n'
        '      "adapted_title": "chapter에 적합한 제목. source 도메인에 맞춰 자연스럽게 결정. original_title과 같든 다르든 OK."\n'
        "    }\n"
        "  ],\n"
        '  "header": {\n'
        '    "<header_role_name>": "값 (두 형태 중 하나):\\n'
        '      - 단일 문자열: \\"새 텍스트\\" (template_parts 없는 경우)\\n'
        '      - parts list: [{\\"charPrIDRef\\": \\"양식 charPr 그대로\\", \\"text\\": \\"새 텍스트\\"}, ...]\\n'
        '        (template_parts 있는 경우 — 같은 parts 수 + 같은 charPrIDRef 순서로 출력)"\n'
        "  }\n"
        "}\n\n"
        "header 추출 규칙 (옛 2a에서 흡수):\n"
        "- 위 [header_roles]에 명시된 role만 key로 사용 (목록에 없는 role 만들지 X).\n"
        "- 각 role의 description과 template_sample을 같이 보고 source에서 적합한 값을 채움.\n"
        "- **제목 성격 role** (description에 '제목' 또는 'title' 포함) 처리:\n"
        "  - chapter title의 adapted_title 처리 방식과 동일하게.\n"
        "  - **template_sample의 표현 형식·구조·동작어를 유지하고 source 도메인 명사로 token만 보정**.\n"
        "  - template_sample의 base phrase 의미·구조 유지. source 도메인과 불일치하는 token만 최소 수정.\n"
        "- **그 외 role** (날짜·기관 등): source에서 정확한 값을 그대로 추출. 양식 형식 모방 X.\n"
        "- 보안등급/분류표시(예: 대외비)는 제목·날짜·기관 슬롯에 넣지 X.\n"
        "- source에 해당 슬롯에 맞는 값이 없으면 빈 문자열 \"\" (양식 원본 보존).\n"
        "- [header_roles]가 비어있거나 입력에 없으면 \"header\": {} 빈 객체로 출력.\n"
        "- **template_parts (t별 폰트 분포) 처리**:\n"
        "  - template_parts가 있으면 각 part가 양식의 다른 글꼴 영역 (큰 제목/부제 등).\n"
        "  - 출력은 같은 parts 갯수 + 같은 charPrIDRef 순서로 list 형태.\n"
        "  - 각 part의 text는 그 글꼴 영역에 맞는 새 텍스트 (양식 sample의 의미·구조 유지).\n"
        "  - template_parts 없으면 단일 문자열로 출력.\n\n"
        "**한 줄 정리: 제목은 source를 반영해야 하지만, chapter의 역할어와 문서 흐름은 template을 따라야 합니다.**\n"
        "**'먼저 같게 가져갈지 다르게 가져갈지'를 정하지 말고, source 적합성과 chapter role에 따라 자연스러운 제목을 정하면 됩니다.**\n"
        "**TOC 교체는 별도 단계에서 처리하므로 여기에 toc_replacements 출력 X.**\n"
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]



def build_toc_replacement_prompt(
    chapter_title_pairs: list[dict],
    template_toc_t_list: list[dict],
) -> list[dict]:
    """TOC 매핑 prompt (신 2a 와 분리된 별도 단계 2026-05-25).

    신 2a 가 결정한 chapter title (원래 / 새) pair 와 양식 차례 영역 t element 분포를
    받아, 각 t element 에 어떤 텍스트를 박을지 매핑만 결정.

    AI 가 chapter 제목 글자가 양식 차례 어디에 들어있는지 의미상 매칭하여 결정
    (양식 글자와 새 글자가 조금이라도 다르면 substring 매칭 실패 — code 로는 불가, AI 필요).

    Args:
        chapter_title_pairs: [{"chapter_idx": int, "original_title": str, "adapted_title": str}, ...]
        template_toc_t_list: [{"p_idx": int, "t_idx": int, "text": str}, ...] 양식 차례 영역 t 분포

    Returns: messages list
    """
    import json as _json

    if not chapter_title_pairs or not template_toc_t_list:
        # 빈 입력은 빈 결과 — caller 가 호출 안 해도 됨
        return [
            {"role": "system", "content": "TOC 매핑 도구."},
            {"role": "user", "content": "입력 없음. 빈 list 출력."},
        ]

    system_msg = (
        "당신은 양식 차례 영역의 각 글자 조각에 chapter 새 제목을 매핑하는 도구입니다.\n"
        "신 2a 가 결정한 chapter 제목 pair (원래 / 새) 와 양식 차례 영역의 t element 분포를 받아,\n"
        "각 t element 의 글자가 어느 chapter 의 제목 일부인지 의미상 판단해 매핑하세요.\n"
        "JSON 으로만 응답하세요."
    )

    user_msg = (
        "[chapter_title_pairs]\n"
        f"{_json.dumps(chapter_title_pairs, ensure_ascii=False, indent=2)}\n\n"
        "[template_toc_t_list]\n"
        "양식 차례 영역 전체의 t element 분포 (multi-paragraph).\n"
        "각 entry: {p_idx: 양식 paragraph index, t_idx: 그 paragraph 안 t index, text: 그 t 의 원문}.\n"
        "같은 차례 줄은 같은 p_idx 를 공유 — 여러 t 로 나뉘어 있어도 한 줄.\n"
        f"```json\n{_json.dumps(template_toc_t_list, ensure_ascii=False, indent=2)}\n```\n\n"
        "**매핑 규칙**:\n"
        "1. 각 chapter 의 차례 줄을 찾기 — chapter title pair 의 original_title 이 들어있는 t element 식별.\n"
        "2. 그 t 의 글자가 chapter 본문 (Roman numeral / 마커 제외) 이면 adapted_title 로 교체.\n"
        "3. 글자가 마커만 ('Ⅰ', '.', '◈', '□', 'ㅇ', '󰊱' 등) 또는 페이지 번호 / 라벨 ('순 서', '목 차') / 빈 공백이면 **entry 생략** (양식 원본 보존).\n"
        "4. 마커 + 본문이 한 t 에 묶여있으면 마커 보존하여 출력 (예: text='󰊳 추진성과 및 평가' → new_text='󰊳 새 chapter title').\n"
        "5. **chapter title 의 자식 항목** (양식 본문의 ◈ / □ / ㅇ 같은 chapter 안 sub-section 들이 차례에 보임) 의 본문 t 는 new_text='?' 로 (자식 항목은 신 2a 가 결정 안 했음).\n"
        "6. (p_idx, t_idx) 쌍은 [template_toc_t_list] 에 등장한 그대로 사용. 임의 값 금지.\n"
        "7. 매핑하지 않는 t 는 entry 생략 → 양식 원본 그대로 유지.\n\n"
        "JSON 출력:\n"
        "{\n"
        '  "toc_replacements": [\n'
        '    {"p_idx": int, "t_idx": int, "new_text": "그 t element 에 들어갈 새 텍스트"},\n'
        "    ...\n"
        "  ]\n"
        "}\n\n"
        "[chapter_title_pairs] 의 모든 chapter 에 대해 adapted_title 매핑 entry 가 적어도 1개 있어야 합니다."
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def parse_toc_replacement_from_llm(llm_raw_response: str) -> dict:
    """build_toc_replacement_prompt 의 응답 parse.

    Returns: {"toc_replacements": [...], "_validation": {...}}
    """
    import json as _json
    import re as _re

    _raw = (llm_raw_response or "").strip()
    _result: dict = {
        "toc_replacements": [],
        "_validation": {"ok": False, "errors": [], "raw_response_len": len(_raw)},
    }
    if not _raw:
        _result["_validation"]["errors"].append("empty_response")
        return _result

    _stripped = _raw
    _m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, _re.DOTALL)
    if _m:
        _stripped = _m.group(1)
    else:
        _m2 = _re.search(r"(\{.*\})", _raw, _re.DOTALL)
        if _m2:
            _stripped = _m2.group(1)

    try:
        _parsed = _json.loads(_stripped)
    except Exception as e:
        _result["_validation"]["errors"].append(f"json_parse_failed: {e}")
        return _result

    if not isinstance(_parsed, dict):
        _result["_validation"]["errors"].append("response_not_object")
        return _result

    _tocr = _parsed.get("toc_replacements")
    _clean: list = []
    if isinstance(_tocr, list):
        _seen_keys: set = set()
        for _item in _tocr:
            if not isinstance(_item, dict):
                continue
            _p_idx = _item.get("p_idx")
            _t_idx = _item.get("t_idx")
            _new_text = _item.get("new_text")
            if not isinstance(_t_idx, int) or _t_idx < 0:
                continue
            if not isinstance(_new_text, str):
                continue
            if _p_idx is not None and not isinstance(_p_idx, int):
                continue
            _key = (_p_idx, _t_idx)
            if _key in _seen_keys:
                _clean = [r for r in _clean if (r.get("p_idx"), r.get("t_idx")) != _key]
            _seen_keys.add(_key)
            _entry = {"t_idx": _t_idx, "new_text": _new_text}
            if _p_idx is not None:
                _entry["p_idx"] = _p_idx
            _clean.append(_entry)
    _result["toc_replacements"] = _clean
    _result["_validation"]["ok"] = True
    return _result


def parse_adaptation_plan_from_llm(
    llm_raw_response: str,
    expected_chapter_indices: list[int],
) -> dict:
    """13.7c: adaptation_plan LLM 응답 parse.

    Returns:
        {
          "chapter_decisions": [decision, ...],
          "_validation": {"ok": bool, "errors": list[str],
                          "missing_indices": list[int],
                          "raw_response_len": int}
        }
    """
    import json as _json
    import re as _re

    _raw = (llm_raw_response or "").strip()
    _result = {
        "chapter_decisions": [],
        "overall_source_focus": None,  # 13.7e v2: top-level focus
        "header": {},  # 옛 2a 흡수: header 슬롯 값
        "toc_replacements": [],  # 양식 TOC text 안 substring 교체 list
        "_validation": {
            "ok": False,
            "errors": [],
            "missing_indices": list(expected_chapter_indices),
            "raw_response_len": len(_raw),
        },
    }

    if not _raw:
        _result["_validation"]["errors"].append("empty_response")
        return _result

    _stripped = _raw
    _m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, _re.DOTALL)
    if _m:
        _stripped = _m.group(1)
    else:
        _m2 = _re.search(r"(\{.*\})", _raw, _re.DOTALL)
        if _m2:
            _stripped = _m2.group(1)

    try:
        _parsed = _json.loads(_stripped)
    except Exception as e:
        _result["_validation"]["errors"].append(f"json_parse_failed: {e}")
        return _result

    if not isinstance(_parsed, dict):
        _result["_validation"]["errors"].append("response_not_object")
        return _result

    # 13.7e v2: top-level overall_source_focus 추출 (topic 만 — reason 제거됨)
    _osf = _parsed.get("overall_source_focus")
    if isinstance(_osf, dict):
        _result["overall_source_focus"] = {"topic": _osf.get("topic")}
    else:
        _result["overall_source_focus"] = {"topic": None}

    # 옛 2a 흡수: header 슬롯 추출
    # value는 두 형태:
    #   - str: 단일 텍스트 (template_parts 없는 경우)
    #   - list[{charPrIDRef, text}]: t별 분배 (template_parts 있는 경우)
    _hdr = _parsed.get("header")
    if isinstance(_hdr, dict):
        _clean_hdr = {}
        for _k, _v in _hdr.items():
            _key = str(_k)
            if isinstance(_v, list):
                # parts list
                _parts = []
                for _p in _v:
                    if isinstance(_p, dict):
                        _parts.append({
                            "charPrIDRef": str(_p.get("charPrIDRef") or ""),
                            "text": "" if _p.get("text") is None else str(_p.get("text")),
                        })
                _clean_hdr[_key] = _parts
            elif _v is None:
                _clean_hdr[_key] = ""
            else:
                _clean_hdr[_key] = str(_v)
        _result["header"] = _clean_hdr
    else:
        _result["header"] = {}

    # TOC replacements 는 별도 단계 (build_toc_replacement_prompt) 에서 결정.
    # 신 2a 응답에 toc_replacements 가 들어있어도 무시 — 호환성 위해 빈 list.
    _result["toc_replacements"] = []

    _decisions = _parsed.get("chapter_decisions")
    if not isinstance(_decisions, list):
        _result["_validation"]["errors"].append("chapter_decisions_not_list")
        return _result

    _seen_indices = set()
    _valid_decisions = []
    for i, d in enumerate(_decisions):
        if not isinstance(d, dict):
            _result["_validation"]["errors"].append(f"decision[{i}]_not_object")
            continue
        idx = d.get("chapter_idx")
        if not isinstance(idx, int):
            _result["_validation"]["errors"].append(f"decision[{i}]_chapter_idx_not_int")
            continue
        _seen_indices.add(idx)
        _valid_decisions.append(d)

    _result["chapter_decisions"] = _valid_decisions
    _result["_validation"]["missing_indices"] = [
        i for i in expected_chapter_indices if i not in _seen_indices
    ]
    _result["_validation"]["ok"] = (
        not _result["_validation"]["errors"]
        and not _result["_validation"]["missing_indices"]
    )
    return _result


# ──────────────────────────────────────────────────────────────────────
# 2b-source: chapter별 source 범위 분배 (본문 채우기 부하 감소)
# ──────────────────────────────────────────────────────────────────────


def build_source_range_prompt(
    source_text: str,
    chapter_inputs: list[dict],
    overall_source_focus: dict | None = None,
) -> list[dict]:
    """2b-source: source 전체에서 각 chapter 작성에 활용 가능한 정보 range 를 회수·매핑하는 prompt.

    source 와 chapter 의 주장 방향이 달라도 OK.
    목적은 chapter 별 source 분배가 아니라, chapter 작성에 쓸 수 있는 재료 / 근거 / 맥락을
    최대한 회수하는 것. 겹침 OK, 중복 OK, 간접 활용 가능 정보도 포함.

    Args:
        source_text: 전체 source text
        chapter_inputs: [{idx, adapted_title, original_title}, ...]
        overall_source_focus: chapter set 전체의 source 중심 주제 (참고)

    Returns: messages list
    """
    import json as _json

    _ch_brief = []
    for ch in chapter_inputs:
        _ch_brief.append({
            "idx": ch.get("idx"),
            "adapted_title": ch.get("adapted_title", ""),
        })

    _focus_str = ""
    if isinstance(overall_source_focus, dict) and overall_source_focus.get("topic"):
        _focus_str = (
            "\n[overall_source_focus]\n"
            f"topic: {overall_source_focus.get('topic')}\n\n"
        )

    system_msg = (
        "당신은 source 본문에서 각 chapter 작성에 활용 가능한 정보 영역을 찾아 매핑하는 도구입니다.\n"
        "source 의 주장 방향과 chapter 의 방향이 달라도, chapter 작성에 재료로 쓸 수 있으면 포함합니다.\n"
        "분배가 아니라 회수(recall)와 매핑이 목적입니다.\n"
        "JSON 으로만 응답하세요."
    )

    user_msg = (
        "[chapters]\n"
        f"{_json.dumps(_ch_brief, ensure_ascii=False, indent=2)}\n\n"
        f"{_focus_str}"
        f"[source_text] (총 {len(source_text):,}자)\n"
        "```\n"
        f"{source_text}\n"
        "```\n\n"
        "각 chapter 를 작성할 때 활용 가능한 source 영역을 char idx 범위 (start, end) 로 찾으세요.\n\n"
        "**핵심 원칙 (강제)**: source 내용과 chapter 내용의 방향성이 달라도 됩니다. "
        "같은 주장 / 같은 결론을 말하는 부분만 고르지 마세요. "
        "chapter 에 쓸 수 있는 정보, 근거, 수치, 사례, 배경, 정의, 문제점, 반론, 비교, 맥락이면 포함하세요. "
        "애매하면 포함. 겹침 / 중복 허용. 한 source 영역이 여러 chapter 에 들어가도 OK. "
        "chapter 에 필요한 재료가 source 여러 위치에 흩어져 있으면 ranges 에 여러 range.\n\n"
        "범위 결정 기준:\n"
        "- adapted_title 과 source 내용이 직접 일치하지 않아도, chapter 작성에 활용 가능하면 포함.\n"
        "- source 의 결론 / 관점이 chapter 와 달라도, 대조 / 근거 / 배경 / 예시 / 한계 설명에 쓸 수 있으면 포함.\n"
        "- 단순 키워드 매칭보다 '이 내용을 chapter 문단 작성에 사용할 수 있는가' 를 기준으로 판단.\n"
        "- 너무 좁게 핵심 문장만 고르지 말고, 해당 정보가 이해되는 문단 / 소제목 단위까지 포함.\n\n"
        "JSON 출력:\n"
        "{\n"
        '  "chapter_ranges": [\n'
        '    {\n'
        '      "chapter_idx": int,\n'
        '      "ranges": [\n'
        '        {\n'
        '          "start": int,\n'
        '          "end": int,\n'
        '          "use_type": "direct|indirect|background|stat|example|counterpoint|definition|context",\n'
        '          "reason": "이 영역을 chapter 작성에 어떻게 쓸 수 있는지 짧게"\n'
        '        }\n'
        '      ]\n'
        '    }\n'
        "  ]\n"
        "}\n\n"
        "- chapter_idx 는 input 과 정확히 일치 (모든 chapter 포함).\n"
        "- start/end 는 source_text 의 char idx (0-based). end >= start.\n"
        "- ranges 는 list — 한 chapter 당 여러 range 가능.\n"
        "- range 가 source 전체에 가까우면 [{start:0, end:전체길이, ...}] 도 OK.\n"
        "- use_type / reason 은 회수 의도 확인용. parser 는 start/end 만 사용하지만 모델이 \"어떻게 써먹을 수 있나\" 를 생각하게 합니다.\n"
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def parse_source_ranges_from_llm(
    llm_raw_response: str,
    expected_chapter_indices: list[int],
    source_length: int,
) -> dict:
    """2b-source LLM 응답 parse.

    Returns:
        {
          "chapter_ranges": {chapter_idx: [(start, end), ...]},
          "_validation": {"ok", "errors", "missing_indices", "raw_response_len"}
        }
    """
    import json as _json
    import re as _re

    _raw = (llm_raw_response or "").strip()
    _result = {
        "chapter_ranges": {},
        "_validation": {
            "ok": False,
            "errors": [],
            "missing_indices": list(expected_chapter_indices),
            "raw_response_len": len(_raw),
        },
    }

    if not _raw:
        _result["_validation"]["errors"].append("empty_response")
        return _result

    _stripped = _raw
    _m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, _re.DOTALL)
    if _m:
        _stripped = _m.group(1)
    else:
        _m2 = _re.search(r"(\{.*\})", _raw, _re.DOTALL)
        if _m2:
            _stripped = _m2.group(1)

    try:
        _parsed = _json.loads(_stripped)
    except Exception as e:
        _result["_validation"]["errors"].append(f"json_parse_failed: {e}")
        return _result

    _items = _parsed.get("chapter_ranges")
    if not isinstance(_items, list):
        _result["_validation"]["errors"].append("chapter_ranges_not_list")
        return _result

    _seen = set()
    for it in _items:
        if not isinstance(it, dict):
            continue
        idx = it.get("chapter_idx")
        if not isinstance(idx, int):
            continue
        ranges_raw = it.get("ranges") or []
        if not isinstance(ranges_raw, list):
            continue
        clean = []
        for r in ranges_raw:
            if not isinstance(r, dict):
                continue
            s = r.get("start")
            e = r.get("end")
            if not isinstance(s, int) or not isinstance(e, int):
                continue
            s = max(0, min(s, source_length))
            e = max(0, min(e, source_length))
            if e < s:
                continue
            clean.append((s, e))
        if clean:
            _result["chapter_ranges"][idx] = clean
            _seen.add(idx)

    _result["_validation"]["missing_indices"] = [
        i for i in expected_chapter_indices if i not in _seen
    ]
    _result["_validation"]["ok"] = (
        not _result["_validation"]["errors"]
        and not _result["_validation"]["missing_indices"]
    )
    return _result


def apply_source_ranges_with_safety(
    source_text: str,
    chapter_ranges: dict,
    expand_chars: int = 5000,
) -> dict:
    """LLM 결정 range에 안전망 ±expand_chars 적용 + chunk 텍스트 추출.

    - 각 range start/end에 ±expand_chars 확장 (chapter 경계 너무 좁게 자르는 것 방지)
    - 같은 chapter의 여러 range 합쳐 chunk 추출
    - 빈 chapter는 전체 source fallback

    Returns:
        {chapter_idx: chunk_text}
    """
    out: dict = {}
    src_len = len(source_text or "")
    if src_len == 0:
        return out
    for ch_idx, ranges in (chapter_ranges or {}).items():
        if not ranges:
            out[ch_idx] = source_text  # fallback: 전체
            continue
        # 안전망 ± expand
        expanded = []
        for (s, e) in ranges:
            s2 = max(0, s - expand_chars)
            e2 = min(src_len, e + expand_chars)
            expanded.append((s2, e2))
        # 겹치는 range 합치기
        expanded.sort()
        merged = [expanded[0]]
        for s, e in expanded[1:]:
            ls, le = merged[-1]
            if s <= le:
                merged[-1] = (ls, max(le, e))
            else:
                merged.append((s, e))
        # chunk 추출
        chunks = [source_text[s:e] for s, e in merged]
        out[ch_idx] = "\n...\n".join(chunks) if len(chunks) > 1 else chunks[0]
    return out


def validate_adaptation_decision(decision: dict) -> dict:
    """슬림화: adapted_title + chapter_idx만 필수 검증.

    옛 부수 필드(template_role_hint, confidence, evidence, aspects 등)는 LLM이 더 이상 결정 안 함 → 검증 X.
    """
    violations: list[str] = []

    adapted_title = decision.get("adapted_title")
    if not isinstance(adapted_title, str) or not adapted_title.strip():
        violations.append("adapted_title_required")

    if decision.get("chapter_idx") is None:
        violations.append("chapter_idx_required")

    if not violations:
        return {"valid": True, "should_demote": False, "demote_reason": None, "violations": []}
    return {"valid": False, "should_demote": True, "demote_reason": "validation_failed", "violations": violations}


def compute_reference_metrics(
    decision: dict,
    broad_source: str = "",
    generated_body_text: str = "",
) -> dict:
    """13.7c: debug-only 참고 metric 계산.

    원칙: 정책 판단에 사용 X. 다음 모든 용도 금지:
        - preserve 강등
        - validation fail
        - confidence 조정
        - hallucination 확정
    좋은 paraphrase에서도 낮을 수 있음. broad source 한계나 evidence
    trace를 관찰하기 위한 참고 지표.

    Returns:
        {
          "supporting_evidence_substring_match_ratio": float | None,
          "generated_body_evidence_overlap_ratio": float | None,
        }
    """
    metrics = {
        "supporting_evidence_substring_match_ratio": None,
        "generated_body_evidence_overlap_ratio": None,
    }

    # supporting_evidence가 source 원문에 substring match된 비율 (paraphrase면 낮음, OK)
    se = decision.get("supporting_evidence") or []
    if isinstance(se, list) and se and broad_source:
        _match_count = 0
        _total = 0
        for ev in se:
            if not isinstance(ev, str) or not ev.strip():
                continue
            _total += 1
            # 짧은 fragment (앞 40자)만 비교 — 긴 paraphrase 영향 최소화
            _frag = ev.strip()[:40]
            if _frag and _frag in broad_source:
                _match_count += 1
        if _total > 0:
            metrics["supporting_evidence_substring_match_ratio"] = round(
                _match_count / _total, 3
            )

    # generated_body가 evidence hint의 source fragment를 포함한 비율
    if isinstance(se, list) and se and generated_body_text:
        _frag_count = 0
        _hit_count = 0
        for ev in se:
            if not isinstance(ev, str) or not ev.strip():
                continue
            _frag = ev.strip()[:40]
            if not _frag:
                continue
            _frag_count += 1
            if _frag in generated_body_text:
                _hit_count += 1
        if _frag_count > 0:
            metrics["generated_body_evidence_overlap_ratio"] = round(
                _hit_count / _frag_count, 3
            )

    return metrics


def normalize_adaptation_decision(
    decision: dict,
    original_title: str,
) -> dict:
    """슬림 schema: chapter_idx + original_title + adapted_title만.

    옛 부수 필드(role_hint, chapter_title_mode, aspects, evidence 등)는 LLM이 더 이상 결정 안 함.
    DB tool 호환 위해 일부 옛 필드명은 빈 default로 유지 (코드 변경 최소화).
    """
    return {
        "chapter_idx": decision.get("chapter_idx"),
        "source_chapter_idx": decision.get("source_chapter_idx", decision.get("chapter_idx")),
        "original_title": original_title,
        "adapted_title": decision.get("adapted_title"),
        # 옛 호환 default (DB tool/debug에서 .get() 호출 시 안전한 default)
        "title_action": "adapt_topic_terms",
        "content_action": "generate_from_source",
        "action": "adapt_topic_terms",
        "preserved_template_aspects": [],
        "adapted_aspects": [],
        "supporting_evidence": [],
    }


def make_unavailable_decision(
    chapter_idx: int,
    original_title: str,
    reason_detail: str,
) -> dict:
    """13.7e v2: AI 호출 실패 시 fallback. supported_as_is + adapted_title=original_title (글자 일치)."""
    return {
        "chapter_idx": chapter_idx,
        "source_chapter_idx": chapter_idx,
        "original_title": original_title,
        "title_action": "adapt_topic_terms",
        "content_action": "generate_with_template_scaffold",
        "template_role_hint": "other",
        "depth_hint": "medium",
        "role_hint_evidence": "fallback_unavailable",
        "title_adaptation_reason": "AI 호출 실패 fallback — 양식 원본 유지",
        "adapted_title": original_title,
        # v2: title_source_fit + 메타 (supported_as_is — original 그대로)
        "title_source_fit": "supported_as_is",
        "title_fit_reason": "fallback — AI 호출 실패로 안전한 supported_as_is로 강등",
        "chapter_title_mode": "mixed_title",
        "template_title_nature": "generic_role_title",
        "genre_markers": [],
        "template_phrase_signal": None,
        "source_genre_match": "unclear",
        "source_genre_reason": "fallback_unavailable",
        # legacy 호환
        "matched_source_block_label": None,
        "matched_source_block_role_hint": None,
        "matched_source_block_order": None,
        "source_block_match_strength": "none",
        "ordering_hint": {"template_position": "middle", "source_block_order": None, "debug_merge_hint": []},
        "preserved_template_aspects": [],
        "adapted_aspects": [],
        "supporting_evidence": [],
        "counter_evidence": [],
        "source_gap_flags": ["plan_unavailable"],
        "missing_source_requirements": [reason_detail[:200]],
        "ambiguity_flags": ["fallback_unavailable"],
        "adaptation_degree": "small",
        "confidence": "low",
        "action": "adapt_topic_terms",
        "preserve_reason": "plan_unavailable",
        "preserve_reason_detail": reason_detail,
    }


def make_validation_failed_decision(
    chapter_idx: int,
    original_title: str,
    violations: list[str],
) -> dict:
    """13.7e v2: schema validation 실패 fallback. supported_as_is + 양식 원본 유지."""
    detail = "schema_validation_failures: " + "; ".join(violations[:5])
    return {
        "chapter_idx": chapter_idx,
        "source_chapter_idx": chapter_idx,
        "original_title": original_title,
        "title_action": "adapt_topic_terms",
        "content_action": "generate_with_template_scaffold",
        "template_role_hint": "other",
        "depth_hint": "medium",
        "role_hint_evidence": "fallback_validation_failed",
        "title_adaptation_reason": "validation 실패 fallback — 양식 원본 유지",
        "adapted_title": original_title,
        # v2 메타
        "title_source_fit": "supported_as_is",
        "title_fit_reason": "fallback — validation 실패로 supported_as_is로 강등",
        "chapter_title_mode": "mixed_title",
        "template_title_nature": "generic_role_title",
        "genre_markers": [],
        "template_phrase_signal": None,
        "source_genre_match": "unclear",
        "source_genre_reason": "fallback_validation_failed",
        # legacy 호환
        "matched_source_block_label": None,
        "matched_source_block_role_hint": None,
        "matched_source_block_order": None,
        "source_block_match_strength": "none",
        "ordering_hint": {"template_position": "middle", "source_block_order": None, "debug_merge_hint": []},
        "preserved_template_aspects": [],
        "adapted_aspects": [],
        "supporting_evidence": [],
        "counter_evidence": [],
        "source_gap_flags": ["validation_failed"],
        "missing_source_requirements": [],
        "ambiguity_flags": ["fallback_validation_failed"],
        "adaptation_degree": "small",
        "confidence": "low",
        "action": "adapt_topic_terms",
        "preserve_reason": "validation_failed",
        "preserve_reason_detail": detail,
    }


def summarize_adaptation_plan(
    decisions: list[dict],
    source_topic: dict,
    ai_call_info: dict | None = None,
    overall_source_focus: dict | None = None,
) -> dict:
    """슬림 summary — adapted_title 결정 결과만 요약. 옛 부수 분포 제거."""
    validation_failure_count = sum(
        1 for d in decisions if d.get("preserve_reason") == "validation_failed"
    )
    title_pairs = [
        {
            "chapter_idx": d.get("chapter_idx"),
            "original_title": d.get("original_title", ""),
            "adapted_title": d.get("adapted_title", ""),
        }
        for d in decisions
    ]
    return {
        "source_topic": source_topic,
        "overall_source_focus": overall_source_focus,
        "chapter_count": len(decisions),
        "validation_failure_count": validation_failure_count,
        "title_pairs": title_pairs,
        "ai_calls": ai_call_info or {},
    }


# ──────────────────────────────────────────────────────────────────────
# 13.7b B2.2: Section Role Proposal AI sub-step
#
# section의 다른 section과의 구조 관계 + 처리 추천을 AI로 받는다.
# - structural_relationship / placement_recommendation: free-form (enum 폐기)
# - supporting/counter_evidence / ambiguity_flags / confidence 강제
# - code는 schema 위반만 본다 (의미 해석 X)
# - AI 호출/parse 실패 또는 schema 위반은 보수적 fallback proposal
# 참조: docs/13_7b_plan.md §4.5 / §9
# ──────────────────────────────────────────────────────────────────────


def summarize_section_for_proposal(
    section_id: int,
    section_result: dict,
    census_entry: dict,
    max_text_preview: int = 80,
    max_heading_samples: int = 6,
    max_paragraph_samples: int = 5,
) -> dict:
    """13.7b B2.2: section_role_proposal AI input 압축.

    section_result(B2.1.2 산출물) + census_entry(B0a 산출물)에서
    AI가 structural_relationship/placement_recommendation 판단에 필요한
    raw evidence만 모은다. paragraph 전체는 input에 X (token 폭증 방지).

    schema는 정책 분기 anchor가 아니라 AI 판단용 raw evidence만 담는다.
    숫자 metric은 reference로만 (§2.8) — hard rule 안 됨.
    """
    structure = section_result.get("structure", {}) or {}
    chapter_types = section_result.get("chapter_types", {}) or {}
    marker_policy = section_result.get("marker_policy_1f", {}) or {}
    idx_texts = section_result.get("idx_texts", {}) or {}
    paragraphs = structure.get("paragraphs", []) or []

    # chapter_types compact summary (key + title_role)
    chapter_types_summary: list[dict] = []
    for ct_key, ct_val in chapter_types.items():
        if not isinstance(ct_val, dict):
            continue
        chapter_types_summary.append({
            "type": ct_key,
            "title_role": ct_val.get("title_role"),
            "root_roles": ct_val.get("root_roles", []),
        })

    # role cluster fingerprint
    role_counter: dict[str, int] = {}
    level_counter: dict[int, int] = {}
    has_parent_count = 0
    for p in paragraphs:
        if not isinstance(p, dict):
            continue
        r = p.get("role")
        if r:
            role_counter[r] = role_counter.get(r, 0) + 1
        lvl = p.get("level")
        if isinstance(lvl, int):
            level_counter[lvl] = level_counter.get(lvl, 0) + 1
        if p.get("parent_idx") is not None:
            has_parent_count += 1

    top_roles = sorted(role_counter.items(), key=lambda kv: kv[1], reverse=True)
    role_cluster_summary = {
        "distinct_count": len(role_counter),
        "top_roles": [
            {"role": r, "count": c} for r, c in top_roles[:8]
        ],
        "level_distribution": {str(k): v for k, v in sorted(level_counter.items())},
        "paragraphs_with_parent": has_parent_count,
    }

    # marker_policy compact summary
    marker_policy_summary: dict = {}
    if isinstance(marker_policy, dict):
        types_present = sorted({
            (v.get("type") if isinstance(v, dict) else None)
            for v in marker_policy.values()
            if isinstance(v, dict)
        } - {None})
        marker_policy_summary = {
            "role_count": len(marker_policy),
            "types_present": list(types_present),
        }

    # heading-like sample (marker 있는 low-level paragraph 위주)
    # heuristic: level 0~2 with non-empty marker OR explicit title_role
    title_roles: set[str] = set()
    for ct_val in chapter_types.values():
        if not isinstance(ct_val, dict):
            continue
        tr = ct_val.get("title_role")
        if isinstance(tr, str):
            title_roles.add(tr)
        elif isinstance(tr, list):
            for x in tr:
                if isinstance(x, str):
                    title_roles.add(x)

    heading_samples: list[dict] = []
    for p in paragraphs:
        if len(heading_samples) >= max_heading_samples:
            break
        if not isinstance(p, dict):
            continue
        lvl = p.get("level")
        marker = (p.get("marker") or "").strip()
        role = p.get("role")
        is_title_role = role in title_roles if role else False
        if (isinstance(lvl, int) and lvl <= 2 and marker) or is_title_role:
            idx_key = str(p.get("idx"))
            txt = idx_texts.get(idx_key, "") if isinstance(idx_texts, dict) else ""
            heading_samples.append({
                "idx": p.get("idx"),
                "role": role,
                "level": lvl,
                "marker": marker,
                "is_title_role_for_some_chapter_type": is_title_role,
                "text": (txt or "")[:max_text_preview],
            })

    # paragraph sample (first N text-non-empty)
    paragraph_samples: list[dict] = []
    if isinstance(idx_texts, dict):
        sorted_idx_keys = sorted(idx_texts.keys(), key=lambda k: int(k) if str(k).isdigit() else 0)
        for k in sorted_idx_keys:
            if len(paragraph_samples) >= max_paragraph_samples:
                break
            txt = idx_texts.get(k, "")
            if not txt or not txt.strip():
                continue
            paragraph_samples.append({
                "idx": int(k) if str(k).isdigit() else k,
                "text": txt[:max_text_preview],
            })

    layout = census_entry.get("layout", {}) if isinstance(census_entry, dict) else {}

    return {
        "section_id": section_id,
        "name": census_entry.get("name") if isinstance(census_entry, dict) else None,
        "paragraph_count": census_entry.get("paragraph_count", len(paragraphs)) if isinstance(census_entry, dict) else len(paragraphs),
        "table_count": census_entry.get("table_count", 0) if isinstance(census_entry, dict) else 0,
        "section_xml_size": census_entry.get("section_xml_size") if isinstance(census_entry, dict) else None,
        "secpr_present": census_entry.get("secpr_present") if isinstance(census_entry, dict) else None,
        "idx_range": census_entry.get("idx_range") if isinstance(census_entry, dict) else None,
        "layout_brief": {
            "orientation": layout.get("orientation"),
            "page_width": layout.get("page_width"),
            "page_height": layout.get("page_height"),
            "margin_left": layout.get("margin_left"),
            "margin_right": layout.get("margin_right"),
            "margin_top": layout.get("margin_top"),
            "margin_bottom": layout.get("margin_bottom"),
            "inherited": layout.get("inherited"),
        },
        "first_paragraph_preview": (census_entry.get("first_paragraph_preview") or "")[:max_text_preview * 2] if isinstance(census_entry, dict) else "",
        "last_paragraph_preview": (census_entry.get("last_paragraph_preview") or "")[:max_text_preview * 2] if isinstance(census_entry, dict) else "",
        "chapter_types_summary": chapter_types_summary,
        "role_cluster_summary": role_cluster_summary,
        "marker_policy_summary": marker_policy_summary,
        "heading_samples": heading_samples,
        "paragraph_samples": paragraph_samples,
    }


def build_section_role_proposal_prompt(
    sections_summary: list[dict],
    document_context: dict | None = None,
) -> list[dict]:
    """13.7b B2.2: section_role_proposal batch prompt.

    docs/13_7b_plan.md §9.3 system 메시지 + §4.5 output schema.

    원칙:
    - 각 section의 structural_relationship + placement_recommendation을 free-form 텍스트로
    - 양식 layout 사실을 자유 텍스트로 기술 (enum 카테고리에 끼워 맞추기 X)
    - 숫자 기준이나 자동 분류로 판단 X (양식 content/structure evidence 기반)
    - supporting/counter_evidence / ambiguity_flags / confidence 필수
    """
    import json as _json

    _doc_ctx = document_context or {}
    _doc_brief = {
        "template_title": _doc_ctx.get("template_title", ""),
        "section_count": _doc_ctx.get("section_count", len(sections_summary)),
        "route": _doc_ctx.get("route", "chapter"),
    }

    _sections_brief = []
    for s in sections_summary:
        _sections_brief.append({
            "section_id": s.get("section_id"),
            "name": s.get("name"),
            "paragraph_count": s.get("paragraph_count"),
            "table_count": s.get("table_count"),
            "secpr_present": s.get("secpr_present"),
            "idx_range": s.get("idx_range"),
            "layout_brief": s.get("layout_brief", {}),
            "first_paragraph_preview": s.get("first_paragraph_preview", ""),
            "last_paragraph_preview": s.get("last_paragraph_preview", ""),
            "chapter_types_summary": s.get("chapter_types_summary", []),
            "role_cluster_summary": s.get("role_cluster_summary", {}),
            "marker_policy_summary": s.get("marker_policy_summary", {}),
            "heading_samples": s.get("heading_samples", []),
            "paragraph_samples": s.get("paragraph_samples", []),
        })

    system_msg = (
        "당신은 양식 문서의 section 구조 관계와 처리 추천을 제안하는 도구입니다. "
        "숫자 기준이나 자동 분류로 판단하지 마시고, section의 content/structure evidence를 보고 판단하세요. "
        "양식에 고정된 카테고리(top_level/nested/attachment 등)에 억지로 끼워 맞추지 마시고, "
        "양식 layout 사실과 처리 추천을 자유 텍스트로 기술하세요. "
        "JSON 객체로만 응답하세요. 다른 텍스트 없이 JSON만 출력합니다."
    )

    user_msg = (
        "[document_context]\n"
        f"{_json.dumps(_doc_brief, ensure_ascii=False, indent=2)}\n\n"
        "[sections]\n"
        f"{_json.dumps(_sections_brief, ensure_ascii=False, indent=2)}\n\n"
        "각 section에 대해 다음 JSON schema로 응답하세요:\n"
        "{\n"
        '  "section_role_proposals": [\n'
        "    {\n"
        '      "section_id": int,\n'
        '      "structural_relationship": "section의 다른 section과의 구조 관계를 자유 텍스트로 기술 (예: \\"section0 본문 chapter 다음에 오는 독립 chapter group처럼 보임\\", \\"section0 ChX의 nested/continuation 후보\\", \\"별첨/부록 형태\\")",\n'
        '      "placement_recommendation": "처리 추천을 자유 텍스트로 기술 (예: \\"독립 chapter list 항목으로 추가\\", \\"section0 ChX 하위 nested 배치\\", \\"preserve only\\")",\n'
        '      "supporting_evidence": ["section content/structure에서 인용한 근거"],\n'
        '      "counter_evidence": ["분류 위험 신호 / 다른 해석 가능성"],\n'
        '      "ambiguity_flags": ["unable_to_classify | evidence_too_sparse | layout_only | ..."],\n'
        '      "confidence": "high|medium|low",\n'
        '      "_debug": {"reference_label": "top_level | nested | attachment | empty | unknown | other (자유 hint, 통계용)"}\n'
        "    }, ...\n"
        "  ]\n"
        "}\n\n"
        "원칙:\n"
        "- structural_relationship/placement_recommendation은 free-form. 고정 enum에 매핑하지 말 것.\n"
        "- supporting_evidence는 section content/structure 인용 (특정 paragraph text, marker pattern, layout fact 등).\n"
        '- counter_evidence는 분류 위험 신호 (예: \\"별첨처럼 보이지만 본문 일부 가능성\\").\n'
        "- ambiguity_flags는 자유 텍스트 list. 명백히 모호하면 채움. 명확하면 비움.\n"
        "- confidence high를 주려면 supporting_evidence 명시 + counter_evidence 검토 필수.\n"
        "- 양식에 fixed/repeatable/hybrid 같은 분류를 임의로 부여하지 말 것. 양식 evidence가 가리키는 사실만 기술.\n"
        '- paragraph_count/table_count 같은 숫자는 reference metric. 그 자체로 \\"본문\\" 또는 \\"attachment\\"라고 결정 X.\n'
        "- 모든 section에 대해 proposal 생성. 누락 X.\n"
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def parse_section_role_proposal_from_llm(
    llm_raw_response: str,
    expected_section_ids: list[int],
) -> dict:
    """13.7b B2.2: section_role_proposal LLM 응답 parse.

    Returns:
        {
          "section_role_proposals": [proposal, ...],
          "_validation": {
            "ok": bool,
            "errors": [str, ...],
            "missing_section_ids": [int, ...],
            "raw_response_len": int,
          }
        }
    """
    import json as _json
    import re as _re

    _raw = (llm_raw_response or "").strip()
    _result = {
        "section_role_proposals": [],
        "_validation": {
            "ok": False,
            "errors": [],
            "missing_section_ids": list(expected_section_ids),
            "raw_response_len": len(_raw),
        },
    }

    if not _raw:
        _result["_validation"]["errors"].append("empty_response")
        return _result

    _stripped = _raw
    _m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _raw, _re.DOTALL)
    if _m:
        _stripped = _m.group(1)
    else:
        _m2 = _re.search(r"(\{.*\})", _raw, _re.DOTALL)
        if _m2:
            _stripped = _m2.group(1)

    try:
        _parsed = _json.loads(_stripped)
    except Exception as e:
        _result["_validation"]["errors"].append(f"json_parse_failed: {e}")
        return _result

    if not isinstance(_parsed, dict):
        _result["_validation"]["errors"].append("response_not_object")
        return _result

    _proposals = _parsed.get("section_role_proposals")
    if not isinstance(_proposals, list):
        _result["_validation"]["errors"].append("section_role_proposals_not_list")
        return _result

    _seen_ids = set()
    _valid_proposals = []
    for i, p in enumerate(_proposals):
        if not isinstance(p, dict):
            _result["_validation"]["errors"].append(f"proposal[{i}]_not_object")
            continue
        sid = p.get("section_id")
        if not isinstance(sid, int):
            _result["_validation"]["errors"].append(f"proposal[{i}]_section_id_not_int")
            continue
        _seen_ids.add(sid)
        _valid_proposals.append(p)

    _result["section_role_proposals"] = _valid_proposals
    _result["_validation"]["missing_section_ids"] = [
        i for i in expected_section_ids if i not in _seen_ids
    ]
    _result["_validation"]["ok"] = (
        not _result["_validation"]["errors"]
        and not _result["_validation"]["missing_section_ids"]
    )
    return _result


def validate_section_role_proposal(proposal: dict) -> dict:
    """13.7b B2.2: 단일 section_role_proposal schema validation.

    docs/13_7b_plan.md §9.4 — 다음 경우 validation_failed:
    - structural_relationship 빈 string 또는 누락
    - placement_recommendation 빈 string 또는 누락
    - supporting_evidence 키 누락
    - counter_evidence 키 누락
    - ambiguity_flags 키 누락
    - confidence enum 외

    code는 의미 해석 X. 명백한 schema 위반만 본다.
    placement_recommendation 텍스트의 의미 매핑은 B0b review에서 사용자+claude 합의 (§9.5).

    Returns:
        {"ok": bool, "errors": [str, ...]}
    """
    errors: list[str] = []

    if not isinstance(proposal, dict):
        return {"ok": False, "errors": ["not_object"]}

    sr = proposal.get("structural_relationship")
    if not isinstance(sr, str) or not sr.strip():
        errors.append("structural_relationship_missing_or_empty")

    pr = proposal.get("placement_recommendation")
    if not isinstance(pr, str) or not pr.strip():
        errors.append("placement_recommendation_missing_or_empty")

    if "supporting_evidence" not in proposal:
        errors.append("supporting_evidence_key_missing")
    elif not isinstance(proposal["supporting_evidence"], list):
        errors.append("supporting_evidence_not_list")

    if "counter_evidence" not in proposal:
        errors.append("counter_evidence_key_missing")
    elif not isinstance(proposal["counter_evidence"], list):
        errors.append("counter_evidence_not_list")

    if "ambiguity_flags" not in proposal:
        errors.append("ambiguity_flags_key_missing")
    elif not isinstance(proposal["ambiguity_flags"], list):
        errors.append("ambiguity_flags_not_list")

    conf = proposal.get("confidence")
    if conf not in CONFIDENCE_LEVELS:
        errors.append(f"confidence_not_in_enum:{conf!r}")

    return {"ok": not errors, "errors": errors}


def make_fallback_section_role_proposal(
    section_id: int,
    reason: str,
    detail: str = "",
) -> dict:
    """13.7b B2.2: AI 호출/parse/validation 실패 시 보수적 fallback proposal.

    docs/13_7b_plan.md §9.4 — AI 호출 fail은
    ambiguity_flags=[reason], confidence="low", placement_recommendation="preserve only".

    fallback은 section을 preserve 처리하게 하는 보수적 추천. code 의미 결정 아님.
    실제 preserve 적용 여부는 B0b review/B3에서 정책으로 결정 (§9.5).
    """
    return {
        "section_id": section_id,
        "structural_relationship": (
            "AI proposal 미확보 — 양식 evidence 기반 자유 텍스트 분석 부재"
        ),
        "placement_recommendation": "preserve only",
        "supporting_evidence": [],
        "counter_evidence": [],
        "ambiguity_flags": [reason] + ([detail] if detail else []),
        "confidence": "low",
        "_debug": {
            "reference_label": "unknown",
            "fallback_reason": reason,
            "fallback_detail": detail,
        },
    }


def summarize_section_role_proposals(
    proposals: list[dict],
    validation_results: list[dict] | None = None,
    ai_call_info: dict | None = None,
    fallback_count: int = 0,
) -> dict:
    """13.7b B2.2: _debug_payload["section_role_proposals"] summary.

    의미 분포는 free-form이라 카운트 불가. 대신 confidence 분포, ambiguity 비율,
    validation 통과 비율 등 reference metric만 기록 (§2.8).
    """
    confidence_counts = {c: 0 for c in CONFIDENCE_LEVELS}
    sections_with_ambiguity = 0
    sections_with_counter_evidence = 0
    sections_with_supporting_evidence = 0
    reference_label_counts: dict[str, int] = {}

    for p in proposals:
        if not isinstance(p, dict):
            continue
        c = p.get("confidence")
        if c in confidence_counts:
            confidence_counts[c] += 1
        if p.get("ambiguity_flags"):
            sections_with_ambiguity += 1
        if p.get("counter_evidence"):
            sections_with_counter_evidence += 1
        if p.get("supporting_evidence"):
            sections_with_supporting_evidence += 1
        _dbg = p.get("_debug", {}) or {}
        _rl = _dbg.get("reference_label") if isinstance(_dbg, dict) else None
        if isinstance(_rl, str):
            reference_label_counts[_rl] = reference_label_counts.get(_rl, 0) + 1

    validation_pass_count = 0
    validation_fail_count = 0
    if validation_results:
        for v in validation_results:
            if isinstance(v, dict) and v.get("ok"):
                validation_pass_count += 1
            else:
                validation_fail_count += 1

    return {
        "proposal_count": len(proposals),
        "fallback_count": fallback_count,
        "confidence_distribution": confidence_counts,
        "sections_with_ambiguity_flags": sections_with_ambiguity,
        "sections_with_counter_evidence": sections_with_counter_evidence,
        "sections_with_supporting_evidence": sections_with_supporting_evidence,
        "reference_label_distribution": reference_label_counts,
        "validation_pass_count": validation_pass_count,
        "validation_fail_count": validation_fail_count,
        "ai_calls": ai_call_info or {},
        "proposals": proposals,
        "validation_results": validation_results or [],
    }


# ──────────────────────────────────────────────────────────────────────
# 13.7b B0b: Post-1a Merge Feasibility Measurement
#
# B2.1.2 결과 (section_results)와 B0a 결과 (section_census)를 받아
# section별 비교 metric을 측정. B3 merge 정책 결정 evidence.
#
# 원칙:
# - 측정만. 정책 결정 X (B0b review에서 사용자+claude 합의)
# - section 간 role_cluster equivalence를 code가 확정하지 않음
# - chapter_types / marker_policy document-level 통합 X
# - cross-section parent는 Level 1 (debug only) — Level 2/3은 별도 stage
# - 모든 수치는 reference metric (§2.8)
# 참조: docs/13_7b_plan.md §6 / §8 / §10
# ──────────────────────────────────────────────────────────────────────


def detect_cross_section_parent_violations(
    section_results: dict,
) -> dict:
    """13.7b B0b Level 1: cross-section parent reference 검출 (debug-only).

    section_results 각 paragraph의 parent_idx가 section-local 범위를
    벗어나는 경우를 찾는다. detect만, validation fail X (Level 3은 별도).

    docs/13_7b_plan.md §10.1 / §10.4 schema.

    Returns:
        {
          "cross_section_parent_violations": [{...}, ...],
          "cross_section_parent_violation_count": int,
          # Level 2 분류 (a/b/c)는 B0b review에서 사용자+claude 합의 후 채움.
          "cross_section_parent_classifications": {
              "a_out_of_range": [],
              "b_section_break_ambiguous": [],
              "c_legitimate_continuation_candidate": [],
          }
        }
    """
    violations: list[dict] = []

    if not isinstance(section_results, dict):
        return {
            "cross_section_parent_violations": [],
            "cross_section_parent_violation_count": 0,
            "cross_section_parent_classifications": {
                "a_out_of_range": [],
                "b_section_break_ambiguous": [],
                "c_legitimate_continuation_candidate": [],
            },
        }

    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        structure = (sr or {}).get("structure", {}) if isinstance(sr, dict) else {}
        paragraphs = structure.get("paragraphs", []) if isinstance(structure, dict) else []
        idx_texts = (sr or {}).get("idx_texts", {}) if isinstance(sr, dict) else {}

        if not isinstance(paragraphs, list):
            continue
        local_count = len(paragraphs)

        for local_idx, p in enumerate(paragraphs):
            if not isinstance(p, dict):
                continue
            parent_idx = p.get("parent_idx")
            if parent_idx is None:
                continue
            if not isinstance(parent_idx, int):
                continue
            if 0 <= parent_idx < local_count:
                continue
            # out of section-local bounds — cross-section reference 후보
            actual_section = "out_of_range"
            if parent_idx >= 0:
                for other_sid_key, other_sr in section_results.items():
                    try:
                        other_sid_int = int(other_sid_key)
                    except (TypeError, ValueError):
                        other_sid_int = other_sid_key
                    if other_sid_int == sid_int:
                        continue
                    _other_paras = (other_sr or {}).get("structure", {}).get("paragraphs", []) if isinstance(other_sr, dict) else []
                    if isinstance(_other_paras, list) and 0 <= parent_idx < len(_other_paras):
                        actual_section = other_sid_int
                        break
            text_preview = ""
            if isinstance(idx_texts, dict):
                _t = idx_texts.get(str(p.get("idx", local_idx)), "")
                if isinstance(_t, str):
                    text_preview = _t[:80]

            violations.append({
                "section_id": sid_int,
                "section_local_paragraph_idx": local_idx,
                "reported_parent_idx": parent_idx,
                "expected_parent_section": sid_int,
                "actual_parent_section": actual_section,
                "text_preview": text_preview,
                "role": p.get("role"),
            })

    return {
        "cross_section_parent_violations": violations,
        "cross_section_parent_violation_count": len(violations),
        "cross_section_parent_classifications": {
            "a_out_of_range": [],
            "b_section_break_ambiguous": [],
            "c_legitimate_continuation_candidate": [],
        },
    }


def _compare_section_role_clusters(section_results: dict) -> dict:
    """section별 role_cluster fingerprint 비교 (reference metric only).

    docs/13_7b_plan.md §8.3 — A 옵션(document_reclustering) vs B 옵션
    (section_local_mapping) 결정 evidence. code 자동 결정 X.
    """
    per_section: dict = {}
    all_clusters: set[str] = set()

    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        structure = (sr or {}).get("structure", {}) if isinstance(sr, dict) else {}
        paragraphs = structure.get("paragraphs", []) if isinstance(structure, dict) else []
        cluster_counts: dict[str, int] = {}
        if isinstance(paragraphs, list):
            for p in paragraphs:
                if not isinstance(p, dict):
                    continue
                r = p.get("role")
                if isinstance(r, str) and r:
                    cluster_counts[r] = cluster_counts.get(r, 0) + 1
                    all_clusters.add(r)
        per_section[sid_int] = {
            "distinct_cluster_count": len(cluster_counts),
            "cluster_counts": cluster_counts,
            "top_clusters": sorted(
                cluster_counts.items(), key=lambda kv: kv[1], reverse=True
            )[:8],
        }

    # pairwise Jaccard (reference metric only)
    sids = sorted(per_section.keys(), key=lambda x: (isinstance(x, str), x))
    pairwise_jaccard: list[dict] = []
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            a = set(per_section[sids[i]]["cluster_counts"].keys())
            b = set(per_section[sids[j]]["cluster_counts"].keys())
            if not a and not b:
                jac = None
            else:
                jac = round(len(a & b) / len(a | b), 3) if (a | b) else None
            pairwise_jaccard.append({
                "section_a": sids[i],
                "section_b": sids[j],
                "shared_clusters_count": len(a & b),
                "union_clusters_count": len(a | b),
                "jaccard_similarity": jac,
            })

    return {
        "per_section": per_section,
        "total_distinct_clusters": len(all_clusters),
        "pairwise_jaccard": pairwise_jaccard,
    }


def _compare_section_chapter_types(section_results: dict) -> dict:
    """section별 chapter_types 비교 (reference metric only).

    docs/13_7b_plan.md §8.4 — X/Y/Z 옵션 결정 evidence.
    """
    per_section: dict = {}
    all_type_keys: set[str] = set()
    title_roles_per_section: dict = {}

    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        chapter_types = (sr or {}).get("chapter_types", {}) if isinstance(sr, dict) else {}
        type_keys = list(chapter_types.keys()) if isinstance(chapter_types, dict) else []
        title_roles: list = []
        for ct_val in (chapter_types.values() if isinstance(chapter_types, dict) else []):
            if not isinstance(ct_val, dict):
                continue
            tr = ct_val.get("title_role")
            if isinstance(tr, str):
                title_roles.append(tr)
            elif isinstance(tr, list):
                for x in tr:
                    if isinstance(x, str):
                        title_roles.append(x)
        per_section[sid_int] = {
            "type_count": len(type_keys),
            "type_keys": type_keys,
            "title_roles": sorted(set(title_roles)),
        }
        title_roles_per_section[sid_int] = set(title_roles)
        all_type_keys.update(type_keys)

    # title_role overlap (pairwise)
    sids = sorted(per_section.keys(), key=lambda x: (isinstance(x, str), x))
    title_role_overlap: list[dict] = []
    for i in range(len(sids)):
        for j in range(i + 1, len(sids)):
            a = title_roles_per_section.get(sids[i], set())
            b = title_roles_per_section.get(sids[j], set())
            title_role_overlap.append({
                "section_a": sids[i],
                "section_b": sids[j],
                "shared_title_roles_count": len(a & b),
                "section_a_title_roles": sorted(a),
                "section_b_title_roles": sorted(b),
            })

    return {
        "per_section": per_section,
        "total_distinct_type_keys": len(all_type_keys),
        "title_role_overlap": title_role_overlap,
    }


def _compare_section_marker_policy(section_results: dict) -> dict:
    """section별 marker_policy_1f 비교 (reference metric only).

    docs/13_7b_plan.md §8.5.
    """
    per_section: dict = {}
    all_roles_with_policy: set[str] = set()

    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        mp = (sr or {}).get("marker_policy_1f", {}) if isinstance(sr, dict) else {}
        if not isinstance(mp, dict):
            mp = {}
        per_role_types: dict[str, str] = {}
        types_present: set[str] = set()
        for role, policy in mp.items():
            if not isinstance(policy, dict):
                continue
            t = policy.get("type")
            if isinstance(t, str):
                per_role_types[role] = t
                types_present.add(t)
                all_roles_with_policy.add(role)
        per_section[sid_int] = {
            "role_count": len(mp),
            "per_role_types": per_role_types,
            "types_present": sorted(types_present),
        }

    # 같은 role이 section 간 다른 type을 가지는 conflict 후보 검출 (debug only)
    role_type_conflicts: list[dict] = []
    for role in sorted(all_roles_with_policy):
        types_seen: dict = {}
        for sid_int, section_info in per_section.items():
            t = section_info["per_role_types"].get(role)
            if t:
                types_seen.setdefault(t, []).append(sid_int)
        if len(types_seen) > 1:
            role_type_conflicts.append({
                "role": role,
                "types_to_sections": {t: sids for t, sids in types_seen.items()},
            })

    return {
        "per_section": per_section,
        "role_type_conflict_count": len(role_type_conflicts),
        "role_type_conflicts": role_type_conflicts,
    }


def _compare_section_format_rules(section_results: dict) -> dict:
    """section별 format_rules 비교 (reference metric only).

    docs/13_7b_plan.md §8.6 — 차이 없으면 document-level 통합, 있으면
    section-aware. B0b review에서 합의.
    """
    per_section: dict = {}
    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        structure = (sr or {}).get("structure", {}) if isinstance(sr, dict) else {}
        fr = structure.get("format_rules", {}) if isinstance(structure, dict) else {}
        per_section[sid_int] = {
            "rule_count": len(fr) if isinstance(fr, dict) else 0,
            "rule_keys": sorted(fr.keys()) if isinstance(fr, dict) else [],
        }
    return {"per_section": per_section}


def _measure_section_paragraph_count_consistency(
    section_results: dict,
    section_census: dict,
) -> dict:
    """1a 분석 paragraph 수 vs section_xml paragraph 수 정합성.

    docs/13_7b_plan.md §6.2 — 1a가 section_xml의 paragraph를 전부 분석했는지.
    """
    census_by_sid: dict = {}
    if isinstance(section_census, dict):
        for s in section_census.get("sections", []) or []:
            if isinstance(s, dict):
                census_by_sid[s.get("section_id")] = s

    per_section: list = []
    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        structure = (sr or {}).get("structure", {}) if isinstance(sr, dict) else {}
        analyzed_count = len(structure.get("paragraphs", [])) if isinstance(structure, dict) else 0
        census_entry = census_by_sid.get(sid_int, {}) or {}
        census_count = census_entry.get("paragraph_count")
        per_section.append({
            "section_id": sid_int,
            "analyzed_paragraph_count": analyzed_count,
            "census_paragraph_count": census_count,
            "diff": (
                analyzed_count - census_count
                if isinstance(census_count, int) else None
            ),
        })
    return {"per_section": per_section}


def measure_merge_feasibility(
    section_results: dict,
    section_census: dict | None,
) -> dict:
    """13.7b B0b: section-aware merge 가능성 측정 (debug-only).

    docs/13_7b_plan.md §6.

    측정만. 정책 결정 X (B0b review에서 사용자+claude 합의).
    모든 수치는 reference metric (§2.8). hard rule X.

    Returns:
        {
          "section_role_cluster_comparison": {...},
          "section_chapter_types_comparison": {...},
          "section_marker_policy_comparison": {...},
          "section_format_rules_comparison": {...},
          "section_paragraph_count_consistency": {...},
          "cross_section_parent": {...},  # Level 1 only
          "13_7c_re_run_feasibility": {...},
        }
    """
    if not isinstance(section_results, dict):
        return {"error": "section_results not dict"}

    role_cluster_cmp = _compare_section_role_clusters(section_results)
    chapter_types_cmp = _compare_section_chapter_types(section_results)
    marker_policy_cmp = _compare_section_marker_policy(section_results)
    format_rules_cmp = _compare_section_format_rules(section_results)
    paragraph_count_cmp = _measure_section_paragraph_count_consistency(
        section_results, section_census or {}
    )
    cross_section_parent = detect_cross_section_parent_violations(section_results)

    # 13.7c re-run feasibility — section별 chapter_types에서 chapter 후보 수 추정
    new_chapter_candidates: dict = {}
    for sid_key, sr in section_results.items():
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        ct = (sr or {}).get("chapter_types", {}) if isinstance(sr, dict) else {}
        # chapter_types[type].root_roles에서 root_role을 가진 paragraph 카운트가 chapter 후보 수
        root_roles_per_type: dict = {}
        for ct_key, ct_val in (ct.items() if isinstance(ct, dict) else []):
            if isinstance(ct_val, dict):
                rr = ct_val.get("root_roles", [])
                if isinstance(rr, list):
                    root_roles_per_type[ct_key] = rr
        paragraphs = (sr or {}).get("structure", {}).get("paragraphs", []) if isinstance(sr, dict) else []
        # paragraph 중 root_role를 가진 것 카운트 (per type)
        root_paragraph_counts: dict = {}
        for type_key, root_roles in root_roles_per_type.items():
            cnt = sum(
                1 for p in paragraphs
                if isinstance(p, dict) and p.get("role") in root_roles
            )
            root_paragraph_counts[type_key] = cnt
        new_chapter_candidates[sid_int] = {
            "chapter_types": list(root_roles_per_type.keys()),
            "root_paragraph_counts_per_type": root_paragraph_counts,
        }

    return {
        "section_role_cluster_comparison": role_cluster_cmp,
        "section_chapter_types_comparison": chapter_types_cmp,
        "section_marker_policy_comparison": marker_policy_cmp,
        "section_format_rules_comparison": format_rules_cmp,
        "section_paragraph_count_consistency": paragraph_count_cmp,
        "cross_section_parent": cross_section_parent,
        "13_7c_re_run_feasibility": {
            "new_chapter_candidates": new_chapter_candidates,
            "note": (
                "section별 root_role paragraph count. 13.7c re-run 시 chapter 수 확장 후보 "
                "(B3.2 document-level seed regenerate 입력). code가 chapter 추가 결정 X — review/B3 정책."
            ),
        },
    }


def build_b0b_observation_artifact(
    merge_feasibility: dict,
    section_role_proposals_summary: dict | None = None,
) -> dict:
    """13.7b B0b: `13_7b_b0b_observation.json` artifact 구성.

    docs/13_7b_plan.md §6.5 — review_decisions는 빈 상태로 둠.
    B0b review point에서 사용자+claude가 4정책 합의 후 채움:
    - role_clustering_strategy: A (document_level_reclustering) | B (section_local_mapping)
    - chapter_types_merge_strategy: X | Y | Z
    - marker_policy_merge_strategy: section_aware | document_level
    - section_placement_strategy_per_proposal: 각 section을 Case A/B/C/D 매핑
    """
    artifact = {
        "merge_feasibility_metrics": {
            "section_role_cluster_comparison": merge_feasibility.get(
                "section_role_cluster_comparison", {}
            ),
            "section_chapter_types_comparison": merge_feasibility.get(
                "section_chapter_types_comparison", {}
            ),
            "section_marker_policy_comparison": merge_feasibility.get(
                "section_marker_policy_comparison", {}
            ),
            "section_format_rules_comparison": merge_feasibility.get(
                "section_format_rules_comparison", {}
            ),
            "section_paragraph_count_consistency": merge_feasibility.get(
                "section_paragraph_count_consistency", {}
            ),
        },
        "cross_section_parent": merge_feasibility.get("cross_section_parent", {}),
        "13_7c_re_run_feasibility": merge_feasibility.get("13_7c_re_run_feasibility", {}),
        "section_role_proposals_summary": section_role_proposals_summary or {},
        "review_decisions": {
            "role_clustering_strategy": "",
            "chapter_types_merge_strategy": "",
            "marker_policy_merge_strategy": "",
            "section_placement_strategy_per_proposal": [],
            "reasoning": "",
            "counter_evidence": [],
            "unresolved_ambiguity": [],
            "reviewer": "user+claude",
            "b3_entry_authorized": False,
        },
    }
    return artifact


# ──────────────────────────────────────────────────────────────────────
# 13.7b Section-Local Generation-Lite (deadline path)
#
# B3 document-level merge 없이 각 section을 자기 section_results 구조로
# 독립 generation. section 간 parent/role/chapter merge X.
#
# section 0: 기존 13.4b extract_chapter_template_plan_seed 경로 유지 (regression 최소화).
# section N (N != 0): 아래 함수들로 section-local chapter list 추출 + 처리 결정.
#
# 원칙:
# - section X 내용이 section Y region에 들어가지 않음 (assembly anchor section-aware)
# - section-local idx를 document-global idx로 변환해서 assembly에 전달
# - 의미 매핑 X — code는 evidence 기반 schema 결정만
# - source allocation은 broad source (deadline 임시, 사용자 §5)
# 참조: 사용자 directive 2026-05-15 section-local generation-lite
# ──────────────────────────────────────────────────────────────────────


def compute_section_offsets(
    section_results: dict,
    section_xml_paragraph_counts: dict | None = None,
) -> dict:
    """13.7b section-local generation-lite: document-global offset per section.

    section_xml_paragraph_counts가 있으면 census 기준 (assembly의
    _section_top_level_paragraphs idx와 일치). 없으면 1a analyzed count fallback
    (단 매핑 깨질 위험 — 1a paragraph 누락 영향).

    light_xml의 _top_level_paragraphs ordering이 section_id 순서라는 가정
    (B1 extract_all_sections_xml sorted name 결과).

    Args:
        section_results: section별 1a 결과
        section_xml_paragraph_counts: {section_id: top_level_p_count} (census 기준)

    Returns:
        {section_id: document_global_offset} dict
    """
    offsets: dict = {}
    cumulative = 0
    for sid_key in sorted(
        section_results.keys(),
        key=lambda k: int(k) if str(k).isdigit() else 999
    ):
        try:
            sid_int = int(sid_key)
        except (TypeError, ValueError):
            sid_int = sid_key
        offsets[sid_int] = cumulative
        if section_xml_paragraph_counts and sid_int in section_xml_paragraph_counts:
            cumulative += section_xml_paragraph_counts[sid_int]
        else:
            sr = section_results.get(sid_key) or {}
            struct = sr.get("structure", {}) if isinstance(sr, dict) else {}
            paras = struct.get("paragraphs", []) if isinstance(struct, dict) else []
            cumulative += len(paras) if isinstance(paras, list) else 0
    return offsets


def _build_1a_to_xml_p_idx_mapping(
    idx_texts: dict,
    section_xml_paragraph_texts: list,
) -> dict:
    """13.7b: 1a paragraph idx → section_xml top-level p idx 매핑.

    1a가 일부 paragraph를 누락한 경우 (paragraph_count_consistency.diff < 0),
    1a idx와 section_xml top-level p idx 사이에 shift 발생. text 정규화 +
    substring 매칭으로 보정.

    매칭 실패 시 마지막 valid xml_pos 다음 idx 추정 (identity fallback은
    누적 shift 일으킴).

    Args:
        idx_texts: section_results[N].idx_texts (1a paragraph idx → text)
        section_xml_paragraph_texts: section_xml의 top-level p text 순서대로

    Returns:
        {1a_idx: xml_p_idx} dict
    """
    if not isinstance(idx_texts, dict) or not section_xml_paragraph_texts:
        return {}

    def _normalize(s: str) -> str:
        """공백, 탭, 줄바꿈, 특수 whitespace 모두 제거 (강력한 정규화)."""
        if not s:
            return ""
        return "".join(s.split())

    xml_norm = [_normalize(t) for t in section_xml_paragraph_texts]

    sorted_ai_idx = sorted(
        int(k) for k in idx_texts.keys() if str(k).isdigit()
    )

    mapping: dict = {}
    xml_pos = 0
    last_valid_xml = -1

    for ai_idx in sorted_ai_idx:
        target = _normalize(idx_texts.get(str(ai_idx), ""))
        if not target:
            # 빈 1a paragraph — 다음 valid mapping 사용 후 보정
            mapping[ai_idx] = max(last_valid_xml + 1, xml_pos)
            continue

        # forward search from xml_pos, 빈 paragraph skip
        found_idx = -1

        # 1순위: exact match (정규화 후)
        for j in range(xml_pos, len(xml_norm)):
            if not xml_norm[j]:
                continue  # 빈 xml paragraph skip
            if xml_norm[j] == target:
                found_idx = j
                break

        # 2순위: substring 양방향 (양식 marker/spacing 차이 흡수)
        if found_idx < 0:
            target_short = target[:40] if len(target) >= 40 else target
            for j in range(xml_pos, len(xml_norm)):
                xn = xml_norm[j]
                if not xn:
                    continue
                # 짧은 쪽 30+ chars 기준 substring 매칭
                xn_short = xn[:40] if len(xn) >= 40 else xn
                min_len = min(len(target_short), len(xn_short))
                if min_len < 8:
                    # 너무 짧으면 exact만 (이미 위에서 시도)
                    continue
                # 양방향 substring
                if (target_short in xn) or (xn_short in target):
                    found_idx = j
                    break

        if found_idx >= 0:
            mapping[ai_idx] = found_idx
            xml_pos = found_idx + 1
            last_valid_xml = found_idx
        else:
            # 매칭 실패 → 마지막 valid 다음 (누적 shift 최소화)
            mapping[ai_idx] = max(last_valid_xml + 1, xml_pos)
            # xml_pos 그대로 (다음 ai_idx가 같은 위치에서 search)

    return mapping


def build_chapter_local_exemplars(
    chapter_list_per_section: dict,
    section_results: dict,
    ai_to_xml_mappings: dict,
) -> dict:
    """13.7b §4 chapter-local pattern preservation: chapter별 role 본보기 매핑.

    각 chapter의 자기 영역(section의 chapter range) paragraph에서 role별 첫 등장
    paragraph를 본보기로 추출. assembly가 body item insert 시 chapter-local
    exemplar 우선 사용. local에 없는 role만 outer (section 0) fallback.

    §4 원칙 실현: Ⅰ장 body는 Ⅰ장 트리 안 paragraph 본보기로 생성. multi-section
    양식에서는 section 4 chapter body는 section 4 paragraph 본보기로 생성
    (section 0 element 안 가져다 씀).

    Args:
        chapter_list_per_section: {section_id: extract_section_chapter_list result}
        section_results: section_results[N]
        ai_to_xml_mappings: {section_id: {1a_idx: xml_idx}}

    Returns:
        {chapter_obj_idx: {"section_id": int, "role_to_xml_idx": {role: xml_idx}}}
        chapter_obj_idx는 assembly의 _chapter_objects list index 기준 (0-based 누적)
    """
    result: dict = {}
    chapter_obj_idx = 0

    # 13.4b path (section 0 기존 path) chapter는 별도 처리 — 여기서는 section_local
    # chapter list만 처리. section 0 13.4b chapter는 DB tool에서 별도 매핑 추가.

    for sid_int in sorted(
        chapter_list_per_section.keys(),
        key=lambda k: int(k) if isinstance(k, (int, str)) and str(k).isdigit() else 999
    ):
        scl = chapter_list_per_section.get(sid_int) or {}
        chapters = scl.get("chapters") or []
        sr = section_results.get(sid_int) or section_results.get(str(sid_int)) or {}
        struct = sr.get("structure", {}) if isinstance(sr, dict) else {}
        paragraphs = struct.get("paragraphs", []) if isinstance(struct, dict) else []
        ai_to_xml = ai_to_xml_mappings.get(sid_int, {})

        # paragraph_idx → role 매핑
        para_role_map: dict = {}
        for p in paragraphs:
            if isinstance(p, dict):
                idx = p.get("idx")
                role = p.get("role")
                if idx is not None and role:
                    para_role_map[idx] = role

        # 각 chapter의 role 본보기 추출 (chapter 범위 안 paragraph)
        for ch in chapters:
            ch_section_local_indices_1a = ch.get("section_local_paragraph_1a_indices")
            # NOTE: extract_section_chapter_list 결과의 section_local_paragraph_indices는
            # xml mapping 후 idx. 1a idx 따로 보관 필요. 일단 1a paragraphs.idx 기준 매칭.
            # ch.section_local_paragraph_indices가 xml idx로 변환됐으면,
            # 1a paragraphs idx와 매칭 필요 — para_role_map의 1a idx 기준.
            # 단순화: 1a paragraphs 순서 = sorted_paragraphs. chapter range start_pos~end_pos.
            # 그 1a paragraphs의 role을 모음 + 각 role의 첫 1a idx → xml idx.

            role_to_xml_idx: dict = {}
            # chapter range는 section_local_paragraph_indices (xml idx)
            # 다시 1a idx로 역매핑 어려움. 대신 1a paragraphs에서 chapter title role을
            # 찾고 그 후로 다음 chapter title 전까지 paragraph 모음.
            title_role = ch.get("title_role")
            title_1a_idx = ch.get("title_section_local_1a_idx")  # 1a idx 보관됨
            if title_1a_idx is None:
                continue

            # 다음 chapter title 1a idx (chapter range end)
            next_idx = None
            ch_idx_in_list = ch.get("chapter_idx")
            if isinstance(ch_idx_in_list, int) and ch_idx_in_list + 1 < len(chapters):
                next_ch = chapters[ch_idx_in_list + 1]
                next_idx = next_ch.get("title_section_local_1a_idx")

            # 1a paragraphs 중 chapter range (title_1a_idx ~ next_idx - 1)
            for p in paragraphs:
                if not isinstance(p, dict):
                    continue
                p_idx = p.get("idx")
                if p_idx is None:
                    continue
                if p_idx < title_1a_idx:
                    continue
                if next_idx is not None and p_idx >= next_idx:
                    break
                role = p.get("role")
                if not role:
                    continue
                if role in role_to_xml_idx:
                    continue
                # 1a idx → xml idx 변환
                xml_idx = ai_to_xml.get(p_idx, p_idx)
                role_to_xml_idx[role] = xml_idx

            if role_to_xml_idx:
                result[chapter_obj_idx] = {
                    "section_id": sid_int,
                    "role_to_xml_idx": role_to_xml_idx,
                    "chapter_local_idx": ch_idx_in_list,
                }
            chapter_obj_idx += 1

    return result


def extract_paragraph_run_parts(
    hwpx_source,
    paragraph_real_idx: int,
) -> list:
    """양식 paragraph의 run/t 분포를 (charPrIDRef + text) parts list로 분해.

    header 영역(예: 표지 제목 박스)이 양식에서 여러 charPr로 분리된 경우
    이 분해 결과를 LLM에 보내 t별 폰트 보존하면서 새 텍스트 생성.

    Args:
        hwpx_source: 양식 파일 경로 또는 bytes
        paragraph_real_idx: doc.paragraphs 기준 idx (양식 raw XML top-level p 순번)

    Returns:
        [{"charPrIDRef": str, "text": str}, ...] (text 비어있는 t는 제외)
        paragraph 없거나 추출 실패 시 빈 list.
    """
    import zipfile as _zf
    parts: list = []
    try:
        if isinstance(hwpx_source, str):
            with open(hwpx_source, "rb") as f:
                data = f.read()
        elif isinstance(hwpx_source, bytes):
            data = hwpx_source
        else:
            data = hwpx_source.read()
        with _zf.ZipFile(io.BytesIO(data)) as zf:
            section_names = sorted(
                n for n in zf.namelist()
                if "section" in n.lower() and n.endswith(".xml")
            )
            all_top_ps = []
            for s in section_names:
                root = etree.fromstring(zf.read(s))
                for p in root.findall(f"{NS_HP}p"):
                    all_top_ps.append(p)
            if paragraph_real_idx < 0 or paragraph_real_idx >= len(all_top_ps):
                return []
            p_elem = all_top_ps[paragraph_real_idx]
            for run in p_elem.findall(f"{NS_HP}run"):
                cp = run.get("charPrIDRef", "0")
                text = "".join(t.text or "" for t in run.findall(f"{NS_HP}t"))
                if text:
                    parts.append({"charPrIDRef": cp, "text": text})
    except Exception as e:
        log.warning(f"extract_paragraph_run_parts 실패 (idx={paragraph_real_idx}): {e}")
        return []
    return parts


def extract_section_xml_paragraph_texts(hwpx_path: str, section_name: str) -> list:
    """13.7b: section_xml의 top-level p element text 추출 (mapping 입력용).

    section_xml top-level p 순서대로 text list.
    """
    import zipfile
    import xml.etree.ElementTree as _ET

    def _lt(el):
        return el.tag.split("}")[-1] if "}" in el.tag else el.tag

    def _get_text(elem):
        parts = []
        for t in elem.iter():
            if _lt(t) == "t" and t.text:
                parts.append(t.text)
        return "".join(parts).strip()

    try:
        with zipfile.ZipFile(hwpx_path, "r") as zf:
            raw = zf.read(section_name)
    except Exception:
        return []

    try:
        root = _ET.fromstring(raw)
    except _ET.ParseError:
        return []

    top_p = [c for c in root if _lt(c) == "p"]
    return [_get_text(p) for p in top_p]


def extract_section_chapter_list(
    section_id: int,
    section_result: dict,
    section_offset: int,
    ai_to_xml_idx_mapping: dict | None = None,
) -> dict:
    """13.7b section-local generation-lite: section-local chapter list 추출.

    section_results[N]의 structure를 받아 section-local title_roles 기반으로
    chapter title paragraphs를 찾는다. document-global idx로 변환된
    paragraph_indices를 함께 출력 (assembly anchor mapping용).

    이 함수는 section ≠ 0 만 사용. section 0은 기존 13.4b
    extract_chapter_template_plan_seed 경로 유지 (regression 최소화).

    13.7b fix: ai_to_xml_idx_mapping이 있으면 1a paragraph idx → section_xml
    top-level p idx로 변환. 1a 누락 paragraph 영향 보정. 없으면 1a idx 그대로
    (assembly anchor mapping 부정확 위험).

    Args:
        section_id: section 번호
        section_result: section_results[section_id]
        section_offset: document-global offset
        ai_to_xml_idx_mapping: {1a_idx: xml_p_idx} (위 _build_1a_to_xml_p_idx_mapping 결과)

    Returns:
        {
          "section_id": int,
          "section_offset": int,
          "paragraph_count": int (section-local total),
          "title_roles_used": [str],
          "chapters": [
            {
              "section_id": int,
              "chapter_idx": int,
              "template_title": str,
              "marker": str,
              "title_role": str,
              "title_section_local_idx": int,
              "title_document_global_idx": int,
              "section_local_paragraph_indices": [int, ...],
              "document_global_paragraph_indices": [int, ...],
              "paragraph_count": int,
              "description": str,
              "dominant_chapter_type": str | None,
            }, ...
          ],
          "confidence": "high" | "medium" | "low",
          "fallback_reason": str | None,
        }
    """
    structure = section_result.get("structure", {}) if isinstance(section_result, dict) else {}
    chapter_types = section_result.get("chapter_types", {}) if isinstance(section_result, dict) else {}
    idx_texts = section_result.get("idx_texts", {}) if isinstance(section_result, dict) else {}
    paragraphs = structure.get("paragraphs", []) if isinstance(structure, dict) else []

    if not isinstance(paragraphs, list) or not paragraphs:
        return {
            "section_id": section_id,
            "section_offset": section_offset,
            "paragraph_count": 0,
            "title_roles_used": [],
            "chapters": [],
            "confidence": "low",
            "fallback_reason": "no_paragraphs",
        }

    title_roles: set = set()
    for ct_val in (chapter_types.values() if isinstance(chapter_types, dict) else []):
        if not isinstance(ct_val, dict):
            continue
        tr = ct_val.get("title_role")
        if isinstance(tr, str) and tr:
            title_roles.add(tr)
        elif isinstance(tr, list):
            for x in tr:
                if isinstance(x, str) and x:
                    title_roles.add(x)

    if not title_roles:
        return {
            "section_id": section_id,
            "section_offset": section_offset,
            "paragraph_count": len(paragraphs),
            "title_roles_used": [],
            "chapters": [],
            "confidence": "low",
            "fallback_reason": "no_title_roles_in_chapter_types",
        }

    sorted_paragraphs = sorted(
        (p for p in paragraphs if isinstance(p, dict)),
        key=lambda p: p.get("idx", 0)
    )

    title_meta: list[dict] = []
    for i, p in enumerate(sorted_paragraphs):
        if p.get("role") in title_roles:
            local_idx = p.get("idx", i)
            title_meta.append({
                "position": i,
                "section_local_idx": local_idx,
                "role": p.get("role"),
                "marker": (p.get("marker") or "").strip(),
                "text": (
                    idx_texts.get(str(local_idx), "")
                    if isinstance(idx_texts, dict) else ""
                ),
            })

    if not title_meta:
        return {
            "section_id": section_id,
            "section_offset": section_offset,
            "paragraph_count": len(sorted_paragraphs),
            "title_roles_used": sorted(title_roles),
            "chapters": [],
            "confidence": "low",
            "fallback_reason": "no_title_paragraphs_matched_in_section",
        }

    # 13.7b: 1a paragraph idx → section_xml top-level p idx 매핑 적용
    def _resolve_xml_idx(ai_idx: int) -> int:
        if ai_to_xml_idx_mapping and ai_idx in ai_to_xml_idx_mapping:
            return ai_to_xml_idx_mapping[ai_idx]
        return ai_idx

    chapters: list[dict] = []
    for ci, tm in enumerate(title_meta):
        start_pos = tm["position"]
        end_pos = (
            title_meta[ci + 1]["position"]
            if ci + 1 < len(title_meta) else len(sorted_paragraphs)
        )
        chapter_paras = sorted_paragraphs[start_pos:end_pos]
        section_local_indices = [
            p.get("idx", -1) for p in chapter_paras if isinstance(p, dict)
        ]
        # 13.7b: xml idx로 변환 (assembly _section_top_level_paragraphs idx 일치)
        xml_section_local_indices = [
            _resolve_xml_idx(idx) for idx in section_local_indices if idx >= 0
        ]
        document_global_indices = [
            section_offset + xml_idx for xml_idx in xml_section_local_indices
        ]

        description_parts: list[str] = []
        for p in chapter_paras[1:4]:
            if not isinstance(p, dict):
                continue
            t = (
                idx_texts.get(str(p.get("idx", -1)), "")
                if isinstance(idx_texts, dict) else ""
            )
            if t and t.strip():
                description_parts.append(t.strip()[:80])
        description = " | ".join(description_parts) if description_parts else ""

        type_scores: dict = {}
        for ct_key, ct_val in (chapter_types.items() if isinstance(chapter_types, dict) else []):
            if not isinstance(ct_val, dict):
                continue
            ct_roots = set(ct_val.get("root_roles", []) or [])
            score = sum(
                1 for p in chapter_paras
                if isinstance(p, dict) and p.get("role") in ct_roots
            )
            type_scores[ct_key] = score
        dominant_type = None
        if type_scores:
            _max_type = max(type_scores, key=type_scores.get)
            if type_scores[_max_type] > 0:
                dominant_type = _max_type

        # 13.7b: title의 xml idx
        _title_xml_local_idx = _resolve_xml_idx(tm["section_local_idx"])
        chapters.append({
            "section_id": section_id,
            "chapter_idx": ci,
            "template_title": tm["text"],
            "marker": tm["marker"],
            "title_role": tm["role"],
            "title_section_local_idx": _title_xml_local_idx,  # xml idx 기준
            "title_section_local_1a_idx": tm["section_local_idx"],  # 1a idx (debug)
            "title_document_global_idx": section_offset + _title_xml_local_idx,
            "section_local_paragraph_indices": xml_section_local_indices,
            "document_global_paragraph_indices": document_global_indices,
            "paragraph_count": len(chapter_paras),
            "description": description,
            "dominant_chapter_type": dominant_type,
            "chapter_type_scores": type_scores,
        })

    if chapters and all(c["paragraph_count"] >= 2 for c in chapters):
        confidence = "high"
    elif chapters:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "section_id": section_id,
        "section_offset": section_offset,
        "paragraph_count": len(sorted_paragraphs),
        "title_roles_used": sorted(title_roles),
        "chapters": chapters,
        "confidence": confidence,
        "fallback_reason": None,
    }


def decide_section_processing(
    section_id: int,
    b22_proposal: dict | None,
    section_chapter_list: dict | None,
) -> dict:
    """13.7b section-local generation-lite: section 처리 결정.

    deadline 정책 (사용자 directive 2026-05-15):
    - B2.2 proposal의 reference_label + confidence 우선
    - reference_label이 top_level/body/other 이면서 confidence != "low" → generate
    - 그 외 (attachment/empty/nested/low confidence) → preserve

    ambiguity_flags는 generate 진행 시 debug에 기록만 (hard preserve gate X).
    이유: ambiguity_flags가 "분류 불확실"이 아니라 "부수적 위험 신호"인 경우
    많음 (예: "본문이지만 attachment 사이에 끼어있다"는 layout 정보).
    분류 자체는 high confidence이면 confident. 13.7c가 chapter 단위로
    generate/preserve를 다시 판단함.

    "분류 불확실"은 confidence="low"로 표시. low confidence면 preserve.

    NOTE: code는 free-form structural_relationship/placement_recommendation 텍스트를
    해석하지 않는다. reference_label은 양식 evidence statistical hint로 사용 — deadline
    임시 정책 (사용자 명시). B0b 정식 정책 결정 후 합리화 예정.

    "other" reference_label 포함: 조달청 single-section처럼 AI가 분류 카테고리에 강제
    매핑 안 한 경우. 양식 evidence 부재가 아니므로 generate 후보.

    Returns:
        {
          "action": "generate" | "preserve",
          "reason": str,
          "deadline_policy_relaxation": bool,
          "details": [str] | None,
          "ambiguity_acknowledged": bool,  # generate인데 ambig flag 있었으면 True
        }
    """
    if not section_chapter_list or not isinstance(section_chapter_list, dict):
        return {
            "action": "preserve",
            "reason": "no_section_chapter_list",
            "deadline_policy_relaxation": False,
            "details": None,
            "ambiguity_acknowledged": False,
        }

    chapters = section_chapter_list.get("chapters") or []
    if not chapters:
        return {
            "action": "preserve",
            "reason": "empty_section_chapter_list",
            "deadline_policy_relaxation": False,
            "details": [section_chapter_list.get("fallback_reason") or "no_chapters"],
            "ambiguity_acknowledged": False,
        }

    if not b22_proposal or not isinstance(b22_proposal, dict):
        return {
            "action": "preserve",
            "reason": "no_b22_proposal",
            "deadline_policy_relaxation": False,
            "details": None,
            "ambiguity_acknowledged": False,
        }

    conf = b22_proposal.get("confidence")
    ambig = b22_proposal.get("ambiguity_flags") or []
    ref_label = ""
    _dbg = b22_proposal.get("_debug")
    if isinstance(_dbg, dict):
        ref_label = _dbg.get("reference_label", "") or ""

    if conf == "low":
        return {
            "action": "preserve",
            "reason": "ai_low_confidence",
            "deadline_policy_relaxation": False,
            "details": list(ambig) + [f"reference_label={ref_label}"],
            "ambiguity_acknowledged": bool(ambig),
        }

    if ref_label in ("top_level", "body", "other"):
        return {
            "action": "generate",
            "reason": "ai_reference_label_indicates_body",
            "deadline_policy_relaxation": True,
            "details": [
                f"reference_label={ref_label}",
                f"confidence={conf}",
                f"chapter_count={len(chapters)}",
            ] + ([f"ambiguity_flags={ambig}"] if ambig else []),
            "ambiguity_acknowledged": bool(ambig),
        }

    return {
        "action": "preserve",
        "reason": f"ai_reference_label_not_body:{ref_label or 'unknown'}",
        "deadline_policy_relaxation": True,
        "details": [f"reference_label={ref_label}"] + ([f"ambiguity_flags={ambig}"] if ambig else []),
        "ambiguity_acknowledged": bool(ambig),
    }


def summarize_section_local_decisions(
    decisions: dict,
    section_chapter_lists: dict,
) -> dict:
    """13.7b section-local generation-lite: _debug_payload summary.

    Args:
        decisions: {section_id: decide_section_processing result}
        section_chapter_lists: {section_id: extract_section_chapter_list result}

    Returns:
        {
          "section_count": int,
          "generate_section_ids": [int, ...],
          "preserve_section_ids": [int, ...],
          "per_section": {section_id: {...}, ...},
          "deadline_policy_note": str,
        }
    """
    generate_sids: list = []
    preserve_sids: list = []
    per_section: dict = {}

    for sid, dec in decisions.items():
        if not isinstance(dec, dict):
            continue
        action = dec.get("action")
        if action == "generate":
            generate_sids.append(sid)
        else:
            preserve_sids.append(sid)
        scl = section_chapter_lists.get(sid, {}) or {}
        per_section[sid] = {
            "action": action,
            "reason": dec.get("reason"),
            "deadline_policy_relaxation": dec.get("deadline_policy_relaxation"),
            "details": dec.get("details"),
            "section_chapter_count": len(scl.get("chapters") or []),
            "section_paragraph_count": scl.get("paragraph_count"),
            "section_offset": scl.get("section_offset"),
            "title_roles_used": scl.get("title_roles_used"),
            "section_chapter_list_confidence": scl.get("confidence"),
            "section_chapter_list_fallback_reason": scl.get("fallback_reason"),
        }

    return {
        "section_count": len(decisions),
        "generate_section_ids": sorted(
            generate_sids,
            key=lambda x: int(x) if isinstance(x, (int, str)) and str(x).isdigit() else 999
        ),
        "preserve_section_ids": sorted(
            preserve_sids,
            key=lambda x: int(x) if isinstance(x, (int, str)) and str(x).isdigit() else 999
        ),
        "per_section": per_section,
        "deadline_policy_note": (
            "사용자 directive 2026-05-15: B3 document-level merge 없이 section-local "
            "generation-lite. reference_label은 deadline 임시 policy hint (top_level/body/other). "
            "B0b 정식 정책 결정 시 schema 합리화 예정."
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# 13.3b-1: Shallow Section Plan Seed
# ──────────────────────────────────────────────────────────────────────


def _clean_heading_text(raw_text: str, marker: str = "") -> str:
    """heading raw text에서 marker를 strip하고 whitespace를 정규화."""
    text = raw_text.strip()
    if marker and text.startswith(marker):
        text = text[len(marker):].strip()
    return " ".join(text.split())


def extract_shallow_section_plan_seed(
    target_unit_plan: dict,
    structure: dict,
    idx_full_texts: dict,
    marker_policies: dict | None = None,
    template_observation: dict | None = None,
) -> dict | None:
    """
    13.3b-1: template evidence 기반 shallow section plan seed 추출.

    template의 shallow_block region에서 top-level heading 후보를 code-driven facts로
    수집하고, evidence scoring을 거쳐 seed로 선별한다.

    이 결과는 완성형 planner가 아닌 "관측 기반 seed"이며,
    source content는 고려하지 않는다 (template_only).

    Returns:
        seed dict or None (fallback 시)
    """
    # ── 1. shallow_block region 찾기 ──
    regions = target_unit_plan.get("regions", [])
    if not regions:
        regions = target_unit_plan.get("ai_plan", {}).get("regions", [])

    shallow_region = None
    for r in regions:
        if r.get("unit_type") == "shallow_block":
            shallow_region = r
            break

    if not shallow_region:
        return {
            "seed": None,
            "fallback_reason": "no_shallow_block_region",
            "extracted_heading_candidates": [],
            "skipped_heading_candidates": [],
        }

    shallow_indices = set(shallow_region.get("paragraph_indices", []))
    if not shallow_indices:
        return {
            "seed": None,
            "fallback_reason": "empty_shallow_block_paragraph_indices",
            "extracted_heading_candidates": [],
            "skipped_heading_candidates": [],
        }

    # ── 2. template facts 수집 ──
    paragraphs = structure.get("paragraphs", [])
    para_by_idx = {p["idx"]: p for p in paragraphs}

    # role_text_types: text_type (heading/body/supporting/summary)
    role_text_types = structure.get("role_text_types", {})

    # grammar: has_children 판단
    global_grammar = structure.get("template_grammar", {}).get("global", {})

    # subregion_candidates from target_unit_plan (12.2 AI judgment)
    internal = shallow_region.get("internal_structure", {})
    ai_heading_roles = set()
    for cand in internal.get("subregion_candidates", []):
        if cand.get("candidate_type") == "heading":
            ai_heading_roles.add(cand.get("role_id", ""))

    # marker lookup: role → marker string
    marker_lookup = {}
    if marker_policies:
        for role_key, policy in marker_policies.items():
            if isinstance(policy, dict):
                samples = policy.get("markers", policy.get("markers_sample", []))
                if samples:
                    marker_lookup[role_key] = samples[0]
    # fallback: structure.paragraphs의 marker 필드
    for p in paragraphs:
        role = p.get("role", "")
        if role and role not in marker_lookup:
            m = (p.get("marker") or "").strip()
            if m:
                marker_lookup[role] = m

    # table roles: content_table_candidate 관련 (template_observation 또는 table_handling)
    table_roles = set()
    table_handling = shallow_region.get("table_handling", {})
    for tr in table_handling.get("table_role_ids", []):
        table_roles.add(tr)

    # ── 3. shallow_block 내 paragraph level 분포 → top-level 결정 ──
    levels_in_region = []
    for idx in shallow_indices:
        p = para_by_idx.get(idx)
        if p:
            levels_in_region.append(p.get("level", 99))

    if not levels_in_region:
        return {
            "seed": None,
            "fallback_reason": "no_paragraphs_with_level_in_region",
            "extracted_heading_candidates": [],
            "skipped_heading_candidates": [],
        }

    min_level = min(levels_in_region)
    # "top-level에 가까운" = min_level 또는 min_level + 1까지 허용
    top_level_threshold = min_level

    # ── 4. heading 후보 수집 + evidence scoring ──
    extracted_candidates = []
    skipped_candidates = []

    # role별 등장 횟수 (region 내): repeatable pattern 판단용
    role_count_in_region: dict[str, int] = {}
    for idx in sorted(shallow_indices):
        p = para_by_idx.get(idx)
        if p:
            r = p.get("role", "")
            role_count_in_region[r] = role_count_in_region.get(r, 0) + 1

    for idx in sorted(shallow_indices):
        p = para_by_idx.get(idx)
        if not p:
            continue

        role = p.get("role", "")
        level = p.get("level", 99)
        full_text = idx_full_texts.get(str(idx), "").strip()

        if not full_text:
            continue

        # ── required filters ──
        skip_reason = None

        # R1: table/content_table_candidate 제외
        if role in table_roles:
            skip_reason = "table_role"
        # R2: level이 top-level이 아님
        elif level > top_level_threshold:
            skip_reason = f"level_{level}_above_threshold_{top_level_threshold}"

        if skip_reason:
            skipped_candidates.append({
                "idx": idx, "role": role, "level": level,
                "text_preview": full_text[:60],
                "skip_reason": skip_reason, "filter_stage": "required",
            })
            continue

        # ── evidence scoring ──
        evidence = []
        evidence_score = 0

        # E1: subregion_candidates에서 heading으로 식별됨 (12.2 AI)
        if role in ai_heading_roles:
            evidence.append("subregion_candidate_heading")
            evidence_score += 2

        # E2: role_text_types에서 heading
        rtt = role_text_types.get(role, {})
        if rtt.get("text_type") == "heading":
            evidence.append("role_text_type_heading")
            evidence_score += 2

        # E3: grammar has_children
        g = global_grammar.get(role, {})
        if g.get("allowed_children"):
            evidence.append("grammar_has_children")
            evidence_score += 1

        # E4: 같은 role이 region 내에서 반복 등장 (section boundary 패턴)
        count = role_count_in_region.get(role, 0)
        if count >= 2:
            evidence.append(f"repeatable_in_region_count_{count}")
            evidence_score += 1

        # E5: marker가 section-level 성격 (고정 마커, 단일 문자)
        marker = marker_lookup.get(role, "")
        if marker and len(marker) <= 2:
            evidence.append(f"marker_present_{marker}")
            # marker 자체는 weak evidence — 본문 role도 marker 있음
            evidence_score += 0  # 점수 부여 안 함, 기록만

        # E6: body-like paragraph 감지 (negative evidence)
        if rtt.get("text_type") in ("body", "supporting"):
            evidence.append("text_type_body_or_supporting")
            evidence_score -= 2

        # ── heading candidate 기록 ──
        cleaned = _clean_heading_text(full_text, marker)
        candidate = {
            "idx": idx,
            "role": role,
            "level": level,
            "text_raw": full_text,
            "text_clean": cleaned,
            "description": p.get("description", ""),
            "evidence": evidence,
            "evidence_score": evidence_score,
            "marker": marker,
        }
        extracted_candidates.append(candidate)

    # ── 5. seed 선별: evidence_score >= 2인 후보만 ──
    seed_headings = []
    for cand in extracted_candidates:
        if cand["evidence_score"] >= 2:
            seed_headings.append({
                "idx": cand["idx"],
                "role": cand["role"],
                "text_raw": cand["text_raw"],
                "text_clean": cand["text_clean"],
                "evidence": cand["evidence"],
                "evidence_score": cand["evidence_score"],
            })
        else:
            skipped_candidates.append({
                "idx": cand["idx"],
                "role": cand["role"],
                "level": cand["level"],
                "text_preview": cand["text_raw"][:60],
                "skip_reason": f"evidence_score_{cand['evidence_score']}_below_threshold_2",
                "filter_stage": "evidence",
                "evidence": cand["evidence"],
            })

    # ── 6. fallback: seed가 비거나 1개면 seed 없이 기존 동작 ──
    if len(seed_headings) < 2:
        fallback_reason = (
            "no_heading_candidates_passed_evidence"
            if not seed_headings
            else "only_one_heading_insufficient_for_section_flow"
        )
        return {
            "seed": None,
            "fallback_reason": fallback_reason,
            "extracted_heading_candidates": extracted_candidates,
            "skipped_heading_candidates": skipped_candidates,
        }

    # ── 7. seed 구성 ──
    # heading_role: seed에 포함된 heading들의 대표 role (가장 많은 role)
    role_counts: dict[str, int] = {}
    for h in seed_headings:
        role_counts[h["role"]] = role_counts.get(h["role"], 0) + 1
    primary_heading_role = max(role_counts, key=role_counts.get)

    return {
        "seed": {
            "heading_count": len(seed_headings),
            "primary_heading_role": primary_heading_role,
            "headings": seed_headings,
        },
        "seed_completeness": "template_only",
        "limitations": [
            "source_content_not_considered",
            "heading_adaptation_delegated_to_2b",
        ],
        "fallback_reason": None,
        "extracted_heading_candidates": extracted_candidates,
        "skipped_heading_candidates": skipped_candidates,
    }


# ──────────────────────────────────────────────────────────────────────
# 2b AI: 패턴 + 소스 → 섹션별 텍스트 채우기
# ──────────────────────────────────────────────────────────────────────

SECTION_FILL_PROMPT = """당신은 한국 행정문서 작성 전문가입니다.
하나의 대제목 섹션에 대해, 주어진 **role 패턴**에 따라 소스 내용을 배치합니다.

# ⚠️ 절대 규칙 — 소스 원문 그대로 사용 (변환 X)

당신은 1차 스켈레톤 작성자. **소스의 문장 / 단어 / 표현 / 글자를 가능한 그대로 가져다 쓰세요**. 말투 정제 / 형식 모방은 다음 단계 (2b-b) 가 처리합니다.

## 절대 X — 인공지능 자기 멋대로 변환 금지

- **한자 변환 절대 금지** — 소스가 한글이면 그대로 한글. 한국어 한자어 (`점검 결과`, `행정`, `정보시스템`, `업무` 등) 를 **한자 (`点检结果`, `行政`, `信息系统`, `業務`) 로 변환 절대 X**. 현대 한국 행정문서는 한글 위주.
- **영어 변환 절대 금지** — 소스가 한글이면 그대로. `클라우드` 를 `cloud` 로 변환 X.
- **새 한자 / 새 영어 단어 생성 절대 금지** — 소스에 정확히 등장한 한자 / 영어만 그대로 쓰고, 그 외 모든 한자 / 영어 단어 만들기 X. 인공지능 중국어 / 일본어 학습 데이터의 한자 (`点检`, `信息`, `行政`, `公司`, `通信` 등) 출력 절대 X.

## 양식 sample 의 단어 가져오기 금지

- 양식 sample 의 한자 / 영어 / 행정 약어 등을 새 본문에 가져오기 X.
- 양식 sample 은 트리 구조 / role 의미 이해용 참고만. **단어 / 말투 / 형식 모방은 2b-b 책임**.
- 양식 sample 이 `(Back to the Basic)` 같은 영어 부제면 → 당신은 그것 안 따라가도 됨. 단어는 소스 원문에서만.

## 자유도 한계

- 소스의 사실 / 숫자 / 주체 / 시기 → 정확히 그대로 (`6 층`, `'25. 6. 5.`, `4 억 4,894 만원` 등 변경 X)
- 짧게 만들기 위해 단어 줄이지 마세요. 정보 손실 X.
- 말투 / 술어 / 분할 형식 변환 X — 2b-b 가 처리.
- 의역 / 단어 교체 X.

## 정보 압축 금지 (강제)

각 item text 는 source 의 관련 내용을 **과도하게 압축하지 않습니다**.

- source 에 대상 / 시기 / 규모 / 수단 / 결과 함께 있으면 → 가능한 한 같은 item 또는 적절한 자식 item 에 **모두 보존**.
- `계약 체결`, `사업 착공`, `추진` 처럼 **결과어만 남기고 대상 / 시기 / 규모 / 수단 누락 X**.
- 잘못된 예: `(부지계약) 사업 착공` (대상/시기 누락)
- 올바른 예: `(부지계약) 새만금산단 (2 공구) 용지 계약 체결` + 자식에 `(시기) '26 년 상반기` (정보 분산 보존)
- 단, 양식 말투나 종결어미로 다듬는 건 2b-b 책임 — 정보 손실만 안 되게.

## 출력 직전 자체 점검 (반드시)

JSON 출력 직전, 모든 item 의 text 를 다시 훑어서:

1. **한자가 있는가?** 있다면 → source 원문에 그 한자가 정확히 있었는지 확인. 없으면 한글 source 표현으로 되돌리기.
2. **영어 단어가 있는가?** 있다면 → source 원문에 그 영어가 정확히 있었는지 확인. 없으면 한글 표현으로 되돌리기.
3. **새 한자 / 새 영어가 단 한 글자라도 남으면 안 됨** — 양식 sample 의 한자/영어 가져온 것도 X. source 원문에 정확히 일치하는 글자만 허용.

이 자체 점검을 거치지 않은 출력은 wrong. 출력 전에 반드시 점검하세요.

## 핵심 규칙 (강제)

1. **패턴에 명시된 role만 사용하세요** — 새 role 생성 금지
2. **개수 제약 (강제) — target_count 우선**:
   - `정확히 1개/부모`: 부모 인스턴스 아래 딱 1개만 생성. 2개 이상 절대 금지.
   - `여러 개 가능`: **양식 관찰의 mean (반올림 정수) 을 target_count 로 기본 생성**. min 만 만들지 마세요.
     - 양식이 같은 parent 아래 같은 role 을 평균 N 개 관찰했으면 → 새 출력에서도 **N 개의 독립 instance 분리 생성**.
     - **min 까지 줄이는 건 source 에 명확히 N 개 분리할 내용이 없을 때만**. 단순히 source 가 짧다는 이유로 N 개 → 1 개 합치기 절대 X.
     - **max 초과 절대 금지**.
     - 단일 관찰 (min=max=N) 이면 그 값 N 그대로.

   **합치기 금지 (강제 룰)**:
   - 양식에서 같은 parent 아래 같은 child role 이 N 개 관찰됐다면, source 가 한 문단이어도 **쉼표 / 세미콜론 / 번호 / 날짜 / 기관 / 조치 단위로 분리해 N 개 instance 에 배치**.
   - 같은 role 이 반복 등장하는 게 정상. 합치면 wrong.
   - 잘못된 예: 하나의 detail_item 안에 세 가지 조치 다 포함 ← 절대 X
   - 올바른 예: detail_item 1, 2, 3 으로 분리
3. **필수/선택**:
   - `필수(최소 1개)`: 반드시 1개 이상 포함
   - `선택(생략 가능)`: 해당 내용이 소스에 없으면 생략
4. **children 관계를 지키세요** — 부모 role 뒤에 자식 role이 와야 합니다
5. **형제 자식 variant (hard constraint — 한 instance는 한 variant)**:
   - 각 parent role의 한 인스턴스가 가질 수 있는 자식 set은 prompt의 "형제 배타 규칙" 섹션의
     "자식 variant 목록" 에서 명시됩니다 (양식 관찰).
   - **한 인스턴스 안에는 단 하나의 variant 자식 set만 사용**. 두 variant 섞기 금지.
   - 새 인스턴스 만들 때마다 variant 중 하나 선택 (source 내용 성격에 맞게).
   - 예: parent의 variant 목록이 [{A, B}, {C}]면 → 한 인스턴스는 (A+B) 또는 (C) 둘 중 하나만.
     A+C 또는 B+C 같이는 금지.

## ⚠️ 소스와 양식의 주제가 완전히 다를 수 있음

양식은 **어떤 주제** (예: 과일 가격) 를 다뤘더라도, 당신이 채울 소스는 **전혀 다른 주제** (예: 야구장 관객 수) 일 수 있습니다.

- **role 의 description 은 구조적·관계적 역할만** 기술. 주제 무관.
- **role 의 sample text 는 트리 구조 / role 의미 이해용 참고만**. **단어 / 문체 / 형식 모방 X — 다음 단계 (2b-b) 가 처리**.
- sample 이 어떤 주제든 → 당신은 **소스의 단어 / 글자** 그대로 사용.
- sample 의 양식 단어 (영어 / 한자 / 행정 약어 등) 가져오기 절대 X.

## source 내용 재구성 (양식 구조 우선)

source는 양식과 다른 도메인일 가능성이 큽니다. source의 자연 단위 갯수와
양식의 instance 갯수가 일치하지 않을 수 있어요.

**원칙: 양식 instance 갯수가 우선. source 자연 갯수에 매몰되지 마세요.**

- source가 7개 항목, 양식 instance가 3개 → source를 양식 3개 instance에
  나눠 담도록 묶어 재구성
- source가 2개 항목, 양식 instance가 4개 → source 내용을 양식 4개 자리에
  맞춰 분할 (단 양식 max 초과 X)
- source 자연 갯수가 양식 갯수와 달라도 양식 갯수에 맞춰 재배치

**판단 순서**:
1. 양식 role의 instance 갯수 결정 (min~max 사이에서)
2. source의 관련 내용을 그 갯수만큼 묶거나 나눠서 각 instance에 배치
3. **source 갯수에 맞춰 양식 갯수(min~max)를 어기는 것 금지**

## ⚠️ chapter title 답습 금지 — 트리 단계 의미 분리

당신이 작성하는 모든 item의 텍스트는 **자기 부모 item의 텍스트와 의미적으로
구별되는 더 구체적인 sub-내용**이어야 합니다. 부모 텍스트를 그대로 복제하거나
거의 똑같이 paraphrase 하면 안 됩니다.

특히 **root sub-item**(트리의 최상위 child, `parent_id=null`인 item)은 주어진
chapter title을 그대로 복제하지 마세요. chapter title은 양식 전체 대제목으로
별도 위치에 이미 박히고, 당신은 그 아래 트리 자식들만 채웁니다.

판단 기준 — 양식 role 카탈로그의 sample text 는 **트리 구조 / role 의미 이해용** 참고:
- sample 이 chapter title 자체가 아니라 chapter 안의 **별도 측면** (구체적 성과, 세부 전략, sub-과제, intro 요약 등) 을 보여주면 — 그 **위치 / 깊이** 를 트리 구조로 반영.
- chapter title 보다 한 단계 좁고 구체적인 sub-주제로 작성.
- **양식 sample 의 단어 / 문체 / 길이 모방 X — 다음 단계 (2b-b) 가 처리**. 당신은 소스 내용 정확히 추출만.
- **marker 는 본문에 넣지 마세요** — 다음 단계 (형식 입히기) 가 자동 부착.

이 규칙은 트리 모든 단계에 동일: 부모→자식으로 내려갈수록 더 구체적 정보로
좁혀져야 하며, 같은 정보가 부모와 자식에 중복되면 안 됩니다.

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

## 텍스트 작성 규칙 (2b-a 책임 범위)

- **role 의 description 이나 번호 ("과제 1", "전략 2" 등) 를 텍스트에 넣지 마세요**
- **소스의 실제 내용만 작성** — 사실 / 숫자 / 주체 / 시기 정확히
- **양식 sample 단어 / 말투 / 형식 모방하지 마세요** — 그건 다음 단계 (2b-b) 가 처리합니다.
- 단어는 소스에서. 형식 정제는 신경 X. 정보를 정확히 트리에 배치하는 데 집중.

## ⚠️ 2b-a 의 책임은 정보 + 구조

이 단계는 **1차 본문 (스켈레톤)** 작성. 양식 sample 은 트리 구조 / role 의미 이해용 참고만, **단어 / 술어 / 분할 모방 X**.

- 양식 sample 이 `[전략1] ... (Back to the Basic)` 형태라도 → 새 본문은 소스 단어로 자유롭게. 양식 영어 / 한자 가져오기 금지.
- 양식 sample 이 명사구로 끝나든 서술문이든 → 신경 쓰지 마세요. 정보 정확히 적기만.
- 양식 sample 의 분할 패턴 (괄호 부제 / 키워드 위치) → 모방 X. 2b-b 가 처리.

다음 단계 (2b-b) 가 1차 본문을 받아 양식 sample 말투/형식으로 정제합니다. 2b-a 는 raw 정보만.

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "items": [
    {"id": 0, "parent_id": null, "role": "<최상위 role>", "text": "<텍스트>"},
    {"id": 1, "parent_id": 0,    "role": "<하위 role>",   "text": "<텍스트>"},
    {"id": 2, "parent_id": 1,    "role": "<더 하위 role>", "text": "<텍스트>"}
  ]
}
```

- **id**: 0부터 시작하는 순서 번호. 빠짐없이 순차 증가 (0, 1, 2, …)
- **parent_id**: 이 항목의 부모 항목 id
  - **`parent_id: null`로 둘 수 있는 role은 패턴 트리의 최상위 role(들) 뿐** (hard constraint).
  - 패턴 트리에서 자식 role로 분류된 role은 **절대 `parent_id: null`로 두지 마세요**.
    그 role의 양식 부모 role 인스턴스 id를 parent_id로 명시해야 합니다.
  - root role이 여러 개 있을 수 있음 — 각각 `parent_id: null` (단 위 hard 룰 충족 시)
  - root가 아닌 항목은 반드시 부모 item의 id를 parent_id로 지정
- **패턴 트리의 계층 관계를 parent_id로 정확히 표현하세요**
  - root role의 자식은 parent_id = 해당 root의 id
  - 같은 부모 아래 형제 항목은 parent_id가 같음
  - **자식 role을 root로 박지 마세요** — 양식 트리 구조 위반. 양식 cluster_X가 cluster_Y의
    자식이면 출력에서도 cluster_X.parent_id는 cluster_Y 인스턴스 id (절대 null X).
  - 양식 parent의 인스턴스가 N개 필요하면 (양식 instance 갯수 hint 참조) **N개를 모두
    생성**하세요. 그 자식들을 root에 박아 합치지 마세요. 한 instance에 다 못 박으면
    parent instance 더 만들어서 분리.
- role과 text는 양식의 role 카탈로그·format_rules에 따라 결정. role 이름은 양식 카탈로그에 있는 그대로 사용.

## 중요
- **소스에 없는 내용을 만들어내지 마세요**
- **하나의 role 항목에는 하나의 계층 내용만** — 여러 계층을 합치지 마세요
- 반드시 JSON만 출력. 다른 설명 포함 금지
"""


def _format_pattern_tree(
    pattern: dict,
    role_markers: dict,
    indent: int = 0,
    role_text_types: dict | None = None,
    per_type_semantics: dict | None = None,
    chapter_type_name: str = "",
    multi_variant_parents: set | None = None,
) -> str:
    """패턴 트리를 사람이 읽기 좋은 텍스트로 변환.

    per_type semantics가 있으면 해당 type context의 description과 text_type 사용.
    없으면 role_text_types(global) fallback.

    multi_variant_parents: 양식 instance 단위 child variant 가진 parent role set.
    이 role의 자식은 union 표현이라는 경고 표시 (변경/추가 가능).
    """
    multi_variant_parents = multi_variant_parents or set()
    lines = []
    prefix = "  " * indent
    for role_name, info in pattern.items():
        # marker 정보 제거 — 코드가 자동 부착하므로 AI prompt에 노출 X
        marker_str = ""
        per_parent = info.get("per_parent", "single")
        optional = info.get("optional", False)
        suggested = info.get("suggested_count", 1)
        observed = info.get("observed_counts", [])
        children = info.get("children", {})
        flags = []
        # 개수 제약
        if per_parent == "single":
            flags.append("정확히 1개/부모")
        else:
            flags.append("여러 개 가능")
        if optional:
            flags.append("선택(생략 가능)")
        else:
            flags.append("필수(최소 1개)")
        if observed:
            _min = min(observed)
            _max = max(observed)
            _mean = sum(observed) / len(observed)
            _target = round(_mean) if _mean > 0 else _min
            observed_preview = observed[:6]
            more = "…" if len(observed) > len(observed_preview) else ""
            flags.append(
                f"양식 관찰 갯수 = {observed_preview}{more}, "
                f"target_count={_target} (이만큼 분리 생성 권장, 합치기 금지), "
                f"min={_min}, max={_max}"
            )
        # per_type semantics 우선, global fallback
        pts = (per_type_semantics or {}).get(role_name, {})
        type_sem = pts.get("per_type", {}).get(chapter_type_name, {})
        if type_sem:
            text_type = type_sem.get("inferred_text_type", "body")
            desc = type_sem.get("representative_description", "")
            if desc:
                flags.append(f"역할: {desc[:50]}")
            flags.append(f"text_type={text_type}")
        else:
            tt = (role_text_types or {}).get(role_name, {})
            text_type = tt.get("text_type", "heading" if children else "body")
            length_hint = tt.get("length_hint", "짧은 한 줄" if children else "한 문장")
            flags.append(f"text_type={text_type}, {length_hint}")
        # multi-variant parent role 경고
        variant_warn = ""
        if role_name in multi_variant_parents:
            variant_warn = "  ⚠️ 이 role은 양식 instance마다 다른 child variant를 가짐 — children은 union 표현. 실제 출력 시 \"형제 자식 variant\" 섹션 참조 (한 instance에 variant 중 하나만)."
        flags_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"{prefix}- {role_name}{marker_str}{flags_str}{variant_warn}")
        if children:
            lines.append(_format_pattern_tree(
                children, role_markers, indent + 1,
                role_text_types, per_type_semantics, chapter_type_name,
                multi_variant_parents=multi_variant_parents,
            ))
    return "\n".join(lines)


def extract_chapter_template_tree(
    paragraphs: list[dict],
    chapter_id: int,
    include_markers: bool = False,
) -> str:
    """양식 paragraphs에서 특정 chapter의 instance 트리를 문자열로 추출.

    텍스트 내용 제외 — cluster role + parent 관계만 표시. 2b prompt에 넣어
    LLM이 양식의 실제 instance 분포(부모마다 자식 갯수 다른 패턴)를 모방
    할 수 있게 함.

    Args:
        paragraphs: structure["paragraphs"]
        chapter_id: 추출할 chapter id (paragraph.chapter_id 기준)
        include_markers: True면 marker도 같이 표시 (예: " □")

    Returns:
        들여쓰기된 트리 문자열 (chapter 안 paragraph 없으면 빈 문자열)
    """
    in_chapter = [p for p in paragraphs if p.get("chapter_id") == chapter_id]
    if not in_chapter:
        return ""
    # idx → paragraph
    by_idx = {p.get("idx"): p for p in in_chapter}
    # depth 계산 (chapter 안 paragraph 기준 최소 level을 root level로)
    levels = [p.get("level", 0) for p in in_chapter]
    base_level = min(levels) if levels else 0
    lines = []
    for p in in_chapter:
        depth = p.get("level", 0) - base_level
        indent = "  " * max(depth, 0)
        role = p.get("role", "?")
        marker = (" " + p.get("marker", "").strip()) if include_markers and p.get("marker") else ""
        lines.append(f"{indent}- {role}{marker}")
    return "\n".join(lines)


def build_section_fill_prompt(
    chapter_title: str,
    chapter_type_name: str,
    pattern: dict,
    role_catalog: dict,
    content_text: str = "",
    content_images: list[str] = None,
    pdf_text: str = "",
    exclusive_rules: list = None,
    format_rules: dict = None,
    role_text_types: dict | None = None,
    per_type_role_semantics: dict | None = None,
    content_only_mode: bool = False,
    shallow_mode: bool = False,
    section_plan_seed: dict | None = None,
    template_chapter_context: dict | None = None,
    cooccurrence_rules: list = None,
    style_profiles: dict | None = None,
    emphasis_layers: dict | None = None,
    paragraph_emphasis_map: dict | None = None,
    marker_policy_1f: dict | None = None,
    template_chapter_tree: str = "",
    broad_source: str = "",
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
        exclusive_rules: 1.5b의 형제 배타 규칙 (선택)
        format_rules: 1.5c의 role별 포맷 규칙 (선택)
        role_text_types: classify_role_text_types() 결과 (text_type, length_hint)
        per_type_role_semantics: build_per_type_role_semantics() 결과 (per-type description)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # role 마커 매핑
    role_markers = {}
    for role_name, info in role_catalog.items():
        role_markers[role_name] = info.get("marker", "")

    # multi-variant parent role set — cooccurrence_rules 기준 (variant 2개 이상)
    _multi_variant_parents: set = set()
    if cooccurrence_rules:
        for _r in cooccurrence_rules:
            _p = _r.get("parent")
            _vs = _r.get("variants") or []
            if _p and len(_vs) >= 2:
                _multi_variant_parents.add(_p)

    # 양식 sample text 머리 마커 제거 helper — 1f marker_policy로 role별 markers 조회.
    # AI한테 양식 sample 박을 때 머리 마커는 보내지 말 것 (책임 분리: code가 자동 부착).
    # AI가 sample 보고 마커 따라하는 동작(예: 강조 안쪽에 마커 박기) 방지 목적.
    _role_markers_map: dict = {}
    if marker_policy_1f and isinstance(marker_policy_1f, dict):
        for _entry in (marker_policy_1f.get("roles") or []):
            _rname = _entry.get("role", "")
            if not _rname:
                continue
            _ms = []
            for _ev in (_entry.get("evidence") or []):
                _m = (_ev.get("detected_marker") or "").strip()
                if _m and _m not in _ms:
                    _ms.append(_m)
            if _ms:
                # 긴 마커 먼저 (예: "**" 우선 후 "*")
                _role_markers_map[_rname] = sorted(set(_ms), key=len, reverse=True)

    def _strip_leading_marker(text: str, role_name: str) -> str:
        if not text or role_name not in _role_markers_map:
            return text
        _stripped = text.lstrip()
        _lead_ws = text[:len(text) - len(_stripped)]
        for _m in _role_markers_map[role_name]:
            if _stripped.startswith(_m):
                # 마커와 그 뒤 모든 공백·탭 함께 제거. 1개만 떼면 AI가 sample
                # 따라 본문 머리에 공백 박는 경우 코드가 부착한 "* " 뒤에 공백 중복.
                _rest = _stripped[len(_m):].lstrip(" \t")
                return _lead_ws + _rest
        return text

    # 패턴 트리 텍스트
    pattern_text = _format_pattern_tree(
        pattern, role_markers,
        role_text_types=role_text_types,
        per_type_semantics=per_type_role_semantics,
        chapter_type_name=chapter_type_name,
        multi_variant_parents=_multi_variant_parents,
    )

    # 이번 패턴에 등장하는 role들만 수집 → 관련된 배타 규칙만 추림
    def _collect_roles(pat: dict, acc: set):
        for r, info in pat.items():
            acc.add(r)
            ch = info.get("children", {})
            if ch:
                _collect_roles(ch, acc)

    pattern_roles = set()
    _collect_roles(pattern, pattern_roles)

    # format_rules 섹션 제거 — marker 정보를 AI에 노출하지 않음 (코드가 자동 부착).
    format_text = ""

    # 형제 자식 variant (instance-aware white-list — hard constraint)
    # cooccurrence_rules가 새 variants 형식 (sample 포함) 가져옴. 옛 exclusive_rules는 사용 X.
    exclusive_text = ""
    if cooccurrence_rules:
        relevant = []
        for rule in cooccurrence_rules:
            parent = rule.get("parent", "")
            if parent not in pattern_roles:
                continue
            all_children = [
                r for r in (rule.get("all_children_clusters") or [])
                if r in pattern_roles
            ]
            if len(all_children) < 2:
                continue
            # variants filter — pattern_roles 안 cluster만
            variants_raw = rule.get("variants") or []
            filtered_variants = []
            for v in variants_raw:
                cs = [r for r in (v.get("child_set") or []) if r in pattern_roles]
                if cs:
                    filtered_variants.append({
                        "variant_id": v.get("variant_id", ""),
                        "child_set": cs,
                        "samples": v.get("samples") or [],
                        "instance_count": v.get("instance_count", 0),
                    })
            relevant.append({
                "parent": parent,
                "all_children": all_children,
                "instance_count": rule.get("instance_count", 0),
                "variants": filtered_variants,
            })
        if relevant:
            lines = ["## ⚠️ 형제 자식 variant (instance-aware — hard constraint)\n"]
            lines.append(
                "**핵심 룰 (hard constraint)**: 각 parent role의 한 인스턴스가 가질 수 있는 자식 set은\n"
                "아래 \"자식 variant\" 중 **단 하나**입니다. 양식 instance마다 sample text가 명시되니,\n"
                "새 출력 instance도 자기 의미 역할에 맞는 variant를 1개 선택해서 그 자식만 박으세요.\n"
            )
            lines.append(
                "- 한 instance에 두 variant 섞기 금지 (양식에서 함께 등장한 적 없음).\n"
                "- 새 instance마다 source 내용 성격에 맞는 variant 선택 (양식 sample 참고).\n"
                "- **instance 갯수는 유동적** — 양식 관찰 갯수에 매이지 X. source 분량에 따라.\n"
                "- ⚠️ 아래 'role 패턴'의 children 목록은 양식의 여러 instance variant의 union 표현입니다.\n"
                "   pattern_tree만 보고 children 다 박지 마세요. 반드시 variant별 자식 set만 박기.\n"
            )
            for rule in relevant:
                parent = rule["parent"]
                # parent marker 정보 제거 — 코드가 자동 부착
                instance_count = rule.get("instance_count", 0)
                lines.append(f"\n### 부모: `{parent}`")
                if instance_count:
                    lines.append(f"- 양식 관찰: {instance_count} instance, {len(rule['variants'])} variant")

                if not rule["variants"]:
                    lines.append("- 자식 variant: 없음")
                else:
                    for v in rule["variants"]:
                        vid = v.get("variant_id", "")
                        cs = v.get("child_set", [])
                        samples = v.get("samples", [])
                        vic = v.get("instance_count", 0)

                        # child marker 정보 제거 — role name만
                        child_strs = [f"`{r}`" for r in cs]

                        lines.append(f"\n  **[variant {vid}]** (양식 관찰: {vic} instance)")
                        # 양식 instance sample — text만 표시 (marker는 제외)
                        # text_preview에는 양식 paragraph 마커가 박혀있어, 1f policy로 떼서 보냄
                        for s in samples[:3]:
                            stx = s.get("text_preview", "")
                            if stx:
                                stx = _strip_leading_marker(stx, parent)
                                lines.append(f"    - 양식 instance text: \"{stx[:80]}\"")
                        # variant 자식 set
                        lines.append(f"    - 이 variant의 자식 set: {{ " + ", ".join(child_strs) + " }}")
                        lines.append(
                            f"    → 이 양식 sample과 같은 의미/위치의 새 instance는 위 자식 set만 박기."
                        )
            exclusive_text = "\n".join(lines) + "\n\n"

    # role 카탈로그 텍스트 — marker 정보 제거 (코드가 자동 부착)
    # sample 텍스트 머리에 박힌 양식 마커(예: "* (운영규모)..." 의 "* ")도 1f policy로 떼서 보냄.
    # 안 떼면 AI가 sample 따라 마커를 본문에 박는 동작 발생 가능.
    catalog_lines = []
    for role_name, info in role_catalog.items():
        desc = info.get("description", "")
        sample = info.get("sample", "")
        if sample:
            sample = _strip_leading_marker(sample, role_name)
        count = info.get("count", 0)  # 양식 전체 등장 횟수 (instance 갯수 hint)
        count_str = f', 양식 instance: {count}개' if count else ""
        sample_str = f'\n  예시: "{sample}"' if sample else ""
        catalog_lines.append(f"- **{role_name}**{count_str}: {desc}{sample_str}")
    catalog_text = (
        "\n".join(catalog_lines)
        + "\n\n(`양식 instance: N개`는 양식에서 이 role이 N번 등장했다는 뜻. "
        "출력 트리에도 가능하면 N개 인스턴스를 만드세요. 한 인스턴스에 다 못 박으면 "
        "더 만들어서 분리. 인스턴스 부족 시 자식 role을 root로 박는 거 금지.)"
    )

    # 2b-a 는 말투 책임 X — style_profiles 받지만 prompt 박지 않음.
    # 양식 sample 의 말투/술어/분할 모방은 2b-b (build_section_polish_prompt) 의 책임.
    style_text = ""

    # 2c 분리 (2026-05-22): 강조 markup 생성은 2c 책임 — 2b prompt에 강조 가이드 박지 않음.
    # emphasis_layers / paragraph_emphasis_map 인자는 호환성 위해 받지만 prompt에 박지 않음.

    # 양식 실제 instance 트리 (chapter 안 paragraph 순서 + parent 관계, 텍스트 X)
    template_tree_text = ""
    if template_chapter_tree and template_chapter_tree.strip():
        template_tree_text = (
            "## 양식 실제 instance 트리 (이 chapter — 패턴 참고)\n"
            "아래는 양식의 실제 paragraph 분포입니다. **패턴과 구조 참고용**:\n\n"
            "```\n"
            f"{template_chapter_tree}\n"
            "```\n\n"
            "**해석 규칙**:\n"
            "1. **cluster 종류**: 위 트리에 등장한 cluster 만 사용. 새 cluster 추가 금지.\n"
            "2. **부모-자식 관계**: 위 트리에서 cluster A 가 cluster B 의 자식이면 새 본문도 동일 관계로 parent_id 부여. 트리에서 자식인 cluster 를 형제로 평탄 배치 X.\n"
            "3. **반복 가능성**: 위 트리에서 cluster X 가 여러 번 등장하면 새 본문에도 **여러 번 등장 가능** (source 자료 분량에 따라). 1번만 등장하면 새 본문도 보통 1번.\n"
            "4. **instance 수는 source 가 결정**: 양식이 □ 3번이라고 무조건 새 본문 □ 3번 X. source 에 □ 에 해당하는 독립 자료가 3개 있으면 3번, 1개 있으면 1번, 5개 있으면 5번 (양식 cluster 가 반복 가능한 경우).\n"
            "5. **양식 흐름 존중**: 양식의 자식 그룹 순서 (□ → ㅇ → □ → ➊ → □ 등) 가 자연스러우면 따르되, source 자료 순서가 더 합리적이면 source 따라가도 OK.\n"
            "6. **너무 짧게 끝내지 X**: source 에 활용 가능한 자료가 양식 분포 수준으로 있다면 양식 분포에 맞춰 본문 생성. source 충분한데 cluster 별 1번씩만 만들고 끝내지 X.\n\n"
        )

    user_parts = []
    text_block = (
        f"## 대제목\n"
        f"**{chapter_title}**\n\n"
        f"## 이 섹션의 role 패턴\n"
        f"아래 패턴에 따라 내용을 배치하세요:\n{pattern_text}\n\n"
        f"{template_tree_text}"
        f"{format_text}"
        f"{exclusive_text}"
        f"## 사용 가능한 role 상세\n"
        f"{catalog_text}\n\n"
        f"{style_text}"
        f"## 소스 자료\n"
        f"아래 소스에서 **\"{chapter_title}\"** 섹션 작성에 활용 가능한 모든 정보를 찾아 배치하세요.\n\n"
        f"**중요**: source 내용의 주장 방향이나 주제가 chapter_title 과 직접 일치할 필요는 없습니다. "
        f"이 섹션을 쓰는 데 재료로 사용할 수 있으면 포함하세요. "
        f"직접 근거, 간접 근거, 배경, 정의, 수치, 사례, 비교, 반론, 한계, 맥락 정보 모두 활용 대상입니다. "
        f"같은 결론을 말하는 부분만 고르지 마세요. 애매하면 포함.\n\n"
    )

    has_pdf_text = bool(pdf_text and pdf_text.strip())
    has_images = bool(content_images)
    has_content = bool(content_text and content_text.strip())

    if has_pdf_text:
        text_block += "### 집중 자료 (2b-source 가 이 chapter 에 매핑한 영역)\n"
        text_block += f"```\n{pdf_text}\n```\n\n"
        # broad source 가 따로 제공되고 집중 자료와 다르면 안전망으로 같이 노출.
        # 집중 자료가 좁아 양식 분포 채울 재료 부족할 때 회수 가능.
        if broad_source and broad_source.strip() and broad_source.strip() != pdf_text.strip():
            text_block += "### 전체 source (안전망 — 집중 자료에 없는 재료도 회수 가능)\n"
            text_block += (
                "집중 자료가 좁아 양식 분포를 채울 재료가 부족하면 아래 전체 source 에서 "
                "활용 가능한 정보 (배경 / 수치 / 사례 / 비교 / 반론 / 맥락) 를 추가 회수하세요.\n"
            )
            text_block += f"```\n{broad_source}\n```\n\n"
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

    system_prompt = SECTION_FILL_PROMPT
    if content_only_mode:
        # Phase 2: 마커 규칙 교체 — AI에게 content만 출력하도록 지시
        _old_marker_block = """## 마커 — 코드가 자동 처리

**marker (➊, ➋, ◈, 과제 N, [전략N], □ 등)는 본문에 넣지 마세요.**
조립 단계에서 role에 맞는 marker를 sibling_index 기반으로 자동 부착합니다.
AI는 marker 없이 **본문 내용만** 출력하세요.

## 들여쓰기 — 신경 쓰지 마세요

출력 text에 **앞 공백/탭 넣지 마세요**. 조립 단계에서 자동 부착됩니다.

text 구성: **본문 내용만** (marker, 공백, 들여쓰기 모두 코드가 자동 처리)

## 텍스트 작성 규칙
- **role의 description이나 번호("과제 1", "전략 2" 등)를 텍스트에 넣지 마세요**
- **marker(➊, ◈, □, ※, ⇒, *, - 등)도 텍스트에 넣지 마세요** — 코드가 자동 부착
- 소스의 실제 내용만 작성하세요"""

        _new_marker_block = """## 마커 규칙

**마커는 자동으로 부착됩니다. text에 마커를 넣지 마세요.**

- text에는 순수 본문 내용만 작성하세요.
- 마커(□, ○, Ⅰ., 1., 가., ➊ 등)를 text 앞에 붙이지 마세요.
- 들여쓰기(공백/탭)도 넣지 마세요.
- 소스의 원래 마커(※, □, ⇒, - 등)도 제거하세요.

text 구성: 본문 내용만
- 올바른 예: "과제 추진 현황"
- 잘못된 예: "□ 과제 추진 현황", "Ⅰ. 추진 현황", "  과제"

각 role의 markers_sample은 해당 role의 성격을 이해하기 위한 참고 정보입니다.
마커 자체는 후처리에서 자동 부착됩니다.

## 텍스트 작성 규칙
- **role의 description이나 번호("과제 1", "전략 2" 등)를 텍스트에 넣지 마세요**
- 소스의 실제 내용만 작성하세요
- 소스의 원래 마커는 모두 제거하세요 — 양식 마커도, 소스 마커도 넣지 마세요"""

        if _old_marker_block in system_prompt:
            system_prompt = system_prompt.replace(_old_marker_block, _new_marker_block)

    if shallow_mode:
        system_prompt += """

## Shallow mode (이 섹션은 깊은 chapter가 아닌 얕은 보고서 양식입니다)

1. **간결하게**: 양식 원본의 문장 밀도와 길이에 맞게 간결하게 작성하세요. 소스 내용을 길게 풀어 쓰지 마세요.
2. **구조 반복 금지**: 소스에 큰 주제가 여러 개 있어도 위 패턴 구조 전체를 주제별로 반복하지 마세요. 여러 주제를 하나의 보고서 양식 안에 요약·통합하세요.
3. **표는 구조 자리 표시만**: 표가 포함된 role은 자리만 유지하세요. 표 cell 값을 새로 생성하거나 수치를 나열하지 마세요. 표 구조는 후처리에서 보존됩니다 (table cell filling은 deferred). 주제별로 표를 반복 복제하지 마세요.
4. **하나의 shallow report body**: 이 영역 전체가 하나의 짧은 보고서 본문입니다. 여러 개의 독립 보고서처럼 작성하지 마세요."""

        # 13.3b-1: section plan seed가 있으면 구체적 heading list로 대체
        _seed_data = (section_plan_seed or {}).get("seed")
        if _seed_data and _seed_data.get("headings"):
            _headings = _seed_data["headings"]
            _heading_list = "\n".join(
                f"  {i+1}. {h['text_clean']}"
                for i, h in enumerate(_headings)
            )
            system_prompt += f"""

## Template Section Flow (이 양식에서 관측된 보고서 흐름)

이 양식의 주요 섹션 구조:
{_heading_list}

**규칙:**
- 위 섹션 순서와 흐름을 따르세요. 소스의 주제 수만큼 위 구조를 반복하거나 재편하지 마세요.
- 소스의 여러 주제를 위 보고서 흐름 안에 요약·분배하세요.
- 각 섹션 제목의 구조적 의도(개요, 현황, 추진, 계획 등)는 유지하되, 날짜·기간·고유 주제명처럼 양식 원본에 특화된 표현은 소스 내용에 맞게 가볍게 조정할 수 있습니다.
- 소스에 해당 내용이 없는 섹션은 간략 요약으로 채우거나 최소한으로 생성하세요. 섹션을 통째로 생략하지 마세요.
- 출력은 반드시 위에 주어진 role 패턴과 grammar 안에 머물러야 합니다."""
        else:
            # seed 없음: 기존 추상 지시
            system_prompt += """
5. **소제목 의도 유지**: 양식의 소제목에는 문서 구조 역할(개요, 추진상황, 향후계획 등)과 원본 주제 표현이 섞여 있을 수 있습니다. 구조 역할은 유지하고, 원본 주제에 특화된 표현은 새 소스 주제에 맞게 바꾸세요. 소스의 주제 수만큼 섹션 구조를 재편하지 말고, 양식의 보고서 흐름 안에 소스 내용을 요약·통합하세요."""

    # 13.4b: template chapter context — template-driven loop에서 전달
    if template_chapter_context:
        _tcc = template_chapter_context
        _tcc_position = _tcc.get("position", 0) + 1
        _tcc_total = _tcc.get("total_chapters", 1)
        _tcc_title = _tcc.get("template_title", "")
        _tcc_desc = _tcc.get("description", "")
        _tcc_para = _tcc.get("paragraph_count", 0)

        _tcc_block = f"""

## Template Chapter Context (이 장의 양식 원본 위치)

이 장은 양식에서 **{_tcc_position}/{_tcc_total}번째** 장입니다.

- 양식 원본 제목: "{_tcc_title}"
- 양식 설명: {_tcc_desc}
- 원본 분량: 약 {_tcc_para}개 문단

**규칙:**
- 위 양식 제목의 **구조적 의도**(목적, 배경, 현황, 추진, 계획 등)를 보존하세요.
- 제목 텍스트는 새 소스 주제에 맞게 자연스럽게 **adaptation 가능**합니다 (연도, 기관명, 정책명 등 교체 허용).
- 하지만 양식 전체의 **장 순서와 흐름**을 변경하거나 재구성하지 마세요.
- 소스에서 이 장 작성에 **활용 가능한 모든 정보**를 찾아 배치하세요. source 의 주장 방향이 이 장의 결론과 다르더라도, 배경 / 수치 / 사례 / 비교 / 반론 / 한계 / 맥락 정보로 사용할 수 있으면 포함하세요. **정말로 활용 가능한 정보가 전혀 없을 때만** 빈 JSON array `[]` 반환 — 억지로 내용을 만들지 마세요.
- 같은 source 사실이 여러 장에서 서로 다른 역할 (한 장은 근거로, 다른 장은 배경/대조로) 로 필요하면 **중복 사용 OK**. 단 같은 문장을 같은 의미로 두 장에 그대로 박지 말고, 각 장의 구조적 역할에 맞게 가공하세요."""

        system_prompt += _tcc_block

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]


def parse_section_fill_from_llm(llm_response: str) -> list[dict]:
    """
    2b LLM 응답에서 섹션 콘텐츠 items를 파싱합니다.

    AI raw output을 그대로 보존합니다. id/parent_id가 있으면 유지,
    없으면 없는 채로 반환합니다 (정규화는 normalize_section_items 책임).

    Returns:
        [{"role": ..., "text": ..., "id"?: ..., "parent_id"?: ...}, ...]
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

    has_ai_ids = any("id" in it for it in items)
    has_ai_parent_ids = any("parent_id" in it for it in items)
    log.info(
        f"2b 파싱: {len(items)}개 항목, "
        f"has_ai_ids={has_ai_ids}, has_ai_parent_ids={has_ai_parent_ids}"
    )
    return items


# ═══════════════════════════════════════════════════════════════
# 2c: SECTION_STYLE — 본문 트리에 마커 + 강조 markup 입히기
# ═══════════════════════════════════════════════════════════════
# 책임 분리:
# - 2b: 트리 구조 + 본문 의미만 (마커/강조 무관)
# - 2c: 작성된 본문에 양식 형식(마커 + 강조 글꼴 분리 markup) 입힘
# - assemble: 2c output 그대로 본보기에 박음 (마커 부착 코드 없음)
#


SECTION_POLISH_PROMPT = """당신은 한국 행정문서 본문 다듬기 전문가입니다.

1차로 작성된 본문 트리 (스켈레톤) 를 받아, **양식 sample 의 말투 / 분할 / 술어 종결 / 본문 풍부함** 으로 정제합니다. **기존 item 의 문체·술어·분할 정제** 가 주된 책임. instance 추가는 예외적 복구 모드에서만.

# ⚠️ 절대 규칙 1 — 한자 / 일본어 / 영어 자체 점검 (출력 직전 4 단계 강제)

2b-b 의 **가장 중요한 책임은 한자 검증·치환**입니다. 이전 단계 (2b-a) 가 한자 / 일본어 / 영어를 자체 생성한 경우 **2b-b 가 반드시 한글로 되돌립니다**.

JSON 출력 직전, 모든 item 의 text 를 4 단계로 검증·치환합니다:

## Step 1 — 한자 / 일본어 / 영어 단어 수집

각 item 의 text 안에서 다음 패턴을 모두 찾아 list 로 만들기:
- **한자** (CJK Unified Ideographs U+4E00~U+9FFF): `業務`, `行政`, `情報`, `点检`, `信息`, `公司`, `通信`, `服務`, `提供`, `等`, `付き` (일본어 히라가나·가타카나도 포함) 등.
- **영어 단어**: `cloud`, `service`, `system` 등.

## Step 2 — source 원문 그대로 매칭 검증

수집한 각 단어를 **source 원문에서 같은 글자 그대로** 검색:
- **정확 매칭만** (부분 매칭·유사 매칭 X).
- 양식 sample 에 있어도 source 원문에 없으면 → **있는 것 아님**. 양식 단어 가져오기 절대 X.
- source 원문에 등장하면 → 보존 가능 (예: source 가 `(美)`, `(韓)`, `bottleneck` 직접 사용하면 그대로).

## Step 3 — source 원문에 없으면 한글로 치환

source 에 없는 한자 / 일본어 / 영어 단어는 **모두 한글로 치환**합니다. 대응 한글이 없으면 source 원문에 등장하는 다른 한글 표현으로 paraphrase. 그래도 안 되면 그 단어 자체를 제거하고 자연스럽게 잇기.

**자주 잘못 생성되는 한자 → 한글 치환 예** (참고용, source 검증 후 적용):
- `業務` → `업무`, `行政` → `행정`, `情報` → `정보`, `信息` → `정보`, `公司` → `회사`, `通信` → `통신`
- `服務` → `서비스`, `提供` → `제공`, `等` → `등`, `点检` → `점검`, `諮詢` → `자문`
- 일본어 `付き` → `붙은` 또는 제거, `(美)` 가 source 에 없으면 `(미국)` 로

**중요**: 한자가 한국어 한자어로 통용되더라도 (`業務` 같은 게 흔히 쓰여도) **source 에 한자 형태로 등장하지 않으면 무조건 한글**. 현대 한국 행정문서는 한글 위주.

## Step 4 — 출력 직전 최종 스캔

JSON 출력 만든 직후, items 의 모든 text 를 다시 훑어서 한자 (U+4E00~U+9FFF) / 일본어 가나가 한 글자라도 남아있는지 확인:
- 남아있고 source 원문에 그 글자가 정확히 등장 → 보존 OK.
- 남아있는데 source 원문에 없음 → **출력 무효**. 다시 Step 3 으로 돌아가 치환.

이 자체 점검 거치지 않은 출력은 wrong. 한자 한 글자라도 source 에 없는데 남아있으면 batch 전체 실패로 처리됩니다.

# 핵심 책임

## 1. 말투 / 술어 / 분할 정제 (가장 중요)

각 노드의 1차 text 를 양식 sample 에 맞게 정제:

- **양식 sample 의 술어 종결** (`~함`, `~한다`, `~완료`, `~예정`, 명사형 등) 그대로 모방.
- **양식 sample 의 segment 분할 패턴** (`(분류) 본문`, `메인 (괄호 부제)` 등) 그대로 모방.
- **양식 sample 이 단일 layer 면 단일 layer**, 다중 segment 면 다중 segment.

## 2. 정보 밀도 / 구성 단위 모방 (글자 수 기계적 모방 X)

같은 role 의 양식 sample 을 볼 때 **종결 + 분할 + 정보량** 3가지 모두 모방:

1. **종결 방식**: `~함`, `~한다`, `~완료`, `~추진`, 명사형 종결 등.
2. **분할 방식**: `(분류) 본문`, `메인 (괄호 부제)` 등.
3. **정보 구성 단위**: sample 이 `사실 + 수단 + 부연 + 결과` 구조면 source 범위 안에서 같은 수준으로 보강.

- 양식 sample 의 글자 수를 **기계적으로 맞추지 마세요**. 길이는 정보량 맞추기 위한 참고 기준.
- 1차 text 가 `(분류) + 짧은 명사구` 수준이고 양식 sample 이 더 긴 본문 구조 → source 또는 1차 자식 item 참고해 정보 보강.
- **종결만 맞추고 본문 지나치게 짧아지는 것은 실패** (예: `(부지계약) 사업 착공` — 대상/시기 정보 누락).
- 보강 시 들어가는 사실 / 시기 / 대상 / 결과는 **반드시 source 또는 1차 트리에 존재** 해야. source 에 없는 내용으로 길이 늘리기 X.

## 3. 중복 방지 — 자식 정보 합치기 조건부

부모 text 보강 시 자식 item 정보 참고할 수 있지만 **무조건 합치기 X**:

- **양식 sample 에서 시기 / 부연이 부모 본문 안에 통합되는 패턴** → 부모에 통합 가능.
- **양식 sample 에서 시기 / 부연이 별도 자식 (note / detail) 로 분리되는 패턴** → 자식에 유지. 부모에 중복 X.
- 기본값: 부모에는 핵심 추진 내용, 자식에는 시기 / 금액 / 근거 같은 보충 정보.
- 양식 sample 의 분할 위치 확인 후 결정.

## 4. 누락 instance 복구 + source 재료 회수 보충

1차 트리가 양식 sample 의 instance 분포에 못 미치면 source 원문에서 재료를 다시 회수해서 instance 를 추가하거나 본문을 보강하세요.

**언제 회수·보강**:
1. 1차 트리가 양식의 target_count 를 미달 (예: 양식 cluster_X 가 3 번 등장하는데 1차에 1 개)
2. 1차 본문이 양식 sample 의 정보 밀도 (사실+수단+부연+결과) 에 못 미침
3. source 안에 1차에 사용 안 된 활용 가능 정보가 있음

**source 재료 회수 원칙** (중요):
- source 내용의 주장 방향이 chapter / item 의 결론과 **같을 필요 없음**.
- 배경 / 정의 / 수치 / 사례 / 비교 / 반론 / 한계 / 맥락 정보도 보강 재료로 사용 가능.
- 새 item 의 role 은 양식 role 카탈로그에 있는 role 만 사용.
- 보강한 사실은 **반드시 source 원문 또는 1차 트리에 존재** — 없는 내용 생성 X.
- 기존 item 의 의미를 바꾸지 말고 정보 밀도만 양식 sample 수준으로 보강.

**금지**:
- 양식 갯수 맞추기 위해 source 에 없는 내용 생성 X.
- 기존 item 내용 쪼개거나 반복해서 가짜 instance 추가 X.
- sample 의 단어 / 한자 / 영어 가져와 instance 생성 X.
- source 근거 불충분하면 추가하지 않고 기존만 정제.

## 5. 자유도 한계 (소스 원문 글자 보존)

- 소스의 사실 / 숫자 / 주체 / 시기 → 정확히 그대로
- 의역 / 단어 교체 X — 말투 / 술어 / 분할만 정제
- 한자 / 영어 변환 X (위 절대 규칙)

# 출력 형식 — id 최종 재번호

```json
{
  "items": [
    {"id": 0, "parent_id": null, "role": "...", "text": "<정제된 본문>"},
    {"id": 1, "parent_id": 0, "role": "...", "text": "..."}
  ]
}
```

**id 재부여 규칙**:
- 출력 직전, 모든 item 을 **트리 순서 (root → 자식 깊이 우선)** 대로 다시 번호.
- id 는 0 부터 1 씩 증가. 빠짐없이.
- parent_id 는 재부여된 새 id 기준으로 정확히 재계산.
- 기존 1차 item 의 의미와 부모-자식 관계는 유지하되, 기존 id 값 자체는 유지 X.
- 추가 item 도 같은 재번호 체계에 포함.
- old_id / source_id / 메모 등 추가 필드 출력 X.

**기타 규칙**:
- role 은 1차 와 동일. 새 instance 도 양식 cluster ID 에서만.
- 마커 / 강조 markup 추가 금지 — 다음 단계 (2c) 가 처리.
- 들여쓰기 공백 / 탭 추가 금지 — 다음 단계가 처리.

반드시 위 JSON 만 출력.
"""


def build_section_polish_prompt(items_1st: list[dict], **fill_kwargs) -> list[dict]:
    """2b-b: 1차 본문 트리를 받아 양식 sample 말투로 정제 + 보충 + variant 누락 추가.

    2b-a (build_section_fill_prompt) 와 동일한 양식 정보 + **말투 가이드 (style_profiles)** 받음.
    2b-b 만 말투 모방 책임 — 2b-a 는 말투 정보 안 받음.

    Args:
        items_1st: 2b-a (1차) 결과 트리 [{id, parent_id, role, text}, ...]
        **fill_kwargs: build_section_fill_prompt 와 동일한 인자. style_profiles 가 들어있으면
            polish user content 에 말투 가이드 박힘.
    """
    import json as _json

    base_messages = build_section_fill_prompt(**fill_kwargs)

    items_json = _json.dumps(
        [{"id": it.get("id"), "parent_id": it.get("parent_id"),
          "role": it.get("role"), "text": it.get("text", "")} for it in items_1st],
        ensure_ascii=False, indent=2,
    )

    # 2b-b 책임: 양식 sample 의 말투/술어/분할 모방.
    # style_profiles (11.2 결과) 가 있으면 role 별 말투 rule 박음.
    style_profiles = fill_kwargs.get("style_profiles") or {}
    pattern = fill_kwargs.get("pattern") or {}
    pattern_roles_local: set = set()

    def _collect(p, acc):
        for r, info in p.items():
            acc.add(r)
            ch = info.get("children") or {}
            if ch:
                _collect(ch, acc)
    _collect(pattern, pattern_roles_local)

    style_section = ""
    if style_profiles:
        _lines = []
        for role in sorted(pattern_roles_local):
            sp = style_profiles.get(role) or {}
            rules = sp.get("content_style_rules_for_generation") or []
            if not rules:
                continue
            _lines.append(f"\n### {role}")
            for r in rules[:3]:
                _lines.append(f"- {r}")
        if _lines:
            style_section = (
                "## role별 말투 rule (양식 sample 분석 — 새 본문에 그대로 적용)\n"
                "각 노드의 1차 text 를 아래 rule + 양식 sample 의 술어 종결/분할 패턴에 맞춰 정제.\n"
                + "\n".join(_lines)
                + "\n\n"
            )

    polish_user_prefix = (
        "## 1차 본문 트리 (스켈레톤)\n"
        "아래 1차 본문을 받아 양식 sample 말투/술어/분할로 정제 + 짧은 본문 보충 + variant 누락 instance 추가.\n"
        f"```json\n{items_json}\n```\n\n"
        + style_section
        + "---\n\n"
        + "## 양식 정보 (2b-a 와 동일 — 참고)\n"
    )

    base_user = base_messages[-1]
    base_user_content = base_user.get("content", "") if isinstance(base_user, dict) else ""
    if isinstance(base_user_content, list):
        for part in base_user_content:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = polish_user_prefix + part.get("text", "")
                break
        polished_user = {"role": "user", "content": base_user_content}
    else:
        polished_user = {"role": "user", "content": polish_user_prefix + base_user_content}

    return [
        {"role": "system", "content": SECTION_POLISH_PROMPT},
        polished_user,
    ]


def parse_section_polish_from_llm(llm_response: str) -> list[dict]:
    """2b-b LLM 응답에서 정제된 items 트리 파싱. 실패 시 빈 list."""
    import re as _re
    text = (llm_response or "").strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except Exception as e:
        log.warning(f"[2b-b POLISH] JSON 파싱 실패: {e}")
        return []
    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    cleaned = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cleaned.append({
            "id": it.get("id"),
            "parent_id": it.get("parent_id"),
            "role": it.get("role", ""),
            "text": str(it.get("text", "") or "").strip(),
        })
    return cleaned
# 2c는 양식 sample(마커 + 강조 markup 포함 원본) + 양식 마커 힌트 +
# 강조 layer rule을 보고 각 item의 text에 마커/강조를 입힘.
# - 양식 마커 단어가 chapter 의미와 부조화면 단어 변경 허용 (예: 전략→보완과제)
# - 시퀀스 번호는 같은 cluster의 N번째 instance에 부여
# - 강조 markup은 짝 강제, 양식 분할 패턴 모방
# - 본문 의미는 그대로 (단어/문장 안 바꿈, 마커+강조만 입힘)


SECTION_STYLE_PROMPT = """당신은 한국 행정문서 형식 전문가입니다.

## 역할
이미 작성된 본문 트리에 양식의 **형식** (outer_marker + style layer markup + 들여쓰기) 을 입힙니다.
본문 의미·문장은 거의 그대로 보존.

## 핵심 개념 분리 (먼저 익히세요)

각 item text 는 다음 segment 로 나누어 처리:

- **outer_marker**: 문단 앞 장식 기호 / 번호. 예: `□`, `➊`, `Ⅰ.`, `1)`, `[전략1]`.
- **content_label**: 본문 초반의 분류 라벨. 예: `(부지계약)`, `(시기)`, `(투자규모)`. **본문 내용** — 삭제 / 교체 X.
- **body**: content_label 뒤의 실제 본문.
- **inner_parenthetical**: body 안에 들어간 보충 괄호. 예: `(2 공구)`, `(순 공사비 6 천억원 등)`.

**outer_marker 와 content_label 을 혼동하지 마세요**. content_label 은 마커가 아닙니다.

## 글꼴 layer 용어 (중요)

`[[emN]]...[[/emN]]` 은 **강조 표시가 아닙니다**. 원본 양식의 **글꼴 layer 재현용 style 표시** 입니다.

- **base layer**: 그 역할(cluster) 의 기본 글꼴. 본문 모든 글자의 기본값.
- **non-base layer**: 양식이 특정 위치 (마커, 분류 라벨 등) 에 다른 글꼴 박은 자리.

## ⚠️ 글꼴 layer 적용 정책 (가장 중요 — 다른 모든 규칙보다 우선)

1. **목표는 non-base layer 를 줄이는 것이 아니라, 양식 sample 의 style 분할 규칙을 재현하는 것이다.**

2. **본문 중간 non-base layer 는 허용한다.** 단, 반드시 양식 sample 또는 role 별 layer guide 에 근거가 있어야 한다.

3. **새 본문에서 "중요해 보이는 표현" 을 자유롭게 고르지 않는다.** 대신 같은 role 의 sample 에서 반복되는 분할 패턴을 따른다.

4. **sample 의 분할 패턴을 우선한다.**
   - sample 에서 본문이 base / non-base / base / non-base 로 나뉘면 새 본문도 비슷한 수와 위치로 나눈다.
   - sample 에서 특정 의미 기능 segment 가 layer 를 받으면 새 본문에서도 같은 기능 segment 에 적용한다.
   - sample 보다 더 많은 non-base span 을 만들지 않는다.

5. **layer guide 의 rules_for_generation 이 있으면 그 규칙을 따른다.**
   예: content_label, 항목번호, 괄호 안 카테고리, 정책수단, 결과 명사구, 인용구, 수치 구간 등.

6. **단순히 "핵심어", "중요 표현" 처럼 너무 넓은 규칙은 sample 의 분할 개수와 위치 패턴 안에서만 적용한다.** 아무 단어나 추가로 고르지 않는다.

7. **불확실하면 해당 segment 만 base 로 둔다. 하지만 문단 전체를 무조건 base 로 뭉개지 않는다.**

## 입력 (user 메시지)
1. **본문 트리**: 각 item에 `id, parent_id, role, text` 있음. text는 마커·layer 없는 본문.
2. **양식 role 카탈로그**: 각 role(cluster) 별 양식 sample (마커 + layer 포함 원본).
3. **양식 마커 힌트**: 각 role 별 markers 리스트, family, separator.
4. **글꼴 layer 가이드**: 각 role 의 base layer + non-base layer + 적용 규칙 (규칙 있는 것만 사용).
5. **chapter 의미**: 대제목 텍스트.

## 작업

### 1. 마커 결정 (item마다)
각 item의 role을 보고 그 role의 양식 sample + 마커 힌트로 판단:

- **양식 마커 그대로 사용 (기본)** — sample 단어가 chapter·본문 의미와 어울리면 그대로.
  예: 양식 `□`, 양식 `Ⅰ.`, 양식 `[전략1]` 그대로.
- **단어 변경은 semantic_template_marker 만 (제한적 허용)**:
  - `[전략1]`, `[과제1]`, `[목표1]` 처럼 **단어 + 번호 결합** 된 marker 에서만 단어 변경 허용.
  - `□`, `ㅇ`, `-`, `※`, `➊`, `Ⅰ.`, `1)` 같은 **fixed / sequence marker 는 단어 변경 X**. 그대로 사용.
  - semantic_template_marker 의 단어 바꿀 때도:
    - **입력 text / chapter_title / role description 에 이미 있는 한글 단어** 만 사용
    - sample 고유 단어 / source 에 없는 한자 / 영어 새로 만들기 X
    - 같은 cluster 의 모든 instance 는 같은 단어로 통일
- **시퀀스 번호 — 반드시 `parent_id` 단위로 카운트 (절대 강제)**:
  - 각 item 의 `parent_id` 를 보고, **그 부모 아래 같은 role 형제** 만 sibling 으로 본다.
  - `parent_id` 가 다르면 **항상 새로 첫 번째 마커부터 시작**. 같은 role 여도, 직전 형제 직후여도, 같은 sub-section 안이어도 — `parent_id` 가 다르면 reset.
  - **흔한 실수 (절대 금지)**:
    - 트리 전체에서 같은 role 카운트 누적 — 부모 다른데 마커 번호 이어감.
    - 같은 sub-section 안에서 다른 부모 아래 같은 role 인데 마커 번호 이어감.
  - **카운트 절차**:
    1. 자기 `parent_id` 확인.
    2. **input 트리 안에서 자기와 같은 `parent_id` + 같은 `role`** 인 item 목록 나열.
    3. 자기가 그 목록에서 몇 번째 (1-based) = sibling_index.
    4. 양식 sample 의 시퀀스 마커 list 에서 `markers[sibling_index - 1]` 사용.
  - **추상적 예시** (실제 마커 글자/role 이름은 양식에 따라 다름):
    - 부모 A 아래 첫 R 형제 → sibling_index=1 → 첫 마커.
    - 부모 B 아래 첫 R 형제 → A 와 부모 다름 → sibling_index=1 → 첫 마커 (이전 부모 카운트 무관).
    - 부모 A 아래 두 번째 R 형제 → sibling_index=2 → 두 번째 마커.
  - 양식 sample 시퀀스가 1~3번만 보였더라도 같은 패턴으로 4·5번 만들어 사용.
- **마커 없음** — 양식 sample에 마커 없는 cluster는 마커 추가하지 마세요.
- **이미 마커가 있으면** — text 앞에 마커 비슷한 게 이미 있으면: 적절하면 그대로 유지, 양식 형식과 다르면 양식 형식으로 교체, 마커가 두 개 보이면 하나만 남김.

### 2. 글꼴 layer 적용 (item 마다)

**위 정책 1~7 을 반드시 따릅니다.**

#### 절차

1. **본문 시작 외부 마커** (있으면) — 본보기의 마커 layer 적용.
2. **본문 시작 직후 분류 라벨** (`(경제상황)`, `(부지계약)` 등 본보기 일관 패턴) — 본보기 분류 layer 적용.
3. **본문 한가운데 segment** — 본보기 sample 에서 같은 종류 단락이 반복적으로 같은 자리 / 같은 의미 기능에 layer 박는 패턴이 보이면 새 본문에도 적용. layer guide 의 rules_for_generation 있으면 그 규칙 따름.
4. 위 패턴이 모호하거나 새 본문 segment 와 안 맞으면 그 segment 만 base. 문단 전체 base 로 뭉개지 X.

#### 본보기 sample 활용

- 본보기 sample 의 **분할 개수·순서·layer 배치를 우선 모방**.
- sample 보다 더 많은 non-base span 을 새로 만들지 X.
- sample 의 의미 기능 (시기 / 금액 / 분류 라벨 / 정책수단 등) 이 layer 받으면 새 본문에서도 같은 기능 자리에 적용.
- 단 새 본문에 그 의미 기능 segment 가 없으면 억지로 만들지 X (base).

#### 임의 추가 금지

- "이 단어가 핵심이니까 강조" 식 자유 선택 X. sample 분할 패턴 안에서만.
- sample 에 1개 자리 강조면 새 본문도 1개. 2개면 2개. 더 많이 만들지 X.

### 3. 허용·금지 patterns

**허용**:
- 외부 마커 (`□`, `Ⅰ.`, `➊` 등) 에 본보기 마커 layer.
- 본문 시작 직후 분류 라벨 (`(경제상황)`, `(부지계약)` 등) 에 본보기 분류 layer.
- **본문 한가운데 segment** — 본보기 sample 의 반복 분할 규칙에 따른 자리 (시기 / 금액 / 정책수단 / 결과 명사구 / 인용구 / 수치 구간 등 layer guide 의 rules_for_generation 명시 자리).
- 그 외 base layer.

**짝 맞춤 강제** — 여는 `[[emN]]` 과 닫는 `[[/emN]]` 은 같은 N. 짝 없는 단독 표시 출력 금지.

**금지 (절대)**:
- 양식 sample / layer guide 에 **근거 없는 자리** 에 non-base layer 박기.
- sample 분할 패턴 (수·위치) 보다 더 많은 non-base span 만들기.
- "이 단어가 핵심이니까 강조" 식 자유 선택. sample 분할 패턴 안에서만.
- 가이드에 규칙 없는 layer 사용 (cluster 정의에 등장만 하고 적용 규칙 없는 layer 는 사용 금지).
- paragraph_count ≤ 1 / char_count 1~2 같은 노이즈 layer 사용.
- 본문 전체를 non-base layer 한 색으로 감싸기 — base 정책 위반.

### 3. 들여쓰기 (마커처럼 양식 sample 그대로)
- 양식 sample의 **앞 공백·탭(들여쓰기)을 그대로 복제**해서 text 머리에 포함.
- 예: 양식 sample이 `"    1) 토론..."`이면 본문도 `"    "` 4공백으로 시작.
- 같은 cluster여도 paragraph마다 들여쓰기 다를 수 있음 — 각 paragraph의 본보기 들여쓰기를 봐.
- 조립 단계는 자식 도구 출력 들여쓰기를 그대로 박음. 코드가 따로 들여쓰기 추가/제거 X.

### 4. 본문 의미 보존
- text 단어·문장은 의미상 거의 그대로. 마커·강조·들여쓰기만 입힘.
- 띄어쓰기·구두점 등 양식 형식상 자연스러운 미세 조정만 허용.
- 새 내용 추가 금지. 단어 의미 변경 금지.

## 출력 형식
입력 트리와 **같은 구조**, **text 필드만** 최종 형식 입힌 텍스트로 교체.

```json
{
  "items": [
    {"id": 0, "parent_id": null, "role": "<root>", "text": "<마커·강조 입힌 최종 텍스트>"},
    {"id": 1, "parent_id": 0, "role": "<자식>", "text": "<...>"}
  ]
}
```

- `id`, `parent_id`, `role`은 **입력과 동일**하게 유지 (변경 금지).
- `text`만 변경.
- 글꼴 layer 표시는 text 안에 inline.
- 트리 항목 추가/삭제 금지.

반드시 위 JSON만 출력. 다른 설명 포함 금지.
"""


def build_section_style_prompt(
    chapter_title: str,
    chapter_type_name: str,
    items_from_2b: list[dict],
    role_catalog: dict,
    marker_policies: dict | None = None,
    style_profiles: dict | None = None,
    emphasis_layers: dict | None = None,
    paragraph_emphasis_map: dict | None = None,
    chapter_position: int | None = None,
    total_chapters: int | None = None,
) -> list[dict]:
    """
    2c 호출: 2b 본문 트리 → 마커 + 글꼴 layer 입힌 트리.

    Args:
        chapter_title: chapter 대제목 (의미 판단용)
        chapter_type_name: chapter 타입 이름
        items_from_2b: 2b parse 결과 트리 [{id, parent_id, role, text}, ...]
            chapter title은 트리 root로 합쳐서 들어감 (id=0, parent_id=null, role=chapter_title cluster)
        role_catalog: role별 양식 sample + description (원본 그대로, 마커 포함)
        marker_policies: 1f marker policies — role별 markers 리스트 + family + separator
        style_profiles: 11.2 말투 rule (참고용)
        emphasis_layers: 11.2b 글꼴 layer rule
        paragraph_emphasis_map: 양식 paragraph annotated_text sample (원본, 마커 포함)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # 이번 트리에 등장하는 role만 추림
    used_roles: set = set()
    for it in items_from_2b:
        r = it.get("role", "")
        if r:
            used_roles.add(r)

    # role 카탈로그 — description 만 노출 (2026-05-25 fix).
    # 1차 분석 캐시의 sample 은 들여쓰기 strip + em 마크업 없음 → 2c 가 들여쓰기·강조 박을 때
    # 노이즈. 양식 본보기는 아래 paragraph_emphasis_map.sample_paragraphs.annotated_text
    # (들여쓰기 + 마커 + em 마크업 다 보존) 만 사용.
    catalog_lines = []
    for role_name in sorted(used_roles):
        info = role_catalog.get(role_name) or {}
        desc = info.get("description", "")
        count = info.get("count", 0)
        count_str = f", 양식 등장: {count}회" if count else ""
        lines = [f"- **{role_name}**{count_str}"]
        if desc:
            lines.append(f"  설명: {desc}")
        catalog_lines.append("\n".join(lines))
    catalog_text = "\n".join(catalog_lines)

    # 마커 힌트 — role별 markers 리스트 + family + separator
    marker_hint_lines = []
    if marker_policies:
        for role_name in sorted(used_roles):
            policy = marker_policies.get(role_name) or {}
            markers = policy.get("markers") or []
            family = policy.get("family", "")
            separator = policy.get("separator", " ")
            policy_type = policy.get("policy_type", "")
            if not markers and not family:
                continue
            sep_display = repr(separator)
            markers_display = ", ".join(f'"{m}"' for m in markers[:5]) or "(없음)"
            marker_hint_lines.append(
                f"- **{role_name}**: 양식 마커 = [{markers_display}], "
                f"family={family or '?'}, separator={sep_display}, type={policy_type or '?'}"
            )
    marker_hint_text = ""
    if marker_hint_lines:
        marker_hint_text = (
            "## 양식 마커 힌트 (role별)\n"
            "양식 sample에서 추출한 마커 정보입니다. sample과 일치하지 않으면 sample을 더 믿으세요.\n"
            + "\n".join(marker_hint_lines)
            + "\n\n"
        )

    # 글꼴 layer 가이드 — 규칙 있는 layer 만 노출. 정의만 있고 규칙 없는 layer 는
    # 가이드에서 빼서 모델이 사용 불가하게 함 (2026-05-25 정책 강화).
    emphasis_text = ""
    if emphasis_layers:
        em_lines = []
        for role_name in sorted(used_roles):
            em = emphasis_layers.get(role_name) or {}
            ems_list = em.get("emphasis_layers") or []
            base_lid = em.get("base_layer_id", "")
            if not base_lid and not ems_list:
                continue
            # 규칙 있는 non-base layer 만 추림
            _layers_with_rules = []
            for layer in ems_list:
                lid = layer.get("layer_id", "")
                rules = [r for r in (layer.get("rules_for_generation") or []) if r]
                if lid and rules:
                    _layers_with_rules.append((lid, rules))
            em_lines.append(f"\n### {role_name}")
            em_lines.append(f"- base layer: `{base_lid}` — 본문 모든 글자의 기본 글꼴 (default).")
            if _layers_with_rules:
                em_lines.append("- 규칙 있는 non-base layer (적용 가능):")
                for lid, rules in _layers_with_rules:
                    em_lines.append(f"    - `[[{lid}]]...[[/{lid}]]`:")
                    for r in rules:
                        em_lines.append(f"        - {r}")
            else:
                em_lines.append("- 규칙 있는 non-base layer 없음 → 본문 전체 base 하나로만 감싸기.")
            # 본보기 sample — 마커 / 분류 라벨 / 본문 내부 style 분할 규칙 확인용. sample 분할 패턴 우선 모방.
            if paragraph_emphasis_map:
                pem = paragraph_emphasis_map.get(role_name) or {}
                samples = pem.get("sample_paragraphs") or []
                if samples:
                    from collections import OrderedDict
                    _by_parent = OrderedDict()
                    for sp in samples:
                        pkey = sp.get("parent_idx")
                        _by_parent.setdefault(pkey, []).append(sp)
                    _picked = []
                    for pkey, pl in list(_by_parent.items())[:3]:
                        _picked.extend(pl[:2])
                    em_lines.append(
                        "- 양식 본보기 sample "
                        "(마커 / 분류 라벨 / 본문 내부 style 분할 규칙 확인용. "
                        "sample 의 분할 개수·순서·layer 배치를 우선 모방하세요. "
                        "단 sample 보다 더 많은 non-base span 을 새로 만들지 마세요):"
                    )
                    for sp in _picked:
                        ann = sp.get("annotated_text") or ""
                        pidx_v = sp.get("parent_idx")
                        if ann:
                            em_lines.append(f"    - parent={pidx_v}: {ann!r}")
                    em_lines.append(
                        "    **마커 시퀀스만 parent 단위 reset 패턴 따라가세요** "
                        "(예: parent=A 아래 󰊱, 󰊲 → parent=B 아래 다시 󰊱, ...). "
                        "본문 한가운데 layer 도 양식 sample 의 반복 분할 규칙이면 적용하세요. "
                        "다만 규칙 없이 새 본문에서 중요 표현을 추가 선정하지 마세요."
                    )
        if em_lines:
            emphasis_text = (
                "## 글꼴 layer 가이드 (role 별)\n"
                "위 정책 1~7 을 반드시 따릅니다. 양식 sample 의 분할 패턴 (수·위치·순서) 을 우선 모방하세요.\n"
                "외부 마커 + 분류 라벨 + 본보기 반복 분할 규칙 자리는 non-base 허용. "
                "단 sample 보다 더 많은 non-base span 을 새로 만들지 X.\n"
                "여는 태그와 닫는 태그는 반드시 같은 N (짝 강제).\n"
                + "\n".join(em_lines)
                + "\n\n"
            )

    # 2c 책임 단순화: 말투 (style_profiles) 는 2b 단독 책임으로 일원화.
    # 2c 는 형식 (마커 + 강조 markup + 들여쓰기) 만 입힘. 말투 가이드 prompt 박지 않음.
    style_text = ""

    # 본문 트리 (2b 결과)
    import json as _json
    items_json = _json.dumps(
        [
            {
                "id": it.get("id"),
                "parent_id": it.get("parent_id"),
                "role": it.get("role"),
                "text": it.get("text", ""),
            }
            for it in items_from_2b
        ],
        ensure_ascii=False,
        indent=2,
    )

    # chapter 위치 힌트 (시퀀스 마커 정확성 위해)
    _pos_hint = ""
    if chapter_position is not None and total_chapters is not None:
        _pos_hint = (
            f"\n**이 chapter는 전체 {total_chapters}개 chapter 중 "
            f"{chapter_position + 1}번째입니다 (0-based index: {chapter_position}).**\n"
            f"chapter 시퀀스 마커(Ⅰ/Ⅱ/Ⅲ, 1./2./3., (1)/(2) 등) 부여 시 이 위치 번호를 사용하세요.\n"
        )

    user_text = (
        f"## chapter 의미\n"
        f"**{chapter_title}**\n"
        f"{_pos_hint}\n"
        f"## 본문 트리 (2b 결과 — 마커·강조 없음)\n"
        f"```json\n{items_json}\n```\n\n"
        f"## 사용 role 양식 sample (원본 — 마커 + 강조 markup 포함)\n"
        f"{catalog_text}\n\n"
        f"{marker_hint_text}"
        f"{emphasis_text}"
        f"{style_text}"
        f"각 item의 text에 마커 + 강조 markup을 입혀 같은 트리 구조로 출력하세요.\n"
        f"본문 의미는 보존하고 형식만 입힘. 반드시 JSON만 출력.\n"
    )

    return [
        {"role": "system", "content": SECTION_STYLE_PROMPT},
        {"role": "user", "content": user_text},
    ]


async def apply_section_style_to_items(
    items_from_2b: list[dict],
    chapter_title: str,
    chapter_type_name: str,
    call_llm_fn,
    role_catalog: dict | None = None,
    marker_policies: dict | None = None,
    style_profiles: dict | None = None,
    emphasis_layers: dict | None = None,
    paragraph_emphasis_map: dict | None = None,
    ch_idx_for_log: int = 0,
    title_role: str | None = None,
    chapter_position: int | None = None,
    total_chapters: int | None = None,
) -> tuple[list[dict], str]:
    """2b items + chapter title → 2c 호출 → 마커/강조 입힌 items + 변경된 chapter title.

    공통 진입점 — 모든 본문 만들기 흐름이 process_section_fill_result를 통과하고,
    그 함수가 이 함수를 부른다. 호출 지점마다 따로 박지 않음.

    실패 시 items_from_2b/chapter_title 그대로 반환 (안전망).
    진단 dump: /tmp/hwpx_debug/2c_apply_log.jsonl
    """
    import os as _2c_os, json as _2c_json
    _2c_dump_path = "/tmp/hwpx_debug/2c_apply_log.jsonl"
    try:
        _2c_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
    except Exception:
        pass

    def _dump(stage, payload):
        try:
            with open(_2c_dump_path, "a", encoding="utf-8") as _f:
                _f.write(_2c_json.dumps(
                    {"ch_idx": ch_idx_for_log, "stage": stage, **payload},
                    ensure_ascii=False,
                ) + "\n")
        except Exception:
            pass

    _dump("entered", {
        "items_2b_count": len(items_from_2b) if items_from_2b else 0,
        "chapter_title": (chapter_title or "")[:60],
        "has_marker_policies": bool(marker_policies),
        "has_emphasis_layers": bool(emphasis_layers),
        "has_paragraph_emphasis_map": bool(paragraph_emphasis_map),
        "role_catalog_size": len(role_catalog) if role_catalog else 0,
        "has_call_llm_fn": call_llm_fn is not None,
    })

    if not items_from_2b or call_llm_fn is None:
        _dump("early_return", {"reason": "no_items" if not items_from_2b else "no_llm_fn"})
        return items_from_2b, chapter_title

    # chapter title을 root item으로 prepend, items 자식들 id +1 shift.
    # role은 진짜 chapter title cluster 이름 사용 — 그래야 catalog/마커/강조 sample이
    # prompt에 박혀서 자식 도구가 마커 결정 가능.
    items_for_2c = [{
        "id": 0,
        "parent_id": None,
        "role": title_role or "_chapter_title",
        "text": chapter_title or "",
    }]
    for it in items_from_2b:
        new_it = dict(it)
        _old_id = new_it.get("id")
        new_it["id"] = (_old_id + 1) if isinstance(_old_id, int) else None
        _old_pid = new_it.get("parent_id")
        if _old_pid is None:
            new_it["parent_id"] = 0
        elif isinstance(_old_pid, int):
            new_it["parent_id"] = _old_pid + 1
        items_for_2c.append(new_it)

    try:
        messages_2c = build_section_style_prompt(
            chapter_title=chapter_title,
            chapter_type_name=chapter_type_name,
            items_from_2b=items_for_2c,
            role_catalog=role_catalog or {},
            marker_policies=marker_policies,
            style_profiles=style_profiles,
            emphasis_layers=emphasis_layers,
            paragraph_emphasis_map=paragraph_emphasis_map,
            chapter_position=chapter_position,
            total_chapters=total_chapters,
        )
        _dump("prompt_built", {
            "messages_count": len(messages_2c),
            "user_content_len": len(messages_2c[-1].get("content", "")) if messages_2c else 0,
            "user_content_full": messages_2c[-1].get("content", "") if messages_2c else "",
            "system_content_full": messages_2c[0].get("content", "") if messages_2c else "",
        })
        llm_content_2c = await call_llm_fn(messages_2c, f"hwpx_section_style_{ch_idx_for_log}")
        _dump("llm_returned", {
            "raw_len": len(llm_content_2c) if llm_content_2c else 0,
            "raw_full": (llm_content_2c or ""),
        })
        items_2c = parse_section_style_from_llm(llm_content_2c)
        _dump("parsed", {
            "items_2c_count": len(items_2c) if items_2c else 0,
            "first_3_texts": [(it.get("text", "") or "")[:80] for it in items_2c[:3]] if items_2c else [],
        })
    except Exception as _2c_e:
        _dump("failed", {
            "error_type": type(_2c_e).__name__,
            "error_msg": str(_2c_e)[:300],
        })
        log.warning(f"[2c ch_idx={ch_idx_for_log}] 호출/파싱 실패 — 2b text 그대로: {_2c_e}")
        return items_from_2b, chapter_title

    # items_2c → 원본 items text 교체
    new_chapter_title = chapter_title
    id_to_text: dict = {}
    for it in items_2c:
        _id = it.get("id")
        _text = it.get("text", "")
        if _id == 0:
            if _text:
                new_chapter_title = _text
        elif isinstance(_id, int) and _id > 0:
            id_to_text[_id - 1] = _text

    updated = []
    for it in items_from_2b:
        new_it = dict(it)
        _old_id = new_it.get("id")
        if isinstance(_old_id, int) and _old_id in id_to_text:
            new_it["text"] = id_to_text[_old_id]
        updated.append(new_it)

    _dump("applied", {
        "updated_count": len(updated),
        "id_to_text_keys": list(id_to_text.keys())[:10],
        "new_chapter_title": (new_chapter_title or "")[:80],
        "sample_updated_texts": [(it.get("text", "") or "")[:80] for it in updated[:3]],
    })
    log.info(f"[2c ch_idx={ch_idx_for_log}] applied: {len(updated)} items, title='{(new_chapter_title or '')[:40]}'")
    return updated, new_chapter_title


def parse_section_style_from_llm(llm_response: str) -> list[dict]:
    """
    2c LLM 응답에서 형식 입힌 트리 items를 파싱합니다.

    Returns:
        [{"id", "parent_id", "role", "text"}, ...]
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
            raise ValueError("2c 응답에서 JSON을 찾을 수 없습니다")

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            data = json.loads(repaired, strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"2c JSON 파싱 실패: {e}")

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", [])
    else:
        raise ValueError(f"2c 결과 형식 오류: {type(data)}")

    log.info(f"2c 파싱: {len(items)}개 항목")
    return items


# ═══════════════════════════════════════════════════════════════
# 8.0a: normalize / validate / build_chapter_trees
# ═══════════════════════════════════════════════════════════════


def normalize_section_items(
    raw_items: list[dict],
    chapter_idx: int,
    chapter_title: str,
    chapter_type: str,
    title_role: str,
) -> dict:
    """
    2b parse 결과를 시스템이 소비 가능한 구조로 정규화합니다.

    Mechanical transform만 수행합니다:
    - Pass 1: AI id → 0-based sequential 재부여 + parent_id remap
    - Pass 2: parent_id 타입 정리, 누락 필드 기본값
    - Pass 3 (8.0b): title node 주입 (id=0), body id +1 shift, parent_id null→0

    판단/교정은 하지 않습니다 (validate_ai_parent_ids 책임).

    Args:
        raw_items: parse_section_fill_from_llm 반환값 (AI raw 보존)
        chapter_idx: 이 chapter의 인덱스
        chapter_title: 2a에서 결정된 대제목
        chapter_type: 선택된 type 이름 (e.g. "type_2")
        title_role: chapter title role 이름

    Returns:
        {
            "items": [...],           # normalized items (title node 포함)
            "raw_items": [...],       # AI original (deepcopy)
            "chapter_context": {...}, # chapter root context
            "normalize_diff": {...},  # AI raw vs normalized 차이
        }
    """
    import copy

    # AI original 보존 (debug용)
    raw_snapshot = copy.deepcopy(raw_items)

    id_reassigned = 0
    parent_id_coerced = 0
    parent_id_missing = 0
    parent_id_type_error = 0
    parent_id_remapped = 0
    parent_id_null_to_title = 0
    has_ai_ids = False
    has_ai_parent_ids = False

    # --- Pass 1: AI id → 0-based sequential 매핑 구축 ---
    old_to_new: dict[int, int] = {}
    for i, item in enumerate(raw_items):
        ai_id = item.get("id")
        if ai_id is not None:
            has_ai_ids = True
            try:
                old_id = int(ai_id)
                if old_id != i:
                    old_to_new[old_id] = i
                    id_reassigned += 1
            except (ValueError, TypeError):
                id_reassigned += 1

    needs_remap = len(old_to_new) > 0

    # --- Pass 2: normalize body items (0-based) ---
    body_normalized = []
    for i, item in enumerate(raw_items):
        out = {"role": item.get("role", ""), "text": item.get("text", "")}
        out["id"] = i  # 0-based (Pass 3에서 +1 shift)

        raw_pid = item.get("parent_id", "_MISSING_")
        if raw_pid == "_MISSING_":
            parent_id_missing += 1
            out["parent_id"] = None
            out["_parent_id_missing"] = True
        else:
            has_ai_parent_ids = True
            pid, coerce_error = _coerce_parent_id(raw_pid)
            if coerce_error:
                parent_id_type_error += 1
                out["parent_id"] = None
                out["_parent_id_type_error"] = True
                out["_parent_id_raw"] = str(raw_pid)[:50]
            else:
                if pid != raw_pid and raw_pid is not None:
                    parent_id_coerced += 1
                if needs_remap and pid is not None:
                    new_pid = old_to_new.get(pid, pid)
                    if new_pid != pid:
                        parent_id_remapped += 1
                    out["parent_id"] = new_pid
                else:
                    out["parent_id"] = pid

        body_normalized.append(out)

    # --- Pass 3 (8.0b): title node 주입 + id shift + null→0 remap ---
    # title node: id=0, parent_id=null, is_chapter_title=true
    title_node = {
        "id": 0,
        "parent_id": None,
        "role": title_role,
        "text": chapter_title,
        "is_chapter_title": True,
    }

    # body items: id +1 shift, parent_id도 +1 (non-null), null→0 (title child)
    for item in body_normalized:
        item["id"] = item["id"] + 1
        pid = item["parent_id"]
        if pid is None:
            # null → 0 (title의 child) — mechanical convention 적용
            # _parent_id_missing이나 _parent_id_type_error인 경우에도 0으로
            item["parent_id"] = 0
            parent_id_null_to_title += 1
        else:
            item["parent_id"] = pid + 1

    normalized = [title_node] + body_normalized

    chapter_context = {
        "chapter_idx": chapter_idx,
        "chapter_title": chapter_title,
        "chapter_type": chapter_type,
        "title_role": title_role,
        "root_mode": "explicit_title_root",
        "title_node_in_tree": True,
    }

    normalize_diff = {
        "id_reassigned_count": id_reassigned,
        "parent_id_coerced_count": parent_id_coerced,
        "parent_id_missing_count": parent_id_missing,
        "parent_id_type_error_count": parent_id_type_error,
        "parent_id_remapped_count": parent_id_remapped,
        "parent_id_null_to_title_count": parent_id_null_to_title,
        "has_ai_ids": has_ai_ids,
        "has_ai_parent_ids": has_ai_parent_ids,
        "item_count": len(normalized),
        "ai_body_item_count": len(body_normalized),
    }

    return {
        "items": normalized,
        "raw_items": raw_snapshot,
        "chapter_context": chapter_context,
        "normalize_diff": normalize_diff,
    }


def _coerce_parent_id(value) -> tuple[int | None, bool]:
    """parent_id 값을 int | None으로 정리합니다.

    Returns:
        (coerced_value, has_type_error)
        - has_type_error=True: int 변환 불가능한 값 (e.g. "abc")
    """
    if value is None:
        return None, False
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("null", "none", ""):
            return None, False
        try:
            return int(s), False
        except ValueError:
            return None, True  # "abc" 같은 값 → 타입 오류
    if isinstance(value, (int, float)):
        v = int(value)
        return (v if v >= 0 else None), False
    return None, True  # 변환 불가능한 타입


def validate_ai_parent_ids(
    items: list[dict],
    type_grammar: dict,
    root_roles: list[str],
    title_role: str = "",
) -> dict:
    """
    AI가 제공한 parent_id를 grammar 기반으로 검증합니다 (8.0b).

    items[0]은 normalize가 주입한 title node (is_chapter_title=True).
    title node는 AI 지표에서 분리합니다.

    Body items에 대해:
    - structural checks: self_parent, out_of_range, cycle
    - grammar checks: parent의 allowed_children에 이 role이 있는지
    - convention checks (8.0b): parent_id=null은 title만 허용,
      root_role은 parent_id=0 (title) 기대

    invalid item은 마킹만 합니다 (교정은 fallback 책임).
    기존 reconstruct_tree_from_flat 결과와 agreement도 비교합니다.

    Args:
        items: normalize_section_items 반환의 "items" (title node 포함)
        type_grammar: {role: {"allowed_children": [...], ...}}
        root_roles: chapter title 직속 자식으로 허용되는 role 목록
        title_role: chapter title role

    Returns:
        {
            "items": [...],              # validated (invalid_reason 마킹됨)
            "parent_id_stats": {...},    # 검증 통계
            "needs_full_fallback": bool, # AI parent가 전혀 없거나 대부분 invalid
        }
    """
    n = len(items)
    # body items = title 제외
    body_items_list = [it for it in items if not it.get("is_chapter_title")]
    n_body = len(body_items_list)

    # --- reconstruct 결과 생성 (agreement 비교용, body-only) ---
    # Phase 1 한정: 매 호출마다 reconstruct도 실행하여 diff.
    # Phase 2에서 fallback 졸업 후 제거 대상.
    flat_for_recon = [{"role": it["role"], "text": it["text"]} for it in body_items_list]
    recon_result = reconstruct_tree_from_flat(
        flat_for_recon, type_grammar, root_roles, title_role
    )
    # reconstruct body-only: id=0~N-1, root parent=None
    # normalized body: id=1~N, root parent=0 (title)
    # compare transform: recon id K → norm id K+1, recon parent None → 0, K → K+1
    recon_parent_map = {}  # norm_id → recon parent (offset-adjusted)
    for node in recon_result.nodes:
        norm_id = node["id"] + 1  # offset
        recon_pid = node.get("parent_id")
        recon_pid_adjusted = 0 if recon_pid is None else recon_pid + 1
        recon_parent_map[norm_id] = recon_pid_adjusted

    # --- parent_id graph 구축 ---
    id_to_role = {it["id"]: it["role"] for it in items}

    stats = {
        "total_nodes": n,
        "injected_title_nodes": 0,
        "ai_body_items": n_body,
        "ai_parent_provided": 0,
        "ai_parent_valid": 0,
        "ai_parent_invalid": 0,
        "title_parent_valid": 0,
        "fallback_used": 0,
        "fallback_reasons": {
            "missing_parent_id": 0,
            "self_parent": 0,
            "out_of_range": 0,
            "cycle": 0,
            "grammar_violation": 0,
            "root_with_parent": 0,
            "non_root_without_parent": 0,
            "parent_id_type_error": 0,
        },
        "recovered_by_fallback": 0,
        "agreement_with_reconstruct": 0,
        "disagreement_with_reconstruct": 0,
        "orphan_count": 0,
        "empty_chapter": n_body == 0,
    }

    for it in items:
        item_id = it["id"]
        role = it["role"]
        pid = it["parent_id"]

        # --- title node: 별도 검증, AI 지표에서 제외 ---
        if it.get("is_chapter_title"):
            stats["injected_title_nodes"] += 1
            if pid is None:
                stats["title_parent_valid"] += 1
            # title node는 AI가 만든 게 아니므로 ai_* 지표 건너뜀
            continue

        is_missing = it.pop("_parent_id_missing", False)
        is_type_error = it.pop("_parent_id_type_error", False)

        invalid_reason = None

        if is_missing:
            stats["fallback_reasons"]["missing_parent_id"] += 1
            invalid_reason = "missing_parent_id"

        elif is_type_error:
            stats["fallback_reasons"]["parent_id_type_error"] += 1
            invalid_reason = "parent_id_type_error"

        elif pid is not None:
            stats["ai_parent_provided"] += 1

            # structural checks
            if pid == item_id:
                invalid_reason = "self_parent"
                stats["fallback_reasons"]["self_parent"] += 1
            elif pid < 0 or pid >= n:
                invalid_reason = "out_of_range"
                stats["fallback_reasons"]["out_of_range"] += 1
            elif pid >= item_id:
                invalid_reason = "out_of_range"
                stats["fallback_reasons"]["out_of_range"] += 1
            else:
                # cycle check
                visited = {item_id}
                cur = pid
                has_cycle = False
                while cur is not None and 0 <= cur < n:
                    if cur in visited:
                        has_cycle = True
                        break
                    visited.add(cur)
                    cur = items[cur]["parent_id"] if cur < len(items) else None
                if has_cycle:
                    invalid_reason = "cycle"
                    stats["fallback_reasons"]["cycle"] += 1

            # grammar check (only if structural OK)
            # 8.0b: parent=0 (title) → root_role 검증
            if invalid_reason is None:
                if pid == 0 and items[0].get("is_chapter_title"):
                    # parent is title → role must be root_role
                    if role not in root_roles:
                        invalid_reason = "non_root_as_title_child"
                        stats["fallback_reasons"].setdefault(
                            "non_root_as_title_child", 0
                        )
                        stats["fallback_reasons"]["non_root_as_title_child"] += 1
                else:
                    parent_role = id_to_role.get(pid, "")
                    parent_grammar = type_grammar.get(parent_role, {})
                    allowed = parent_grammar.get("allowed_children", [])
                    if role not in allowed:
                        invalid_reason = "grammar_violation"
                        stats["fallback_reasons"]["grammar_violation"] += 1

        else:
            # parent_id = null — 8.0b: title만 허용, body item은 안 됨
            # normalize에서 null→0으로 바꿨으므로 여기 오면 normalize 오류
            stats["ai_parent_provided"] += 1
            invalid_reason = "non_title_null_parent"
            stats["fallback_reasons"].setdefault("non_title_null_parent", 0)
            stats["fallback_reasons"]["non_title_null_parent"] += 1

        if invalid_reason:
            it["_invalid_reason"] = invalid_reason
            it["_ai_parent_id"] = pid
            stats["ai_parent_invalid"] += 1
        else:
            stats["ai_parent_valid"] += 1

        # agreement 비교 (body items만, title 제외)
        recon_pid_adjusted = recon_parent_map.get(item_id)
        if pid == recon_pid_adjusted:
            stats["agreement_with_reconstruct"] += 1
        else:
            stats["disagreement_with_reconstruct"] += 1

    # orphan = parent_id=null인 body item (8.0b에서는 발생하면 안 됨)
    stats["orphan_count"] = sum(
        1 for it in items
        if it["parent_id"] is None and not it.get("is_chapter_title")
    )

    # needs_full_fallback: AI parent가 전혀 없거나 body items의 >50% invalid
    ai_provided = stats["ai_parent_provided"]
    ai_invalid = stats["ai_parent_invalid"]
    needs_full = (ai_provided == 0 and n_body > 0) or (
        n_body > 0 and ai_invalid > n_body * 0.5
    )

    stats["fallback_used"] = (
        stats["fallback_reasons"]["missing_parent_id"] + ai_invalid
    )

    return {
        "items": items,
        "parent_id_stats": stats,
        "needs_full_fallback": needs_full,
    }


def apply_parent_id_fallback(
    items: list[dict],
    type_grammar: dict,
    root_roles: list[str],
    title_role: str = "",
    needs_full_fallback: bool = False,
    parent_id_stats: dict | None = None,
) -> list[dict]:
    """
    validate_ai_parent_ids에서 invalid로 마킹된 item에 대해
    기존 reconstruct_tree_from_flat으로 fallback parent_id를 적용합니다.

    Transitional: Phase 2에서 fallback 졸업 후 제거 대상.

    needs_full_fallback=True이면 전체 reconstruct로 교체합니다.
    False이면 invalid item만 개별 복구합니다.

    parent_id_stats가 주어지면 recovered_by_fallback 카운트를 업데이트합니다.

    Args:
        items: validate_ai_parent_ids 반환의 "items"
        type_grammar, root_roles, title_role: grammar 정보
        needs_full_fallback: 전체 reconstruct 필요 여부
        parent_id_stats: validate에서 생성한 stats (in-place 업데이트)

    Returns:
        items (in-place 수정됨, fallback_parent_id 마킹 포함)
    """
    flat_for_recon = [{"role": it["role"], "text": it["text"]} for it in items]
    recon = reconstruct_tree_from_flat(
        flat_for_recon, type_grammar, root_roles, title_role
    )
    recon_map = {n["id"]: n.get("parent_id") for n in recon.nodes}

    recovered_count = 0

    if needs_full_fallback:
        for it in items:
            fallback_pid = recon_map.get(it["id"])
            if it["parent_id"] != fallback_pid:
                it["_ai_parent_id"] = it.get("_ai_parent_id", it["parent_id"])
                it["_fallback_parent_id"] = fallback_pid
                it["_recovered_by_fallback"] = True
                it["parent_id"] = fallback_pid
                recovered_count += 1
    else:
        for it in items:
            if "_invalid_reason" in it:
                fallback_pid = recon_map.get(it["id"])
                it["_fallback_parent_id"] = fallback_pid
                it["_recovered_by_fallback"] = True
                it["parent_id"] = fallback_pid
                recovered_count += 1

    # stats 업데이트
    if parent_id_stats is not None:
        parent_id_stats["recovered_by_fallback"] = recovered_count

    return items


# 13.7a-A1: build_chapter_trees는 호출처 0건 dead code였음 — 삭제됨.
# chapter object는 build_chapter_object()로 생성 (위 13.7a-A1 section).


async def process_section_fill_result(
    llm_response: str,
    ch_idx: int,
    ch_title: str,
    ch_type: str,
    title_role: str,
    template_grammar: dict,
    role_text_types: dict | None = None,
    pattern_roles: list | None = None,
    section_pdf_text_len: int = 0,
    shallow_mode: bool = False,
    override_grammar: dict | None = None,
    override_root_roles: list[str] | None = None,
    call_llm_fn=None,
    role_catalog: dict | None = None,
    paragraphs_info: list | None = None,
    marker_policy_1f: dict | None = None,
    style_profiles: dict | None = None,
    emphasis_layers: dict | None = None,
    paragraph_emphasis_map: dict | None = None,
    chapter_position: int | None = None,
    total_chapters: int | None = None,
) -> dict:
    """
    2b LLM 응답을 처리합니다: parse → normalize → validate → fallback → grammar validation.

    DB tool의 orchestration을 서버 함수로 추출한 것입니다 (8-infra).
    LLM 호출 이후의 모든 처리를 담당합니다.

    Args:
        llm_response: 2b LLM raw response
        ch_idx: chapter index
        ch_title: chapter title (2a에서 결정)
        ch_type: chapter type name
        title_role: chapter title role
        template_grammar: structure["template_grammar"]
        role_text_types: structure["role_text_types"]
        pattern_roles: 이 chapter 패턴에 사용되는 role 목록
        section_pdf_text_len: source text 길이 (debug용)
        override_grammar: per-chapter local_pattern에서 변환한 grammar (13.6-B)
        override_root_roles: per-chapter root roles (13.6-B)

    Returns:
        {
            "body_items": [title_item, ...items],  # assemble용 (role/text only)
            "chapter_tree_nodes": [...] | None,     # chapter_trees용
            "debug_entry": {...},                    # _section_fill_debug용
            "grammar_passed": bool,
            "items_count": int,
        }
    """
    # 1. parse
    # DIAG: process_section_fill_result 진입 시 llm_response 형태 dump — 2c wire 진단용
    try:
        import os as _psr_os, json as _psr_json
        _psr_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
        with open("/tmp/hwpx_debug/2c_psr_trace.jsonl", "a", encoding="utf-8") as _psr_f:
            _psr_resp = llm_response or ""
            _psr_f.write(_psr_json.dumps({
                "ch_idx": ch_idx,
                "ch_title": (ch_title or "")[:60],
                "ch_type": ch_type,
                "raw_len": len(_psr_resp),
                "raw_first_100": _psr_resp[:100],
                "starts_with_items_json": _psr_resp.strip().startswith('{"items"'),
                "has_emphasis_markup": "[[em" in _psr_resp,
                "has_bracket_label": "[전략" in _psr_resp or "[보완과제" in _psr_resp,
                "shallow_mode": shallow_mode,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    raw_items = parse_section_fill_from_llm(llm_response)
    log.info(f"2b[{ch_idx}] 완료: {ch_title} → {len(raw_items)}개 항목")

    # 2c: 공통 진입점 — 모든 본문 만들기 흐름이 이 함수를 통과하므로 여기 한 곳에서 호출.
    # 양식 마커 정보는 조립 함수와 같은 패턴으로 함수 안에서 매번 가공 (raw 1f + paragraphs).
    if call_llm_fn is not None and raw_items:
        try:
            _marker_policies_local = extract_marker_policies(
                paragraphs_info or [],
                marker_policy_1f=marker_policy_1f or {},
            )
            raw_items, ch_title = await apply_section_style_to_items(
                raw_items, ch_title, ch_type,
                call_llm_fn=call_llm_fn,
                role_catalog=role_catalog,
                marker_policies=_marker_policies_local,
                style_profiles=style_profiles,
                emphasis_layers=emphasis_layers,
                paragraph_emphasis_map=paragraph_emphasis_map,
                ch_idx_for_log=ch_idx,
                title_role=title_role,
                chapter_position=chapter_position,
                total_chapters=total_chapters,
            )
        except Exception as _2c_e:
            log.warning(f"[2c ch_idx={ch_idx}] 적용 실패 — 2b text 그대로: {_2c_e}")

    # 2. grammar 정보 추출 — override가 있으면 local grammar 사용
    if override_grammar and override_root_roles:
        _type_grammar = override_grammar
        _root_roles = override_root_roles
        _grammar_source = "local_pattern_override"
    else:
        _type_grammar_info = template_grammar.get("per_type", {}).get(ch_type, {})
        _type_grammar = _type_grammar_info.get("grammar", {})
        _root_roles = _type_grammar_info.get("root_roles", [])
        _grammar_source = "type_grammar_fallback"

    # 3. normalize
    _norm_result = normalize_section_items(
        raw_items, ch_idx, ch_title, ch_type, title_role,
    )
    _norm_items = _norm_result["items"]
    _pid_stats = None

    # 4. validate + fallback
    if _type_grammar:
        _val_result = validate_ai_parent_ids(
            _norm_items, _type_grammar, _root_roles, title_role,
        )
        _norm_items = _val_result["items"]
        _pid_stats = _val_result["parent_id_stats"]

        if _val_result["needs_full_fallback"] or _pid_stats["ai_parent_invalid"] > 0:
            apply_parent_id_fallback(
                _norm_items, _type_grammar, _root_roles, title_role,
                needs_full_fallback=_val_result["needs_full_fallback"],
                parent_id_stats=_pid_stats,
            )

    # 5. grammar validation (body-only — reconstruct는 title 불포함)
    _body_only = [it for it in _norm_items if not it.get("is_chapter_title")]
    _grammar_result = None
    if _type_grammar and _body_only:
        _grammar_result = reconstruct_tree_from_flat(
            [{"role": it["role"], "text": it["text"]} for it in _body_only],
            _type_grammar, _root_roles, title_role,
        )
        validate_reconstruction(_grammar_result, _type_grammar, _root_roles)

        if _grammar_result.success:
            log.info(f"2b[{ch_idx}] grammar validation 통과")
        else:
            log.warning(
                f"2b[{ch_idx}] grammar validation 실패: "
                f"{_grammar_result.failure_type}, "
                f"{len(_grammar_result.violations)}개 violation"
            )
            for _v in _grammar_result.violations[:5]:
                log.warning(
                    f"  [{_v.violation_type}] idx={_v.item_index} "
                    f"{_v.role}: {_v.detail[:60]}"
                )

    # 6. 결과 구성 (8.0b: title node 포함)
    # shallow_mode: title injection skip — title은 slot/preserve로 이미 보존됨
    if shallow_mode:
        chapter_tree_nodes = None
        body_items = [{"role": it["role"], "text": it["text"]} for it in _norm_items
                      if not it.get("is_chapter_title")]

        debug_entry = {
            "idx": ch_idx,
            "chapter_title": ch_title,
            "chapter_type": ch_type,
            "validation_grammar_source": _grammar_source,
            "shallow_mode": True,
            "title_injection_skipped_for_shallow": True,
            "pattern_roles": list(pattern_roles) if pattern_roles else [],
            "section_pdf_text_len": section_pdf_text_len,
            "items_count": len(body_items),
            "items": body_items,
            "grammar_validation": (
                _grammar_result.to_dict() if _grammar_result else None
            ),
            "normalize_diff": _norm_result.get("normalize_diff"),
            "parent_id_stats": _pid_stats,
        }

        return {
            "body_items": body_items,
            "chapter_tree_nodes": None,
            "debug_entry": debug_entry,
            "grammar_passed": (_grammar_result.success if _grammar_result else True),
            "items_count": len(body_items),
        }

    # chapter_tree_nodes: title(id=0) + grammar nodes(id shifted +1)
    _title_tree_node = {
        "id": 0, "parent_id": None, "role": title_role,
        "text": ch_title, "is_chapter_title": True,
    }
    if _grammar_result and _grammar_result.nodes:
        # grammar nodes는 body-only (id=0~N-1) → +1 shift, parent null→0
        _shifted_grammar_nodes = []
        for gn in _grammar_result.nodes:
            shifted = {
                "id": gn["id"] + 1,
                "parent_id": (
                    0 if gn.get("parent_id") is None
                    else gn["parent_id"] + 1
                ),
                "role": gn["role"],
                "text": gn["text"],
            }
            if gn.get("violation"):
                shifted["violation"] = gn["violation"]
            _shifted_grammar_nodes.append(shifted)
        chapter_tree_nodes = [_title_tree_node] + _shifted_grammar_nodes
    else:
        # empty chapter 또는 grammar 없음 → title only
        chapter_tree_nodes = [_title_tree_node]

    # body_items: normalized items에서 role/text 추출 (title 포함, 별도 prepend 없음)
    body_items = [{"role": it["role"], "text": it["text"]} for it in _norm_items]

    # debug용 items (body only, title 제외)
    debug_body_items = [
        {"role": it["role"], "text": it["text"]}
        for it in _norm_items if not it.get("is_chapter_title")
    ]

    debug_entry = {
        "idx": ch_idx,
        "chapter_title": ch_title,
        "chapter_type": ch_type,
        "validation_grammar_source": _grammar_source,
        "override_root_roles": override_root_roles if override_grammar else None,
        "override_grammar_role_count": len(override_grammar) if override_grammar else None,
        "pattern_roles": list(pattern_roles) if pattern_roles else [],
        "section_pdf_text_len": section_pdf_text_len,
        "llm_raw_response": llm_response,
        "items_count": len(debug_body_items),
        "items": debug_body_items,
        "grammar_validation": (
            _grammar_result.to_dict() if _grammar_result else None
        ),
        "text_quality_warnings": validate_text_quality(
            debug_body_items, role_text_types=role_text_types,
        ),
        # 8.0a/8.0b: parent_id 지표
        "raw_items": _norm_result.get("raw_items"),
        "normalize_diff": _norm_result.get("normalize_diff"),
        "chapter_context": _norm_result.get("chapter_context"),
        "parent_id_stats": _pid_stats,
    }

    return {
        "body_items": body_items,
        "chapter_tree_nodes": chapter_tree_nodes,
        "debug_entry": debug_entry,
        "grammar_passed": (
            _grammar_result.success if _grammar_result else True
        ),
        "items_count": len(debug_body_items),
        "chapter_title": ch_title,  # 2c가 마커/강조 입혀 변경했을 수 있음
    }


# ──────────────────────────────────────────────────────────────────────
# 13.3b-1: Shallow Section Plan Seed Compliance Observation
# ──────────────────────────────────────────────────────────────────────


def observe_section_plan_compliance(
    body_items: list[dict],
    section_plan_seed_result: dict,
) -> dict:
    """
    생성 결과가 section plan seed를 얼마나 따랐는지 관측한다 (debug-only).

    Args:
        body_items: process_section_fill_result의 body_items
        section_plan_seed_result: extract_shallow_section_plan_seed의 반환값

    Returns:
        compliance observation dict (hard gate 아님)
    """
    seed = (section_plan_seed_result or {}).get("seed")
    if not seed:
        return {"status": "no_seed", "detail": "section_plan_seed was None or empty"}

    plan_headings = seed.get("headings", [])
    plan_count = len(plan_headings)
    primary_role = seed.get("primary_heading_role", "")

    # 생성된 items에서 heading role 찾기
    generated_heading_items = []
    generated_non_heading_items = []
    for item in body_items:
        if item.get("role") == primary_role:
            generated_heading_items.append(item)
        else:
            generated_non_heading_items.append(item)

    gen_count = len(generated_heading_items)

    # heading text preview
    gen_heading_texts = [
        (it.get("text") or "")[:60] for it in generated_heading_items
    ]
    plan_heading_texts = [h.get("text_clean", "") for h in plan_headings]

    # count match
    count_match = gen_count == plan_count

    # missing / extra estimate
    missing_estimate = max(0, plan_count - gen_count)
    extra_estimate = max(0, gen_count - plan_count)

    # order plausibility: 생성된 heading texts가 plan texts와 대략 같은 순서인지
    # (exact match는 요구하지 않음 — adaptation 허용)
    order_plausible = True  # default optimistic
    if gen_count >= 2 and plan_count >= 2:
        # simple heuristic: 첫 heading과 마지막 heading이 plan 순서와 일치하는지
        # (adaptation으로 text가 바뀌므로 text 비교 불가 — count 순서만 확인)
        order_plausible = gen_count <= plan_count + 2  # 대폭 초과하면 의심

    # thin_section_suspicion: heading 직후 body items가 0~1개인 section
    thin_sections = []
    if generated_heading_items:
        heading_indices_in_body = [
            i for i, it in enumerate(body_items)
            if it.get("role") == primary_role
        ]
        for pos, hi in enumerate(heading_indices_in_body):
            next_hi = (
                heading_indices_in_body[pos + 1]
                if pos + 1 < len(heading_indices_in_body)
                else len(body_items)
            )
            body_between = next_hi - hi - 1
            if body_between <= 1:
                thin_sections.append({
                    "heading_position": hi,
                    "heading_text": (body_items[hi].get("text") or "")[:40],
                    "body_items_after": body_between,
                })

    # source-topic-driven repetition suspicion
    # heuristic: gen_count가 plan_count보다 2배 이상 → source topic별 반복 의심
    repetition_suspicion = gen_count >= plan_count * 2 if plan_count > 0 else False

    return {
        "status": "observed",
        "planned_heading_count": plan_count,
        "generated_heading_count": gen_count,
        "count_match": count_match,
        "missing_planned_headings_estimate": missing_estimate,
        "extra_generated_headings": extra_estimate,
        "order_plausible": order_plausible,
        "thin_section_suspicion": thin_sections if thin_sections else None,
        "source_topic_repetition_suspicion": repetition_suspicion,
        "plan_heading_texts": plan_heading_texts,
        "generated_heading_texts": gen_heading_texts,
        "primary_heading_role": primary_role,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Phase E — TOC-based Chapter Unit Planner (debug-only, debug에 둘 다 dump)
#
# 책임 분리:
#   - has_toc_gate           : 차례/목차 paragraph 존재 약한 detection (role + text)
#   - build_..._prompt       : 양식 self-description(차례) primary + 본문 secondary
#                              + 1c reference-only로 chapter unit 결정 prompt
#   - parse_..._from_llm     : JSON 추출
#   - validate_..._plan      : paragraph_refs 실존 검증 + schema 필드 검증
#                              invalid는 해당 claim ambiguity 강등 (전체 plan 유지)
#   - diagnose_1c_non_body_handling : 1c가 비-본문 paragraph parent/level
#                                     양식 의도와 맞는지 측정 (1c 개선 task용)
# ═══════════════════════════════════════════════════════════════════════════


# 차례/목차/순서 detect 키워드 (약한 detection — false negative 방지)
_TOC_TEXT_HINT_PATTERNS = [
    r"^\s*차\s*례\s*$",
    r"^\s*차\s*례\s+",
    r"^\s*목\s*차\s*$",
    r"^\s*목\s*차\s+",
    r"^\s*순\s*서\s*$",
    r"^\s*순\s*서\s+",
    r"^\s*목\s*록\s*$",
    r"^\s*목\s*록\s+",
    r"^\s*Contents\s*$",
    r"^\s*CONTENTS\s*$",
    r"^\s*Table\s+of\s+Contents",
]
_TOC_ROLE_HINTS = {"table_of_contents", "toc"}


def has_toc_gate(section_results: dict) -> dict:
    """
    Phase E gate: 양식에 차례/목차 paragraph가 존재하는지 약하게 detect.

    role(1d) hit 또는 text pattern hit이 한 건이라도 있으면 has_toc=True.
    false negative 방지가 우선. 정확한 toc paragraph 식별은 toc AI 책임.

    Args:
        section_results: {str(section_id) or int: {"structure": {paragraphs, ...},
                                                    "idx_texts": {...} or "idx_full_texts": ...}}
    Returns:
        {
          "has_toc": bool,
          "toc_paragraph_hints": [
            {"section_id": int, "local_idx": int, "role": str,
             "text_preview": str, "hit_by": "role"|"text"|"both"}
          ],
          "detection_method": "role"|"text"|"both"|"none",
          "scanned_section_count": int,
          "scanned_paragraph_count": int,
        }
    """
    hints: list[dict] = []
    role_hits = 0
    text_hits = 0
    scanned_sections = 0
    scanned_paragraphs = 0

    patterns = [re.compile(p) for p in _TOC_TEXT_HINT_PATTERNS]

    for raw_sid, sresult in section_results.items():
        try:
            section_id = int(raw_sid)
        except (TypeError, ValueError):
            continue
        scanned_sections += 1

        structure = (sresult or {}).get("structure") or {}
        paragraphs = structure.get("paragraphs") or []
        idx_texts = (sresult or {}).get("idx_texts") or {}
        idx_full_texts = (sresult or {}).get("idx_full_texts") or {}

        for p in paragraphs:
            scanned_paragraphs += 1
            local_idx = p.get("idx")
            if local_idx is None:
                continue
            role = (p.get("canonical_role") or p.get("role") or "").strip().lower()
            text = (
                idx_full_texts.get(str(local_idx))
                or idx_full_texts.get(local_idx)
                or idx_texts.get(str(local_idx))
                or idx_texts.get(local_idx)
                or ""
            )

            role_hit = role in _TOC_ROLE_HINTS
            text_hit = any(pat.search(text or "") for pat in patterns)

            if role_hit or text_hit:
                if role_hit:
                    role_hits += 1
                if text_hit:
                    text_hits += 1
                hit_by = "both" if role_hit and text_hit else ("role" if role_hit else "text")
                hints.append({
                    "section_id": section_id,
                    "local_idx": local_idx,
                    "role": role,
                    "text_preview": (text or "")[:120],
                    "hit_by": hit_by,
                })

    if role_hits and text_hits:
        detection = "both"
    elif role_hits:
        detection = "role"
    elif text_hits:
        detection = "text"
    else:
        detection = "none"

    return {
        "has_toc": bool(hints),
        "toc_paragraph_hints": hints,
        "detection_method": detection,
        "scanned_section_count": scanned_sections,
        "scanned_paragraph_count": scanned_paragraphs,
    }


TOC_BASED_CHAPTER_PLAN_PROMPT = """당신은 한국어 HWPX 양식의 chapter 단위(generation unit)를 결정합니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (title / reason / 분석) 은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 TOC 글자 인용은 그대로 — 자체 표현과 인용 구분.

[INPUT]
1. TOC paragraphs — 양식 self-description (가장 신뢰)
2. Body paragraphs (모든 section)
3. 1c tree (level, parent_idx) — reference only, 정답 X

[chapter 정의]
chapter = 양식의 흐름 단위. 다른 주제 source가 적용돼도 같은 chapter 흐름이 유지되는 단위.
chapter title이 양식 specific 단어 (특정 정책명/연도/주제어)를 포함해도 chapter입니다.
다른 주제 source 적용 시 title 변경은 후속 stage(adaptation_plan)가 처리. Phase E는 chapter 단위 결정만.

[TOC tree 해석 — 첫 단계]

먼저 TOC를 tree로 해석하십시오:
- 각 항목의 level/depth와 parent-child 관계 정리
- sibling group 식별 (같은 parent + 같은 depth 항목들)

[chapter level 선택 — sibling group 단위 일관 적용]

핵심 원칙: chapter level은 **TOC tree의 sibling group 단위로 선택**합니다.
개별 항목을 cherry pick으로 chapter / container / subpattern으로 가르지 마십시오.

**같은 sibling group 안에서는 parent level과 child level을 절대 섞지 마십시오.**

선택 logic:

1. TOC 최상위 sibling group (level 0)을 chapter로 선택한 경우:
   - 그 level의 **모든 항목**이 chapter (예외 없음. 분량/topic specific 차이 무관)
   - 그 아래 child level은 모두 subpattern

2. 또는 TOC 최상위 sibling group이 container 역할이고 child level이 작성 흐름이면:
   - 최상위 sibling group의 **모든 항목**이 container (예외 없음)
   - child level의 모든 항목이 chapter

3. TOC level 1만 있으면: level 0이 chapter (자동, child level 없음)

**금지된 분류** (level 섞임):
- Ⅰ/Ⅱ = chapter, Ⅲ = container, Ⅲ 아래 = chapter
- 같은 sibling group 안 일부 chapter + 일부 container
- 의미상 어울려 보이는 child 항목 cherry pick으로 chapter 선택

**선택 기준** (level 선택의 보조 근거. 개별 항목 cherry pick에 사용 금지):

A. 양식 흐름:
   - 다른 주제 source가 적용돼도 같은 chapter 흐름이 유지되는 단위가 어느 level인가?
   - parent level의 항목들이 sub-list 전체를 대표하는 큰 흐름 단위이면 → parent level이 chapter
   - parent level이 단순 구분 라벨이고 child level이 실제 문서 작성 흐름이면 → child level이 chapter

B. 보편적 chapter 의미 (level 단위 비교):
   - "어느 level의 항목들이 보편적 chapter 의미 (목적/추진배경/추진방향/결론/행정사항/평가/계획/관리/여건/방향/일정/현황 등)를 더 잘 표현하는가?"
   - **개별 항목에 보편적 단어가 박혀있다는 이유로 cherry pick X**
   - "관리/대응/구축/확립" 같은 단어가 양식 specific topic 안에 끼어 있으면 그건 양식 specific 표현이며, level 선택 근거 약함

C. 같은 sibling group 일관 처리:
   - 선택한 level의 **모든 항목**이 같은 분류 (chapter / container)로 일관 처리 가능한가?
   - 항목 일부가 자체 본문 짧거나 양식 specific topic 박혀있어도, 같은 sibling이면 같은 분류

[양식별 예시 — 사용자 정책 명시]

조달청 차례: Ⅰ. 추진성과 / Ⅱ. 여건·방향 / Ⅲ. 추진과제 (+ Ⅲ 아래 9개)
→ level 0 (Ⅰ/Ⅱ/Ⅲ)이 chapter. 9개 = subpattern.
→ Ⅲ가 자체 본문 적어도 Ⅰ/Ⅱ와 같은 sibling이므로 모두 chapter.

민원인 차례: 제1장 / 제2장 (+ 각 장 아래 Ⅰ~Ⅷ, Ⅰ~Ⅴ)
→ 제1장/제2장 = container (양식 흐름의 큰 구분 라벨)
→ 각 장 아래 로마자 = chapter (실제 문서 작성 흐름)

[output]

unit_decision.selected_generation_unit_reason 에 다음 명시 필수:
- 선택한 chapter level (0 또는 1)
- 두 level 비교 평가 (어느 level이 양식 흐름/보편적 chapter 의미/일관 처리 측면에서 더 적합한지)
- 같은 sibling group 일관 적용 확인 (모든 항목이 같은 분류로 처리됐는지)

[unit 종류]

- chapter = 양식 흐름 단위 (개수 고정). chapter level의 모든 항목.
- container = 여러 chapter 묶는 상위 그룹 (EXCEPTION case만). 자체 생성 단위 아님.
- subpattern = chapter 아래 가변 sub-content. 양식 specific N개 (다른 주제면 개수/내용 변동).

chapter level 아래 level = subpattern. chapter level 위 level = container (EXCEPTION case만 존재).

[out_of_toc_preserve_regions — 엄격]

다음만 포함:
- 차례 본문에 등장하지 않는 paragraph (표지, header, footer, 차례 외 부록 등)
- 차례 자체 paragraph (table_of_contents role)

차례 본문에 등장한 chapter 단위는 절대 preserve 격하 X.

[evidence cite + ambiguity]

- 모든 claim은 paragraph_refs (section_id, local_idx) cite
- 존재하지 않는 idx 만들지 마십시오. INPUT 안 paragraph 중 하나여야 함.
- evidence 항목마다 confidence (high/medium/low)
- 차례/본문/1c 충돌 시 ambiguity_flags 기록 (예: "1c_disagreement")
- 차례 위계 모호 시 alternative_interpretations + unit_decision.confidence=low

[hardcode 금지]
특정 marker family / 양식명 / 제목 문자열 분기 금지. sub-list 항목 텍스트 의미 분석으로 판단.

[idx_range — list of spans]
[{"section_id", "start_local_idx", "end_local_idx"}, ...]
- chapter/container/subpattern: 자기 시작 paragraph ~ 자기 영역 끝.
- 결정 불가 시 null + ambiguity_flag.

[OUTPUT — JSON ONLY, 다른 텍스트 금지]

{
  "toc_detection": {
    "has_toc": true,
    "toc_paragraphs": [{"section_id": <int>, "local_idx": <int>, "evidence_text": <str>}],
    "confidence": "high"|"medium"|"low"
  },
  "toc_interpretation": {
    "container_units": [
      {
        "title_text": <str>,
        "paragraph_ref": {"section_id": <int>, "local_idx": <int>} | null,
        "marker_family_hint": <str>,
        "child_unit_count": <int>,
        "idx_range": [{"section_id": <int>, "start_local_idx": <int>, "end_local_idx": <int>}] | null
      }
    ],
    "generation_units": [
      {
        "title_text": <str>,
        "paragraph_ref": {"section_id": <int>, "local_idx": <int>} | null,
        "parent_container_index": <int> | null,
        "marker_family_hint": <str>,
        "idx_range": [{"section_id": <int>, "start_local_idx": <int>, "end_local_idx": <int>}] | null
      }
    ],
    "subpattern_units": [
      {
        "title_text": <str>,
        "parent_generation_unit_index": <int>,
        "paragraph_ref": {"section_id": <int>, "local_idx": <int>} | null,
        "idx_range": [{"section_id": <int>, "start_local_idx": <int>, "end_local_idx": <int>}] | null
      }
    ],
    "out_of_toc_preserve_regions": [
      {
        "region_label": <str>,
        "paragraph_refs": [{"section_id": <int>, "local_idx": <int>}],
        "reason_free_text": <str>
      }
    ]
  },
  "matching_failed": [
    {"toc_entry_text": <str>, "reason_free_text": <str>}
  ],
  "unit_decision": {
    "selected_generation_unit_reason": <str>,
    "alternative_interpretations": [
      {"description": <str>, "reason_rejected": <str>}
    ],
    "ambiguity_flags": [<str>],
    "confidence": "high"|"medium"|"low"
  },
  "evidence": [
    {
      "claim": <str>,
      "paragraph_refs": [{"section_id": <int>, "local_idx": <int>}],
      "quoted_text": <str>,
      "confidence": "high"|"medium"|"low"
    }
  ]
}
"""


def build_toc_based_chapter_plan_prompt(
    toc_paragraphs: list[dict],
    body_paragraphs_by_section: dict,
    one_c_tree_by_section: dict,
    max_body_text_preview: int = 200,
) -> list[dict]:
    """
    Phase E: toc-based chapter unit AI planner prompt 생성.

    Args:
        toc_paragraphs: [
            {"section_id": int, "local_idx": int, "role": str, "text": str (full)}
        ]
        body_paragraphs_by_section: {
            section_id (int): [
                {"local_idx": int, "marker": str, "role": str, "text": str}
            ]
        }
        one_c_tree_by_section: {
            section_id (int): [
                {"local_idx": int, "level": int, "parent_idx": int|None}
            ]
        }
        max_body_text_preview: body paragraph text truncate 길이 (token 절약, 양식 의도는 보존)
    """
    # TOC paragraphs: full text 그대로 (양식 self-description, primary evidence)
    toc_entries = []
    for tp in toc_paragraphs:
        toc_entries.append({
            "section_id": tp.get("section_id"),
            "local_idx": tp.get("local_idx"),
            "role": tp.get("role"),
            "text": tp.get("text", ""),  # full text
        })

    # BODY paragraphs: text는 truncate (token 절약).
    body_by_section_serializable = {}
    for sid, plist in (body_paragraphs_by_section or {}).items():
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        out_list = []
        for p in plist:
            text = p.get("text", "") or ""
            if len(text) > max_body_text_preview:
                text_preview = text[:max_body_text_preview] + "…"
            else:
                text_preview = text
            out_list.append({
                "local_idx": p.get("local_idx"),
                "marker": p.get("marker", ""),
                "role": p.get("role", ""),
                "text": text_preview,
            })
        body_by_section_serializable[str(sid_int)] = out_list

    # 1c tree: level + parent_idx만
    tree_by_section_serializable = {}
    for sid, plist in (one_c_tree_by_section or {}).items():
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        out_list = []
        for p in plist:
            out_list.append({
                "local_idx": p.get("local_idx"),
                "level": p.get("level"),
                "parent_idx": p.get("parent_idx"),
            })
        tree_by_section_serializable[str(sid_int)] = out_list

    payload = {
        "toc_paragraphs": toc_entries,
        "body_paragraphs_by_section": body_by_section_serializable,
        "one_c_tree_by_section_reference_only": tree_by_section_serializable,
    }

    user_msg = (
        "## INPUT\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n위 양식의 generation unit 결정을 OUTPUT SCHEMA에 따라 JSON으로만 답하십시오."
    )

    return [
        {"role": "system", "content": TOC_BASED_CHAPTER_PLAN_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_toc_based_chapter_plan_from_llm(llm_response: str) -> dict:
    """Phase E: AI 응답 JSON 파싱. 실패 시 parse_error 필드 포함 dict 반환."""
    text = (llm_response or "").strip()

    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(text))
        except Exception as e:
            log.warning(f"Phase E toc plan JSON 파싱 실패: {e}")
            return {"parse_error": str(e), "raw_preview": (llm_response or "")[:200]}

    if not isinstance(parsed, dict):
        return {"parse_error": "top-level is not dict", "raw_preview": (llm_response or "")[:200]}

    return parsed


def validate_toc_based_chapter_plan(
    plan: dict,
    all_paragraphs_by_section: dict,
) -> dict:
    """
    Phase E: AI output schema + paragraph_refs 실존 검증.

    invalid paragraph_ref는 해당 claim ambiguity로 강등 (전체 plan은 valid 유지).
    schema field 누락은 ambiguity_flags에 'schema_missing_<field>'로 기록.

    Args:
        plan: parse_toc_based_chapter_plan_from_llm 결과
        all_paragraphs_by_section: {section_id (int): set of local_idx (int)}

    Returns:
        plan에 validation_result 필드 추가:
        {
            "valid": bool,                                # schema 큰 결손 없으면 True
            "invalid_paragraph_refs": [...],              # 존재 안 하는 ref
            "schema_missing_fields": [...],
            "downgraded_claim_count": int,                # ambiguity 강등 수
            "fallback_required": bool,                    # True면 _build_chapter_types fallback
        }
    """
    if "parse_error" in plan:
        plan["validation_result"] = {
            "valid": False,
            "invalid_paragraph_refs": [],
            "schema_missing_fields": ["parse_error"],
            "downgraded_claim_count": 0,
            "fallback_required": True,
        }
        return plan

    # paragraph_refs 실존 집합 변환
    valid_refs: dict[int, set] = {}
    for sid, idxs in (all_paragraphs_by_section or {}).items():
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        if isinstance(idxs, (list, tuple)):
            valid_refs[sid_int] = {int(i) for i in idxs if i is not None}
        elif isinstance(idxs, set):
            valid_refs[sid_int] = {int(i) for i in idxs if i is not None}
        else:
            valid_refs[sid_int] = set()

    def _ref_exists(ref) -> bool:
        if not isinstance(ref, dict):
            return False
        sid = ref.get("section_id")
        lid = ref.get("local_idx")
        if sid is None or lid is None:
            return False
        try:
            return int(lid) in valid_refs.get(int(sid), set())
        except (TypeError, ValueError):
            return False

    invalid_refs: list[dict] = []
    downgraded = 0

    # toc_detection.toc_paragraphs
    toc_det = plan.get("toc_detection") or {}
    for tp in toc_det.get("toc_paragraphs", []) or []:
        if not _ref_exists(tp):
            invalid_refs.append({"location": "toc_detection.toc_paragraphs", "ref": tp})

    # toc_interpretation 안 각 unit list
    toc_interp = plan.get("toc_interpretation") or {}

    def _check_unit_refs(unit_list, list_name):
        nonlocal downgraded
        for i, unit in enumerate(unit_list or []):
            pref = unit.get("paragraph_ref")
            if pref is not None and not _ref_exists(pref):
                invalid_refs.append({"location": f"{list_name}[{i}].paragraph_ref", "ref": pref})
                unit["paragraph_ref"] = None
                flags = unit.setdefault("_validation_flags", [])
                flags.append("invalid_paragraph_ref_downgraded")
                downgraded += 1
            # idx_range 안 span의 start/end도 실존 ref 검증
            ir = unit.get("idx_range")
            if isinstance(ir, list):
                for j, span in enumerate(ir):
                    if not isinstance(span, dict):
                        continue
                    sid = span.get("section_id")
                    start = span.get("start_local_idx")
                    end = span.get("end_local_idx")
                    for which, val in (("start_local_idx", start), ("end_local_idx", end)):
                        if sid is None or val is None:
                            continue
                        if not _ref_exists({"section_id": sid, "local_idx": val}):
                            invalid_refs.append({
                                "location": f"{list_name}[{i}].idx_range[{j}].{which}",
                                "ref": {"section_id": sid, "local_idx": val},
                            })

    _check_unit_refs(toc_interp.get("container_units"), "container_units")
    _check_unit_refs(toc_interp.get("generation_units"), "generation_units")
    _check_unit_refs(toc_interp.get("subpattern_units"), "subpattern_units")

    # out_of_toc_preserve_regions
    for i, region in enumerate(toc_interp.get("out_of_toc_preserve_regions") or []):
        for j, pref in enumerate(region.get("paragraph_refs") or []):
            if not _ref_exists(pref):
                invalid_refs.append({
                    "location": f"out_of_toc_preserve_regions[{i}].paragraph_refs[{j}]",
                    "ref": pref,
                })

    # evidence
    for i, ev in enumerate(plan.get("evidence") or []):
        ref_invalid_any = False
        for j, pref in enumerate(ev.get("paragraph_refs") or []):
            if not _ref_exists(pref):
                invalid_refs.append({
                    "location": f"evidence[{i}].paragraph_refs[{j}]",
                    "ref": pref,
                })
                ref_invalid_any = True
        if ref_invalid_any:
            ev["_validation_flags"] = ev.get("_validation_flags", []) + ["invalid_paragraph_ref"]
            if ev.get("confidence") in ("high", "medium"):
                ev["confidence"] = "low"
            downgraded += 1

    # schema 필수 필드
    required_top = ["toc_detection", "toc_interpretation", "unit_decision", "evidence"]
    schema_missing = [f for f in required_top if f not in plan]
    required_interp = [
        "container_units",
        "generation_units",
        "subpattern_units",
        "out_of_toc_preserve_regions",
    ]
    for f in required_interp:
        if f not in toc_interp:
            schema_missing.append(f"toc_interpretation.{f}")

    fallback_required = bool(schema_missing)  # 필수 필드 결손 시 fallback 권고

    plan["validation_result"] = {
        "valid": not schema_missing,
        "invalid_paragraph_refs": invalid_refs,
        "schema_missing_fields": schema_missing,
        "downgraded_claim_count": downgraded,
        "fallback_required": fallback_required,
    }
    return plan


# ═══════════════════════════════════════════════════════════════════════════
# Phase E parallel — 1c diagnostic
#
# 1c가 추정한 parent/level이 양식 의도와 맞는지 측정 (debug-only).
# 1c 개선 task 범위 결정용 evidence.
# ═══════════════════════════════════════════════════════════════════════════

# 비-본문 role hint (1d AI가 부여한 role 기준)
# Track D-2: 컨테이너 역할 / leaf 역할 분리.
# - 컨테이너 비-본문: 그 영역의 root로, 자식 가지는 게 양식 의도 (예: appendix_title이
#   부록 내용의 parent, document_title이 문서 root). 자식 가져도 case A wrong 아님.
# - leaf 비-본문: 단일 정보. 자식 가지면 양식 의도와 어긋남 (예: table_of_contents가
#   본문 chapter의 parent로 잡히면 1c wrong).
_NON_BODY_CONTAINER_ROLES = {
    "document_title",
    "document_subtitle",
    "appendix_title",
    "appendix_subtitle",
    "cover",
}
_NON_BODY_LEAF_ROLES = {
    "table_of_contents",
    "toc",
    "document_date",
    "header_slot",
    "footer_slot",
    "spacer",
    "spacer_text",
    "fixed",
}
_NON_BODY_ROLE_HINTS = _NON_BODY_CONTAINER_ROLES | _NON_BODY_LEAF_ROLES


def diagnose_1c_non_body_handling(section_results: dict) -> dict:
    """
    Phase E 보조 진단: 1c parent/level이 비-본문 paragraph 처리에서 양식 의도와 맞는지 측정.

    1d role을 기준으로 비-본문 paragraph 식별 (1d 자체 정확도는 별 watch).
    1c가 만든 parent 관계가 양식 의도와 어긋날 가능성을 case별로 집계.

    측정 case:
      A. non_body_as_parent_of_body : **LEAF** 비-본문이 본문의 parent — 진짜 1c wrong 후보
                                       (container 비-본문 [appendix_title/document_title 등]이
                                        parent인 경우는 양식 의도 — case A에서 제외)
      B. body_as_parent_of_non_body : 본문이 비-본문의 parent — 부록/header 등 분류 확인 필요
      C. non_body_orphans           : 비-본문 paragraph 중 parent 없음 (level 0) — 정상 가능
      D. section_level_distribution : section별 1c level 분포 (들쭉날쭉 신호)
      E. section_role_disagreement  : 같은 양식 다른 section에서 비-본문 role 처리 일관성

    Args:
        section_results: cache section_results dict

    Returns:
        {
          "per_section": {section_id: {...}},
          "summary": {
            "total_non_body_paragraphs": int,
            "case_A_non_body_as_parent_of_body": int,
            "case_B_body_as_parent_of_non_body": int,
            "case_C_non_body_orphans": int,
            "section_level_distribution": {section_id: {level: count}},
            "section_role_disagreement_signals": [...]
          },
          "samples": {
            "case_A": [{section_id, local_idx, role, parent_idx, parent_role}],
            "case_B": [...]
          }
        }
    """
    per_section: dict[int, dict] = {}
    case_a_total = 0
    case_b_total = 0
    case_c_total = 0
    case_a_samples: list[dict] = []
    case_b_samples: list[dict] = []
    section_level_dist: dict[int, dict] = {}
    section_non_body_roles: dict[int, set] = {}
    total_non_body = 0

    for raw_sid, sresult in (section_results or {}).items():
        try:
            sid = int(raw_sid)
        except (TypeError, ValueError):
            continue

        structure = (sresult or {}).get("structure") or {}
        paragraphs = structure.get("paragraphs") or []

        # idx -> paragraph lookup
        para_by_idx: dict = {}
        for p in paragraphs:
            pidx = p.get("idx")
            if pidx is not None:
                para_by_idx[pidx] = p

        def _is_non_body(p_obj) -> bool:
            role = (p_obj.get("canonical_role") or p_obj.get("role") or "").strip().lower()
            return role in _NON_BODY_ROLE_HINTS

        def _is_non_body_leaf(p_obj) -> bool:
            """LEAF 비-본문 (자식 가지면 wrong 가능): table_of_contents, document_date 등."""
            role = (p_obj.get("canonical_role") or p_obj.get("role") or "").strip().lower()
            return role in _NON_BODY_LEAF_ROLES

        non_body_count = 0
        case_a_count = 0
        case_b_count = 0
        case_c_count = 0
        level_dist: dict = {}
        non_body_roles_in_section: set = set()

        for p in paragraphs:
            level = p.get("level")
            if level is not None:
                level_dist[level] = level_dist.get(level, 0) + 1

            if _is_non_body(p):
                non_body_count += 1
                role = (p.get("canonical_role") or p.get("role") or "").strip().lower()
                non_body_roles_in_section.add(role)

                parent_idx = p.get("parent_idx")
                if parent_idx is None:
                    case_c_count += 1
                else:
                    parent_p = para_by_idx.get(parent_idx)
                    if parent_p is not None and not _is_non_body(parent_p):
                        # 비-본문 paragraph의 parent가 본문 — case B
                        case_b_count += 1
                        if len(case_b_samples) < 30:
                            case_b_samples.append({
                                "section_id": sid,
                                "local_idx": p.get("idx"),
                                "role": role,
                                "parent_idx": parent_idx,
                                "parent_role": (parent_p.get("canonical_role")
                                                or parent_p.get("role") or ""),
                            })
            else:
                # 본문 paragraph — parent가 LEAF 비-본문이면 case A (진짜 1c wrong 후보)
                # Track D-2: container 비-본문 (appendix_title/document_title 등)이 parent면
                # 양식 의도 (부록 root, 문서 root) — case A에서 제외.
                parent_idx = p.get("parent_idx")
                if parent_idx is not None:
                    parent_p = para_by_idx.get(parent_idx)
                    if parent_p is not None and _is_non_body_leaf(parent_p):
                        case_a_count += 1
                        if len(case_a_samples) < 30:
                            case_a_samples.append({
                                "section_id": sid,
                                "local_idx": p.get("idx"),
                                "role": (p.get("canonical_role") or p.get("role") or ""),
                                "parent_idx": parent_idx,
                                "parent_role": (parent_p.get("canonical_role")
                                                or parent_p.get("role") or ""),
                            })

        per_section[sid] = {
            "paragraph_count": len(paragraphs),
            "non_body_count": non_body_count,
            "case_A_non_body_as_parent_of_body": case_a_count,
            "case_B_body_as_parent_of_non_body": case_b_count,
            "case_C_non_body_orphans": case_c_count,
            "level_distribution": level_dist,
            "non_body_roles": sorted(non_body_roles_in_section),
        }
        total_non_body += non_body_count
        case_a_total += case_a_count
        case_b_total += case_b_count
        case_c_total += case_c_count
        section_level_dist[sid] = level_dist
        section_non_body_roles[sid] = non_body_roles_in_section

    # section 간 disagreement: 같은 양식 다른 section에서 다른 role 처리
    disagreement_signals: list[dict] = []
    if len(section_non_body_roles) >= 2:
        # 모든 section에 공통 role이 있나
        section_ids_sorted = sorted(section_non_body_roles.keys())
        all_roles_union = set()
        for s in section_ids_sorted:
            all_roles_union |= section_non_body_roles[s]
        for role in sorted(all_roles_union):
            sections_having = [s for s in section_ids_sorted if role in section_non_body_roles[s]]
            sections_missing = [s for s in section_ids_sorted if role not in section_non_body_roles[s]]
            if sections_having and sections_missing:
                # 일부 section에만 있는 role — 양식 specific일 수도, 1c/1d 들쭉날쭉일 수도
                disagreement_signals.append({
                    "role": role,
                    "sections_having": sections_having,
                    "sections_missing": sections_missing,
                })

    return {
        "per_section": per_section,
        "summary": {
            "total_non_body_paragraphs": total_non_body,
            "case_A_non_body_as_parent_of_body": case_a_total,
            "case_B_body_as_parent_of_non_body": case_b_total,
            "case_C_non_body_orphans": case_c_total,
            "section_level_distribution": section_level_dist,
            "section_role_disagreement_signals": disagreement_signals,
        },
        "samples": {
            "case_A_non_body_parent_of_body": case_a_samples,
            "case_B_body_parent_of_non_body": case_b_samples,
        },
    }


def run_phase_e_chapter_planner(
    section_results: dict,
    call_llm,
    log_obj=None,
    task_name: str = "hwpx_phase_e_toc_plan",
) -> dict:
    """
    Phase E orchestrator (DB tool에서 1줄 호출).

    - has_toc_gate (약한 detection)
    - has_toc=true면 toc AI 호출 + parse + validate
    - has_toc=false면 AI 호출 X, no_toc_deferred 기록
    - 1c diagnostic은 has_toc 무관 항상 실행

    debug-only. cache 안 건드림.

    Args:
        section_results: cache의 section_results dict (v5+ schema)
        call_llm: DB tool의 LLM caller. signature: (task_name, messages) -> str
        log_obj: logger (None이면 module log)
        task_name: AI 호출 task name (default "hwpx_phase_e_toc_plan")

    Returns:
        {
          "gate": {has_toc, toc_paragraph_hints, detection_method, ...},
          "status": "ok"|"no_toc_deferred"|"ai_call_failed"|"validation_fallback",
          "toc_plan": validated_plan or None,
          "one_c_diagnostic": {...},
          "retry_count": int,
        }
    """
    log_obj = log_obj or log

    # 1c diagnostic — has_toc 무관 항상 실행
    one_c_diag = diagnose_1c_non_body_handling(section_results)

    # has_toc gate
    gate = has_toc_gate(section_results)

    if not gate["has_toc"]:
        return {
            "gate": gate,
            "status": "no_toc_deferred",
            "toc_plan": None,
            "one_c_diagnostic": one_c_diag,
            "retry_count": 0,
        }

    # toc paragraphs + body paragraphs + 1c tree 수집
    toc_paragraphs: list[dict] = []
    seen_toc_keys: set[tuple] = set()
    for hint in gate["toc_paragraph_hints"]:
        sid = hint["section_id"]
        lid = hint["local_idx"]
        key = (sid, lid)
        if key in seen_toc_keys:
            continue
        seen_toc_keys.add(key)
        sresult = (
            section_results.get(str(sid))
            or section_results.get(sid)
            or {}
        )
        idx_full = sresult.get("idx_full_texts") or {}
        text = (
            idx_full.get(str(lid))
            or idx_full.get(lid)
            or hint.get("text_preview", "")
        )
        toc_paragraphs.append({
            "section_id": sid,
            "local_idx": lid,
            "role": hint.get("role", ""),
            "text": text,
        })

    body_by_section: dict[int, list[dict]] = {}
    tree_by_section: dict[int, list[dict]] = {}
    all_paragraphs_by_section: dict[int, set] = {}

    for raw_sid, sresult in section_results.items():
        try:
            sid = int(raw_sid)
        except (TypeError, ValueError):
            continue
        structure = (sresult or {}).get("structure") or {}
        paragraphs = structure.get("paragraphs") or []
        idx_texts = (sresult or {}).get("idx_texts") or {}
        body_list: list[dict] = []
        tree_list: list[dict] = []
        all_idxs: set = set()
        for p in paragraphs:
            lid = p.get("idx")
            if lid is None:
                continue
            all_idxs.add(int(lid))
            text = idx_texts.get(str(lid)) or idx_texts.get(lid) or ""
            body_list.append({
                "local_idx": lid,
                "marker": p.get("marker", ""),
                "role": p.get("canonical_role") or p.get("role") or "",
                "text": text,
            })
            tree_list.append({
                "local_idx": lid,
                "level": p.get("level"),
                "parent_idx": p.get("parent_idx"),
            })
        body_by_section[sid] = body_list
        tree_by_section[sid] = tree_list
        all_paragraphs_by_section[sid] = all_idxs

    # AI 호출 (재시도 1회)
    messages = build_toc_based_chapter_plan_prompt(
        toc_paragraphs=toc_paragraphs,
        body_paragraphs_by_section=body_by_section,
        one_c_tree_by_section=tree_by_section,
    )

    plan = None
    last_parse_error = None
    last_exception = None
    retry_count = 0
    max_retries = 1

    while retry_count <= max_retries:
        try:
            llm_response = call_llm(task_name, messages)
            parsed = parse_toc_based_chapter_plan_from_llm(llm_response)
            if "parse_error" not in parsed:
                plan = parsed
                break
            last_parse_error = parsed.get("parse_error")
            log_obj.warning(
                f"Phase E parse error (retry {retry_count}): {last_parse_error}"
            )
        except Exception as e:
            last_exception = str(e)
            log_obj.warning(f"Phase E AI 호출 실패 (retry {retry_count}): {e}")
        retry_count += 1

    if plan is None:
        return {
            "gate": gate,
            "status": "ai_call_failed",
            "toc_plan": None,
            "one_c_diagnostic": one_c_diag,
            "retry_count": retry_count,
            "last_parse_error": last_parse_error,
            "last_exception": last_exception,
        }

    validated = validate_toc_based_chapter_plan(plan, all_paragraphs_by_section)
    val_result = validated.get("validation_result") or {}
    status = "validation_fallback" if val_result.get("fallback_required") else "ok"

    return {
        "gate": gate,
        "status": status,
        "toc_plan": validated,
        "one_c_diagnostic": one_c_diag,
        "retry_count": retry_count,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Track C — Chapter Pattern Family Analysis (Phase E 후 sub-step, debug-only)
#
# 책임 분리 (§22):
#   - extract_generation_unit_subtrees : code fact 추출만 (similarity 계산 X,
#                                        family 후보 미리 판단 X). raw/summary fact만.
#   - build_chapter_pattern_family_prompt : AI에게 fact 제공 + 판단 원칙
#   - parse + validate : member index 실존 + confidence medium/low →
#                        expandable=false 강제 (보수적 안전망)
#
# 목적: 같은 양식이 반복 사용한 단위(template이 반복 허용)를 family로 묶고
#       expandable 여부 판단. 추후 source 가변 대응 시 chapter 확장 evidence로 사용.
#
# 단일 반복 후보(반복 가능한 구조가 양식 안에 1개만 존재)는 family로 못 묶일 수 있음.
# 양식 5+ 검증 시 watch.
# ═══════════════════════════════════════════════════════════════════════════


def extract_generation_unit_subtrees(toc_plan: dict, section_results: dict) -> list[dict]:
    """
    Track C fact extraction (code): generation_unit별 subtree fact 정리.

    code는 raw/summary fact만 추출. similarity 계산 X. family 후보 판단 X.
    AI가 evidence 기반으로 family 판단.

    Returns: [
      {
        "unit_index": int,
        "title": str,
        "paragraph_ref": {section_id, local_idx} | None,
        "idx_range": list,
        "subtree_paragraphs": [{section_id, local_idx, marker, role, level,
                                parent_local_idx, text_preview}],
        "subtree_paragraph_total": int,
        "structural_summary": {
          "depth": int,
          "role_distribution": {role: count},
          "marker_set": [unique markers sorted],
          "level_distribution": {level_str: count},
          "direct_children_count": int,
        },
        "parent_container_index": int | None,
        "fact_extraction_status": "ok" | "no_idx_range",
      }
    ]
    """
    interp = (toc_plan or {}).get("toc_interpretation") or {}
    gen_units = interp.get("generation_units") or []
    results: list[dict] = []

    for unit_idx, unit in enumerate(gen_units):
        idx_range = unit.get("idx_range") or []
        if not idx_range:
            results.append({
                "unit_index": unit_idx,
                "title": unit.get("title_text", ""),
                "paragraph_ref": unit.get("paragraph_ref"),
                "idx_range": None,
                "subtree_paragraphs": [],
                "subtree_paragraph_total": 0,
                "structural_summary": None,
                "parent_container_index": unit.get("parent_container_index"),
                "fact_extraction_status": "no_idx_range",
            })
            continue

        all_paras: list[dict] = []
        for span in idx_range:
            if not isinstance(span, dict):
                continue
            try:
                sid = int(span.get("section_id"))
                start = int(span.get("start_local_idx"))
                end = int(span.get("end_local_idx"))
            except (TypeError, ValueError):
                continue
            sresult = (
                section_results.get(sid)
                or section_results.get(str(sid))
                or {}
            )
            structure = (sresult or {}).get("structure") or {}
            paragraphs = structure.get("paragraphs") or []
            idx_texts = (sresult or {}).get("idx_texts") or {}
            for p in paragraphs:
                pidx = p.get("idx")
                if pidx is None:
                    continue
                try:
                    pidx_int = int(pidx)
                except (TypeError, ValueError):
                    continue
                if pidx_int < start or pidx_int > end:
                    continue
                text = idx_texts.get(str(pidx_int)) or idx_texts.get(pidx_int) or ""
                all_paras.append({
                    "section_id": sid,
                    "local_idx": pidx_int,
                    "marker": p.get("marker", ""),
                    "role": p.get("canonical_role") or p.get("role") or "",
                    "level": p.get("level"),
                    "parent_local_idx": p.get("parent_idx"),
                    "text_preview": text[:80],
                })

        # structural summary: raw fact만 (similarity / family 판단 X)
        role_dist: dict = {}
        marker_set: set = set()
        level_dist: dict = {}
        direct_children = 0

        unit_ref = unit.get("paragraph_ref") or {}
        unit_local_idx = unit_ref.get("local_idx") if isinstance(unit_ref, dict) else None

        for p in all_paras:
            role = p.get("role") or ""
            if role:
                role_dist[role] = role_dist.get(role, 0) + 1
            mk = p.get("marker") or ""
            if mk:
                marker_set.add(mk)
            lvl = p.get("level")
            if lvl is not None:
                level_dist[lvl] = level_dist.get(lvl, 0) + 1
            if p.get("parent_local_idx") == unit_local_idx:
                direct_children += 1

        levels_present = [l for l in level_dist.keys() if l is not None]
        depth = (max(levels_present) - min(levels_present)) if levels_present else 0

        # subtree_paragraphs truncate to limit token (unit당 최대 50 paragraph)
        sub_truncated = all_paras[:50]

        results.append({
            "unit_index": unit_idx,
            "title": unit.get("title_text", ""),
            "paragraph_ref": unit_ref or None,
            "idx_range": idx_range,
            "subtree_paragraphs": sub_truncated,
            "subtree_paragraph_total": len(all_paras),
            "structural_summary": {
                "depth": depth,
                "role_distribution": role_dist,
                "marker_set": sorted(marker_set),
                "level_distribution": {str(k): v for k, v in level_dist.items()},
                "direct_children_count": direct_children,
            },
            "parent_container_index": unit.get("parent_container_index"),
            "fact_extraction_status": "ok",
        })

    return results


CHAPTER_PATTERN_FAMILY_PROMPT = """당신은 양식의 generation_unit들이 같은 local pattern family에 속하는지 판단하는 분석가입니다.
이 판단은 양식이 어떤 unit을 반복 허용했는지를 보고, 추후 source가 더 많을 때 chapter를 같은 pattern으로 늘릴 수 있는지 결정하는 근거가 됩니다.

# ⚠️ 응답 언어 — 한국어 전용
- 자체 표현 (analysis / reason / pattern_signature) 은 반드시 한국어. 한자 / 일본어 가나 / 외국어 단어 사용 금지.
- 양식 sample 인용은 그대로 — 자체 표현과 인용 구분.

[INPUT]

generation_unit list. 각 unit:
- title, paragraph_ref, idx_range
- subtree_paragraphs (자기 영역 안 paragraph: local_idx, marker, role, level, parent, text_preview)
- structural_summary: depth, role_distribution, marker_set, level_distribution, direct_children_count
- parent_container_index (container 그룹 단위)

[판단 원칙]

- 완전 동일이 아니라 구조적 골격 유사성 기준입니다.
- 같은 parent 아래에서 marker / role / depth / 하위 구조 흐름이 유사하게 반복되면 같은 family입니다.
- 제목 텍스트, 하위 항목 개수, 분량 차이는 family 판단 기준이 아닙니다.
- 역할이 서로 다르면 같은 family가 아닙니다 (예: 한 unit은 도입/서두 성격, 다른 unit은 본문 추진 성격, 다른 unit은 결론 성격 — 이런 경우 family X).
- 같은 parent container 아래 단위들이 family 후보입니다. 단 parent가 같다고 무조건 family는 아닙니다.

[expandable 의미]

- expandable=true는 "양식이 이 family pattern을 더 반복할 수 있다"는 의미입니다.
- supporting_evidence와 counter_evidence를 모두 채우십시오.
- confidence가 medium 또는 low이면 expandable=false로 두십시오.
  확장은 양식 구조를 바꾸는 작업이므로 보수적으로 판단합니다.
- counter_evidence가 supporting_evidence보다 강하면 expandable=false로 두십시오.

[evidence cite 의무]

- supporting / counter evidence는 generation_unit_indices 또는 paragraph_refs로 cite하십시오.
- 골격이 같다는 근거는 어떤 marker / role / depth / 하위 구조 흐름이 일치하는지 구체적으로 인용하십시오.
- 임의로 unit_index를 만들지 마십시오. INPUT의 unit_index 중 하나여야 합니다.

[hardcode 금지]

- 특정 marker family, 특정 role 이름, 특정 양식명에 따른 분기 금지.
- 구조적 골격 비교는 input fact에 기반하십시오.
- 분량 균일성, 개수 정확 일치를 기준으로 사용하지 마십시오. 구조 흐름의 유사성만 기준입니다.

[OUTPUT — JSON ONLY, 다른 텍스트 금지]

{
  "pattern_families": [
    {
      "family_id": <str>,
      "members": [<unit_index>, ...],
      "expandable": <bool>,
      "structural_summary": <str>,
      "supporting_evidence": [<str>],
      "counter_evidence": [<str>],
      "confidence": "high"|"medium"|"low"
    }
  ],
  "non_grouped_units": [<unit_index>, ...],
  "ambiguity_flags": [<str>]
}
"""


def build_chapter_pattern_family_prompt(unit_subtrees: list[dict]) -> list[dict]:
    """Track C: chapter pattern family AI prompt 생성."""
    payload = {"generation_units": unit_subtrees}
    user_msg = (
        "## INPUT\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n위 generation_unit들의 pattern family를 OUTPUT SCHEMA에 따라 JSON으로만 답하십시오."
    )
    return [
        {"role": "system", "content": CHAPTER_PATTERN_FAMILY_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_chapter_pattern_family_from_llm(llm_response: str) -> dict:
    """Track C: AI 응답 JSON 파싱."""
    text = (llm_response or "").strip()

    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(text))
        except Exception as e:
            log.warning(f"Track C pattern family JSON 파싱 실패: {e}")
            return {"parse_error": str(e), "raw_preview": (llm_response or "")[:200]}

    if not isinstance(parsed, dict):
        return {"parse_error": "top-level is not dict", "raw_preview": (llm_response or "")[:200]}

    return parsed


def assign_chapter_ids_from_phase_e(
    structure: dict,
    phase_e_result: dict | None,
) -> dict:
    """Phase E의 generation_units idx_range 보고 paragraph에 chapter_id 부여.

    1e canonical clustering 직전에 호출. 1e prompt가 chapter_id를 보고
    같은 marker라도 다른 chapter면 다른 cluster로 분리.

    chapter_id 의미:
    - 0-based generation_unit index (양식 chapter 순서: 0, 1, 2, ...)
    - -1: chapter 밖 (container/TOC/표지/header/footer 등)

    Phase E status가 'ok'/'validation_fallback'이 아니면 모든 paragraph에
    chapter_id=-1 (1e가 chapter-aware 분리 안 함, 기존 동작과 동일).

    Side effect: structure["paragraphs"][i]["chapter_id"] 부여.

    Args:
        structure: 1c 결과까지 들고 있는 structure (paragraphs 필요)
        phase_e_result: Phase E 결과 dict (toc_plan.toc_interpretation.generation_units 사용)
            또는 None (fallback path)

    Returns:
        {"assigned": int, "no_chapter": int, "chapter_count": int}
    """
    paragraphs = (structure or {}).get("paragraphs") or []

    # Phase E 실패/no_toc/error → 모든 paragraph chapter_id=-1
    if not phase_e_result or phase_e_result.get("status") not in ("ok", "validation_fallback"):
        for p in paragraphs:
            p["chapter_id"] = -1
        return {"assigned": 0, "no_chapter": len(paragraphs), "chapter_count": 0}

    plan = phase_e_result.get("toc_plan") or {}
    interp = plan.get("toc_interpretation") or {}
    gen_units = interp.get("generation_units") or []

    # idx → chapter_id (0-based) 매핑 구성
    # section 0 only (multi-section은 section 0만 처리 — 사용자 정책 2026-05-17)
    idx_to_chapter: dict[int, int] = {}
    for ci, unit in enumerate(gen_units):
        for span in (unit.get("idx_range") or []):
            try:
                sid = int(span.get("section_id", 0))
                start = int(span.get("start_local_idx"))
                end = int(span.get("end_local_idx"))
            except (TypeError, ValueError):
                continue
            if sid != 0:
                continue
            for idx in range(start, end + 1):
                idx_to_chapter[idx] = ci

    # paragraph에 chapter_id 부여
    assigned = 0
    no_chapter = 0
    for p in paragraphs:
        pidx = p.get("idx")
        if pidx is None:
            p["chapter_id"] = -1
            no_chapter += 1
            continue
        try:
            ci = idx_to_chapter.get(int(pidx), -1)
        except (TypeError, ValueError):
            ci = -1
        p["chapter_id"] = ci
        if ci >= 0:
            assigned += 1
        else:
            no_chapter += 1

    return {
        "assigned": assigned,
        "no_chapter": no_chapter,
        "chapter_count": len(gen_units),
    }


def _phase_e_to_target_unit_plan(phase_e_result: dict, structure: dict) -> dict:
    """
    Phase E generation_units + out_of_toc_preserve_regions → target_unit_plan schema.

    Production 전환 1단계: 변환 결과를 debug에 dump하여 legacy AI 결과와 비교.
    실제 target_unit_plan은 덮어쓰지 않음 (단계적 안전 전환).

    매핑:
      - generation_unit → region (unit_type="chapter")
      - out_of_toc_preserve_regions → region (unit_type="slot")
      - container/subpattern은 chapter region에 metadata로만 포함 (별 region 아님)

    Multi-section 양식 (section_id != 0)은 section 0만 매핑하고 나머지는
    _multi_section_units_skipped에 기록 (13.7b multi-section 정책과 일관).

    Returns:
        target_unit_plan-compatible dict:
        {
          "regions": [{region_id, unit_type, paragraph_indices, description, ...}],
          "source": "phase_e",
          "_phase_e_status": str,
          "_generation_unit_count": int,
          "_out_of_toc_count": int,
          "_multi_section_units_skipped": [...]
        }
    """
    plan = (phase_e_result or {}).get("toc_plan") or {}
    interp = plan.get("toc_interpretation") or {}
    gen_units = interp.get("generation_units") or []
    out_of_toc = interp.get("out_of_toc_preserve_regions") or []

    regions: list[dict] = []
    region_counter = 0
    multi_section_skipped: list[dict] = []

    def _expand_idx_range(idx_range, allow_section_id: int = 0):
        """idx_range list of spans → paragraph_indices flat list (section_id == allow_section_id만)."""
        out: list[int] = []
        skipped_sections: set[int] = set()
        for span in idx_range or []:
            if not isinstance(span, dict):
                continue
            try:
                sid = int(span.get("section_id"))
            except (TypeError, ValueError):
                continue
            if sid != allow_section_id:
                skipped_sections.add(sid)
                continue
            try:
                start = int(span.get("start_local_idx"))
                end = int(span.get("end_local_idx"))
            except (TypeError, ValueError):
                continue
            if start > end:
                continue
            out.extend(range(start, end + 1))
        return out, skipped_sections

    # generation_units → chapter regions
    for unit_idx, unit in enumerate(gen_units):
        idx_range = unit.get("idx_range") or []
        paragraph_indices, skipped = _expand_idx_range(idx_range)
        if skipped:
            multi_section_skipped.append({
                "unit_index": unit_idx,
                "title": unit.get("title_text", ""),
                "skipped_section_ids": sorted(skipped),
                "reason": "multi_section_handling_deferred_to_13_7b",
            })
        if not paragraph_indices:
            continue
        region_counter += 1
        regions.append({
            "region_id": f"phase_e_chapter_{region_counter}",
            "unit_type": "chapter",
            "paragraph_indices": paragraph_indices,
            "description": unit.get("title_text", ""),
            "_phase_e_source": True,
            "_unit_index": unit_idx,
            "_marker_family_hint": unit.get("marker_family_hint", ""),
            "_parent_container_index": unit.get("parent_container_index"),
        })

    # out_of_toc_preserve → slot regions (단순 매핑: 모두 slot)
    for region_idx, region in enumerate(out_of_toc):
        paragraph_indices: list[int] = []
        skipped_sections: set[int] = set()
        for ref in region.get("paragraph_refs", []) or []:
            if not isinstance(ref, dict):
                continue
            try:
                sid = int(ref.get("section_id"))
            except (TypeError, ValueError):
                continue
            if sid != 0:
                skipped_sections.add(sid)
                continue
            try:
                paragraph_indices.append(int(ref.get("local_idx")))
            except (TypeError, ValueError):
                continue
        if skipped_sections:
            multi_section_skipped.append({
                "preserve_region_index": region_idx,
                "label": region.get("region_label", ""),
                "skipped_section_ids": sorted(skipped_sections),
                "reason": "multi_section_handling_deferred_to_13_7b",
            })
        if not paragraph_indices:
            continue
        region_counter += 1
        regions.append({
            "region_id": f"phase_e_preserve_{region_counter}",
            "unit_type": "slot",
            "paragraph_indices": paragraph_indices,
            "description": region.get("region_label", "preserve"),
            "_phase_e_source": True,
            "_preserve_reason": region.get("reason_free_text", ""),
        })

    return {
        "regions": regions,
        "source": "phase_e",
        "_phase_e_status": phase_e_result.get("status"),
        "_generation_unit_count": len(gen_units),
        "_out_of_toc_count": len(out_of_toc),
        "_multi_section_units_skipped": multi_section_skipped,
    }


def _phase_e_to_chapter_types(
    phase_e_result: dict,
    track_c_result: dict | None,
    structure: dict,
) -> dict:
    """
    Phase E generation_units + Track C family → chapter_types schema 매핑.

    매핑 정책 (3-A 단순 매핑, 호환 유지):
    - Track C family 멤버 → 같은 type
    - non_grouped unit → 각자 singleton type
    - title_role: 첫 unit의 paragraph_ref → 1d role
    - description: title_text
    - pattern: {} (legacy field. 13.6 per_chapter_pattern로 대체됨)
    - merged_chapter_count: family size

    chapter_types schema 호환 유지로 2a/13.4b/13.6/13.7a/13.7b/13.7c 기존 코드 무변경.

    Returns: chapter_types-compatible dict {"type_1": {...}, "type_2": {...}}
    """
    plan = (phase_e_result or {}).get("toc_plan") or {}
    interp = plan.get("toc_interpretation") or {}
    gen_units = interp.get("generation_units") or []

    paragraphs = (structure or {}).get("paragraphs") or []
    para_by_idx = {}
    for p in paragraphs:
        pidx = p.get("idx")
        if pidx is not None:
            para_by_idx[(0, int(pidx))] = p  # section_id=0 매핑 (section 0 only)

    # Track C family로 grouping
    family_map: dict[int, str] = {}  # unit_idx → family_id
    if track_c_result and track_c_result.get("status") == "ok":
        result = track_c_result.get("result") or {}
        for fam in result.get("pattern_families", []) or []:
            fid = fam.get("family_id")
            if not fid:
                continue
            for m in fam.get("members", []) or []:
                try:
                    family_map[int(m)] = fid
                except (TypeError, ValueError):
                    continue

    # type grouping: family_id 또는 singleton
    type_groups: dict[str, list[int]] = {}
    for i, _unit in enumerate(gen_units):
        key = family_map.get(i, f"singleton_{i}")
        type_groups.setdefault(key, []).append(i)

    chapter_types: dict[str, dict] = {}
    for type_counter, (type_key, indices) in enumerate(type_groups.items(), 1):
        type_name = f"type_{type_counter}"
        first_unit = gen_units[indices[0]] if indices else {}
        ref = first_unit.get("paragraph_ref") or {}
        title_role = ""
        if isinstance(ref, dict):
            try:
                sid = int(ref.get("section_id", 0))
                lid = int(ref.get("local_idx"))
                para = para_by_idx.get((sid, lid))
                if para:
                    title_role = para.get("canonical_role") or para.get("role") or ""
            except (TypeError, ValueError):
                pass

        chapter_types[type_name] = {
            "title_role": title_role,
            "description": first_unit.get("title_text", ""),
            "pattern": {},  # legacy field. 13.6 per_chapter_pattern로 대체
            "merged_chapter_count": len(indices),
            "_phase_e_source": True,
            "_phase_e_family_id": type_key,
            "_phase_e_member_unit_indices": indices,
        }

    return chapter_types


def build_target_unit_plan_dispatcher_decision(
    phase_e_result: dict | None,
) -> dict:
    """
    §6 책임 분리: target_unit_plan 결정 route 선택.

    Phase E status=ok → "phase_e" (Phase E 결과를 target_unit_plan으로 변환)
    Phase E 실패 / no_toc_deferred / 없음 → "legacy_ai" (기존 target_unit_planning AI 호출)

    Returns: {"route": "phase_e"|"legacy_ai", "reason": str}
    """
    if not phase_e_result:
        return {"route": "legacy_ai", "reason": "phase_e_result_missing"}
    status = phase_e_result.get("status")
    if status == "ok" and phase_e_result.get("toc_plan"):
        return {"route": "phase_e", "reason": f"phase_e_status_{status}"}
    return {"route": "legacy_ai", "reason": f"phase_e_status_{status}"}


def validate_chapter_pattern_family(plan: dict, n_units: int) -> dict:
    """
    Track C validation:
    - member unit_index 실존 검증 (0 <= idx < n_units)
    - confidence medium/low이면 expandable=false 강제 (보수적 안전망)
    - 단일 member family는 singleton_family_weak_evidence flag

    expandable_forced_false_count 기록.
    """
    if "parse_error" in plan:
        plan["validation_result"] = {
            "valid": False,
            "invalid_member_indices": [],
            "expandable_forced_false_count": 0,
            "fallback_required": True,
        }
        return plan

    invalid_members: list[dict] = []
    forced_false = 0

    for fam in plan.get("pattern_families", []) or []:
        invalid_in_family: list = []
        valid_members: list[int] = []
        for m in fam.get("members", []) or []:
            try:
                m_int = int(m)
            except (TypeError, ValueError):
                invalid_in_family.append(m)
                continue
            if 0 <= m_int < n_units:
                valid_members.append(m_int)
            else:
                invalid_in_family.append(m)
        fam["members"] = valid_members
        if invalid_in_family:
            invalid_members.extend([
                {"family_id": fam.get("family_id"), "invalid_ref": m}
                for m in invalid_in_family
            ])

        # confidence-based expandable enforcement (보수적 안전망)
        conf = (fam.get("confidence") or "").lower()
        if conf in ("medium", "low"):
            if fam.get("expandable"):
                fam["expandable"] = False
                fam["_validation_flags"] = fam.get("_validation_flags", []) + [
                    "expandable_forced_false_by_confidence"
                ]
                forced_false += 1

        # 단일 멤버 family는 family 의미 약함 watch
        if len(valid_members) < 2:
            fam["_validation_flags"] = fam.get("_validation_flags", []) + [
                "singleton_family_weak_evidence"
            ]

    # non_grouped_units index 검증
    invalid_non_grouped: list = []
    valid_non_grouped: list[int] = []
    for m in plan.get("non_grouped_units", []) or []:
        try:
            m_int = int(m)
        except (TypeError, ValueError):
            invalid_non_grouped.append(m)
            continue
        if 0 <= m_int < n_units:
            valid_non_grouped.append(m_int)
        else:
            invalid_non_grouped.append(m)
    plan["non_grouped_units"] = valid_non_grouped
    if invalid_non_grouped:
        invalid_members.extend([
            {"location": "non_grouped_units", "invalid_ref": m}
            for m in invalid_non_grouped
        ])

    plan["validation_result"] = {
        "valid": True,
        "invalid_member_indices": invalid_members,
        "expandable_forced_false_count": forced_false,
        "fallback_required": False,
    }
    return plan
