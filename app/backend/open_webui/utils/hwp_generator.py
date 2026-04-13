"""
HWPX 문서 생성 모듈 — AI 명령 JSON 기반 동적 생성

python-hwpx 라이브러리를 사용하여 양식 기반으로 문서를 생성합니다.
"""

import io
import logging
from dataclasses import dataclass, field

from hwpx import HwpxDocument
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


@dataclass
class HwpxResult:
    """HWPX 생성 결과"""
    data: bytes
    success_count: int = 0
    fail_count: int = 0
    errors: list[str] = field(default_factory=list)

def _execute_set_cell(doc, action: dict):
    """표 셀 텍스트 교체 (행/열 부족 시 자동 확장)"""
    from copy import deepcopy

    table_idx = action["table"]
    row = action["row"]
    col = action["col"]
    text = action["text"]

    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    tbl = tables_found[table_idx]
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    # 행 부족 시 자동 확장
    current_rows = int(tbl.element.get("rowCnt", "1"))
    if row >= current_rows:
        tr_elements = tbl.element.findall(f"{NS}tr")
        if not tr_elements:
            raise ValueError(f"표 {table_idx}에 행이 없음")
        last_row_elem = tr_elements[-1]
        for i in range(current_rows, row + 1):
            new_row_elem = deepcopy(last_row_elem)
            for tc in new_row_elem.iter(f"{NS}tc"):
                for addr in tc.iter(f"{NS}cellAddr"):
                    addr.set("rowAddr", str(i))
                for span in tc.iter(f"{NS}cellSpan"):
                    span.set("rowSpan", "1")
                    span.set("colSpan", "1")
                for t_elem in tc.iter(f"{NS}t"):
                    t_elem.text = ""
            tbl.element.append(new_row_elem)
        tbl.element.set("rowCnt", str(row + 1))
        log.info(f"표 {table_idx} 행 확장: {current_rows} → {row + 1}")

    # 열 부족 시 자동 확장
    current_cols = int(tbl.element.get("colCnt", "1"))
    if col >= current_cols:
        _adjust_table_columns(tbl.element, col + 1)

    try:
        tbl.set_cell_text(row, col, text)
    except TypeError:
        # lxml/stdlib SubElement 호환 문제 우회: 직접 XML 레벨로 텍스트 교체
        tr_elements = tbl.element.findall(f"{NS}tr")
        if row < len(tr_elements):
            tcs = tr_elements[row].findall(f"{NS}tc")
            if col < len(tcs):
                for t_elem in tcs[col].iter(f"{NS}t"):
                    t_elem.text = str(text)
                    break
            else:
                raise IndexError(f"열 {col} 범위 초과 (표 {table_idx})")
        else:
            raise IndexError(f"행 {row} 범위 초과 (표 {table_idx})")

    # 셀 안에 여러 문단이 있으면 첫 번째만 남기고 나머지 제거
    try:
        cell = tbl.cell(row, col)
        cell_paras = cell.paragraphs
        for cp in cell_paras[1:]:
            cp.remove()
    except Exception:
        pass


def _execute_clear_body(doc, action: dict):
    """지정 문단부터 끝까지 삭제"""
    from_para = action.get("from_paragraph", 0)
    body_paras = list(doc.paragraphs[from_para:])
    for p in reversed(body_paras):
        p.remove()


def _execute_set_paragraph_text(doc, action: dict):
    """기존 문단의 텍스트만 교체 (서식 완전 보존)"""
    idx = action.get("index")
    text = action.get("text", "")

    if idx is None:
        raise ValueError("set_paragraph_text에 index 필수")
    if idx < 0 or idx >= len(doc.paragraphs):
        raise IndexError(
            f"문단 인덱스 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)"
        )

    doc.paragraphs[idx].text = text


def _execute_add_paragraph(doc, action: dict):
    """문단 추가"""
    text = action.get("text", "")
    para_pr = action.get("paraPrIDRef", "0")
    char_pr = action.get("charPrIDRef", "0")

    doc.add_paragraph(
        text,
        para_pr_id_ref=str(para_pr),
        char_pr_id_ref=str(char_pr),
    )


