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


def _make_raw_pcm_serializer(sample_rate: int = 16000, channels: int = 1):
    """클라이언트와의 raw PCM 16-bit LE mono 양방향 매핑용 serializer.

    Pipecat FastAPIWebsocketTransport 는 serializer 없이는 inbound 메시지를
    버려서, audio frame 이 VAD 까지 도달 못 함. 가장 단순한 protocol 로 양방향
    매핑한다: binary message <=> raw PCM bytes.
    """
    from pipecat.frames.frames import (
        Frame,
        InputAudioRawFrame,
        OutputAudioRawFrame,
        StartFrame,
    )
    from pipecat.serializers.base_serializer import FrameSerializer

    class RawPcmSerializer(FrameSerializer):
        """raw int16 LE mono PCM <=> InputAudioRawFrame / OutputAudioRawFrame.

        FrameSerializer 의 abstract method 는 serialize / deserialize 두 개뿐이라
        그 두 개만 구현. setup 은 base 의 no-op 사용.
        """

        async def serialize(self, frame: Frame):  # outbound: 봇 → 클라이언트
            if isinstance(frame, OutputAudioRawFrame):
                return frame.audio  # raw PCM bytes 그대로 송신
            # 그 외 frame 은 무시 (text 자막은 별도 _send_caption 으로 push)
            return None

        async def deserialize(self, data):  # inbound: 클라이언트 → 서버
            if isinstance(data, (bytes, bytearray)):
                payload = bytes(data)
                if not payload:
                    return None
                return InputAudioRawFrame(
                    audio=payload,
                    sample_rate=sample_rate,
                    num_channels=channels,
                )
            # text 메시지 (자막 ack 등) 는 무시
            return None

    return RawPcmSerializer()


