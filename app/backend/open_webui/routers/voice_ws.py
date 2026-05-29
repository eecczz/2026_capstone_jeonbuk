"""대도민 음성 챗봇 WebSocket endpoint (Pipecat 1.1 기반).

⚠️ 핵심 — Pipecat 1.1 의 FastAPIWebsocketTransport 는 serializer 가 None 이면
   `_receive_messages` 에서 모든 WS 메시지를 그냥 skip 한다 (transport/websocket/
   fastapi.py:305-306). 즉 별도 serializer 안 주면 클라이언트가 보낸 PCM 이
   VAD 까지 도달도 못 한다. 그래서 본 모듈은 RawPcmSerializer 를 직접 정의해
   binary frame ↔ InputAudioRawFrame, OutputAudioRawFrame ↔ binary frame
   매핑을 처리한다.


설계 의도:
- 텍스트 챗봇 경로 (/api/v1/public/chat) 는 기존 그대로 유지.
- 음성 모드는 turn-taking / interrupt handling 품질이 핵심이라
  Pipecat 의 Silero VAD + Smart Turn 기반 음성 흐름을 도입한다.
- STT (Cohere transcribe) / TTS (Qwen3-TTS Sohee) 는 우리 vLLM 기반 OpenAI 호환
  endpoint 라서 Pipecat 의 OpenAI 어댑터가 그대로 base_url 만 바꿔 호출한다.
- LLM/RAG 단은 우리 자체 _run_chat_internal(=public_chatbot) 을 호출하는
  JeonbukRAGProcessor 로 처리 — 도청 도메인 어휘 / 휴리스틱 / Qdrant RAG /
  답변 humanize 전부 그대로 재사용.

파이프라인:
  ws(in)  →  STT  →  RAG  →  TTS  →  ws(out)
              Silero VAD 는 transport input 에서 발화 경계 감지

WebSocket protocol:
- 클라이언트 → 서버: raw PCM 16kHz 16-bit mono (또는 WAV 헤더 포함)
- 서버 → 클라이언트: 동일 (audio_out_sample_rate 로 합성된 TTS PCM 스트림)
- protobuf 등 별도 serializer 안 씀 (raw bytes 가장 단순)
"""

import asyncio
import contextlib
import logging
import sys
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from open_webui.env import GLOBAL_LOG_LEVEL

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)

# 워커 stdout 이 pipe 로 묶여 추적이 어려우므로 voice_ws 흐름을 별도 파일에도 기록.
# 디버그 후 안정화되면 제거 또는 DEBUG 레벨로 낮춤.
try:
    _voice_fh = logging.FileHandler("/tmp/voice_ws.log", encoding="utf-8")
    _voice_fh.setLevel(logging.DEBUG)
    _voice_fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    log.addHandler(_voice_fh)
    log.setLevel(logging.DEBUG)
    # Pipecat 내부 로거도 같이 받기 — VAD / STT / Transport 진단 용이
    for _other_name in (
        "pipecat",
        "pipecat.processors.audio.vad_processor",
        "pipecat.transports.websocket.fastapi",
        "pipecat.services.openai.stt",
        "pipecat.services.openai.tts",
    ):
        _other = logging.getLogger(_other_name)
        _other.addHandler(_voice_fh)
        _other.setLevel(logging.DEBUG)
except Exception:
    pass

router = APIRouter()


# PCM 양방향 serializer 는 utils/voice_pcm_serializer.py 로 분리.
from open_webui.utils.voice_pcm_serializer import (
    make_raw_pcm_serializer as _make_raw_pcm_serializer,
)


# TTS 한국어 발음 변환은 utils/voice_tts_text.py 로 분리. 별칭으로 import.
from open_webui.utils.voice_tts_text import (
    tts_text_postprocess as _tts_text_postprocess,
    int_to_sino as _int_to_sino,
)


# PipeCat FrameProcessor 3종 + RAG 파이프라인 빌더는 utils/voice_rag_pipeline.py 로 분리.
from open_webui.utils.voice_rag_pipeline import build_rag_processor as _build_rag_processor

