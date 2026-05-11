"""대도민 음성 챗봇 WebSocket endpoint (Pipecat 1.1 기반).

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

router = APIRouter()


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
                # 봇 답변 자막 push (TTS 합성 전에 — 시각적으론 즉시 보임)
                await _send_caption("reply", reply_text)

                # 멀티턴 히스토리 누적
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply_text})

                await self.push_frame(TextFrame(reply_text))
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
    app = websocket.app
    owi_request_proxy: Any = type(
        "_OwiRequestProxy",
        (),
        {"app": app},
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

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            audio_in_channels=1,
            audio_out_channels=1,
            add_wav_header=False,
            session_timeout=600,  # 10분 idle 시 자동 종료
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
    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=0.5,
                start_secs=0.18,
                stop_secs=0.4,
                min_volume=0.3,
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

    tts = OpenAITTSService(
        base_url=tts_base,
        api_key=tts_key,
        model=tts_model,
        voice=tts_voice,
        sample_rate=16000,
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
