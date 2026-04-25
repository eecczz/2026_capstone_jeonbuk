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
from itertools import combinations
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
양식을 분석하여 각 필드의 **의미적 역할(role)**, 용도(description), 마커, 표 구조를 JSON으로 출력합니다.

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
- **marker**: 번호 기호가 있으면 그대로 기록 (➊, 󰊲, Ⅰ., 1., 1), 가. 등), 없으면 ""

  **마커 추출 엄수 사항**:
  - 마커는 **첫 공백 직전까지의 모든 기호/숫자** 전체를 포함. 끝에 붙은 구두점(`)`, `.`, `,`)도 절대 빼지 마세요
    ✓ "1) 내용" → marker=`"1)"` (괄호 포함)
    ✗ "1) 내용" → marker=`"1"` (괄호 누락 — 금지)
    ✓ "가. 내용" → marker=`"가."` (마침표 포함)
    ✓ "1 내용" → marker=`"1"` (공백이 바로 오면 단독 숫자 OK)
  - 다른 문단의 마커와 비슷해 보여도 **눈에 보이는 문자 그대로** 기록. 정규화·변환 금지.

- **description**: 이 자리에 **어떤 내용이 어떤 형식으로** 들어가야 하는지 구체적으로 설명
- **paraPrIDRef**: <hp:p>의 paraPrIDRef 속성값
- **charPrIDRef**: 첫 번째 <hp:run>의 charPrIDRef 속성값

**role은 출력하지 마세요.** role 분류는 별도 단계(1b)에서 수행합니다.

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
    {"idx": 0, "marker": "", "description": "문서 전체 제목 (연도+기관+사업명+문서종류 형식)", "paraPrIDRef": "5", "charPrIDRef": "12"},
    {"idx": 1, "marker": "", "description": "작성일자 (yyyy. m. d. 형식)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 2, "marker": "", "description": "발신 기관명", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 3, "marker": "", "description": "목차 (텍스트 상자)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 4, "marker": "Ⅰ.", "description": "대분류 제목 (텍스트 상자)", "paraPrIDRef": "3", "charPrIDRef": "8"},
    {"idx": 5, "marker": "□", "description": "중분류 항목 제목", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 6, "marker": "ㅇ", "description": "세부 항목의 설명 본문", "paraPrIDRef": "0", "charPrIDRef": "0"},
    {"idx": 7, "marker": "*", "description": "참고/보충 설명", "paraPrIDRef": "0", "charPrIDRef": "0"}
  ],
  "tables": [
    {"table": 0, "rows": 5, "cols": 3, "description": "사업별 예산 배분 현황표",
     "headers": [{"row": 0, "col": 0, "text": "구분"}, {"row": 0, "col": 1, "text": "금액"}],
     "value_cells": [{"row": 1, "col": 1}, {"row": 2, "col": 1}]}
  ]
}
```

## 중요
- **role도, level도 절대 출력하지 마세요** — 1b(role), 1c(structure)에서 별도 결정합니다
- 양식의 텍스트는 샘플입니다. 샘플 텍스트 자체를 description에 넣지 마세요
- _idx가 있는 문단을 하나도 빠뜨리지 마세요
- 표의 headers(라벨)와 value_cells(데이터)를 정확히 구분하세요
- **1x1 표의 value_cells는 [{"row": 0, "col": 0}]** (빈 배열 금지)
"""


LEVEL_ANALYSIS_PROMPT = """당신은 HWPX 양식의 **level 판단** 전문가입니다 (1c).
1b가 제공한 role 후보 + features를 받아 **각 문단의 level과 후보 index**를 결정합니다.

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

## 임무 (2가지만)

각 문단에 대해:

1. **level**: 계층 깊이 (0=최상위, 1=대제목, 2,3,...)
2. **selected_role_candidate_index**: 1b 후보 중 어느 것 채택할지 (0 = 1순위)
   - 기본은 0 (1순위 채택)
   - 위치·구조상 다른 후보가 더 맞으면 1, 2 등 선택
   - **0이 아니면 `selection_reason_code` 필수**

## 결정 원칙

### A. 구조 신호로 level 결정

- **same_paraPr_run = true 연속**: 양식 작성자가 같은 위계로 묶음 → 같은 level (강한 신호)
- **marker_family 같은 연속**: enumeration siblings → 같은 level
- **marker_family 전환 (interleaved)**: 기존 family 사이 끼어 있으면 → 자식 (level+1)
- **marker_family 전환 (replace)**: 기존 family 끝나고 통째 교체 → 같은 level 가능
- **description**: 위 신호 모호할 때 보조

### B. level 일관성 체크 (코드 알고리즘 이해)

코드는 너의 level만 보고 다음 알고리즘으로 parent를 만든다:
```
parent = 현재 문단보다 앞에 나온 문단 중,
         level이 더 낮은 가장 가까운 문단
