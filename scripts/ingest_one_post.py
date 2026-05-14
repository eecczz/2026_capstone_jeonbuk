"""단일 게시물 + 첨부 HWPX 직접 적재 PoC — OWI HTTP API 만 사용.

OCR wrapper 의 HWPX 처리가 LibreOffice/H2Orestart 미설치로 500 실패 우회.
HWPX 는 ZIP 컨테이너라 직접 풀어 텍스트 추출.
"""

from __future__ import annotations

import os, re, sys, json, zipfile, time, uuid
from io import BytesIO
import urllib.request, urllib.parse

POST_URL = sys.argv[1] if len(sys.argv) > 1 else \
    "https://www.jeonbuk.go.kr/board/view.jeonbuk?boardId=BBS_0000005&dataSid=656881"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
OWI = "http://localhost:8080"


def http_get(url: str, max_size: int = 50_000_000) -> tuple[dict, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return dict(r.headers), r.read(max_size)


def extract_meta_atts(html: str) -> tuple[str, str, list[str]]:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = m.group(1).strip() if m else "(제목 없음)"
    body = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
    body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    atts = []
    for m in re.finditer(r'/board/download\.jeonbuk\?[^"\'<>&]+(?:&amp;[^"\'<>]+)*', html):
        atts.append(urllib.parse.urljoin("https://www.jeonbuk.go.kr",
                                         m.group(0).replace("&amp;", "&")))
    return title, body, list(dict.fromkeys(atts))


def hwpx_to_text(raw: bytes) -> str:
    parts = []
    try:
        with zipfile.ZipFile(BytesIO(raw)) as z:
            for name in z.namelist():
                if not name.endswith(".xml"): continue
                if "header" in name.lower() or "settings" in name.lower(): continue
                with z.open(name) as f:
                    raw_xml = f.read().decode("utf-8", errors="replace")
                t = re.sub(r"<[^>]+>", " ", raw_xml)
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    parts.append(t)
    except Exception as e:
        print(f"  hwpx parse fail: {e}")
    return "\n".join(parts)


def pdf_via_wrapper(raw: bytes, filename: str) -> str:
    boundary = "----HWPX" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/pdf\r\n\r\n"
    body += raw + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "http://192.168.30.2:30100/text/pdf", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        return d.get("text") or d.get("content") or d.get("result") or ""
    except Exception as e:
        return f"[pdf ocr error: {e}]"


def get_jwt() -> str:
    auth = json.dumps({"email": "sprinter@mail.go.kr", "password": "sprint26!"}).encode()
    req = urllib.request.Request(f"{OWI}/api/v1/auths/signin", data=auth,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]


def ingest_text(jwt: str, name: str, content: str) -> dict:
    payload = {
        "name": name[:200],
        "content": content,
        "collection_name": "jeonbuk_gov",
    }
    req = urllib.request.Request(
        f"{OWI}/api/v1/retrieval/process/text",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {jwt}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    print(f"=== fetch {POST_URL}")
    _, html = http_get(POST_URL)
    html_text = html.decode("utf-8", errors="replace")
    title, body, atts = extract_meta_atts(html_text)
    print(f"title: {title}")
    print(f"body len: {len(body)} chars")
    print(f"attachments: {len(atts)}")

    parts = [(title, body)]
    for att in atts:
        try:
            hdrs, raw = http_get(att)
            cd = hdrs.get("Content-Disposition", "")
            fm = re.search(r"filename=([^;]+)", cd)
            fn = urllib.parse.unquote(fm.group(1).strip().strip('"')) if fm else "attachment"
            print(f"  attachment: {fn} ({len(raw)} bytes)")
            if fn.lower().endswith((".hwpx", ".hwp")):
                txt = hwpx_to_text(raw)
            elif fn.lower().endswith(".pdf"):
                txt = pdf_via_wrapper(raw, fn)
            else:
                txt = ""
            if txt:
                parts.append((fn, txt[:50000]))
                print(f"    text extracted: {len(txt)} chars")
                # show 정읍 snippet
                idx = txt.find("정읍")
                if idx >= 0:
                    print(f"    around 정읍: ...{txt[max(0,idx-50):idx+200]}...")
        except Exception as e:
            print(f"  attachment fail: {e}")

    print("\n=== getting JWT")
    jwt = get_jwt()
    print(f"jwt len={len(jwt)}")

    # Single document: combine all parts with separator.
    combined = "\n\n".join(f"=== {n} ===\n{t}" for n, t in parts)
    print(f"\n=== combined len: {len(combined)}")

    print("\n=== POST /api/v1/retrieval/process/text")
    resp = ingest_text(jwt, title or "(unknown)", combined)
    print(f"response: {json.dumps(resp, ensure_ascii=False)[:800]}")

    coll = resp.get("collection_name") if isinstance(resp, dict) else None
    if coll:
        print(f"\n=== ingested to collection: {coll}")
        print(f"   (note: this is a 'file-XXX' collection, not jeonbuk_gov tenant.")
        print(f"    To make it searchable from public chatbot, we need to either")
        print(f"    (a) attach it to PUBLIC_CHATBOT_KNOWLEDGE_ID, or")
        print(f"    (b) bypass and write directly into jeonbuk_gov tenant.)")


if __name__ == "__main__":
    main()
