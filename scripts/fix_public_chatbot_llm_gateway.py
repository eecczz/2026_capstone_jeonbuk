#!/usr/bin/env python3
"""Point the public voice chatbot at the shared Jeonbuk LLM gateway.

This is an idempotent operational repair for the 2026-06-21 incident where
the public chatbot kept replying "답변 준비 중에 문제가 생겼어요." because
its wrapper model still pointed to a stale Qwen3.5 endpoint.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import psycopg2


DB_HOST = os.getenv("OWI_DB_HOST", "localhost")
DB_NAME = os.getenv("OWI_DB_NAME", "customui")
DB_USER = os.getenv("OWI_DB_USER", "admin")
DB_PASSWORD = os.getenv("OWI_DB_PASSWORD") or os.getenv("PGPASSWORD")

PUBLIC_MODEL_ID = os.getenv("PUBLIC_CHATBOT_MODEL_ID", "jeonbuk-public-chatbot")
LLM_MODEL = os.getenv("PUBLIC_CHATBOT_GATEWAY_MODEL", "ChatGPT-oss-120B")
LLM_GATEWAY = os.getenv("PUBLIC_CHATBOT_GATEWAY_URL", "https://ai2.jb.go.kr/llm/v1")


def main() -> None:
    if not DB_PASSWORD:
        raise RuntimeError("Set OWI_DB_PASSWORD or PGPASSWORD before running this script")

    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    conn.autocommit = False

    with conn.cursor() as cur:
        cur.execute("SELECT id, data::text FROM config ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("config table has no rows")

        config_id, data_text = row
        cfg = json.loads(data_text)

        openai = cfg.setdefault("openai", {})
        urls = openai.setdefault("api_base_urls", [])
        keys = openai.setdefault("api_keys", [])
        configs = openai.setdefault("api_configs", {})

        if len(urls) > 14 and urls[14] == "http://172.17.0.1:30010/v1":
            stale = configs.setdefault("14", {})
            stale["enable"] = False
            stale["disabled_reason"] = (
                "disabled by fix_public_chatbot_llm_gateway.py: stale Docker "
                "gateway endpoint caused Model not found"
            )

        if LLM_GATEWAY in urls:
            gateway_idx = urls.index(LLM_GATEWAY)
        else:
            urls.append(LLM_GATEWAY)
            keys.append("")
            gateway_idx = len(urls) - 1

        while len(keys) < len(urls):
            keys.append("")

        configs[str(gateway_idx)] = {
            "tags": [],
            "enable": True,
            "auth_type": "none",
            "model_ids": [LLM_MODEL],
            "prefix_id": "",
            "connection_type": "external",
        }

        public_chatbot = cfg.setdefault("public_chatbot", {})
        public_chatbot["model_id"] = PUBLIC_MODEL_ID
        public_chatbot["base_model"] = LLM_MODEL

        backup_path = Path(
            f"/tmp/public_chatbot_llm_config_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        backup_path.write_text(data_text, encoding="utf-8")

        cur.execute(
            "UPDATE config SET data=%s::json, updated_at=now() WHERE id=%s",
            [json.dumps(cfg, ensure_ascii=False), config_id],
        )
        cur.execute(
            "UPDATE model SET base_model_id=%s, updated_at=(extract(epoch from now())::bigint) WHERE id=%s",
            [LLM_MODEL, PUBLIC_MODEL_ID],
        )

    conn.commit()
    conn.close()
    print(f"public chatbot LLM gateway fixed: {PUBLIC_MODEL_ID} -> {LLM_MODEL}")
    print(f"gateway: {LLM_GATEWAY}")
    print(f"config backup: {backup_path}")


if __name__ == "__main__":
    main()