```

따라서 level만 정확하면 부모-자식 관계가 자동 생성됨. 너의 책임은:
- **연속된 형제는 같은 level** (예: 같은 enumeration의 변형들)
- **자식은 부모의 level + 1**
- **상위 위계로 돌아가면 그만큼 level이 작아짐** (예: ㅇ 항목들 끝나고 새 □ 나오면 □의 level)

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
      "selected_role_candidate_index": 0
    },
    {
      "idx": 5,
      "level": 2,
      "selected_role_candidate_index": 1,
      "selection_reason_code": "marker_family_fit"
    },
    {
      "idx": 10,
      "level": 3,
      "selected_role_candidate_index": 0
    }
  ]
}
```

## 중요
- **모든 idx 출력**
- 필수 필드: level, selected_role_candidate_index
- selected_role_candidate_index != 0이면 selection_reason_code 필수
- parent_idx, sibling_group_id 출력 금지 (있어도 코드가 무시 가능)
- 반드시 JSON만 출력
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

    for p_idx, p in enumerate(paragraphs):
        para_pr = p.get("paraPrIDRef", "0")
        first_run = p.find(f"{NS_HP}run")
        char_pr = first_run.get("charPrIDRef", "0") if first_run is not None else "0"

        # 표 참조
        tbls_in_p = list(p.iter(f"{NS_HP}tbl"))
        table_refs = [f"T{t.get('_tbl_idx', '?')}" for t in tbls_in_p]
        table_str = ",".join(table_refs) if table_refs else ""

        # 텍스트 (표 내부 텍스트 제외)
        text_parts = []
        for run in p.findall(f"{NS_HP}run"):
            if run.find(f"{NS_HP}tbl") is not None:
                # 표 포함 run은 텍스트 추출 건너뜀
                continue
            for t in run.iter(f"{NS_HP}t"):
                if t.text:
                    text_parts.append(t.text)
        text = "".join(text_parts).strip()
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
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    if use_compact_format:
        compact = serialize_to_compact(light_xml)
        user_msg = (
            "아래는 HWPX 양식의 구조를 **컴팩트 텍스트 포맷**으로 정리한 것입니다.\n"
            "각 문단의 **role, description, marker, paraPrIDRef, charPrIDRef**를 파악하고, "
            "표의 라벨/값 셀을 구분하세요.\n"
            "**level은 이 단계에서 출력하지 마세요** — 별도 단계에서 결정합니다.\n\n"
            "### 입력 포맷 설명\n"
            "- 문단: `idx|paraPr|charPr[|Ttable_ids] | 텍스트`\n"
            "  - `p` 접두사: paraPrIDRef (예: `p5` = paraPrIDRef 5)\n"
            "  - `c` 접두사: 첫 run의 charPrIDRef (예: `c12` = charPrIDRef 12)\n"
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
        para_pr = p.get("paraPrIDRef", "")
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
            f'"prev_marker_family": "{prev_family}"',
            f'"next_marker_family": "{next_family}"',
            f'"same_paraPr_run": {str(same_paraPr).lower()}',
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
        "- same_paraPr_run = true: 직전과 같은 paraPr → 같은 위계의 형제 가능성 높음\n"
        "- marker_family 같은 연속 → enumeration siblings (같은 level)\n"
        "- marker_family 다른 등장 (interleaved 패턴) → 자식 가능성\n"
        "- marker_family 다른 등장 (replace 패턴) → 같은 level 가능\n\n"
        f"## 문단 목록\n```json\n{para_text}\n```\n\n"
        "반드시 JSON만 출력 (paragraphs 배열, 각 문단의 final_role/level/parent_idx/sibling_group_id)."
    )

    return [
        {"role": "system", "content": LEVEL_ANALYSIS_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def parse_level_from_llm(llm_response: str) -> dict:
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
        if selected_idx != 0:
            non_default_index += 1

    log.info(
        f"1c (AI 2) 파싱: {len(decisions)}개 문단, "
        f"non-default candidate index {non_default_index}개"
    )
    return {"decisions": decisions, "level_map": level_map}


def merge_levels_into_structure(
    structure: dict, parsed: dict, exclusive_rules: list = None
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

    # 1.5단계: 코드가 parent_idx + sibling_group_id 자동 계산 (level 시퀀스 기반)
    structure["paragraphs"] = compute_parent_and_sibling_from_levels(
        structure.get("paragraphs", [])
    )

    # 2단계: canonical_role + structure_role 합성
    # canonical_role = 양식 구조 관점의 정규화 (1b의 다양한 semantic_role을 family 기반으로 통합)
    # structure_role = marker_family + canonical_role (chapter_types signature용)
    # semantic_role은 paragraph dict에 그대로 보존 (description과 함께 2b가 활용)
    for p in structure.get("paragraphs", []):
        sem_role = p.get("semantic_role") or p.get("role", "unknown")
        family = p.get("marker_family", "") or ""

        canonical_role = canonicalize_role(family, sem_role)
        p["canonical_role"] = canonical_role

        family_for_label = family or "no_marker"
        if family.startswith("char_"):
            family_short = family[5:]
            family_label = f"char{family_short}"
        else:
            family_label = family_for_label
        structure_role = f"{family_label}__{canonical_role}" if family else canonical_role
        p["structure_role"] = structure_role
        # role 필드는 chapter_types build 등 기존 코드 호환을 위해 structure_role로 덮어씀
        p["role"] = structure_role

    # 3단계: validator
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


def canonicalize_role(marker_family: str, semantic_role: str) -> str:
    """
    marker_family + semantic_role → canonical_role 정규화.

    매핑 우선순위:
    1. 마커 없는 항목은 semantic_role 그대로 (제목·박스류는 의미가 곧 양식 역할)
    2. 매핑된 family + 허용된 override 후보 → semantic_role 그대로
    3. 매핑된 family + 그 외 → family 기본 canonical
    4. 매핑 안 된 family → semantic_role 그대로 (양식별 특수 마커 보존)
    """
    if not marker_family or marker_family == "":
        return semantic_role

    default = _FAMILY_DEFAULT_CANONICAL.get(marker_family)
    if not default:
        return semantic_role

    overrides = _ALLOWED_OVERRIDES.get(marker_family, set())
    if semantic_role in overrides:
        return semantic_role

    return default


def compute_parent_and_sibling_from_levels(paragraphs: list[dict]) -> list[dict]:
    """
    level 시퀀스로부터 parent_idx + sibling_group_id를 stack 알고리즘으로 자동 계산.

    알고리즘:
    - 각 문단의 parent = 직전에 등장한 더 낮은 level 중 가장 가까운 문단
    - sibling_group_id = `children_of_<parent_idx>` (root는 `roots`)
    - level 별 stack 유지: 현재 level보다 깊은 entry는 scope 종료

    원본 paragraphs를 in-place 수정.
    """
    # level → 가장 최근에 그 level로 등장한 문단 (스택)
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

        # 부모 찾기: level-1, level-2, ... 0 까지 가장 가까운 것
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

        # 현재 문단을 그 level의 최신으로 등록
        level_stack[level] = p

        # 현재 level보다 깊은 stack은 scope 종료 (자식들 끝남)
        for deeper in [k for k in level_stack if k > level]:
            del level_stack[deeper]

    return paragraphs


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

## 역할 분담
- **1b (이 단계)**: semantic_role 후보 + 점수 (level·hierarchy 결정 안 함)
- 1c (다음 단계): 전체 시퀀스 + 후보 → level + 후보 index 선택

⚠️ **반드시 후보를 다양하게 줘라**. 단일 후보 박지 마라. 1c가 선택할 여지를 남겨야 한다.

⚠️ **1순위가 명백한 케이스(표지·날짜 등)에도 억지 후보 만들지 마라**. 차선책이 진짜 가능한 것만 출력. 가짜 후보 금지.

## 핵심 개념 분리
당신은 **semantic_role(의미)**만 다룬다. 다음은 별도 시스템이 처리:
- `marker_family` (표면 패턴): 코드가 자동 추출 → 입력에 포함됨
- `level/depth` (구조 깊이): AI 2가 결정
- `structure_role` (signature용): 코드가 `marker_family + semantic_role`로 합성

→ **다른 marker_family를 가진 문단을 같은 semantic_role로 묶어도 됨** (예: ▪과 ㅇ을 둘 다 `bullet_item`). 코드가 structure_role에서 자동 분리함.

## 입력 features (코드 계산)
- marker, marker_family, description
- prev/next marker(family), same_paraPr_run, paraPrIDRef

## 임무 (강제 규칙)

각 문단에 대해 **2~3개 후보**를 출력:

### 규칙 R1: 항상 2개 이상 후보
- "확실해 보이는" 본문이라도 `body + nearest_alternative` 2개
- 명백한 표지·날짜·기관명 같은 unique role도 1순위 + 차선책 2개

### 규칙 R2: 점수 범위 0.55~0.85 주로 사용
- 0.9+ 거의 안 씀 (over-confident 금지)
- 1순위 0.65~0.80, 2순위 0.50~0.65 정도가 자연스러움
- 점수 낮은 후보(< 0.4)는 제외

### 규칙 R3: marker_family 보존 후보 강제 포함
- marker가 있으면, **그 marker_family에 자연스러운 semantic_role 후보를 반드시 1개 이상 포함**

### 규칙 R3.5: marker_family별 canonical 권장 — 양식 구조 관점

**별표 계열(*, **, ***)** — 거의 항상 보충 항목:
- 1순위 후보: `supplement_item` 또는 `note`
- 별표 문장이 수치든 사례든 비교든 참고든 **양식 관점에서는 모두 "보충"**
- 세부 의미(evidence/example/comparison/footnote 등)는 **description에만 남기고 role로 분리하지 마라**
- 코드가 별표 계열을 `supplement_item`으로 정규화하므로 step_item·process_item 같은 후보 줘도 결과는 같음 (혼동 방지 위해 시도하지 마라)

**작은 사각(▪)** — 보통 실행/이행 항목:
- 1순위: `action_subitem` 또는 `bullet_item`

**이응(ㅇ)** — 본문 bullet:
- 1순위: `bullet_item` 또는 `detail_item`

**큰 사각(□)** — 섹션 헤더:
- 1순위: `section_header`

**번호 enumeration(➊➋➌, ①②③, 1)2), 가.나.)** — 해당 family의 표준 의미:
- ➊➋➌: `numbered_item`
- ①②③: `enumerated_item`
- 1)2): `enumerated_detail` (각주성)
- 가.나.: `enumerated_item`

