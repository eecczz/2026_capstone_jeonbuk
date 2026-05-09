"""
HWPX 문서 생성 모듈 — AI 명령 JSON 기반 동적 생성

python-hwpx 라이브러리를 사용하여 양식 기반으로 문서를 생성합니다.
"""

import io
import logging
from dataclasses import dataclass, field

from hwpx.document import HwpxDocument
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _build_parent_map(root):
    """stdlib ElementTree용 parent map 생성 (lxml getparent() 대체)."""
    return {c: p for p in root.iter() for c in p}


def _get_parent(elem, root):
    """elem의 부모를 반환. lxml getparent() 대체."""
    return _build_parent_map(root).get(elem)


def _doc_to_bytes(doc: HwpxDocument) -> bytes:
    """HwpxDocument → bytes. save(BytesIO) 경유."""
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


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
    section_elem = doc.oxml._sections[0].element
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
    section_elem = doc.oxml._sections[0].element
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
                if t_elem.text is not None:
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
        data=_doc_to_bytes(doc),
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
    section_elem = doc.oxml._sections[0].element
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
        data=_doc_to_bytes(doc),
        success_count=success_count,
        fail_count=len(errors),
        errors=errors,
    )


def assemble_hwpx_hybrid(
    template_source,
    structure: dict,
    content: dict,
    removed_indices: list[int] = None,
    idx_map: dict = None,
    enable_marker_rewrite: bool = True,
    chapter_trees: list[list[dict]] | None = None,
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

    # idx_map: AI의 idx(축소본) → 원본 template의 실제 idx
    # 없으면 identity (축소 안 된 경우)
    def _to_real_idx(ai_idx: int) -> int:
        if idx_map:
            return idx_map.get(ai_idx, ai_idx)
        return ai_idx

    # ── 1단계: role → exemplar idx 매핑 (각 role의 첫 번째 idx를 exemplar로) ──
    role_exemplar_idx = {}  # role → 원본 template idx
    role_is_table_box = {}  # role → bool
    # skip: level 0 중 cover/toc 고정 슬롯만.
    # chapter_types의 title_role은 level 0이어도 skip하지 않음 (본문 구조의 일부).
    _title_roles = set()
    for _ct in structure.get("chapter_types", {}).values():
        tr = _ct.get("title_role", "")
        if tr:
            _title_roles.add(tr)
    # children을 가지는 level 0 문단도 skip하지 않음 (장 루트)
    _l0_with_children = set()
    for i, p in enumerate(paragraphs_info):
        if p.get("level", 0) == 0:
            if i + 1 < len(paragraphs_info) and paragraphs_info[i + 1].get("level", 0) > 0:
                _l0_with_children.add(p.get("idx"))

    def _is_skip(para: dict) -> bool:
        if para.get("level", 0) != 0:
            return False
        role = para.get("role", "")
        if role in _title_roles:
            return False
        if para.get("idx") in _l0_with_children:
            return False
        return True

    for p in paragraphs_info:
        role = p.get("role", "")
        ai_idx = p.get("idx", -1)
        real_idx = _to_real_idx(ai_idx)
        if role and role not in role_exemplar_idx and not _is_skip(p):
            role_exemplar_idx[role] = real_idx

    # 표 포함 여부 판별 (표가 있는 문단 = table box로 처리)
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            para = doc.paragraphs[idx]
            role_is_table_box[role] = bool(para.tables)

    log.info(
        f"role→exemplar 매핑: {len(role_exemplar_idx)}개 role, "
        f"table_box: {sum(role_is_table_box.values())}개"
    )

    # ── 2단계: exemplar 요소 저장 (deepcopy + ctrl/linesegarray 제거) ──
    exemplars = {}  # role → deepcopy된 XML element
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            elem = deepcopy(doc.paragraphs[idx].element)
            _strip_document_ctrls(elem, NS)
            _strip_linesegarray(elem, NS)
            exemplars[role] = elem

    # blank exemplars by paraPrIDRef — 1.5c의 blank_rules에서 paraPrIDRef로 선택
    blank_exemplars = {}  # paraPrIDRef → element
    for i, para in enumerate(doc.paragraphs):
        if (para.text or "").strip():
            continue
        pp = para.element.get("paraPrIDRef", "0")
        if pp not in blank_exemplars:
            blank_el = deepcopy(para.element)
            _strip_linesegarray(blank_el, NS)
            _strip_secpr(blank_el, NS)
            blank_exemplars[pp] = blank_el

    # fallback: 어떤 blank도 못 찾으면 첫 문단을 비워 사용
    if not blank_exemplars and len(doc.paragraphs) > 0:
        fb = deepcopy(doc.paragraphs[0].element)
        _strip_linesegarray(fb, NS)
        _strip_document_ctrls(fb, NS)
        _strip_secpr(fb, NS)
        for run in fb.findall(f"{NS}run"):
            t = run.find(f"{NS}t")
            if t is not None:
                t.text = ""
                for child in list(t):
                    t.remove(child)
            for tbl in run.findall(f"{NS}tbl"):
                run.remove(tbl)
            for cont in run.findall(f"{NS}container"):
                run.remove(cont)
        blank_exemplars["0"] = fb

    # role → level 매핑 (전환 관계 판단용)
    role_level = {}
    for p in paragraphs_info:
        role = p.get("role", "")
        if role and role not in role_level:
            role_level[role] = p.get("level", 0)

    # 1.5c 규칙 로드
    format_rules = structure.get("format_rules", {})
    blank_rules = structure.get("blank_rules", [])
    blank_lookup = {}
    for r in blank_rules:
        key = (r.get("from", ""), r.get("to", ""), r.get("relation", ""))
        blank_lookup[key] = r

    # ── 3단계: header 영역 처리 ──
    # header는 {role_name: text} 형태 — role 이름을 AI가 자유롭게 지정
    header_data = content.get("header", {})

    # structure에서 role → real_idx 매핑 (첫 번째 출현만)
    role_to_first_idx = {}
    for p in paragraphs_info:
        role = p.get("role", "")
        if role and role not in role_to_first_idx:
            role_to_first_idx[role] = _to_real_idx(p.get("idx", -1))

    header_indices = set()
    for role_name, text in header_data.items():
        if not text:
            continue
        real_idx = role_to_first_idx.get(role_name, -1)
        if real_idx < 0 or real_idx >= len(doc.paragraphs):
            errors.append(f"header role '{role_name}' not found in structure")
            continue
        header_indices.add(real_idx)
        try:
            _set_element_text(doc.paragraphs[real_idx], text, NS)
            success_count += 1
        except Exception as e:
            errors.append(f"header({role_name}, idx={real_idx}): {e}")

    # level 0 문단(cover/toc/spacer 등) → 보존 (header_indices에 추가)
    for p in paragraphs_info:
        real_idx = _to_real_idx(p.get("idx", -1))
        if _is_skip(p) and 0 <= real_idx < len(doc.paragraphs):
            header_indices.add(real_idx)

    # 첫 번째 문단(secPr 포함) 반드시 보존
    if len(doc.paragraphs) > 0:
        header_indices.add(0)

    # ── 4단계: 본문 영역 비우기 (header/toc/fixed/secPr 제외) ──
    # multi-section 대응: 각 paragraph가 속한 section에서 remove
    _all_sections = doc.oxml._sections
    _section_count = len(_all_sections)

    # paragraph element → owning section element 매핑
    _elem_to_section: dict = {}
    for sec in _all_sections:
        sec_el = sec.element
        for child in list(sec_el):
            _elem_to_section[child] = sec_el

    # paragraph index → section index 매핑 (preserved/residual 분류용)
    _para_to_sec_idx: dict[int, int] = {}
    for i, p in enumerate(doc.paragraphs):
        owning = _elem_to_section.get(p.element)
        if owning is not None:
            for si, sec in enumerate(_all_sections):
                if sec.element is owning:
                    _para_to_sec_idx[i] = si
                    break

    # secPr carrier 감지 (section layout 경계)
    _secpr_carriers: set[int] = set()
    for i, p in enumerate(doc.paragraphs):
        if p.element.find(f".//{NS}secPr") is not None:
            _secpr_carriers.add(i)

    # real_idx → para_info 역매핑 (preserved 분류 시 O(1) 조회)
    _real_idx_to_info: dict[int, dict] = {}
    for pi in paragraphs_info:
        ridx = _to_real_idx(pi.get("idx", -1))
        if ridx >= 0:
            _real_idx_to_info[ridx] = pi

    # header role indices (header_data에서 text 배정된 role의 real idx)
    _header_role_indices: set[int] = set()
    for rn, txt in header_data.items():
        if txt:
            ridx = role_to_first_idx.get(rn, -1)
            if 0 <= ridx < len(doc.paragraphs):
                _header_role_indices.add(ridx)

    # 9.1b: secPr carrier 보존 — section layout 경계 유지
    _secpr_preserved_count = 0
    _secpr_conflict_warnings = []
    for pidx in sorted(_secpr_carriers):
        if pidx in header_indices:
            continue  # already preserved by other rules
        p_info = _real_idx_to_info.get(pidx, {})
        role = p_info.get("role", "")
        text = (p_info.get("text", "") or "").strip()

        header_indices.add(pidx)
        _secpr_preserved_count += 1

        if text or role:
            _secpr_conflict_warnings.append({
                "para_idx": pidx,
                "section_idx": _para_to_sec_idx.get(pidx, -1),
                "role": role,
                "text_preview": text[:60],
                "conflict": "secPr_body_conflict_candidate",
            })

    if _secpr_preserved_count:
        log.info(
            f"assemble: {_secpr_preserved_count} secPr carrier(s) preserved "
            f"({len(_secpr_conflict_warnings)} with body conflict)"
        )

    body_elements = []
    _remove_per_section: dict[int, int] = {}  # section_idx → remove count
    _body_para_indices: set[int] = set()
    for i, p in enumerate(doc.paragraphs):
        if i not in header_indices:
            body_elements.append(p.element)
            _body_para_indices.add(i)

    # secPr carrier warning: body로 분류되어 삭제될 secPr carrier
    _secpr_carrier_warnings = []
    for pidx in sorted(_secpr_carriers & _body_para_indices):
        _w_info = _real_idx_to_info.get(pidx, {})
        _secpr_carrier_warnings.append({
            "para_idx": pidx,
            "section_idx": _para_to_sec_idx.get(pidx, -1),
            "role": _w_info.get("role", ""),
            "text_preview": (_w_info.get("text", "") or "")[:60],
            "status": "will_be_removed",
        })
    if _secpr_carrier_warnings:
        log.warning(
            f"assemble: {len(_secpr_carrier_warnings)} secPr carrier(s) "
            f"in body_elements — section layout may be lost"
        )

    for elem in body_elements:
        owning_section = _elem_to_section.get(elem)
        if owning_section is not None:
            owning_section.remove(elem)
            # debug: section별 remove count
            for si, sec in enumerate(_all_sections):
                if sec.element is owning_section:
                    _remove_per_section[si] = _remove_per_section.get(si, 0) + 1
                    break
        else:
            # fallback: section[0]에서 시도 (단일 section 호환)
            try:
                _all_sections[0].element.remove(elem)
                _remove_per_section[0] = _remove_per_section.get(0, 0) + 1
            except ValueError:
                log.warning(f"assemble: paragraph element not found in any section")

    log.info(
        f"본문 {len(body_elements)}개 문단 제거, "
        f"header {len(header_indices)}개 보존, "
        f"sections={_section_count}, remove_per_section={dict(_remove_per_section)}"
    )

    # preserved/residual candidate 분류 (section별, remove 후 남은 문단)
    _preserved_per_section: dict[str, list] = {}
    _residual_candidates: list[dict] = []
    for i in sorted(header_indices):
        if i >= len(doc.paragraphs):
            continue
        sec_idx = _para_to_sec_idx.get(i, 0)
        p_info = _real_idx_to_info.get(i, {})
        role = p_info.get("role", "")
        text_preview = (p_info.get("text", "") or "")[:60]
        has_secpr = i in _secpr_carriers

        if i in _header_role_indices:
            reason = "preserved_header"
        elif has_secpr:
            reason = "preserved_secPr_carrier"
        elif not text_preview.strip():
            reason = "spacer_candidate"
        else:
            reason = "body_residual_candidate"

        entry = {
            "para_idx": i,
            "section_idx": sec_idx,
            "role": role,
            "text_preview": text_preview,
            "has_secPr": has_secpr,
            "is_level0_skip": _is_skip(p_info) if p_info else False,
            "is_first_para": i == 0,
            "reason": reason,
        }
        sec_key = str(sec_idx)
        if sec_key not in _preserved_per_section:
            _preserved_per_section[sec_key] = []
        _preserved_per_section[sec_key].append(entry)
        if reason in ("spacer_candidate", "body_residual_candidate"):
            _residual_candidates.append(entry)

    # ── 5단계: body 항목으로 문서 재조립 (format_rules 기반 indent + blank_rules 기반 blank) ──
    # append target: remove가 가장 많이 발생한 section (=원래 body가 있던 section)
    if _remove_per_section:
        _target_sec_idx = max(_remove_per_section, key=_remove_per_section.get)
    else:
        _target_sec_idx = 0
    section_elem = _all_sections[_target_sec_idx].element

    # 9.2a: append target candidate 관측
    _body_sections = sorted(si for si, cnt in _remove_per_section.items() if cnt > 0)
    _multi_body_section = len(_body_sections) > 1
    _append_target_candidates = []
    for si in _body_sections:
        _append_target_candidates.append({
            "section_idx": si,
            "removed_count": _remove_per_section[si],
            "is_current_target": si == _target_sec_idx,
        })
    if _multi_body_section:
        log.info(
            f"assemble: multi-body-section detected — "
            f"body_sections={_body_sections}, append_target={_target_sec_idx}"
        )

    structure["_section_info"] = {
        "section_count": _section_count,
        "remove_per_section": _remove_per_section,
        "append_target_section": _target_sec_idx,
        "current_append_policy": "max_remove_section",
        "body_sections": _body_sections,
        "append_target_candidates": _append_target_candidates,
        "multi_body_section_warning": _multi_body_section,
        "preserved_per_section": _preserved_per_section,
        "residual_candidates": _residual_candidates,
        "secpr_carrier_warnings": _secpr_carrier_warnings,
        "secpr_conflict_warnings": _secpr_conflict_warnings,
    }
    body_items = content.get("body", [])
    prev_role = None
    prev_level = None

    # marker rewrite: marker_policy 기반으로 AI text의 marker를 교체
    from open_webui.utils.hwpx_analyzer import extract_marker_policies
    _marker_policy_1f = structure.get("marker_policy_1f")
    _marker_policies = extract_marker_policies(paragraphs_info, marker_policy_1f=_marker_policy_1f)
    _marker_rewrite_log = []
    REWRITE_ALLOWED_POLICIES = {"arabic_sequence", "circled_sequence", "fixed_char"}

    # chapter title roles
    _chapter_title_roles = set()
    for _ct in structure.get("chapter_types", {}).values():
        tr = _ct.get("title_role", "")
        if tr:
            _chapter_title_roles.add(tr)

    # ── chapter_trees 기반 node lookup 구축 ──
    # body_items를 title_role 기준으로 chapter 분할 → node 매칭
    _node_lookup: dict[int, dict] = {}  # body_items index → tree node
    _chapter_idx_lookup: dict[int, int] = {}  # body_items index → chapter_idx
    _tree_available = False

    # rewrite alignment tracking (C2/C3 structured logging)
    _rewrite_alignment = {
        "tree_available": False,
        "body_split_count": 0,
        "tree_chapter_count": len(chapter_trees) if chapter_trees else 0,
        "chapter_count_match": False,
        "per_chapter": [],
    }

    if chapter_trees:
        chapters_split = []  # [(start_idx_in_body, [item_indices_excluding_title])]
        current_start = None
        current_indices = []
        for bi, item in enumerate(body_items):
            if item.get("role", "") in _chapter_title_roles:
                if current_start is not None:
                    chapters_split.append((current_start, current_indices))
                current_start = bi
                current_indices = []
                _chapter_idx_lookup[bi] = len(chapters_split)
            else:
                if current_start is not None:
                    current_indices.append(bi)
                    _chapter_idx_lookup[bi] = len(chapters_split)
        if current_start is not None:
            chapters_split.append((current_start, current_indices))

        _rewrite_alignment["body_split_count"] = len(chapters_split)

        # 매칭: chapter_trees[ci]의 nodes vs chapters_split[ci]
        # 8.0b: title node가 chapter_trees에 포함되므로 title_bi도 매핑
        if len(chapters_split) == len(chapter_trees):
            _rewrite_alignment["chapter_count_match"] = True
            _tree_available = True
            for ci, (title_bi, body_indices) in enumerate(chapters_split):
                nodes = chapter_trees[ci]
                # 8.0b: all_indices = title + body → nodes 1:1
                all_indices = [title_bi] + body_indices
                aligned = len(all_indices) == len(nodes)
                _rewrite_alignment["per_chapter"].append({
                    "chapter_idx": ci,
                    "body_count": len(all_indices),
                    "tree_count": len(nodes),
                    "aligned": aligned,
                })
                if aligned:
                    for node_idx, bi in enumerate(all_indices):
                        _node_lookup[bi] = nodes[node_idx]
                        _chapter_idx_lookup[bi] = ci
                else:
                    log.warning(
                        f"marker_rewrite: chapter {ci} node count mismatch "
                        f"(body={len(body_indices)} vs tree={len(nodes)}), skipping tree"
                    )
                    _tree_available = False
                    _node_lookup.clear()
                    break
        else:
            _rewrite_alignment["chapter_count_match"] = False
            log.warning(
                f"marker_rewrite: chapter count mismatch "
                f"(body_split={len(chapters_split)} vs trees={len(chapter_trees)}), skipping tree"
            )

    _rewrite_alignment["tree_available"] = _tree_available

    # parent_id → role lookup (from tree nodes)
    # chapter별로 node id → node dict
    _chapter_node_maps: list[dict[int, dict]] = []
    if _tree_available and chapter_trees:
        for nodes in chapter_trees:
            node_map = {n["id"]: n for n in nodes}
            _chapter_node_maps.append(node_map)

    # sibling counter: key = (chapter_idx, parent_id, role) → count
    _sibling_counter: dict[tuple, int] = {}
    # fallback counter (no tree): key = role → count
    _fallback_counter: dict[str, int] = {}

    # total chapter title count (arabic fallback strip cap)
    _total_chapter_titles = sum(
        1 for item in body_items if item.get("role", "") in _chapter_title_roles
    )

    # title_role이 grammar에도 등장하는지 (안전장치)
    _title_role_in_grammar = False
    _per_type_grammar = structure.get("template_grammar", {}).get("per_type", {})
    for _tg in _per_type_grammar.values():
        for _tr in _chapter_title_roles:
            if _tr in _tg.get("grammar", {}):
                _title_role_in_grammar = True
                break

    _chapter_title_counter = 0

    def _generate_chapter_title_marker(policy_type: str, idx: int, markers: list) -> str:
        """chapter title 전용 marker 생성. roman_sequence 포함."""
        if policy_type == "roman_sequence":
            # Ⅰ=0x2160 ... Ⅻ=0x216B (1~12)
            if 1 <= idx <= 12:
                return chr(0x215F + idx)
        if policy_type == "arabic_sequence":
            return str(idx)
        # 기타 sequence: markers 배열 범위 내면 사용
        if idx <= len(markers):
            return markers[idx - 1]
        log.warning(
            f"chapter_title_marker: {policy_type} index={idx} "
            f"exceeds generatable range, falling back to last observed"
        )
        return markers[-1] if markers else str(idx)

    def _normalize_chapter_title(text: str, role: str, chapter_idx: int,
                                  policy: dict | None) -> tuple[str, dict]:
        """chapter title leading marker를 strip하고 template marker로 교체."""
        import re as _re

        base_log = {
            "is_chapter_title": True,
            "chapter_title_index": chapter_idx,
            "original_text": text[:80],
            "role": role,
        }

        # 안전장치: title_role이 grammar에도 등장
        if _title_role_in_grammar:
            return text, {**base_log,
                "strip_strategy": "skipped_title_role_in_grammar",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": "", "separator_used": "",
                "possible_marker_duplication": False,
            }

        # policy 없거나 non-sequence
        if not policy:
            return text, {**base_log,
                "strip_strategy": "skipped_no_policy",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": "", "separator_used": "",
                "possible_marker_duplication": False,
            }

        policy_type = policy.get("policy_type", "")
        if policy.get("style") != "sequence":
            return text, {**base_log,
                "strip_strategy": "skipped_not_sequence",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": policy_type, "separator_used": "",
                "possible_marker_duplication": False,
            }

        markers = policy.get("markers", [])
        sep = policy.get("separator", " ")

        # expected marker
        if chapter_idx <= len(markers):
            expected = markers[chapter_idx - 1]
        else:
            expected = _generate_chapter_title_marker(policy_type, chapter_idx, markers)

        # strip
        stripped = text.lstrip()
        content = stripped
        detected = ""
        strip_strategy = "none"

        # 1순위: template known marker
        for m in sorted(markers, key=len, reverse=True):
            if stripped.startswith(m):
                detected = m
                content = stripped[len(m):].lstrip(". \t")
                strip_strategy = "template_marker"
                break

        # 2순위: arabic fallback (number <= total_chapter_titles)
        if not detected:
            m_dot = _re.match(r'^(\d+)\s*[.)]\s*', stripped)
            m_space = _re.match(r'^(\d+)\s+', stripped)
            match = m_dot or m_space
            if match:
                num = int(match.group(1))
                if 1 <= num <= _total_chapter_titles:
                    detected = match.group(1)
                    content = stripped[match.end():]
                    strip_strategy = "arabic_cap_limited"

        # possible_marker_duplication: strip 실패 + 앞이 숫자
        possible_dup = False
        if not detected and stripped and stripped[0].isdigit():
            possible_dup = True

        rewritten = f"{expected}{sep}{content}" if content else f"{expected}{sep}{stripped}"
        applied = rewritten != text

        return rewritten, {**base_log,
            "strip_strategy": strip_strategy,
            "detected_leading_marker": detected,
            "expected_marker": expected,
            "stripped_content": content[:80],
            "rewritten_text": rewritten[:80],
            "rewrite_applied": applied,
            "marker_policy_type": policy_type,
            "separator_used": sep,
            "possible_marker_duplication": possible_dup,
        }

    def _generate_sequence_marker(policy_type: str, sib_idx: int, markers: list) -> str:
        """markers 배열을 초과한 sibling_index에 대해 규칙형 마커를 직접 생성."""
        if policy_type == "arabic_sequence":
            return str(sib_idx)
        if policy_type == "num_paren_sequence":
            return f"{sib_idx})"
        if policy_type == "circled_sequence":
            # ➊=0x278A ... ➓=0x2793 (1~10)
            if 1 <= sib_idx <= 10:
                return chr(0x2789 + sib_idx)
        if policy_type == "circled_num_sequence":
            # ①=0x2460 ... ⑳=0x2473 (1~20)
            if 1 <= sib_idx <= 20:
                return chr(0x245F + sib_idx)
        # 생성 불가 — fallback to last observed
        log.warning(
            f"marker_rewrite: {policy_type} sibling_index={sib_idx} "
            f"exceeds generatable range, falling back to last observed"
        )
        return markers[-1] if markers else ""

    def _rewrite_marker(body_item_idx: int, role: str, text: str) -> str:
        """marker_policy에 따라 text의 leading marker를 교체."""
        node = _node_lookup.get(body_item_idx)
        ch_idx = _chapter_idx_lookup.get(body_item_idx)

        # chapter title → 전용 normalization + fallback counter 리셋
        if role in _chapter_title_roles:
            _fallback_counter.clear()
            nonlocal _chapter_title_counter
            _chapter_title_counter += 1
            rewritten, log_entry = _normalize_chapter_title(
                text, role, _chapter_title_counter,
                _marker_policies.get(role),
            )
            _marker_rewrite_log.append(log_entry)
            return rewritten

        policy = _marker_policies.get(role)
        if not policy:
            return text

        markers = policy.get("markers", [])
        policy_type = policy.get("policy_type", "")
        marker_family = policy.get("family", "")
        sep = policy.get("separator", " ")

        if not markers:
            return text

        # star_depth: preview skip
        if policy_type == "star_depth":
            _marker_rewrite_log.append({
                "chapter_idx": ch_idx,
                "node_id": node["id"] if node else None,
                "role": role,
                "parent_id": node["parent_id"] if node else None,
                "parent_role": None,
                "sibling_group_key": None,
                "marker_policy_type": policy_type,
                "marker_family": marker_family,
                "sibling_index": None,
                "detected_marker": None,
                "expected_marker": None,
                "stripped_content": text[:80],
                "rewritten_text": text[:80],
                "marker_match": None,
                "rewrite_applied": False,
                "apply_reason": None,
                "skip_reason": "star_depth",
            })
            return text

        # sibling index 계산
        parent_id = None
        parent_role = None
        sibling_group_key = None

        if _tree_available and node is not None and ch_idx is not None:
            parent_id = node.get("parent_id")
            # parent role lookup
            if parent_id is not None and ch_idx < len(_chapter_node_maps):
                parent_node = _chapter_node_maps[ch_idx].get(parent_id)
                if parent_node:
                    parent_role = parent_node.get("role")
            sibling_group_key = f"{ch_idx}_{parent_id}_{role}"
            counter_key = (ch_idx, parent_id, role)
            _sibling_counter[counter_key] = _sibling_counter.get(counter_key, 0) + 1
            sib_idx = _sibling_counter[counter_key]
        else:
            # fallback: role별 global counter (chapter title에서 리셋)
            _fallback_counter[role] = _fallback_counter.get(role, 0) + 1
            sib_idx = _fallback_counter[role]
            sibling_group_key = f"fallback_{role}"

        # expected marker 결정
        if policy.get("style") == "sequence":
            if sib_idx <= len(markers):
                expected = markers[sib_idx - 1]
            else:
                # markers 배열 초과 — 규칙형 시퀀스는 직접 생성
                expected = _generate_sequence_marker(policy_type, sib_idx, markers)
        else:
            expected = markers[0]

        # text에서 기존 marker strip
        stripped = text.lstrip()
        stripped_content = stripped
        detected = ""
        for m in sorted(markers, key=len, reverse=True):
            if stripped.startswith(m):
                detected = m
                after = stripped[len(m):]
                if after and after[0] in (" ", "\t"):
                    stripped_content = after[1:]
                elif after:
                    stripped_content = after
                else:
                    stripped_content = ""
                break

        # rewritten text 계산
        if not detected and policy_type in ("roman_sequence", "arabic_sequence",
                                             "circled_sequence", "circled_pua_sequence"):
            rewritten = f"{expected}{sep}{stripped_content}" if stripped_content else f"{expected}"
        elif detected == expected:
            rewritten = text
        else:
            rewritten = f"{expected}{sep}{stripped_content}" if stripped_content else f"{expected}"

        marker_match = (detected == expected) if detected else None
        would_change = rewritten != text

        # apply/skip 판정
        if not enable_marker_rewrite:
            applied = False
            apply_reason = None
            skip_reason = "feature_flag_disabled"
        elif policy_type not in REWRITE_ALLOWED_POLICIES:
            applied = False
            apply_reason = None
            skip_reason = "policy_not_in_allowlist"
        elif not would_change:
            applied = False
            apply_reason = None
            skip_reason = "no_change_needed"
        else:
            applied = True
            apply_reason = "enabled_and_allowed"
            skip_reason = None

        _marker_rewrite_log.append({
            "chapter_idx": ch_idx,
            "node_id": node["id"] if node else None,
            "role": role,
            "parent_id": parent_id,
            "parent_role": parent_role,
            "sibling_group_key": sibling_group_key,
            "marker_policy_type": policy_type,
            "marker_family": marker_family,
            "sibling_index": sib_idx,
            "detected_marker": detected,
            "expected_marker": expected,
            "stripped_content": stripped_content[:80],
            "rewritten_text": rewritten[:80],
            "marker_match": marker_match,
            "rewrite_applied": applied,
            "apply_reason": apply_reason,
            "skip_reason": skip_reason,
        })

        return rewritten if applied else text

    for bi_idx, item in enumerate(body_items):
        role = item.get("role", "")
        text = item.get("text", "")

        if role not in exemplars:
            errors.append(f"unknown role '{role}', skipping: {text[:50]}")
            continue

        # marker rewrite 적용
        text = _rewrite_marker(bi_idx, role, text)

        cur_level = role_level.get(role, 0)

        # ── blank_rules 적용: 전환 관계에 따라 빈 줄 삽입 ──
        if prev_role is not None and prev_level is not None:
            if cur_level == prev_level:
                relation = "sibling"
            elif cur_level > prev_level:
                relation = "descent"
            else:
                relation = "ascent"
            rule = blank_lookup.get((prev_role, role, relation))
            if rule and rule.get("has_blank"):
                paraPr = rule.get("paraPrIDRef") or "0"
                blank_el = (
                    blank_exemplars.get(paraPr)
                    or blank_exemplars.get("0")
                    or (next(iter(blank_exemplars.values())) if blank_exemplars else None)
                )
                if blank_el is not None:
                    section_elem.append(deepcopy(blank_el))

        # ── format_rules 적용: AI 앞공백 제거 후 indent_parts로 재구성 ──
        fmt = format_rules.get(role, {})
        indent_parts = fmt.get("indent_parts", [])
        clean_text = text.lstrip(" \t")

        # 공백은 t.text에 prepend, tab은 <hp:tab/> 요소로 삽입
        space_prefix = ""
        num_tabs = 0
        for part in indent_parts:
            ptype = part.get("type") if isinstance(part, dict) else None
            if ptype == "tab":
                num_tabs += 1
            elif ptype == "space":
                space_prefix += " " * int(part.get("count", 0) or 0)

        # exemplar 복제
        new_elem = deepcopy(exemplars[role])

        # 텍스트 교체 (공백 prefix 포함)
        try:
            is_tbl_box = role_is_table_box.get(role, False)
            _set_cloned_element_text(new_elem, space_prefix + clean_text, NS, is_tbl_box)

            # 탭 삽입 (table_box가 아닐 때만)
            if num_tabs > 0 and not is_tbl_box:
                runs = new_elem.findall(f"{NS}run")
                if runs:
                    first_run = runs[0]
                    t_elem = first_run.find(f"{NS}t")
                    if t_elem is not None:
                        t_index = list(first_run).index(t_elem)
                        for _ in range(num_tabs):
                            tab_elem = etree.Element(f"{NS}tab")
                            first_run.insert(t_index, tab_elem)
                            t_index += 1

            section_elem.append(new_elem)
            success_count += 1
        except Exception as e:
            errors.append(f"assemble({role}): {e}")

        prev_role = role
        prev_level = cur_level

    # marker rewrite log + alignment를 structure에 저장 (debug용)
    changed = sum(1 for r in _marker_rewrite_log if r.get("changed"))
    structure["_marker_rewrite_log"] = _marker_rewrite_log
    structure["_rewrite_alignment"] = _rewrite_alignment
    log.info(
        f"하이브리드 조립 완료: 성공 {success_count}, 실패 {len(errors)}, "
        f"body 항목 {len(body_items)}개, marker rewrite {changed}/{len(_marker_rewrite_log)}"
    )

    return HwpxResult(
        data=_doc_to_bytes(doc),
        success_count=success_count,
        fail_count=len(errors),
        errors=errors,
    )


def _strip_secpr(elem, NS: str):
    """
    복제된 요소에서 secPr(섹션 속성) 요소를 제거합니다.
    secPr이 있는 문단을 clone하면 매번 새 섹션이 시작되어
    불필요한 페이지 나누기가 발생합니다.
    """
    for parent in elem.iter():
        for secpr in list(parent.findall(f"{NS}secPr")):
            parent.remove(secpr)


def _strip_linesegarray(elem, NS: str):
    """
    복제된 exemplar에서 linesegarray 요소를 모두 제거합니다.
    linesegarray는 줄 위치 좌표(고정값)로, 다른 길이의 텍스트가 들어가면
    글자가 겹치는 원인이 됩니다. 제거하면 한글 뷰어가 자동 재계산합니다.
    """
    for parent in elem.iter():
        for lsa in list(parent.findall(f"{NS}linesegarray")):
            parent.remove(lsa)


def _strip_document_ctrls(elem, NS: str):
    """
    복제된 exemplar에서 document-level ctrl 요소를 제거합니다.
    footer, header, newNum, pageHiding 등은 문서에 한 번만 존재해야 하므로
    exemplar를 clone할 때 중복 생성되지 않도록 제거합니다.
    """
    remove_types = {"footer", "header", "newNum", "pageHiding"}
    parent_map = _build_parent_map(elem)
    for ctrl in elem.findall(f".//{NS}ctrl"):
        for child in list(ctrl):
            local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local_name in remove_types:
                run = parent_map.get(ctrl)
                if run is not None:
                    run_parent = parent_map.get(run)
                    if run_parent is not None:
                        run_parent.remove(run)
                break


def _set_element_text(para, text: str, NS: str):
    """기존 문단(HwpxOxmlParagraph)의 텍스트를 교체합니다."""
    from lxml import etree

    # 표가 있는 문단이면 텍스트가 있는 셀을 찾아 교체
    if para.tables:
        try:
            tbl = para.tables[0]
            target_cell = None
            for r in range(tbl.row_count):
                for c in range(tbl.col_count):
                    cell = tbl.cell(r, c)
                    cell_text = "".join(
                        p.text for p in cell.paragraphs if p.text
                    ).strip()
                    if cell_text:
                        target_cell = cell
                        break
                if target_cell:
                    break
            if target_cell is None:
                target_cell = tbl.cell(0, 0)
            cell_paras = target_cell.paragraphs
            if cell_paras:
                cell_paras[0].text = text
                for cp in cell_paras[1:]:
                    cp.text = ""
            return
        except Exception as e:
            log.warning(f"_set_element_text 라이브러리 API 실패, XML 직접 접근: {e}")

    # XML 직접 접근 fallback — 표/container 내부에서 텍스트가 있는 셀을 찾아 교체
    elem = para.element
    # 표 내부 셀에서 텍스트 찾기
    for tc in elem.findall(f".//{NS}tc"):
        sublist = tc.find(f"{NS}subList")
        if sublist is None:
            continue
        for p in sublist.findall(f"{NS}p"):
            for t in p.findall(f".//{NS}t"):
                if t.text and t.text.strip():
                    # 텍스트가 있는 셀 발견 — 첫 문단 교체
                    _replace_text_in_paragraph_elem(sublist.findall(f"{NS}p")[0], text, NS)
                    # 나머지 문단 텍스트 비우기
                    for extra_p in sublist.findall(f"{NS}p")[1:]:
                        for et in extra_p.findall(f".//{NS}t"):
                            et.text = ""
                            for child in list(et):
                                et.remove(child)
                    return

    # 일반 문단
    para.text = text


def _set_cloned_element_text(elem, text: str, NS: str, is_table_box: bool):
    """deepcopy된 XML 요소의 텍스트를 교체합니다."""

    # 1) 표(tbl) 내부 텍스트 교체
    if is_table_box:
        trs = elem.findall(f".//{NS}tr")
        if trs:
            tcs = trs[0].findall(f"{NS}tc")
            if tcs:
                target_tc = tcs[-1] if len(tcs) > 1 else tcs[0]
                sublist = target_tc.find(f"{NS}subList")
                if sublist is not None:
                    paras = sublist.findall(f"{NS}p")
                    if paras:
                        _replace_text_in_paragraph_elem(paras[0], text, NS)
                        for p in paras[1:]:
                            sublist.remove(p)
                    return

    # 2) container(그리기 객체) 내부 텍스트 교체
    #    container > rect/... > drawText > subList > p 구조
    draw_texts = elem.findall(f".//{NS}drawText")
    if draw_texts:
        # 가장 텍스트가 많은 drawText를 선택 (제목 박스가 아닌 본문 영역)
        target_dt = None
        max_text_len = 0
        for dt in draw_texts:
            sub = dt.find(f"{NS}subList")
            if sub is not None:
                dt_text = ""
                for p in sub.findall(f"{NS}p"):
                    for t in p.findall(f".//{NS}t"):
                        if t.text:
                            dt_text += t.text
                if len(dt_text.strip()) > max_text_len:
                    max_text_len = len(dt_text.strip())
                    target_dt = dt
        # 텍스트 있는 drawText가 없으면 마지막 drawText 사용
        if target_dt is None and draw_texts:
            target_dt = draw_texts[-1]
        if target_dt is not None:
            sub = target_dt.find(f"{NS}subList")
            if sub is not None:
                paras = sub.findall(f"{NS}p")
                if paras:
                    _replace_text_in_paragraph_elem(paras[0], text, NS)
                    for p in paras[1:]:
                        sub.remove(p)
                return

    # 3) 일반 문단의 텍스트 교체
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
