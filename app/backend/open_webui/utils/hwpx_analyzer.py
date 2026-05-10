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

    _para_styles = {}  # idx → {"paraPrIDRef": str, "charPrIDRef": str}

    for p_idx, p in enumerate(paragraphs):
        para_pr = p.get("paraPrIDRef", "0")
        first_run = p.find(f"{NS_HP}run")
        char_pr = first_run.get("charPrIDRef", "0") if first_run is not None else "0"
        _para_styles[p_idx] = {"paraPrIDRef": para_pr, "charPrIDRef": char_pr}

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

## 핵심 목적

이 단계는 **grammar/rule extraction (1f) 용 structural node type clustering** 입니다.
의미 분류(semantic taxonomy) 가 아닙니다.

같은 cluster의 paragraph들은 1f에서 grammar/rule 추출 시 **같은 노드 종류**로 취급됩니다. 따라서:
- **다른 노드 종류**가 필요할 때만 split
- 같은 구조 기능이면 semantic sub-genre 가 달라도 merge

## 임무

확정된 parent_first tree 위에서, 각 paragraph에 **cluster_id (numerical 0, 1, 2, ...)** 를 할당하라.

## 판단 기준 — 구조 패턴

다음 신호를 종합해서 판단:

- **부모 패턴**: 같은 부모 종류를 가지는 paragraph 는 같은 cluster 후보
- **자식 패턴**: 같은 자식 구성을 가지는 paragraph 는 같은 cluster 후보
- **반복 위치 패턴**: 같은 부모 아래에서 **반복적으로 같은 위치/순서/기능**으로 나타나는 paragraph 는 같은 cluster 후보
  - ⚠️ 단순히 같은 부모를 공유한다는 이유만으로 같은 cluster X
  - 같은 부모 아래에서도 서로 다른 구조 슬롯이 있을 수 있음 (예: 시퀀스 본체 vs trailing summary)
- **위계**: tree 위 같은 위계의 같은 역할 paragraph 는 같은 cluster
- **description**: 의미 보조 신호 (정답 아님)

### 서식 신호

- **마커 없음 + level 0 + 자식 없음 + 그룹 내 paraPrIDRef가 서로 모두 다름** → 각각 고유 서식의 고정 슬롯이므로 **반드시 별도 클러스터로 분리** (예: 표지의 제목/날짜/기관명은 각각 다른 서식·역할)
- 그 외: paraPrIDRef가 다르더라도 마커가 같거나 반복 패턴이 보이면 같은 클러스터 가능.

### 자식 유무 — 단독 split 금지

- ❌ "자식 보유한 인스턴스 vs 자식 없는 인스턴스" 만 보고 다른 cluster 로 split 금지
- ✅ optional child 누락으로 인스턴스마다 자식 수 다를 수 있음 — 정상
- 자식 유무는 **다른 신호 (부모/형제/위치/반복 패턴)와 종합**해서만 split 판단

## 마커 정규화 규칙 (마커 비교 시 반드시 적용)

- *, **, *** → 모두 같은 마커 "*" (반복 횟수는 depth 표현일 뿐)
- ➊, ➋, ➌, ➍ → 같은 마커 "➊" (순번만 다름)
- ①, ②, ③ → 같은 마커 "①"
- 1), 2), 3) → 같은 마커 "1)"
- 󰊱, 󰊲, 󰊳 → 같은 마커 "󰊱"
- 마커의 "종류"가 같으면 같은 마커. 번호/반복횟수 차이는 무시.

## 절대 원칙 — 양식 무관

- ✓ **마커가 다르면 반드시 다른 클러스터** (정규화 후 비교! hard constraint) — *, **는 정규화 후 같은 마커이므로 같은 클러스터 가능. *와 ①는 정규화 후에도 다르므로 반드시 다른 클러스터.
- ❌ marker 이름이나 marker_family를 cluster 정답으로 보지 말 것 — 단, 정규화 후 마커가 다른 paragraph를 같은 cluster에 넣으면 안 됨
- ❌ 1b/1c가 준 role 이름이 같다고 같은 cluster, 다르다고 다른 cluster 라고 단정 X
- ❌ "이 marker 는 보통 X 의미"라는 외부 convention 사전 가정 X
- ❌ 특정 도메인(한국 문서 등) convention 을 정답으로 보지 말 것
- ❌ **의미 차이만으로 split 금지** — 같은 구조 기능이면 semantic sub-genre 달라도 merge. 단, 마커가 다르면 이 규칙 적용 불가 (마커 분리가 우선)
- ✓ 이 양식 자체의 paragraph 데이터 + tree 구조 패턴 에서만 추론