**무마커(텍스트 박스 등)**:
- description 보고 자유롭게 (`summary_box`, `chapter_title_box`, `task_title` 등)

→ 코드가 family + semantic_role을 canonical_role로 정규화해서 chapter_types signature를 안정시킴.
   **너의 일은 description 의미 보존 + family 기본 후보 1순위로 주는 것**.

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
      "idx": 7,
      "candidates": [
        {"role": "bullet_item", "score": 0.72, "reason": "ㅇ 마커 + 본문성 description"},
        {"role": "detail_item", "score": 0.60, "reason": "section header 직속 자식"}
      ]
    },
    {
      "idx": 10,
      "candidates": [
        {"role": "note", "score": 0.74, "reason": "별표 marker_family + '보충' description"},
        {"role": "supplement_note", "score": 0.62, "reason": "직전 항목 보충 의미"}
      ]
    }
  ]
}
```

## 중요
- **모든 idx 출력** (빠뜨리지 마세요)
- 각 문단 **항상 2개 이상** 후보 (R1)
- 점수 0.55~0.85 범위 (R2)
- semantic_role 이름엔 marker_family·level 박지 마라 (R5)
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

아래 **각 부모 role의 인스턴스별 직계 자식 집합**을 보고, 같은 부모 아래에서
**한 번도 공존하지 않은 자식 쌍**을 찾아 배타 규칙을 출력하세요.

## 규칙 (기계적 적용)

각 부모 role의 인스턴스들을 훑어서:
- 자식 쌍 (A, B) 공존 횟수 = 0 → **배타** (무조건)
- 공존 횟수 ≥ 1 → **배타 아님** (무조건)

OX의 이분법입니다. 판단 여지 없음.

## 절차

1. 각 부모 role에 대해 인스턴스들을 순회하며 자식 쌍 공존 카운트
2. 공존 0회 쌍이 하나라도 있으면 그 부모에 대해 variant 분리
3. variant = 공존 그래프의 maximal clique (서로 공존 OK인 자식들의 묶음)
4. 공존 0회 쌍이 없으면 그 부모는 스킵 (규칙 출력 X)

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
- (detail_item, note): 1 → OK
- (key_point, note): 1 → OK
- (detail_item, key_point): **0 → 배타**

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
      "pairs_never_cooccurred": [["detail_item", "key_point"]]
    }
  ]
}
```

