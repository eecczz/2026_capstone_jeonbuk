# Voice Chatbot Notes

## Public RAG binding

`PUBLIC_CHATBOT_KNOWLEDGE_ID` is now read by the public chatbot router and attached to
the normal Open WebUI RAG `files` flow before `process_chat_payload()`.

Supported comma-separated forms:

```bash
PUBLIC_CHATBOT_KNOWLEDGE_ID=collection:<knowledge_base_id>
PUBLIC_CHATBOT_KNOWLEDGE_ID=<knowledge_base_id>
PUBLIC_CHATBOT_KNOWLEDGE_ID=legacy:<vector_collection_name>
```

Use `collection:<knowledge_base_id>` for normal Knowledge bases. Use
`legacy:<vector_collection_name>` only when the vector collection is managed directly,
for example crawler output such as `legacy:jeonbuk_gov`.

## Voice mode

Public voice chat now returns `audio_url` when Qwen3-TTS succeeds.

Minimum environment:

```bash
STT_ENGINE=cohere
TTS_ENGINE=qwen
AUDIO_STT_COHERE_MODEL=CohereLabs/cohere-transcribe-03-2026
AUDIO_STT_COHERE_LANGUAGE=korean
AUDIO_TTS_QWEN_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B
AUDIO_TTS_QWEN_VOICE=female_kr_01
```

The frontend still falls back to browser speech synthesis when Qwen3-TTS is disabled
or fails.