## Cluster 개수 — 경제성

- **필요한 만큼만 만들고 singleton 남발 금지**
- 1f가 grammar 추출할 때 의미 있는 노드 종류 단위로 cluster
- semantic taxonomy 만들지 말 것 (description 의미 차이로 cluster 늘리지 X)
- 다만 grammar 상 다른 노드 종류로 구분 필요한 경우 (예: 시퀀스 본체 vs trailing summary, 다른 위계 chapter root) 는 split

## 입력

각 paragraph 에 대해 다음 정보가 주어집니다:
- idx, level, marker, marker_family, description
- parent_idx, children_idxs, sibling_idxs (tree 구조)
- 1b role_candidates (참고용, 정답 X)
- 1c selected_role, parent_hint_idx (참고용)
- paraPrIDRef, charPrIDRef (weak formatting signal)

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

    table_lines = []
    table_lines.append(
        "# Paragraph table — idx | L | marker | family | parent | children | siblings | "
        "1b_top | 1c_selected | hint | description | paraPr | charPr"
    )
    for p in paragraphs:
        idx = p.get("idx")
        level = p.get("level")
        marker = p.get("marker", "") or ""
        family = p.get("marker_family", "") or ""
        parent = p.get("parent_idx")
        kids = parent_to_kids.get(idx, [])
        all_sibs = parent_to_kids.get(parent, [])
        sibs = [s for s in all_sibs if s != idx]

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
            f"{idx} | L{level} | {marker!r} | {family} | "
            f"parent={parent} | kids={kids_str} | sibs={sibs_str} | "
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


CANONICAL_CLUSTERING_REPAIR_PROMPT = """당신은 이전 1e structural clustering 결과의 validation 오류를 수정하는 전문가입니다.

## 핵심 목적

이전 분류에 누락 (missing) / 중복 (duplicate) / 범위 밖 (extra) idx 오류가 있어 수정이 필요합니다.
**모든 input idx가 정확히 한 번씩** 한 cluster에 속해야 합니다 (95% 가 아니라 100%).

## 요구사항 (validation 만족 필수)

1. **모든 input paragraph idx 가 정확히 한 번씩** 등장
2. **누락 idx**: 구조 패턴 (parent/child/sibling/repetition/position) 보고 **적절한 cluster 에 배정** — singleton 남발 금지
3. **중복 idx**: 한 cluster 에만 남김 (가장 적절한 곳)
4. **input 범위 밖 idx**: 제거
5. **기존 cluster 구조는 가능한 한 유지** — 미수정 idx 들의 cluster 배정은 그대로

## 판단 원칙 (1e 와 동일)

- 같은 부모 + 같은 위치/순서/기능 = 같은 cluster
- 의미 sub-genre 차이로 split 금지
- 자식 유무만으로 단독 split 금지
- marker 이름·1b/1c role 이름을 정답으로 보지 말 것
- 데이터에서 추론, 외부 convention 가정 X

## 입력

- 전체 paragraph 데이터 (1e 와 동일 형식)
- 이전 1e cluster 출력 (cluster_id 별 paragraph_idxs)
- 발견된 issues (어떤 idx 가 missing/duplicate/extra 인지)

## 출력 형식 (JSON 만)

이전 1e 와 동일한 형식:

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

MARKER_POLICY_PROMPT = """당신은 양식의 role별 **마커(marker) 정책**을 판별하는 전문가입니다.

## 임무

각 role에 대해, 해당 role의 text samples를 보고 **일관된 leading marker가 있는지** 판별하세요.

## 판별 기준

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
      ]
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

## 규칙