def _execute_add_table(doc, action: dict):
    """표 추가"""
    rows = action.get("rows", 2)
    cols = action.get("cols", 2)
    cells = action.get("cells", [])
    border_fill = action.get("borderFillIDRef", "3")

    table = doc.add_table(
        rows=rows,
        cols=cols,
        border_fill_id_ref=str(border_fill),
    )

    # 셀 내용 채우기
    for r_idx, row_data in enumerate(cells):
        for c_idx, cell_text in enumerate(row_data):
            if r_idx < rows and c_idx < cols:
                try:
                    table.set_cell_text(r_idx, c_idx, str(cell_text))
                except Exception as e:
                    log.warning(f"셀({r_idx},{c_idx}) 설정 실패: {e}")

    return table


def _execute_remove_paragraph(doc, action: dict):
    """특정 문단 삭제"""
    idx = action.get("index", -1)
    if 0 <= idx < len(doc.paragraphs):
        doc.paragraphs[idx].remove()
    else:
        raise IndexError(f"문단 인덱스 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)")


def _adjust_table_columns(tbl_elem, target_cols: int):
    """
    테이블의 컬럼 수를 target_cols에 맞게 동적 조절합니다.
    - target_cols > 현재 → 마지막 셀을 복제하여 열 추가 (모든 기존 행)
    - target_cols < 현재 → 초과 셀을 제거 (모든 기존 행)
    """
    from copy import deepcopy

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    current_cols = int(tbl_elem.get("colCnt", "1"))

    if current_cols == target_cols:
        return

    for tr in tbl_elem.findall(f"{NS}tr"):
        tcs = tr.findall(f"{NS}tc")
        if not tcs:
            continue

        if target_cols > current_cols:
            last_tc = tcs[-1]
            for i in range(target_cols - current_cols):
                new_tc = deepcopy(last_tc)
                new_col = current_cols + i
                for addr in new_tc.iter(f"{NS}cellAddr"):
                    addr.set("colAddr", str(new_col))
                for span in new_tc.iter(f"{NS}cellSpan"):
                    span.set("colSpan", "1")
                    span.set("rowSpan", "1")
                for t_elem in new_tc.iter(f"{NS}t"):
                    t_elem.text = ""
                tr.append(new_tc)
        else:
            for tc in reversed(tcs[target_cols:]):
                tr.remove(tc)

    tbl_elem.set("colCnt", str(target_cols))
    log.info(f"테이블 컬럼 조절: {current_cols} → {target_cols}")


def _execute_add_row(doc, action: dict):
    """기존 표에 행 추가 (마지막 행을 복제하여 추가, 컬럼 수 자동 조절)"""
    from copy import deepcopy

    table_idx = action["table"]
    count = action.get("count", 1)
    cells = action.get("cells", None)  # [["셀1","셀2",...]] 행별 셀 내용

    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    tbl = tables_found[table_idx]
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    if cells:
        # 데이터 컬럼 수와 테이블 컬럼 수가 다르면 자동 조절
        data_cols = max(len(row) for row in cells)
        current_cols = int(tbl.element.get("colCnt", "1"))
        if data_cols != current_cols:
            _adjust_table_columns(tbl.element, data_cols)
        # cells 행 수가 count보다 많으면 cells 기준으로
        if len(cells) > count:
            count = len(cells)

    # 컬럼 조절 이후의 마지막 행을 복제 대상으로 사용
    tr_elements = tbl.element.findall(f"{NS}tr")
    if not tr_elements:
        raise ValueError(f"표 {table_idx}에 행이 없음")
    last_row_elem = tr_elements[-1]

    for r in range(count):
        new_row_elem = deepcopy(last_row_elem)

        # 새 행의 rowAddr 업데이트 + 셀 병합 초기화
        cur_row_cnt = int(tbl.element.get("rowCnt", "0"))
        for tc in new_row_elem.iter(f"{NS}tc"):
            # rowAddr 갱신
            for addr in tc.iter(f"{NS}cellAddr"):
                addr.set("rowAddr", str(cur_row_cnt))
            # 셀 병합 초기화 (rowSpan=1, colSpan=1)
            for span in tc.iter(f"{NS}cellSpan"):
                span.set("rowSpan", "1")
                span.set("colSpan", "1")

        tbl.element.append(new_row_elem)

        # rowCnt 업데이트
        tbl.element.set("rowCnt", str(cur_row_cnt + 1))

        # 셀 내용 직접 설정 (XML 레벨)
        if cells and r < len(cells):
            tcs = list(new_row_elem.iter(f"{NS}tc"))
            for c_idx, cell_text in enumerate(cells[r]):
                if c_idx < len(tcs):
                    # 첫 번째 <hp:t> 요소의 텍스트를 교체
                    for t_elem in tcs[c_idx].iter(f"{NS}t"):
                        t_elem.text = str(cell_text)
                        break


