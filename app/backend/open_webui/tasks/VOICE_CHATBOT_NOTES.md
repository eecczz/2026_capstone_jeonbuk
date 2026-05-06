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

## STT post-processing

`open_webui.utils.public_voice` applies the first service-facing correction layer:

- domain term correction such as `국취제` -> `국민취업지원제도`
- Korean speech normalization for common type/date expressions
- short utterance interpretation against recent chat history
- intent hints for schedule, amount, channel, documents, eligibility, apply, cancel
- directedness scoring so background speech can be ignored
- confidence-based `answer`, `clarify`, or `ignore` decisions

The public voice endpoint only sends the normalized question to RAG when the action is
`answer`. It returns a confirmation prompt for `clarify`, and `ignored: true` for
background speech.