- **모든 role에 대해 빠짐없이 출력**
- marker가 확실하지 않으면 `ambiguous`로 표시하세요. 억지로 분류하지 마세요.
- confidence는 0~1. sample 수가 적으면 낮게 (1개: 0.5 이하, 2개: 0.6~0.7, 3개+: 0.7~0.95)
- evidence에 각 sample별로 detected_marker를 남기세요 (없으면 null)
- 반드시 JSON만 출력
"""


def build_marker_policy_prompt(
    paragraphs: list[dict],
    idx_texts: dict,
    max_samples_per_role: int = 5,
) -> list[dict]:
    """
    1f: role별 sample text preview → marker policy induction prompt 생성.
    """
    from collections import defaultdict

    # role → sample indices 수집
    role_samples = defaultdict(list)
    for p in paragraphs:
        role = p.get("role", "")
        if role:
            role_samples[role].append(p.get("idx"))

    # role별 text preview 구성
    role_entries = []
    for role, idxs in sorted(role_samples.items()):
        samples = []
        for idx in idxs[:max_samples_per_role]:
            text = idx_texts.get(str(idx), idx_texts.get(idx, ""))
            samples.append({
                "idx": idx,
                "text_preview": text[:80] if text else "(빈 문단)",
            })
        role_entries.append({
            "role": role,
            "sample_count": len(idxs),
            "samples": samples,
        })

    user_msg = (
        "## role별 text samples\n\n"
        + json.dumps(role_entries, ensure_ascii=False, indent=2)
        + "\n\n위 role들의 marker policy를 판별하세요. 반드시 JSON만 출력."
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

→ **다른 marker_family를 가진 문단을 같은 semantic_role로 묶어도 됨** (구조 기능이 같으면 marker_family는 별도 신호로 보존됨). 코드가 marker_family + semantic_role을 따로 트래킹.

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

### 규칙 R3.5: marker_family는 힌트일 뿐, 의미는 데이터에서 관찰

- marker family는 role 후보 판단의 **보조 신호**일 뿐이다. 특정 기호 = 특정 role이라고 가정하지 말 것.
- 같은 양식 안에서 같은 marker family가 반복적으로 수행하는 기능을 paragraph 시퀀스 전체에서 관찰할 것.
- 주변 문단과의 관계, 들여쓰기, 반복 패턴, 내용상 역할을 함께 보고 role 후보 제안.
- 특정 기호 → 특정 role 1순위라는 사전 룰을 적용하지 말 것. 같은 기호도 양식·문맥에 따라 다른 의미 가능.

**무마커(텍스트 박스 등) — 위계 다양화는 여전히 중요**:
- 무마커 제목 박스가 양식 안에서 여러 위계로 등장하면 **단일 후보 박지 말고 인접 위계 후보 1~2개 같이** 제시 (1c가 위치로 고를 수 있게).
- 같은 description("제목"·"항목 제목")만으로 단일 후보 박지 마라.

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


CACHE_SCHEMA_VERSION = 4


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

아래에 role별 실제 paragraph 샘플이 주어집니다.
각 role의 문체/서술 스타일 특징을 분석하여 **고정 JSON schema**로 출력하세요.

## 핵심 규칙

1. **제공된 샘플 텍스트에서 직접 관찰 가능한 패턴만** 기술하세요.
2. **샘플에 없는 문체 특징을 추측하거나 일반화하지 마세요.**
3. **일반적인 행정문서 규칙을 추가하지 마세요** — 이 양식의 실제 샘플만 기준.
4. **이 role만의 고유 특징**에 집중하세요. 모든 role에 해당하는 공통점은 쓰지 마세요.
5. evidence_samples에는 각 주장의 근거가 되는 **실제 텍스트 발췌**를 포함하세요.
6. do/avoid는 이 role의 생성 텍스트가 양식과 **같은 느낌**이 나도록 하는 지침입니다.

## confidence 기준
- high: 샘플 5개 이상이고 문체가 일관됨
- medium: 샘플 3~4개이거나 문체에 편차가 있음
- low: 샘플 부족이거나 문체가 매우 이질적

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "profiles": [
    {
      "role": "<role_cluster 이름>",
      "sample_count": <제공된 샘플 수>,
      "style_summary": "<1~2문장: 이 role의 문체 핵심 특징>",
      "tone": "<어조 설명 (공식적/간결함/나열형 등)>",
      "sentence_shape": "<문장 구조 설명 (단문/복문/명사구/괄호 수치 등)>",
      "ending_patterns": ["<관찰된 어미 패턴>"],
      "typical_expressions": ["<빈번한 표현/구문>"],
      "do": ["<이 role처럼 쓰려면 해야 할 것>"],
      "avoid": ["<이 role처럼 쓰려면 피해야 할 것>"],
      "confidence": "high|medium|low",
      "notes": "<semantic_tag별 차이, 특이 패턴 등 추가 관찰>",
      "evidence_samples": ["<주장 근거 텍스트 발췌 (200자 이내)>"]
    }
  ]
}
```
"""