def _execute_remove_table(doc, action: dict):
    """표 삭제 (표를 포함하는 문단을 삭제)"""
    table_idx = action["table"]

    tables_found = []
    table_para_map = {}  # table → paragraph index
    for i, p in enumerate(doc.paragraphs):
        for t in p.tables:
            table_para_map[len(tables_found)] = i
            tables_found.append(t)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    para_idx = table_para_map[table_idx]
    doc.paragraphs[para_idx].remove()


def _execute_insert_paragraph(doc, action: dict):
    """특정 위치에 문단 삽입 (index 번째 문단 앞에 삽입)"""
    from copy import deepcopy

    idx = action.get("index", 0)
    text = action.get("text", "")
    para_pr = str(action.get("paraPrIDRef", "0"))
    char_pr = str(action.get("charPrIDRef", "0"))

    if idx < 0 or idx >= len(doc.paragraphs):
        raise IndexError(f"삽입 위치 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)")

    target = doc.paragraphs[idx]
    section_elem = target.element.getparent()
    target_pos = list(section_elem).index(target.element)

    # 대상 문단 복제 후 내용/스타일 교체
    new_elem = deepcopy(target.element)
    new_elem.set("paraPrIDRef", para_pr)

    # 텍스트 설정
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    for run in new_elem.iter(f"{NS}run"):
        run.set("charPrIDRef", char_pr)
        for t in run.iter(f"{NS}t"):
            t.text = text
        break  # 첫 번째 run만 사용
    else:
        # run이 없으면 생성하지 않고 add_paragraph 방식 사용
        section_elem.insert(target_pos, new_elem)
        return

    section_elem.insert(target_pos, new_elem)


def _execute_clone_paragraph(doc, action: dict):
    """기존 문단을 복제하여 바로 뒤에 삽입 (표/텍스트 상자 포함 전체 구조 보존).
    복제 후 텍스트를 교체합니다."""
    from copy import deepcopy

    source_idx = action.get("source")
    text = action.get("text", "")

    if source_idx is None:
        raise ValueError("clone_paragraph에 source 필수")
    if source_idx < 0 or source_idx >= len(doc.paragraphs):
        raise IndexError(
            f"복제 원본 인덱스 {source_idx} 범위 초과 (전체 {len(doc.paragraphs)}개)"
        )

    source = doc.paragraphs[source_idx]
    section_elem = source.element.getparent()
    source_pos = list(section_elem).index(source.element)

    # 문단 전체 복제 (표, 텍스트 상자, 서식 모두 포함)
    new_elem = deepcopy(source.element)

    # 텍스트 교체
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    if text:
        # 표가 있는 문단이면 표 안의 첫 번째 셀 텍스트 교체
        tbl = new_elem.find(f".//{NS}tbl")
        if tbl is not None:
            for t_elem in tbl.iter(f"{NS}t"):
                if t_elem.text is not None or t_elem.getparent().tag.endswith("}run"):
                    t_elem.text = text
                    break
        else:
            # 일반 문단이면 첫 번째 run의 텍스트 교체
            for t_elem in new_elem.iter(f"{NS}t"):
                t_elem.text = text
                break

    # 원본 바로 뒤에 삽입
    section_elem.insert(source_pos + 1, new_elem)
    log.info(f"문단 {source_idx} 복제 완료 (텍스트: {text[:50]}...)")