@router.websocket("/voice-ws")
async def voice_ws(websocket: WebSocket):
    """음성 챗봇 WebSocket 엔드포인트.

    인증 없음 (대도민 공개). request.app.state.config 에서 STT/TTS endpoint 동적 로드.
    """
    await websocket.accept()
    # Pipecat 의 FastAPIWebsocketTransport 가 websocket 만 받으므로 우리는 app 컨텍스트
    # 를 별도 변수로 보관해 RAG processor 에 Request-like 프록시로 전달.
    # Request 객체는 fastapi/starlette 에서 다음 attribute 들이 routinely 접근됨:
    #   - request.app.state.config (PersistentConfig)
    #   - request.state (per-request state — generate_chat_completion 에서 hasattr 체크)
    #   - request.headers / cookies (익명 도민이라 거의 안 씀)
    # 최소한 app + state 만 채워주면 RAG / TTS / STT 흐름 진행 가능.
    app = websocket.app
    from types import SimpleNamespace as _NS

    owi_request_proxy: Any = type(
        "_OwiRequestProxy",
        (),
        {"app": app, "state": _NS(), "headers": {}, "cookies": {}},
    )()

    # ── Pipecat 의존성 lazy import (uvicorn warm-up 부담 최소화) ──
    from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask, PipelineParams
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.services.openai.stt import OpenAISTTService
    from pipecat.services.openai.tts import OpenAITTSService
    from pipecat.transcriptions.language import Language
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketTransport,
        FastAPIWebsocketParams,
    )

    cfg = app.state.config

    # NOTE: AppConfig 의 attribute name 은 OWI main.py 에서 'STT_*'/'TTS_*' prefix 로
    # attach 됨 (env_name 'AUDIO_STT_*'/'AUDIO_TTS_*' 와 별개). 옛 코드가 env_name 으로
    # access 해서 KeyError → fallback ('qwen3-tts'/'Sohee') 만 사용해 옴 — DB 변경/
    # admin UI save 가 전혀 반영 안 되는 버그. 올바른 attribute name 으로 fix.
    stt_base = (
        getattr(cfg, "STT_OPENAI_API_BASE_URL", "")
        or "http://192.168.30.2:30210/v1"
    )
    stt_key = getattr(cfg, "STT_OPENAI_API_KEY", "") or "dummy"
    stt_model = getattr(cfg, "STT_MODEL", "") or "cohere-transcribe"

    tts_base = (
        getattr(cfg, "TTS_OPENAI_API_BASE_URL", "")
        or "http://192.168.30.2:30201/v1"
    )
    tts_key = getattr(cfg, "TTS_OPENAI_API_KEY", "") or "dummy"
    tts_model = getattr(cfg, "TTS_MODEL", "") or "qwen3-tts"
    tts_voice = getattr(cfg, "TTS_VOICE", "") or "Sohee"

    log.info(
        f"voice_ws connected | STT {stt_base}/{stt_model} | TTS {tts_base}/{tts_model}/{tts_voice}"
    )

    # Sample rate 정합 이슈:
    # - 클라이언트(브라우저) 마이크 입력은 16kHz 가 안정 (ScriptProcessor 기본).
    # - Qwen3-TTS endpoint 응답은 PCM 16-bit 이지만 실제 rate 가 24kHz 인 경우
    #   많음. transport 가 16kHz 로 가정하고 클라이언트도 16kHz 로 재생하면
    #   1.5배 느린 톤으로 들림 (남성 음성처럼).
    # → 출력만 24kHz 로 잡고, 클라이언트도 24kHz 로 재생.
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            audio_in_channels=1,
            audio_out_channels=1,
            add_wav_header=False,
            # serializer 미지정 시 Pipecat 가 inbound 메시지를 버린다. 자체 raw
            # PCM serializer 로 양방향 매핑. 입력 16kHz / 출력 24kHz 각자 처리.
            serializer=_make_raw_pcm_serializer(sample_rate=16000, channels=1),
            session_timeout=600,
        ),
    )

    # ── Silero VAD 파라미터 (카페 수준 소음 대응 — P2) ──
    #
    # 팀원 ems-interpret-ui 의 noisy preset (119 출동 현장) 을 참고해 "카페 수준"
    # middle ground 로 조정. 핵심은 "소리 작은 거는 들려도 무시" — min_volume 을
    # 올려 환경 소음 / 멀리서 들리는 대화 / 키보드 두드림 등 작은 RMS noise 가
    # VAD 통과 못 하게 한다.
    #
    # 진단 히스토리: 초기 적용 시 STT transcript 가 빈 결과를 받는 패턴이 동시에
    # 발생해 P2 가 원인일 가능성 의심했으나, 실제 원인은 _PUBLIC_STT_DOMAIN_PROMPT
    # 였음 (Cohere transcribe 가 긴 prompt 받으면 빈 결과 반환). STT prompt 제거
    # 후 P2 의 새 임계값은 안전하게 적용 가능.
    #
    # | 파라미터    | 옛값  | 새값  | 비고                                       |
    # |-------------|-------|-------|--------------------------------------------|
    # | confidence  | 0.4   | 0.55  | Silero 모델 확률. ↑ 시 misfire 억제        |
    # | min_volume  | 0.02  | 0.025 | RMS 임계. ↑ 시 작은 소음 차단 (사용자 요청)│
    # | start_secs  | 0.10  | 0.10  | 0.15 으로 올렸더니 첫 글자가 잘리는 문제   │
    # |             |       |       | ("답례품"→"압례품") 발생 — 0.10 으로 복원   │
    # | stop_secs   | 0.5   | 0.8   | 무음 견디기. ↑ 시 한 문장 두 chunk 분할 ↓ │
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=0.55,
                start_secs=0.10,
                stop_secs=0.8,
                min_volume=0.025,
            )
        ),
    )

    # STT prompt 제거 (commit a60bc1c) — 긴 prompt 는 Cohere 빈 결과,
    # 짧은 prompt 도 효과 미검증. 일단 prompt 없이 운영.
    stt = OpenAISTTService(
        base_url=stt_base,
        api_key=stt_key,
        model=stt_model,
        language=Language.KO_KR,
    )

    rag, caption_observer, barge_in = _build_rag_processor(owi_request_proxy, websocket=websocket)

    # Pipecat OpenAITTSService 는 voice 를 OpenAI 표준 화이트리스트
    # (alloy/echo/nova/...) 로 강제 검증해서 "Sohee" 입력 시 KeyError 가 난다.
    # 우리는 Qwen3-TTS endpoint 라 임의 voice 이름 통과시켜야 하므로 subclass 로
    # run_tts 만 override 해서 voice 를 그대로 endpoint 에 보낸다.
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

    # TTS 톤·말투 초기 세팅 — Qwen3-TTS 에 OpenAI 호환 instructions 인자로 전달.
    # 사용자 피드백: turn 간 말투 일관성 들쭉날쭉 → endpoint 측 prosody 변동 폭 큼.
    # instructions 를 더 단호하게 "동일한 한 명의 안내원" 정체성 명시 + 합성 seed
    # 고정으로 매 호출 재현성 ↑.
    _TTS_INSTRUCTIONS = (
        "당신은 전북도청 도민 안내원 '소희' 한 사람입니다. "
        "매번 동일한 정체성·억양·말투를 유지하세요. "
        "정중하고 또박또박한 한국어 표준어 아나운서 톤. "
        "차분하고 신뢰감 있는 어조로, 너무 느리지도 빠르지도 않게, "
        "감정 변동·연극적 강조 없이 일관된 평조로 발음해 주세요. "
        "외국어 발음 시도 금지 — 영문 약어가 들어와도 한국어 음으로 읽으세요."
    )
    _TTS_SPEED = 1.0
    # 합성 random seed 고정 — endpoint 가 seed 받으면 prosody 재현성 ↑.
    # OpenAI SDK 정식 param 아니라 extra_body 로 전달. endpoint 미지원 시 무시됨 (안전).
    _TTS_SEED = 42

    class _JeonbukOpenAITTSService(OpenAITTSService):
        async def run_tts(self, text: str, context_id: str):
            log.info(
                f"[voice_ws] TTS call voice={self._settings.voice!r} model={self._settings.model!r} "
                f"rate={self.sample_rate} len={len(text)}: {text[:60]!r}"
            )
            # 24kHz 16-bit mono 에서 1920 bytes = 40ms — chunk alignment 보장 +
            # 너무 크면 매 chunk 사이 클릭/끊김. 40ms 가 부드럽고 지연 ↓.
            FRAME_BYTES = 1920
            buf = bytearray()
            try:
                # instructions / speed 는 OpenAI SDK 가 dict 로 endpoint 에 그대로 전달.
                # Qwen3-TTS 가 instructions 안 받아도 무시될 뿐 합성은 정상.
                async with self._client.audio.speech.with_streaming_response.create(
                    input=text,
                    model=self._settings.model,
                    voice=self._settings.voice,
                    response_format="pcm",
                    instructions=_TTS_INSTRUCTIONS,
                    speed=_TTS_SPEED,
                    extra_body={"seed": _TTS_SEED},
                ) as r:
                    if r.status_code != 200:
                        error = await r.text()
                        log.warning(f"[voice_ws] TTS HTTP {r.status_code}: {error[:200]}")
                        yield ErrorFrame(error=f"TTS HTTP {r.status_code}")
                        return
                    await self.start_tts_usage_metrics(text)
                    total_bytes = 0
                    async for chunk in r.iter_bytes(FRAME_BYTES):
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        total_bytes += len(chunk)
                        # 짝수 byte 단위로 잘라서 yield (16-bit sample 안 잘림)
                        while len(buf) >= FRAME_BYTES:
                            out = bytes(buf[:FRAME_BYTES])
                            del buf[:FRAME_BYTES]
                            await self.stop_ttfb_metrics()
                            yield TTSAudioRawFrame(
                                out, self.sample_rate, 1, context_id=context_id
                            )
                    # 잔여 buffer 도 짝수 byte 로 맞춰 yield
                    if buf:
                        if len(buf) % 2 == 1:
                            buf.pop()  # 마지막 1 byte 폐기
                        if buf:
                            yield TTSAudioRawFrame(
                                bytes(buf), self.sample_rate, 1, context_id=context_id
                            )
                log.info(f"[voice_ws] TTS streaming done total={total_bytes}B")
            except Exception as e:
                log.exception(f"[voice_ws] TTS exception: {e}")
                yield ErrorFrame(error=f"TTS exception: {e}")

    # sample_rate=24000 — Qwen3-TTS endpoint 실제 응답 rate. transport 출력도 동일.
    log.info(f"[voice_ws] TTS init: model={tts_model!r} voice={tts_voice!r} rate=24000")
    tts = _JeonbukOpenAITTSService(
        base_url=tts_base,
        api_key=tts_key,
        model=tts_model,
        voice=tts_voice,
        sample_rate=24000,
    )

    pipeline = Pipeline(
        [
            transport.input(),
            vad_processor,
            # barge-in: VAD started + 봇 발화 중일 때 broadcast_interruption() 호출.
            # InterruptionFrame downstream → 모든 processor task cancel + recreate
            # → RAG 의 LLM loop 도 CancelledError 받고 종료. STT 위에 둬서 RAG
            # 가 LLM await 중이어도 동작.
            barge_in,
            stt,
            rag,
            tts,
            # TTS 와 transport 사이 — 첫 audio chunk 흐를 때 자막 reply push.
            caption_observer,
            transport.output(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,  # 사용자가 말하면 봇 TTS 즉시 중단
            enable_metrics=False,
        ),
    )

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except WebSocketDisconnect:
        log.info("voice_ws client disconnected")
    except Exception as e:
        log.exception(f"voice_ws pipeline error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────
# 음성 시뮬레이터 endpoint — STT/VAD frame 을 텍스트로 흉내내 turn-merge
# 로직을 brower 에서 테스트. PipeCat 의존 없이 _stream_public_llm_reply 만
# 사용해 진짜 RAG/LLM 응답까지 검증. 음성 마이크 없이도 turn 합치기, barge-in,
# history pop 동작을 눈으로 확인하기 위한 도구.
# ────────────────────────────────────────────────────────────────────────


@router.websocket("/voice-sim-ws")
async def voice_sim_ws(websocket: WebSocket):
    """텍스트로 STT/VAD 흉내내 turn merge 로직 테스트.

    Client → server (text JSON):
        {"type":"stt","text":"발화 텍스트"}    — TranscriptionFrame 등가
        {"type":"vad_start"}                  — VADUserStartedSpeakingFrame 등가

    Server → client (text JSON):
        {"type":"debug","text":"..."}          — restart/barge-in/cancel 진단
        {"type":"transcription","text":"..."}  — 사용자 발화 echo
        {"type":"reply","text":"..."}          — LLM 최종 응답
        {"type":"clear"}                       — 진행 중 응답 무효화 알림
    """
    import json as _json
    import uuid as _uuid

    await websocket.accept()
    log.info("[voice_sim] connected")

    app = websocket.app
    from types import SimpleNamespace as _NS

    owi_request_proxy: Any = type(
        "_OwiRequestProxy",
        (),
        {"app": app, "state": _NS(), "headers": {}, "cookies": {}},
    )()

    state = {
        "history": [],
        "generation_id": 0,
        "task": None,
        "pending": [],
        "active": [],
        "debounce": 0.85,
        # voice-ws 와 동일: 직전 reply 가 이미 사용자에게 전달됐는지.
        # True 면 다음 restart 에서 history pop / active merge skip → 새 질문 시작점 리셋.
        "reply_played": False,
        # reset 직후 LLM 호출 시 user_text 에 '직전 답변 끝, 새 발화에만 답해라' 안내 prefix 부착 신호.
        "post_reply_reset": False,
    }

    async def send(kind: str, text: str = ""):
        try:
            await websocket.send_text(_json.dumps({"type": kind, "text": text}))
        except Exception:
            pass

    async def debug(msg: str):
        log.info(f"[voice_sim] {msg}")
        await send("debug", msg)

    def is_current(gid: int) -> bool:
        return gid == state["generation_id"]

    def merge_segments(segs: list[str]) -> list[str]:
        out: list[str] = []
        for s in segs:
            s = (s or "").strip()
            if s and (not out or out[-1] != s):
                out.append(s)
        return out

    async def stop_current():
        await send("clear", "")
        t = state["task"]
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def generate_reply(gid: int, user_text: str):
        try:
            from open_webui.routers.public_chatbot import (
                _stream_public_llm_reply,
                _get_public_user,
                _humanize_reply as _hum,
            )

            user_obj = _get_public_user(owi_request_proxy)
            session_id = str(_uuid.uuid4())

            # 직전 reply 가 사용자에게 이미 전달된 후의 새 발화면 LLM 에 안내
            # (1) user_text prefix + (2) history 끝 system message 둘 다 inject.
            effective_user_text = user_text
            effective_history = state["history"]
            if state["post_reply_reset"]:
                effective_user_text = (
                    "(시스템 안내: 직전 사용자 질문에 대한 답변은 이미 완료되어 "
                    "사용자가 들었습니다. 이번 발화는 새 질문이거나, 직전 답변에 "
                    "대한 정정·후속 질문일 수 있습니다. 이전 대화 맥락은 자유롭게 "
                    "참조하되, 직전 질문의 답변을 통째로 반복하지는 마세요.)\n\n"
                    + user_text
                )
                effective_history = state["history"] + [
                    {
                        "role": "system",
                        "content": (
                            "위 대화의 직전 사용자 질문은 이미 답변이 완료되어 사용자가 "
                            "들었습니다. 다음 사용자 발화는 (a) 그 답변과 별개의 새 질문, "
                            "(b) 직전 답변에 대한 후속/심화 질문, (c) 직전 답변의 정정 요청 "
                            "중 하나입니다. 이전 대화 맥락은 그대로 활용해서 발화 의도를 "
                            "정확히 파악하되, 직전 사용자 질문 자체를 다시 처음부터 답변하지는 "
                            "마세요. 이번 새 발화의 의도에만 응답하세요."
                        ),
                    }
                ]
                await debug("injecting post-reply-reset guidance (prefix + system msg)")
                state["post_reply_reset"] = False

            full_reply = ""
            stream_failed = False
            async for kind, payload in _stream_public_llm_reply(
                owi_request_proxy, user_obj, effective_user_text, effective_history,
                session_id, voice_mode=False,
            ):
                if not is_current(gid):
                    await debug(f"stale stream ignored id={gid}")
                    return
                if kind == "delta":
                    full_reply += payload
                elif kind == "done":
                    final_text = payload or _hum(full_reply)
                    if not is_current(gid):
                        return
                    # history append 는 원본 user_text 로 (prefix 노출 X)
                    state["history"].append({"role": "user", "content": user_text})
                    state["history"].append({"role": "assistant", "content": final_text})
                    await send("reply", final_text)
                    # reply 가 사용자에게 전달된 시점 — 다음 turn 은 새 질문 시작점.
                    state["reply_played"] = True
                    await debug(f"reply id={gid} len={len(final_text)}")
                elif kind == "error":
                    stream_failed = True
                    await debug(f"stream error: {payload}")
            if stream_failed and not full_reply:
                await send("reply", "죄송해요. 답변 준비 중에 문제가 생겼어요.")
                state["reply_played"] = True
        except asyncio.CancelledError:
            raise

    async def generate_after_debounce(gid: int):
        try:
            await asyncio.sleep(state["debounce"])
            if not is_current(gid):
                return
            segs = [s for s in state["pending"] if s.strip()]
            state["pending"] = []
            state["active"] = segs
            user_text = " ".join(segs).strip()
            if not user_text:
                state["active"] = []
                return
            await debug(f"debounce passed, calling LLM id={gid} text={user_text[:60]!r}")
            await generate_reply(gid, user_text)
        except asyncio.CancelledError:
            await debug(f"generation cancelled id={gid}")
            raise
        finally:
            if is_current(gid):
                state["active"] = []

    async def restart_generation(user_text: str):
        # 직전 reply 가 이미 사용자에게 전달됐다면 (sim 의 reply 메시지 send 후) 그 turn 은
        # 종결 — history pop / active merge 안 하고 새 질문 시작점으로 리셋.
        if state["reply_played"]:
            await debug("previous reply was played — resetting merge window for new question")
            state["active"] = []
            state["pending"] = [user_text.strip()] if user_text.strip() else []
            state["reply_played"] = False
            state["post_reply_reset"] = True  # LLM 호출 시 안내 prefix 부착
            state["generation_id"] += 1
            gid = state["generation_id"]
            await debug(f"restart id={gid} pending={state['pending']}")
            await stop_current()
            state["task"] = asyncio.create_task(generate_after_debounce(gid))
            return

        prev_user = []
        if not state["active"] and state["history"]:
            if state["history"][-1].get("role") == "assistant":
                dropped = state["history"].pop()
                await debug(f"dropping completed assistant reply len={len(dropped.get('content') or '')}")
            if state["history"] and state["history"][-1].get("role") == "user":
                t = (state["history"].pop().get("content") or "").strip()
                if t:
                    prev_user.append(t)
        state["pending"] = merge_segments(
            [*prev_user, *state["active"], *state["pending"], user_text]
        )
        state["generation_id"] += 1
        gid = state["generation_id"]
        await debug(f"restart id={gid} pending={state['pending']}")
        await stop_current()
        state["task"] = asyncio.create_task(generate_after_debounce(gid))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "stt":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await send("transcription", text)
                await restart_generation(text)
            elif t == "vad_start":
                if state["task"] and not state["task"].done():
                    state["generation_id"] += 1
                    await debug(f"barge-in bumping id={state['generation_id']}")
                    await stop_current()
                else:
                    await debug("vad_start — no active task, skip")
    except WebSocketDisconnect:
        log.info("[voice_sim] disconnected")
    except Exception as e:
        log.exception(f"[voice_sim] error: {e}")
    finally:
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        try:
            await websocket.close()
        except Exception:
            pass