def _collect_style_samples(
    paragraphs: list[dict],
    idx_full_texts: dict,
    semantic_tags: dict | None = None,
    min_samples: int = 3,
    max_samples: int = 8,
) -> list[dict]:
    """
    role_cluster별 style analysis용 샘플을 수집합니다.

    Returns:
        [{role, marker, description, level, sample_count, samples: [str],
          char_lengths: [int], frequent_endings: [str],
          semantic_tag_distribution: {tag: count}}, ...]
    """
    import re
    from collections import defaultdict

    # role별 paragraph idx 수집
    role_idxs = defaultdict(list)
    role_meta = {}
    for p in paragraphs:
        role = p.get("role", "")
        if not role:
            continue
        role_idxs[role].append(p.get("idx"))
        if role not in role_meta:
            role_meta[role] = {
                "marker": p.get("marker", ""),
                "description": p.get("description", "")[:80],
                "level": p.get("level", 0),
            }

    # semantic_tag 분포 (12_structural_intent에서 가져옴)
    tag_dist = defaultdict(lambda: defaultdict(int))
    if semantic_tags:
        for entry in semantic_tags:
            tag_dist[entry.get("role", "")][entry.get("semantic_tag", "")] += 1

    # 어미 패턴 추출
    _ending_re = re.compile(r"([\uAC00-\uD7A3]{1,4})[.!?\s]*$")

    result = []
    for role in sorted(role_idxs.keys()):
        idxs = role_idxs[role]
        texts = [
            idx_full_texts.get(str(idx), idx_full_texts.get(idx, ""))
            for idx in idxs
        ]
        texts = [t for t in texts if t.strip()]

        if len(texts) < min_samples:
            continue

        # raw stats
        char_lengths = [len(t) for t in texts]

        # frequent endings
        endings = []
        for t in texts:
            t_stripped = t.rstrip()
            m = _ending_re.search(t_stripped)
            if m:
                endings.append(m.group(1))
        ending_counts = defaultdict(int)
        for e in endings:
            ending_counts[e] += 1
        frequent_endings = sorted(
            ending_counts.keys(), key=lambda e: -ending_counts[e]
        )[:5]

        # 대표 샘플 선택
        if len(texts) <= max_samples:
            samples = texts
        else:
            by_len = sorted(range(len(texts)), key=lambda i: len(texts[i]))
            selected = {by_len[0], by_len[-1]}  # shortest, longest
            step = len(texts) / (max_samples - 2)
            for i in range(max_samples - 2):
                idx_pick = int(i * step)
                selected.add(idx_pick)
            samples = [texts[i] for i in sorted(selected)][:max_samples]

        meta = role_meta.get(role, {})
        result.append({
            "role": role,
            "marker": meta.get("marker", ""),
            "description": meta.get("description", ""),
            "level": meta.get("level", 0),
            "sample_count": len(texts),
            "samples": samples,
            "char_lengths": char_lengths,
            "char_length_range": [min(char_lengths), max(char_lengths)],
            "frequent_endings": frequent_endings,
            "semantic_tag_distribution": dict(tag_dist.get(role, {})),
        })

    return result