# 명령 타입 → 실행 함수 매핑
ACTION_HANDLERS = {
    "set_cell": _execute_set_cell,
    "set_paragraph_text": _execute_set_paragraph_text,
    "clear_body": _execute_clear_body,
    "add_paragraph": _execute_add_paragraph,
    "add_table": _execute_add_table,
    "remove_paragraph": _execute_remove_paragraph,
    "add_row": _execute_add_row,
    "remove_table": _execute_remove_table,
    "clone_paragraph": _execute_clone_paragraph,
    "insert_paragraph": _execute_insert_paragraph,
}

# 비구조적 액션 (문단 인덱스 변경 없음 → 먼저 실행)
_NONSTRUCTURAL = {"set_cell", "set_paragraph_text", "add_row"}
# 구조 삭제 (높은 인덱스부터 → 시프트 방지)
_STRUCTURAL_REMOVE = {"remove_paragraph", "remove_table"}
# 구조 삽입 (높은 인덱스부터 → 시프트 방지)
_STRUCTURAL_INSERT = {"insert_paragraph", "clone_paragraph"}
# 범위 삭제
_STRUCTURAL_CLEAR = {"clear_body"}
# 문서 끝 추가 (마지막에 실행)
_APPEND = {"add_paragraph", "add_table"}


def _get_action_index(action: dict) -> int:
    """액션에서 정렬용 인덱스 추출"""
    return action.get("index", action.get("table", action.get("from_paragraph", 0)))


def _sort_actions(actions: list[dict]) -> list[tuple[int, dict]]:
    """
    액션을 안전한 실행 순서로 정렬합니다.
    인덱스 시프트를 방지하기 위해:

    Phase 1: 비구조적 (set_cell, set_paragraph_text, add_row)
             → 인덱스 변경 없으므로 원래 순서 유지
    Phase 2: 삭제 (remove_paragraph, remove_table)
             → 높은 인덱스부터 처리하여 시프트 방지
    Phase 3: 삽입 (insert_paragraph)
             → 높은 인덱스부터 처리하여 시프트 방지
    Phase 4: 범위 삭제 (clear_body)
    Phase 5: 추가 (add_paragraph, add_table)
             → 문서 끝에 붙으므로 원래 순서 유지

    Returns:
        [(원래_번호, action)] 리스트
    """
    p1, p2, p3, p4, p5 = [], [], [], [], []

    for i, a in enumerate(actions):
        t = a.get("type")
        if t in _NONSTRUCTURAL:
            p1.append((i, a))
        elif t in _STRUCTURAL_REMOVE:
            p2.append((i, a))
        elif t in _STRUCTURAL_INSERT:
            p3.append((i, a))
        elif t in _STRUCTURAL_CLEAR:
            p4.append((i, a))
        elif t in _APPEND:
            p5.append((i, a))
        else:
            p1.append((i, a))  # 알 수 없는 타입 → 비구조적 취급

    p2.sort(key=lambda x: _get_action_index(x[1]), reverse=True)
    p3.sort(key=lambda x: _get_action_index(x[1]), reverse=True)

    return p1 + p2 + p3 + p4 + p5


def _clear_unmodified_fields(doc, structure, modified_paragraphs, modified_cells):
    """구조 분석 결과를 기반으로 미수정 editable 필드를 비웁니다."""
    cleared = 0

    # 문단 클리어
    for p_info in structure.get("paragraphs", []):
        idx = p_info.get("idx")
        desc = p_info.get("description", "")

        if "고정 텍스트" in desc or "수정 불필요" in desc:
            continue

        if idx is not None and idx not in modified_paragraphs:
            if 0 <= idx < len(doc.paragraphs):
                try:
                    doc.paragraphs[idx].text = ""
                    cleared += 1
                except Exception:
                    pass

    # 표 셀 클리어 — 셀 안의 모든 문단을 제거하고 빈 문단 하나만 남김
    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    for t_info in structure.get("tables", []):
        table_idx = t_info.get("table")
        for vc in t_info.get("value_cells", []):
            r, c = vc.get("row"), vc.get("col")
            if (table_idx, r, c) not in modified_cells:
                if table_idx is not None and table_idx < len(tables_found):
                    try:
                        cell = tables_found[table_idx].cell(r, c)
                        # 셀 내 모든 문단의 텍스트를 비우고 첫 번째만 남김
                        cell_paras = cell.paragraphs
                        for cp in cell_paras:
                            cp.text = ""
                        for cp in cell_paras[1:]:
                            cp.remove()
                        cleared += 1
                    except Exception:
                        pass

    if cleared > 0:
        log.info(f"미수정 필드 {cleared}개 클리어")