- `exclusive_rules`: 공존 0회 쌍이 발견된 **모든** 부모를 포함. 없으면 빈 배열.
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
        [{"parent": str, "variants": [[role,...], ...], "pairs_never_cooccurred": [...]}, ...]
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
                "pairs_never_cooccurred": r.get("pairs_never_cooccurred", []),
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


def get_template_cache_path(cache_key: str) -> str:
    """템플릿 분석 결과 캐시 파일 경로 (cache_key는 보통 content hash)"""
    import os
    safe_key = cache_key.replace("/", "_").replace("..", "_")
    return os.path.join(TEMPLATE_CACHE_DIR, f"{safe_key}.json")


def save_template_cache(cache_key: str, data: dict) -> bool:
    """양식 분석 결과를 캐시에 저장."""
    import os
    path = get_template_cache_path(cache_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        log.info(f"[CACHE] 템플릿 캐시 저장: {path} ({os.path.getsize(path):,}B)")
        return True
    except Exception as e:
        log.warning(f"[CACHE] 저장 실패: {e}")
        return False


def load_template_cache(cache_key: str) -> dict | None:
    """캐시에서 양식 분석 결과 로드. 없거나 실패시 None."""
    import os
    path = get_template_cache_path(cache_key)
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


def clear_template_cache(cache_key: str) -> bool:
    """캐시 파일 삭제"""
    import os
    path = get_template_cache_path(cache_key)
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

    # chapter type 분리 기준은 골격(top-level shape)만. deep optional variant 차이는
    # chapter 분리 트리거가 아님 (2b가 1d 배타규칙으로 인스턴스마다 선택).
    SIGNATURE_MAX_DEPTH = 3

    def _pattern_signature(pattern: dict, max_depth: int = SIGNATURE_MAX_DEPTH,
                          current_depth: int = 0) -> str:
        """
        패턴의 truncated signature — chapter dedup용.
        max_depth 이상은 무시 (deep optional variant들은 type 분리 기준 아님).
        """
        if current_depth >= max_depth:
            return ""
        parts = []
        for role, info in sorted(pattern.items()):
            children_sig = ""
            if "children" in info:
                children_sig = _pattern_signature(
                    info["children"], max_depth, current_depth + 1
                )
            parts.append(f"{role}({children_sig})")
        return "|".join(parts)

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

    chapter_types = {}
    sig_to_type = {}  # signature → type_name
    type_counter = 0

    for title_para, body_paras in chapters:
        title_role = title_para.get("role", "chapter_title")
        title_desc = title_para.get("description", "")

        role_info = _build_role_info(body_paras)
        if not role_info:
            continue

        # 챕터 단위 variant 분리는 하지 않음.
        # truncated signature(top SIGNATURE_MAX_DEPTH)로 chapter 묶음 결정.
        # 같은 sig 챕터들은 pattern union → 모든 variant가 한 type 안에 optional로 포함.
        # 인스턴스 단위 variant 선택은 1d 배타규칙 + 2b가 처리.
        pattern = _build_pattern(role_info)
        sig = _pattern_signature(pattern)

        if sig in sig_to_type:
            # 같은 골격의 chapter — 기존 type pattern에 union 병합
            existing_type_name = sig_to_type[sig]
            _merge_patterns(chapter_types[existing_type_name]["pattern"], pattern)
        else:
            type_counter += 1
            type_name = f"type_{type_counter}"
            sig_to_type[sig] = type_name
            chapter_types[type_name] = {
                "title_role": title_role,
                "description": _pattern_summary(pattern),
                "pattern": pattern,
            }

    # 모든 chapter 처리 후 description 재생성 (병합 반영)
    for type_name, info in chapter_types.items():
        info["description"] = _pattern_summary(info["pattern"])

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
- 소스에 명확한 대제목 있으면 **title에 원문 그대로** (마커 포함)
- 없으면 chapter의 핵심을 한 줄로 요약한 title 작성

## 출력 형식

```json
{
  "chapters": [
    {
      "type": "type_X",
      "title": "소스의 chapter 제목",
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

⭐ **`optimal_structure`와 `type_match_reason` 필드는 필수**.

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
5. **형제 배타 규칙이 주어지면 반드시 지키세요** — 프롬프트에 "형제 배타 규칙" 섹션이 있으면, 각 부모 인스턴스마다 제시된 variant 중 **하나만** 사용. 한 인스턴스 안에서 variant를 섞지 마세요.
   - **인스턴스마다 다른 variant 적극 활용**: 양식이 여러 variant를 제공하는 이유는 인스턴스마다 다른 표현이 가능하다는 뜻. 모든 인스턴스에 같은 variant만 쓰지 말고, **소스 내용의 성격(나열·각주·세부 단계·요약 등)에 맞는 variant를 인스턴스마다 적합하게 선택**하세요.
   - 예: 한 부모의 인스턴스 1번에는 보충 설명 variant, 인스턴스 2번에는 각주 variant, 인스턴스 3번에는 세부 단계 variant 등 — 소스 내용이 그렇게 갈리면 그대로 다양하게 사용.

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

## 마커 규칙 (format_rules 참조)

프롬프트에 주어진 **"포맷 규칙"** 섹션을 확인하고 role별 marker_style에 따라:

- `marker_style: fixed` — 항상 같은 마커 사용 (예: `ㅇ`, `□`)
- `marker_style: enumerate` — markers_sample의 **순서**를 유지하고, 샘플을 넘어가면 **자연스럽게 확장**:
  - `["➊","➋","➌"]` 4번째는 `➍`, 5번째는 `➎` (유니코드 +1)
  - `["*","**","***"]` 4번째는 `****` (반복 확장)
  - `["1)","2)"]` 3번째는 `3)`, 4번째는 `4)` (번호 증가)
  - **절대 다시 ➊, *, 1)로 돌아가지 마세요**

## 들여쓰기 — 신경 쓰지 마세요

출력 text에 **앞 공백/탭 넣지 마세요**. 조립 단계에서 자동 부착됩니다.
마커 + separator + 내용으로 시작하세요:
- 예: `"ㅇ 세부 내용"` (ㅇ + 공백 + 내용)
- 예: `"➊ 첫번째"` (➊ + 공백 + 내용)
- 예: `"순수 본문 내용"` (마커 없는 role)

## 텍스트 작성 규칙
- **role의 description이나 번호("과제 1", "전략 2" 등)를 텍스트에 넣지 마세요** — description은 role 선택의 참고용이며 출력 텍스트에 포함하면 안 됩니다
- 소스의 실제 내용만 작성하세요
- 소스의 원래 마커(◇, ◆, ⇒, ※, □ 등)는 제거하고 양식 마커로 교체하세요

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
    exclusive_rules: list = None,
    format_rules: dict = None,
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

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    # role 마커 매핑
    role_markers = {}
    for role_name, info in role_catalog.items():
        role_markers[role_name] = info.get("marker", "")

    # 패턴 트리 텍스트
    pattern_text = _format_pattern_tree(pattern, role_markers)

    # 이번 패턴에 등장하는 role들만 수집 → 관련된 배타 규칙만 추림
    def _collect_roles(pat: dict, acc: set):
        for r, info in pat.items():
            acc.add(r)
            ch = info.get("children", {})
            if ch:
                _collect_roles(ch, acc)

    pattern_roles = set()
    _collect_roles(pattern, pattern_roles)

    # format_rules 섹션 — 현재 chapter 패턴에 등장하는 role만
    format_text = ""
    if format_rules:
        lines_f = ["## 포맷 규칙 (marker 사용법)\n"]
        for role in pattern_roles:
            rule = format_rules.get(role)
            if not rule:
                continue
            style = rule.get("marker_style", "fixed")
            samples = rule.get("markers_sample", [])
            sep = rule.get("separator", "")
            if style == "enumerate" and samples:
                lines_f.append(
                    f"- `{role}`: marker_style=**enumerate**. "
                    f"샘플 순서 `{samples}`. 샘플을 넘어가면 이어서 확장."
                )
            elif samples and any(s for s in samples):
                mk = samples[0] if samples else ""
                lines_f.append(
                    f"- `{role}`: marker_style=**fixed**, 마커 `{mk}` 고정."
                )
            else:
                lines_f.append(f"- `{role}`: 마커 없음.")
            if sep:
                lines_f.append(f"  (마커 뒤 구분자: `{repr(sep)}`)")
        if len(lines_f) > 1:
            lines_f.append(
                "\n**출력 규칙**: text는 `마커 + separator + 내용`으로 시작. "
                "앞 공백/탭 절대 넣지 마세요 (조립에서 자동 부착)."
            )
            format_text = "\n".join(lines_f) + "\n\n"

    exclusive_text = ""
    if exclusive_rules:
        relevant = []
        for rule in exclusive_rules:
            parent = rule.get("parent", "")
            variants = rule.get("variants", [])
            if parent not in pattern_roles:
                continue
            # variant 내 role도 패턴에 존재하는 것만 유지
            filtered_variants = [
                [r for r in v if r in pattern_roles] for v in variants
            ]
            filtered_variants = [v for v in filtered_variants if v]
            if len(filtered_variants) < 2:
                continue
            relevant.append({
                "parent": parent,
                "variants": filtered_variants,
                "reason": rule.get("reason", ""),
            })
        if relevant:
            lines = ["## ⚠️ 형제 배타 규칙 (인스턴스 단위)\n"]
            lines.append(
                "각 부모 role의 **인스턴스마다** 아래 variant 중 하나를 선택해서 "
                "자식을 배치하세요. 한 인스턴스 안에서 서로 다른 variant의 role을 섞지 마세요.\n"
            )
            lines.append(
                "**인스턴스마다 소스 내용 성격에 맞는 variant를 적극 다양하게 선택하세요.** "
                "양식이 여러 variant를 제공하는 이유는 인스턴스마다 다른 표현이 가능하다는 뜻입니다. "
                "모든 인스턴스에 같은 variant만 쓰지 말고, 소스 내용이 갈리면 그대로 다양하게 사용. "
                "예: 첫 인스턴스는 variant A (예: 보충 설명), 두 번째는 variant B (예: 각주), "
                "세 번째는 variant C (예: 세부 단계).\n"
            )
            for rule in relevant:
                parent = rule["parent"]
                parent_marker = role_markers.get(parent, "")
                marker_str = f" (마커: \"{parent_marker}\")" if parent_marker else ""
                lines.append(f"\n### 부모: `{parent}`{marker_str}")
                for i, variant in enumerate(rule["variants"]):
                    marker_strs = []
                    for r in variant:
                        m = role_markers.get(r, "")
                        marker_strs.append(
                            f"`{r}`" + (f' ("{m}")' if m else "")
                        )
                    lines.append(
                        f"- variant {chr(ord('A')+i)}: " + ", ".join(marker_strs)
                    )
                reason = rule.get("reason", "")
                if reason:
                    lines.append(f"  이유: {reason}")
            exclusive_text = "\n".join(lines) + "\n\n"

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
        f"{format_text}"
        f"{exclusive_text}"
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