def build_style_profile_prompt(
    role_batch: list[dict],
) -> list[dict]:
    """
    role batch에 대한 style profile AI prompt를 생성합니다.

    Args:
        role_batch: _collect_style_samples 결과의 subset (8~10 roles)

    Returns:
        [{"role": "system", ...}, {"role": "user", ...}]
    """
    user_parts = []
    for entry in role_batch:
        role = entry["role"]
        marker = entry.get("marker", "")
        desc = entry.get("description", "")
        level = entry.get("level", 0)
        sc = entry["sample_count"]
        endings = entry.get("frequent_endings", [])
        lengths = entry.get("char_length_range", [0, 0])
        tag_dist = entry.get("semantic_tag_distribution", {})

        header = f"### {role}"
        if marker:
            header += f" (마커: {marker})"
        header += f" — level {level}, {sc}개 샘플"
        if desc:
            header += f"\n설명: {desc}"
        if tag_dist:
            tag_str = ", ".join(f"{t}:{n}" for t, n in sorted(tag_dist.items()))
            header += f"\nsemantic_tag 분포: {tag_str}"
        header += f"\n길이 범위: {lengths[0]}~{lengths[1]}자"
        if endings:
            header += f"\n빈출 어미: {endings}"

        samples_text = "\n".join(
            f"  [{i+1}] {s}" for i, s in enumerate(entry["samples"])
        )
        user_parts.append(f"{header}\n샘플:\n{samples_text}")

    user_content = (
        f"아래 {len(role_batch)}개 role의 문체를 분석하세요.\n\n"
        + "\n\n".join(user_parts)
        + "\n\n반드시 JSON만 출력하세요."
    )

    return [
        {"role": "system", "content": STYLE_PROFILE_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_style_profile_from_llm(llm_response: str) -> list[dict]:
    """AI 응답에서 style profile JSON을 파싱합니다."""
    import re
    text = llm_response.strip()
    # ```json ... ``` 블록 추출
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 배열 직접 시도
        try:
            data = json.loads(f'{{"profiles": {text}}}')
        except json.JSONDecodeError:
            log.warning("[STYLE-PROFILE] JSON 파싱 실패")
            return []

    if isinstance(data, dict):
        return data.get("profiles", [])
    if isinstance(data, list):
        return data
    return []


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
        }

    # 1f 결과 병합: verified + explicit인 것만 우선 사용
    if marker_policy_1f:
        for role_entry in marker_policy_1f.get("roles", []):
            role = role_entry.get("role", "")
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
            chapters_gen.append(ch_entry)

        _write("08_2b_generation_by_chapter.json", {
            "chapter_count": len(chapters_gen),
            "chapters": chapters_gen,
        })
    else:
        _skip("08_2b_generation_by_chapter.json")

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
    # 12b. Style profile observation (Stage 11.2)
    # ═══════════════════════════════════════════════════════════════
    _sp_data = debug_payload.get("style_profiles")
    if _sp_data:
        _write("12b_style_profile.json", _sp_data)
    else:
        _skip("12b_style_profile.json")

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

- `marker_style: fixed` — markers_sample의 **첫 마커**를 매번 사용
- `marker_style: enumerate` — markers_sample의 **순서**를 유지하고, 샘플 길이를 넘어가면 **자연스럽게 확장**:
  - 마커 시퀀스 패턴 (유니코드 +1, 반복 확장, 번호 증가 등) 보고 일관 유지
  - 예: 첫 3개 sample 보면 4번째가 어떻게 와야 할지 추론 가능
  - **절대 다시 sample의 첫 마커로 돌아가지 마세요** (단조 증가)

## 들여쓰기 — 신경 쓰지 마세요

출력 text에 **앞 공백/탭 넣지 마세요**. 조립 단계에서 자동 부착됩니다.

text 구성: marker (해당 role의 markers_sample 참고) + separator + 본문 내용
- 마커 있는 role: 마커 + 공백 + 본문
- 마커 없는 role: 본문만

## 텍스트 작성 규칙
- **role의 description이나 번호("과제 1", "전략 2" 등)를 텍스트에 넣지 마세요** — description은 role 선택의 참고용이며 출력 텍스트에 포함하면 안 됩니다
- 소스의 실제 내용만 작성하세요
- 소스의 원래 마커는 제거하고 양식 role의 markers_sample을 사용하세요

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
  - 패턴 트리의 최상위 role (root role)은 `parent_id: null`
  - root role item이 여러 개 있을 수 있음 — 각각 `parent_id: null`
  - root가 아닌 항목은 반드시 부모 item의 id를 parent_id로 지정
- **패턴 트리의 계층 관계를 parent_id로 정확히 표현하세요**
  - root role의 자식은 parent_id = 해당 root의 id
  - 같은 부모 아래 형제 항목은 parent_id가 같음
- role과 text는 양식의 role 카탈로그·format_rules에 따라 결정. role 이름은 양식 카탈로그에 있는 그대로 사용.

## 중요
- **소스에 없는 내용을 만들어내지 마세요**
- **소스의 해당 섹션 내용을 빠짐없이 반영하세요**
- **하나의 role 항목에는 하나의 계층 내용만** — 여러 계층을 합치지 마세요
- 양식 샘플과 비슷한 길이/문체를 유지하세요
- 반드시 JSON만 출력. 다른 설명 포함 금지
"""


def _format_pattern_tree(
    pattern: dict,
    role_markers: dict,
    indent: int = 0,
    role_text_types: dict | None = None,
    per_type_semantics: dict | None = None,
    chapter_type_name: str = "",
) -> str:
    """패턴 트리를 사람이 읽기 좋은 텍스트로 변환.

    per_type semantics가 있으면 해당 type context의 description과 text_type 사용.
    없으면 role_text_types(global) fallback.
    """
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
        # 개수 제약
        if per_parent == "single":
            flags.append("정확히 1개/부모")
        else:
            flags.append("여러 개 가능")
        if optional:
            flags.append("선택(생략 가능)")
        else:
            flags.append("필수(최소 1개)")
        if suggested and suggested > 0:
            flags.append(f"권장 개수 약 {suggested}")
        if observed:
            observed_preview = observed[:6]
            more = "…" if len(observed) > len(observed_preview) else ""
            flags.append(f"관찰={observed_preview}{more}")
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
        flags_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"{prefix}- {role_name}{marker_str}{flags_str}")
        if children:
            lines.append(_format_pattern_tree(
                children, role_markers, indent + 1,
                role_text_types, per_type_semantics, chapter_type_name,
            ))
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

    # 패턴 트리 텍스트
    pattern_text = _format_pattern_tree(
        pattern, role_markers,
        role_text_types=role_text_types,
        per_type_semantics=per_type_role_semantics,
        chapter_type_name=chapter_type_name,
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


def build_chapter_trees(
    section_fills: list[dict],
) -> list[list[dict]]:
    """
    validated section_fill 결과를 chapter 단위 tree node list로 변환합니다.

    assemble_hwpx_hybrid의 chapter_trees 파라미터용.
    chapter title은 포함하지 않습니다 (body items만).

    Args:
        section_fills: [{"items": [...], "chapter_context": {...}, ...}, ...]
            각 items는 validate → fallback 완료된 normalized items

    Returns:
        [[{"id": N, "parent_id": M, "role": ..., "text": ...}, ...], ...]
        chapter 당 하나의 list, title 제외한 body nodes만 포함
    """
    chapter_trees = []
    for sf in section_fills:
        items = sf.get("items", [])
        nodes = []
        for it in items:
            nodes.append({
                "id": it["id"],
                "parent_id": it["parent_id"],
                "role": it["role"],
                "text": it["text"],
            })
        chapter_trees.append(nodes)
    return chapter_trees


def process_section_fill_result(
    llm_response: str,
    ch_idx: int,
    ch_title: str,
    ch_type: str,
    title_role: str,
    template_grammar: dict,
    role_text_types: dict | None = None,
    pattern_roles: list | None = None,
    section_pdf_text_len: int = 0,
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
    raw_items = parse_section_fill_from_llm(llm_response)
    log.info(f"2b[{ch_idx}] 완료: {ch_title} → {len(raw_items)}개 항목")

    # 2. grammar 정보 추출
    _type_grammar_info = template_grammar.get("per_type", {}).get(ch_type, {})
    _type_grammar = _type_grammar_info.get("grammar", {})
    _root_roles = _type_grammar_info.get("root_roles", [])

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
    }