def generate_hwpx_dynamic(
    template_source,
    actions: list[dict],
    structure: dict = None,
    removed_indices: list[int] = None,
) -> HwpxResult:
    """
    AI가 생성한 명령 리스트를 기반으로 HWPX 문서를 동적 생성합니다.

    Args:
        template_source: 양식 HWPX 파일 경로(str) 또는 bytes
        actions: AI가 출력한 명령 리스트
        structure: 1차 구조 분석 결과 (미수정 필드 자동 클리어용)
        removed_indices: truncate_xml()이 제거한 원본 _idx 목록.
            전달 시 실제 문서에서도 해당 문단을 제거하여
            LLM이 본 축소 XML과 동일한 구조로 맞춤.

    Returns:
        HwpxResult(data=bytes, success_count, fail_count, errors)
    """
    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    # 문서를 LLM이 본 축소 구조와 동일하게 맞춤
    if removed_indices:
        before_count = len(doc.paragraphs)
        for idx in sorted(removed_indices, reverse=True):
            if 0 <= idx < len(doc.paragraphs):
                doc.paragraphs[idx].remove()
        log.info(
            f"문서 축소: {before_count}개 → {len(doc.paragraphs)}개 문단 "
            f"({len(removed_indices)}개 제거)"
        )

    sorted_actions = _sort_actions(actions)
    success_count = 0
    fail_count = 0
    errors = []
    modified_paragraphs = set()
    modified_cells = set()

    for orig_idx, action in sorted_actions:
        action_type = action.get("type")
        handler = ACTION_HANDLERS.get(action_type)

        if handler is None:
            log.warning(f"알 수 없는 명령 타입: {action_type} (#{orig_idx})")
            errors.append(f"#{orig_idx} {action_type}: 알 수 없는 명령")
            fail_count += 1
            continue

        try:
            handler(doc, action)
            log.info(f"명령 #{orig_idx} 실행 완료: {action_type}")
            success_count += 1
            if action_type == "set_paragraph_text":
                modified_paragraphs.add(action.get("index"))
            elif action_type == "set_cell":
                modified_cells.add((action["table"], action["row"], action["col"]))
        except Exception as e:
            log.warning(f"명령 #{orig_idx} 실행 실패: {action_type} - {e}")
            errors.append(f"#{orig_idx} {action_type}: {e}")
            fail_count += 1

    log.info(
        f"명령 실행 결과: 성공 {success_count}/{len(actions)}, 실패 {fail_count}"
        + (f" [{'; '.join(errors)}]" if errors else "")
    )

    if success_count == 0 and actions:
        raise RuntimeError(
            f"모든 명령 실행 실패 ({fail_count}개): {'; '.join(errors)}"
        )

    # 미수정 editable 필드 자동 클리어
    # clear_body가 사용된 경우 문서 구조가 완전히 바뀌었으므로 클리어 건너뜀
    has_clear_body = any(a.get("type") == "clear_body" for a in actions)
    if structure and not has_clear_body:
        _clear_unmodified_fields(doc, structure, modified_paragraphs, modified_cells)

    return HwpxResult(
        data=doc.to_bytes(),
        success_count=success_count,
        fail_count=fail_count,
        errors=errors,
    )


# ============================================================
# 역할 기반 문서 조립 (v2)
# ============================================================