def _build_rag_processor(request: Request, websocket=None):
    """우리 RAG 흐름을 Pipecat FrameProcessor 로 감싼 인스턴스 생성.

    의존성을 import time 에 끌어오면 main.py 로딩 시 사이클 문제가 생길 수 있어
    함수 내부에서 lazy import.

    Args:
        request: OWI Request-like 프록시 (state.config 접근용)
        websocket: 자막 메시지 push 용 raw WebSocket. None 이면 자막 채널 없음.
    """
    import json as _json

    from pipecat.frames.frames import (
        TranscriptionFrame,
        TextFrame,
        TTSSpeakFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

    async def _send_caption(kind: str, text: str):
        """프론트엔드 자막 영역 업데이트용 text 메시지 push.

        Pipecat 의 binary 오디오 스트림과 같은 WebSocket 위에 text JSON 으로 전송.
        프론트 ws.onmessage 가 string 받으면 JSON.parse 해서 vc-user / vc-bot 자막
        bubble 을 갱신한다 (public-chatbot.html 안 setVoiceCaption).
        """
        if websocket is None:
            return
        try:
            await websocket.send_text(_json.dumps({"type": kind, "text": text}))
        except Exception as e:
            log.debug(f"voice_ws caption send failed: {e}")

    class JeonbukRAGProcessor(FrameProcessor):
        """음성 STT 결과를 텍스트 챗봇과 같은 RAG/LLM 흐름으로 처리.

        TranscriptionFrame(text) 이 들어오면 _run_chat_internal 을 await 하고
        결과 텍스트를 TextFrame 으로 푸시한다. TTS service 가 이 TextFrame 을
        받아 음성으로 합성 → transport.output() → 브라우저.

        멀티턴: 단일 WebSocket 세션 동안 history 누적.
        VAD frame 은 추적용으로 로깅만 하고 통과시킨다 (디버그 도움).
        """

        def __init__(self, owi_request: Request):
            super().__init__()
            self._owi_request = owi_request
            self._history: list[dict] = []

        async def process_frame(self, frame, direction):  # type: ignore[override]
            await super().process_frame(frame, direction)

            # 음성 흐름 추적용 로깅 (anomaly 발견 시 빠르게 봄)
            if isinstance(frame, VADUserStartedSpeakingFrame):
                log.info("[voice_ws] VAD: user started speaking")
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                log.info("[voice_ws] VAD: user stopped speaking")

            if isinstance(frame, TranscriptionFrame):
                user_text = (frame.text or "").strip()
                log.info(f"[voice_ws] STT transcript: {user_text!r}")
                if not user_text:
                    return

                # 사용자 발화 자막 즉시 push (RAG 응답 기다리지 않고)
                await _send_caption("transcription", user_text)

                try:
                    from open_webui.routers.public_chatbot import _run_chat_internal

                    reply_text, _session_id, _sources = await _run_chat_internal(
                        self._owi_request, user_text, self._history
                    )
                except Exception as e:
                    log.exception(f"voice_ws RAG failure: {e}")
                    reply_text = "죄송해요. 답변 준비 중에 문제가 생겼어요."

                log.info(f"[voice_ws] reply: {reply_text[:80]!r}")
                # 자막은 전체 답변 (사용자가 화면에서 다 읽을 수 있게)
                await _send_caption("reply", reply_text)

                # 멀티턴 히스토리 누적 (TTS 잘림과 무관하게 전체 본문)
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply_text})

                # TTS 는 짧게 잘라 보냄 — Qwen3-TTS 가 200~300자 한국어를 합성하는 데
                # 30~60초 걸려 사용자 체감 응답 시간이 길어지고 timeout 위험도 ↑.
                # 텍스트 챗봇에서 검증된 같은 자르기 함수 재사용 (140자/2문장).
                try:
                    from open_webui.routers.public_chatbot import _trim_text_for_tts

                    tts_text = _trim_text_for_tts(reply_text)
                except Exception:
                    tts_text = reply_text[:140]
                log.info(f"[voice_ws] TTS text len={len(tts_text)}: {tts_text[:60]!r}")
                # TextFrame 으로 보내면 TTSService 의 text aggregator 가 sentence 모은 후
                # LLMFullResponseEndFrame 을 기다린다. 우리는 LLM 응답 frame 흐름을 안
                # 쓰니까 영원히 buffering 됨. → TTSSpeakFrame 으로 utterance 단위 즉시 합성.
                await self.push_frame(TTSSpeakFrame(text=tts_text))
                return

            # 그 외 frame (오디오/제어) 은 그대로 다음 노드로
            await self.push_frame(frame, direction)

    return JeonbukRAGProcessor(request)


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

    stt_base = (
        getattr(cfg, "AUDIO_STT_OPENAI_API_BASE_URL", "")
        or "http://192.168.30.2:30210/v1"
    )
    stt_key = getattr(cfg, "AUDIO_STT_OPENAI_API_KEY", "") or "dummy"
    stt_model = getattr(cfg, "AUDIO_STT_MODEL", "") or "cohere-transcribe"

    tts_base = (
        getattr(cfg, "AUDIO_TTS_OPENAI_API_BASE_URL", "")
        or "http://192.168.30.2:30201/v1"
    )
    tts_key = getattr(cfg, "AUDIO_TTS_OPENAI_API_KEY", "") or "dummy"
    tts_model = getattr(cfg, "AUDIO_TTS_MODEL", "") or "qwen3-tts"
    tts_voice = getattr(cfg, "AUDIO_TTS_VOICE", "") or "Sohee"

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

    # Pipecat 1.1 에서는 VAD 가 Transport params 가 아닌 별도 Pipeline 노드로 들어간다.
    #
    # 파라미터 튜닝 의도:
    # - min_volume=0.3: 브라우저 마이크 입력은 자동 게인이 약해 기본 0.6 으로는 발화
    #   시작 자체를 못 잡는 경우가 많다. 도민이 작게 말해도 인지하도록 낮춤.
    # - confidence=0.5: 한국어 + 잡음 환경에서 0.65 는 보수적이라 stop 감지가 늦어
    #   "계속 듣는 모드가 안 멈춤" 증상으로 이어짐. 0.5 로 적극화.
    # - start_secs=0.18: 발화 시작 빠르게 포착.
    # - stop_secs=0.4: 사용자가 잠시 텀 두면 turn 끝났다고 판단. Smart Turn V3 가
    #   다시 한 번 "정말 발화 끝인지" 추가 판정하므로 0.4 로 짧게 두어도 안전.
    # Silero VAD 파라미터 — 이전 자체 RMS-기반 VAD 에서 검증된 임계값 그대로 이식:
    # - min_volume=0.02: public-chatbot.html 의 VAD_SPEECH_RMS=0.020 과 동일 스케일.
    #   브라우저 ScriptProcessorNode 입력 RMS 가 일반 발화 시 0.02~0.1 범위라
    #   default 0.6 은 거의 모든 발화를 컷오프. 0.02 가 실제로 동작한 값.
    # - start_secs=0.10: SPEECH_HOLD_MS=100ms 와 매치.
    # - stop_secs=0.5: 발화 텀 흡수. Smart Turn V3 가 추가 판단.
    # - confidence=0.4: SileroVAD model 출력 (0~1) threshold. default 0.7 은 너무
    #   보수적이라 한국어 + 작은 마이크 입력에서 못 잡음.
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=0.4,
                start_secs=0.10,
                stop_secs=0.5,
                min_volume=0.02,
            )
        ),
    )

    stt = OpenAISTTService(
        base_url=stt_base,
        api_key=stt_key,
        model=stt_model,
        language=Language.KO_KR,
    )

    rag = _build_rag_processor(owi_request_proxy, websocket=websocket)

    # Pipecat OpenAITTSService 는 voice 를 OpenAI 표준 화이트리스트
    # (alloy/echo/nova/...) 로 강제 검증해서 "Sohee" 입력 시 KeyError 가 난다.
    # 우리는 Qwen3-TTS endpoint 라 임의 voice 이름 통과시켜야 하므로 subclass 로
    # run_tts 만 override 해서 voice 를 그대로 endpoint 에 보낸다.
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

    class _JeonbukOpenAITTSService(OpenAITTSService):
        async def run_tts(self, text: str, context_id: str):
            log.info(
                f"[voice_ws] TTS call voice={self._settings.voice!r} model={self._settings.model!r} "
                f"rate={self.sample_rate} len={len(text)}: {text[:60]!r}"
            )
            # Pipecat OpenAITTSService 의 chunk_size 디폴트가 0 이라 iter_bytes 가
            # 임의 크기 chunk 를 반환한다. 24kHz 16-bit mono 에서 chunk 크기가
            # 홀수면 한 sample 이 두 chunk 에 걸쳐서 클라이언트 측 Int16Array
            # 변환 시 misalignment → 톤이 낮아지고 잡음 섞여 "느린 남성" 처럼 들림.
            # → 명시적 4800 bytes (100ms @ 24kHz 16-bit mono) 로 yield + buffer 잔여.
            FRAME_BYTES = 4800
            buf = bytearray()
            try:
                async with self._client.audio.speech.with_streaming_response.create(
                    input=text,
                    model=self._settings.model,
                    voice=self._settings.voice,
                    response_format="pcm",
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
            vad_processor,  # VADUserStarted/StoppedSpeakingFrame push → STT 가 utterance 경계 인지
            stt,
            rag,
            tts,
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
