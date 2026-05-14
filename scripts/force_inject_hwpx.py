"""HWPX 첨부 → ZIP parse → BGE M3 임베딩 → Qdrant 직접 insert (wrapper 우회).

OCR wrapper 의 LibreOffice/H2Orestart 한계로 HWPX 표 데이터가 처리 안 되는
케이스 우회. multitenancy Qdrant 의 open-webui_knowledge 에 tenant_id=jeonbuk_gov
로 직접 insert.
"""

from __future__ import annotations

import os, re, sys, json, time, uuid, zipfile
from io import BytesIO
import urllib.request, urllib.parse

POST_URL = sys.argv[1] if len(sys.argv) > 1 else \
    "https://www.jeonbuk.go.kr/board/view.jeonbuk?boardId=BBS_0000005&dataSid=656881"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
QDRANT_URL = "http://host.docker.internal:6333"
BGE_M3_URL = "http://192.168.30.2:8100/v1/embeddings"


def get_qdrant_key():
    return os.popen("sudo cat /proc/61/environ 2>/dev/null | tr '\\0' '\\n' | grep QDRANT_API_KEY | cut -d= -f2").read().strip()


def http_get(url, headers=None, max_size=50_000_000):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return dict(r.headers), r.read(max_size)


def hwpx_to_text(raw):
    parts = []
    with zipfile.ZipFile(BytesIO(raw)) as z:
        for name in z.namelist():
            if not name.endswith(".xml"): continue
            if "header" in name.lower() or "settings" in name.lower(): continue
            with z.open(name) as f:
                raw_xml = f.read().decode("utf-8", errors="replace")
            t = re.sub(r"<[^>]+>", " ", raw_xml)
            t = re.sub(r"\s+", " ", t).strip()
            if t: parts.append(t)
    return "\n".join(parts)


def embed_batch(texts, model="BAAI/bge-m3"):
    import httpx
    with httpx.Client(timeout=120.0) as c:
        r = c.post(BGE_M3_URL, json={"model": model, "input": texts},
                   headers={"Authorization": "Bearer dummy"})
        r.raise_for_status()
        d = r.json()
    return [item["embedding"] for item in d["data"]]


def qdrant_insert(api_key, points):
    body = json.dumps({"points": points}).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/open-webui_knowledge/points?wait=true",
        data=body, method="PUT",
        headers={"api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def chunk_text(text, size=1500, overlap=200):
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i+size])
        i += size - overlap
    return chunks


def fetch_post_info(url):
    """fetch view page → title + attachments."""
    _, html = http_get(url)
    h = html.decode("utf-8", errors="replace")
    title_match = re.search(r'#{1,4}\s*([^<\n#]+)', h) or re.search(r"<title>(.*?)</title>", h, re.S)
    title = "(게시물)"
    # extract from view content
    body = re.sub(r"<[^>]+>", " ", h)
    body = re.sub(r"\s+", " ", body)
    m = re.search(r'(전북특별자치도\s+거점소독시설[^.]*\.)', body)
    if m: title = m.group(1).strip()[:120]
    atts = []
    for m in re.finditer(r'/board/download\.jeonbuk\?[^"\'<>&]+(?:&amp;[^"\'<>]+)*', h):
        atts.append(urllib.parse.urljoin("https://www.jeonbuk.go.kr",
                                         m.group(0).replace("&amp;", "&")))
    return title, list(dict.fromkeys(atts))


def main():
    print(f"=== fetch {POST_URL}")
    title, atts = fetch_post_info(POST_URL)
    print(f"title: {title}")
    print(f"attachments: {len(atts)}")

    text_parts = []
    for att in atts:
        hdrs, raw = http_get(att)
        cd = hdrs.get("Content-Disposition", "")
        fm = re.search(r"filename=([^;]+)", cd)
        fn = urllib.parse.unquote(fm.group(1).strip().strip('"')) if fm else "attachment"
        if fn.lower().endswith((".hwpx", ".hwp")):
            t = hwpx_to_text(raw)
            print(f"  {fn}: {len(t)} chars extracted")
            text_parts.append((fn, t))

    if not text_parts:
        print("no HWPX text — bail"); return

    # Build chunks with rich metadata
    api_key = get_qdrant_key()
    all_items = []
    for fn, txt in text_parts:
        chunks = chunk_text(txt, size=1500, overlap=200)
        print(f"  {fn} → {len(chunks)} chunks")
        for i, ch in enumerate(chunks):
            all_items.append({
                "filename": fn,
                "chunk_idx": i,
                "text": f"[{title} / {fn}]\n{ch}",
            })

    # Embed all chunks (batch by 32 to avoid token limit)
    BATCH = 16
    points = []
    for i in range(0, len(all_items), BATCH):
        b = all_items[i:i+BATCH]
        embs = embed_batch([x["text"] for x in b])
        for x, emb in zip(b, embs):
            pid = str(uuid.uuid4())
            points.append({
                "id": pid,
                "vector": emb,
                "payload": {
                    "text": x["text"],
                    "tenant_id": "jeonbuk_gov",
                    "metadata": {
                        "url": POST_URL,
                        "source": POST_URL,
                        "title": title,
                        "name": x["filename"],
                        "institution": "전북특별자치도",
                        "site_code": "jeonbuk_main",
                        "category": "행정",
                        "contact_phone": "063-280-4642",
                        "homepage_url": "https://www.jeonbuk.go.kr",
                        "crawled_at": int(time.time()),
                        "loader": "manual_hwpx_zipfile",
                        "chunk_idx": x["chunk_idx"],
                        "embedding_config": {"engine": "openai", "model": "BAAI/bge-m3"},
                    },
                },
            })
    print(f"=== inserting {len(points)} points to Qdrant")
    res = qdrant_insert(api_key, points)
    print(f"result: status={res.get('status')} op={res.get('result',{}).get('status')}")


if __name__ == "__main__":
    main()