def assemble_hwpx(
    template_source,
    style_catalog: dict,
    role_map: dict,
    content: dict,
) -> HwpxResult:
    """
    역할 기반으로 HWPX 문서를 조립합니다.

    1. 양식을 열고 각 서식 그룹의 exemplar 문단 요소를 저장
    2. 본문 영역을 비움
    3. header(제목/날짜/기관) 설정
    4. body 항목마다 해당 역할의 exemplar를 복제 + 텍스트 교체
    5. 완성된 문서를 bytes로 반환

    Args:
        template_source: 양식 HWPX 파일 경로(str), bytes, 또는 file-like
        style_catalog: extract_style_groups() 반환값
        role_map: parse_role_interpret_from_llm() 반환값 {gid: {role, label}}
        content: parse_role_content_from_llm() 반환값 {header, body}

    Returns:
        HwpxResult(data=bytes, success_count, fail_count, errors)
    """
    from copy import deepcopy
    from lxml import etree

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    groups = style_catalog["groups"]
    errors = []
    success_count = 0

    # ── 1단계: exemplar 요소 저장 (deepcopy) ──
    exemplars = {}  # gid → deepcopy된 XML element
    for gid, g in groups.items():
        eidx = g["exemplar_idx"]
        if 0 <= eidx < len(doc.paragraphs):
            exemplars[gid] = deepcopy(doc.paragraphs[eidx].element)
            log.debug(f"exemplar 저장: {gid} (idx={eidx})")

    # spacer exemplar도 저장 (spacer 역할이 있으면)
    spacer_gid = None
    for gid, info in role_map.items():
        if info.get("role") == "spacer" and gid in exemplars:
            spacer_gid = gid
            break

    # ── 2단계: header 영역 처리 (title, meta) ──
    header_data = content.get("header", {})
    title_text = header_data.get("title", "")
    meta_items = header_data.get("meta", [])
    if isinstance(meta_items, str):
        meta_items = [meta_items]

    # title/meta 역할의 그룹 찾기
    title_gids = [gid for gid, info in role_map.items() if info.get("role") == "title"]
    meta_gids = [gid for gid, info in role_map.items() if info.get("role") == "meta"]

    # header 영역: 원본에서 title/meta 문단의 idx 수집
    header_indices = set()
    for gid in title_gids + meta_gids:
        if gid in groups:
            header_indices.update(groups[gid]["indices"])

    # title 문단 텍스트 교체
    for gid in title_gids:
        if gid in groups and title_text:
            for idx in groups[gid]["indices"]:
                if 0 <= idx < len(doc.paragraphs):
                    try:
                        _set_element_text(doc.paragraphs[idx], title_text, NS)
                        success_count += 1
                    except Exception as e:
                        errors.append(f"title({idx}): {e}")

    # meta 문단 텍스트 교체
    meta_idx = 0
    for gid in meta_gids:
        if gid in groups:
            for idx in groups[gid]["indices"]:
                if meta_idx < len(meta_items) and 0 <= idx < len(doc.paragraphs):
                    try:
                        _set_element_text(doc.paragraphs[idx], meta_items[meta_idx], NS)
                        success_count += 1
                        meta_idx += 1
                    except Exception as e:
                        errors.append(f"meta({idx}): {e}")

    # ── 3단계: 본문 영역 비우기 (header 제외) ──
    section_elem = doc.paragraphs[0].element.getparent()
    body_elements = []
    for i, p in enumerate(doc.paragraphs):
        if i not in header_indices:
            body_elements.append(p.element)

    for elem in body_elements:
        section_elem.remove(elem)

    log.info(f"본문 {len(body_elements)}개 문단 제거, header {len(header_indices)}개 보존")

    # ── 4단계: body 항목으로 문서 재조립 ──
    body_items = content.get("body", [])

    for item in body_items:
        gid = item.get("group", "")
        text = item.get("text", "")

        if gid not in exemplars:
            # 지정된 그룹이 없으면 가장 가까운 역할의 그룹 찾기
            errors.append(f"unknown group '{gid}', skipping: {text[:30]}")
            continue

        # exemplar 복제
        new_elem = deepcopy(exemplars[gid])

        # 텍스트 교체
        try:
            _set_cloned_element_text(new_elem, text, NS, groups[gid].get("is_table_box", False))
            section_elem.append(new_elem)
            success_count += 1
        except Exception as e:
            errors.append(f"assemble({gid}): {e}")

    log.info(
        f"문서 조립 완료: 성공 {success_count}, 실패 {len(errors)}, "
        f"body 항목 {len(body_items)}개"
    )

    return HwpxResult(
        data=doc.to_bytes(),
        success_count=success_count,
        fail_count=len(errors),
        errors=errors,
    )


def assemble_hwpx_hybrid(
    template_source,
    structure: dict,
    content: dict,
    removed_indices: list[int] = None,
) -> HwpxResult:
    """
    하이브리드 방식으로 HWPX 문서를 조립합니다.

    v1 구조 분석(idx + role) + v2 조립(exemplar 복제).

    1. structure에서 role → exemplar idx 매핑 생성
    2. 양식을 열고 exemplar 문단 요소를 deepcopy로 저장
    3. header 문단 텍스트 교체
    4. 본문 영역 비우기
    5. body 항목마다 role의 exemplar를 복제 + 텍스트 교체
    6. 완성된 문서를 bytes로 반환

    Args:
        template_source: 양식 HWPX 파일 경로(str), bytes, 또는 file-like
        structure: parse_structure_from_llm() 반환값 (role 포함)
                   {"paragraphs": [{"idx": N, "role": "...", ...}], "tables": [...]}
        content: parse_role_content_from_structure_llm() 반환값
                 {"header": {"title": ..., "date": ..., "org": ...}, "body": [{"role": ..., "text": ...}]}
        removed_indices: truncate_xml()에서 제거된 인덱스 목록

    Returns:
        HwpxResult(data=bytes, success_count, fail_count, errors)
    """
    from copy import deepcopy
    from lxml import etree

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    paragraphs_info = structure.get("paragraphs", [])
    errors = []
    success_count = 0

    # ── 1단계: role → exemplar idx 매핑 (각 role의 첫 번째 idx를 exemplar로) ──
    role_exemplar_idx = {}  # role → idx
    role_is_table_box = {}  # role → bool
    header_roles = {"cover_title", "cover_date", "cover_org", "cover_subtitle"}
    skip_roles = {"spacer", "toc", "fixed"}

    for p in paragraphs_info:
        role = p.get("role", "")
        idx = p.get("idx", -1)
        if role and role not in role_exemplar_idx and role not in skip_roles:
            role_exemplar_idx[role] = idx

    # 표 구조 확인 (1x1 = 텍스트 상자)
    tables_info = structure.get("tables", [])
    table_box_indices = set()
    for t in tables_info:
        if t.get("rows", 0) == 1 and t.get("cols", 0) == 1:
            # 이 표가 속한 문단의 idx를 찾기
            tbl_idx = t.get("table", -1)
            for p in paragraphs_info:
                if p.get("idx", -1) >= 0:
                    table_box_indices.add(p.get("idx"))

    # 실제로 1x1 표인지는 문단에 표가 있는지로 판별
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            para = doc.paragraphs[idx]
            role_is_table_box[role] = bool(para.tables)

    log.info(
        f"role→exemplar 매핑: {len(role_exemplar_idx)}개 role, "
        f"table_box: {sum(role_is_table_box.values())}개"
    )

    # ── 2단계: exemplar 요소 저장 (deepcopy) ──
    exemplars = {}  # role → deepcopy된 XML element
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            exemplars[role] = deepcopy(doc.paragraphs[idx].element)
            log.debug(f"exemplar 저장: {role} (idx={idx})")

    # ── 3단계: header 영역 처리 ──
    header_data = content.get("header", {})
    title_text = header_data.get("title", "")
    date_text = header_data.get("date", "")
    org_text = header_data.get("org", "")

    # header에 해당하는 idx 수집 + 텍스트 교체
    header_indices = set()
    header_field_map = {
        "cover_title": title_text,
        "cover_date": date_text,
        "cover_org": org_text,
        "cover_subtitle": header_data.get("subtitle", ""),
    }

    for p in paragraphs_info:
        role = p.get("role", "")
        idx = p.get("idx", -1)
        if role in header_field_map:
            header_indices.add(idx)
            text = header_field_map[role]
            if text and 0 <= idx < len(doc.paragraphs):
                try:
                    _set_element_text(doc.paragraphs[idx], text, NS)
                    success_count += 1
                except Exception as e:
                    errors.append(f"header({role}, idx={idx}): {e}")

    # toc, fixed, spacer도 header로 취급 (보존 또는 제거 판단)
    toc_indices = set()
    for p in paragraphs_info:
        role = p.get("role", "")
        idx = p.get("idx", -1)
        if role in skip_roles:
            if role == "toc":
                toc_indices.add(idx)
            elif role == "fixed":
                header_indices.add(idx)  # fixed는 보존

    # ── 4단계: 본문 영역 비우기 (header + fixed 제외) ──
    section_elem = doc.paragraphs[0].element.getparent()
    body_elements = []
    for i, p in enumerate(doc.paragraphs):
        if i not in header_indices:
            body_elements.append(p.element)

    for elem in body_elements:
        section_elem.remove(elem)

    log.info(
        f"본문 {len(body_elements)}개 문단 제거, "
        f"header {len(header_indices)}개 보존"
    )

    # ── 5단계: body 항목으로 문서 재조립 ──
    body_items = content.get("body", [])

    for item in body_items:
        role = item.get("role", "")
        text = item.get("text", "")

        if role not in exemplars:
            errors.append(f"unknown role '{role}', skipping: {text[:50]}")
            continue

        # exemplar 복제
        new_elem = deepcopy(exemplars[role])

        # 텍스트 교체
        try:
            is_tbl_box = role_is_table_box.get(role, False)
            _set_cloned_element_text(new_elem, text, NS, is_tbl_box)
            section_elem.append(new_elem)
            success_count += 1
        except Exception as e:
            errors.append(f"assemble({role}): {e}")

    log.info(
        f"하이브리드 조립 완료: 성공 {success_count}, 실패 {len(errors)}, "
        f"body 항목 {len(body_items)}개"
    )

    return HwpxResult(
        data=doc.to_bytes(),
        success_count=success_count,
        fail_count=len(errors),
        errors=errors,
    )


def _set_element_text(para, text: str, NS: str):
    """기존 문단(HwpxOxmlParagraph)의 텍스트를 교체합니다."""
    # 표가 있는 문단이면 첫 번째 셀의 텍스트 교체
    if para.tables:
        tbl = para.tables[0]
        cell = tbl.cell(0, 0)
        cell_paras = cell.paragraphs
        if cell_paras:
            cell_paras[0].text = text
            for cp in cell_paras[1:]:
                cp.remove()
        return

    # 일반 문단
    para.text = text


def _set_cloned_element_text(elem, text: str, NS: str, is_table_box: bool):
    """deepcopy된 XML 요소의 텍스트를 교체합니다."""
    if is_table_box:
        # 1행 표 내부의 텍스트 교체
        # 마지막 셀의 텍스트를 교체 (첫 셀이 번호/마커인 경우)
        trs = elem.findall(f".//{NS}tr")
        if trs:
            tcs = trs[0].findall(f"{NS}tc")
            if tcs:
                # 텍스트가 있는 마지막 셀을 교체 대상으로
                target_tc = tcs[-1] if len(tcs) > 1 else tcs[0]
                sublist = target_tc.find(f"{NS}subList")
                if sublist is not None:
                    paras = sublist.findall(f"{NS}p")
                    if paras:
                        # 첫 번째 문단의 텍스트 교체
                        _replace_text_in_paragraph_elem(paras[0], text, NS)
                        # 나머지 문단 제거
                        for p in paras[1:]:
                            sublist.remove(p)
                    return

    # 일반 문단의 텍스트 교체
    _replace_text_in_paragraph_elem(elem, text, NS)


def _replace_text_in_paragraph_elem(p_elem, text: str, NS: str):
    """XML paragraph 요소 내부의 텍스트를 교체합니다. 첫 run만 남기고 나머지 run 제거."""
    runs = p_elem.findall(f"{NS}run")
    if not runs:
        return

    # 첫 번째 run의 텍스트 교체
    first_run = runs[0]
    t_elem = first_run.find(f"{NS}t")
    if t_elem is not None:
        t_elem.text = text
        # t 하위의 탭/특수문자 요소 제거
        for child in list(t_elem):
            t_elem.remove(child)
    else:
        t_elem = etree.SubElement(first_run, f"{NS}t")
        t_elem.text = text

    # ctrl 요소가 있는 run은 보존 (header, footer, pageNum 등)
    for run in runs[1:]:
        has_ctrl = run.find(f"{NS}ctrl") is not None
        if not has_ctrl:
            p_elem.remove(run)
