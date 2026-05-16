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
import time
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from open_webui.env import GLOBAL_LOG_LEVEL
from open_webui.utils.voice_slot_tracker import (
    extract_slots,
    is_filler_only,
    merge_slots,
    compute_delta,
    flatten_slots_for_display,
)

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


_KOR_DIGITS = {"0":"공","1":"일","2":"이","3":"삼","4":"사","5":"오","6":"육","7":"칠","8":"팔","9":"구"}
_KOR_SINO = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]


def _int_to_sino(n: int) -> str:
    """정수 → 한자어 한국어 발음 (0~9999 범위, 그 이상은 단순 처리).

    예: 45 → 사십오, 2114 → 이천일백일십사, 100 → 일백, 22 → 이십이
    TTS 가 "45" 를 "포티 파이브" 또는 "사오" 로 어색하게 읽는 걸 자연스럽게 함.
    """
    if n == 0:
        return "영"
    if n < 10:
        return _KOR_SINO[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        s = ("" if tens == 1 else _KOR_SINO[tens]) + "십"
        if ones:
            s += _KOR_SINO[ones]
        return s
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        s = _KOR_SINO[hundreds] + "백"
        if rest:
            s += _int_to_sino(rest)
        return s
    if n < 10000:
        thousands, rest = divmod(n, 1000)
        s = _KOR_SINO[thousands] + "천"
        if rest:
            s += _int_to_sino(rest)
        return s
    if n < 10**8:
        man, rest = divmod(n, 10000)
        s = _int_to_sino(man) + "만"
        if rest:
            s += " " + _int_to_sino(rest)
        return s
    if n < 10**12:
        eok, rest = divmod(n, 10**8)
        s = _int_to_sino(eok) + "억"
        if rest:
            s += " " + _int_to_sino(rest)
        return s
    return str(n)  # 1조 이상은 그대로


def _tts_text_postprocess(text: str) -> str:
    """TTS 입력 텍스트 후처리 — 자막은 원본 그대로, TTS 만 한국어 발음으로 변환.

    - 전화번호 (063-280-2114 → 공육삼에 이팔공에 이일일사)
    - 백분율 (45% → 사십오 퍼센트)
    - 콤마 숫자 (2,000 → 이천)
    - URL (https://...) → "홈페이지"
    - 5자리 이상 단독 숫자 → 자릿수 발음
    - 단위 동반 숫자 (22억, 9만 4,800원 등) — 콤마 제거 후 자연 그대로 두면
      Sohee 가 한자어 발음으로 읽음. 콤마만 제거.

    참고: 너무 광범위하게 변환하면 LLM 답변 의도에서 멀어질 수 있어 자주 어색한
    패턴만 명시적으로 처리.
    """
    import re as _re

    def _digits_to_kor(d: str) -> str:
        return "".join(_KOR_DIGITS.get(c, c) for c in d)

    # 1. 전화번호 (063-280-2114 등)
    def _repl_phone(m):
        groups = m.group(0).split("-")
        return "에 ".join(_digits_to_kor(g) for g in groups)
    text = _re.sub(r"\b\d{2,4}-\d{3,4}-\d{4}\b", _repl_phone, text)
    text = _re.sub(r"\b\d{2,4}-\d{2,4}\b", _repl_phone, text)

    # 2. URL — Sohee 가 영어 알파벳 못 읽음
    text = _re.sub(r"https?://\S+", "홈페이지", text)

    # 3. 백분율 "45%" → "사십오 퍼센트" / "%" 단독은 "퍼센트"
    def _repl_pct(m):
        n = int(m.group(1))
        return f"{_int_to_sino(n)} 퍼센트"
    text = _re.sub(r"(\d{1,4})\s*%", _repl_pct, text)
    text = _re.sub(r"%", " 퍼센트 ", text)

    # 4. 콤마 숫자 (1,234,567) → 콤마 제거 후 한자어
    # "9억 4,800만 원" → 콤마만 빼면 "9억 4800만 원" — Sohee 가 자연스럽게 읽음
    def _repl_comma_num(m):
        return m.group(0).replace(",", "")
    text = _re.sub(r"\b\d{1,3}(?:,\d{3})+\b", _repl_comma_num, text)

    # 5. 5자리 이상 단독 숫자 (자릿수 발음으로) — 코드/식별자 같은 거
    text = _re.sub(r"\b\d{5,}\b", lambda m: _digits_to_kor(m.group(0)), text)

    return text


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
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
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

    class _BargeInBroadcaster(FrameProcessor):
        """봇 발화 중 사용자 VAD started 감지 시 Pipecat broadcast_interruption() 호출.

        Pipecat 1.1 의 정통 interruption 패턴:
        1) `broadcast_interruption()` → InterruptionTaskFrame upstream push
        2) Pipeline 이 받아 → InterruptionFrame downstream push
        3) 모든 FrameProcessor 의 process_task 가 자동 cancel + recreate
        4) 우리 RAG processor 의 async for LLM loop 도 CancelledError 받고 종료

        VAD started 가 봇 발화 중이 아닐 때 (사용자 첫 발화 / 연속 발화 사이) 는 무시 —
        false-positive interruption 방지. BotStartedSpeakingFrame / BotStoppedSpeakingFrame
        으로 봇 발화 상태 추적.

        이 processor 는 VAD 와 RAG 사이에 배치 → RAG 의 LLM loop 에 의존 안 함.
        """

        def __init__(self):
            super().__init__()
            self._bot_speaking: bool = False

        async def process_frame(self, frame, direction):  # type: ignore[override]
            await super().process_frame(frame, direction)

            if isinstance(frame, BotStartedSpeakingFrame):
                self._bot_speaking = True
                log.info("[voice_ws] bot started speaking")
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self._bot_speaking = False
                log.info("[voice_ws] bot stopped speaking")
            elif isinstance(frame, VADUserStartedSpeakingFrame) and self._bot_speaking:
                log.info("[voice_ws] barge-in detected — broadcasting interruption")
                try:
                    await self.broadcast_interruption()
                except Exception as e:
                    log.warning(f"[voice_ws] broadcast_interruption failed: {e}")

            await self.push_frame(frame, direction)

    class _AudioStartCaptionObserver(FrameProcessor):
        """첫 TTSAudioRawFrame 도착 시점에 자막 reply 를 push.

        commit 3c0c0de 시점에 검증된 "자막-음성 동시" 패턴. 이전엔 TTS 텍스트가
        140자/긴 합성 시간 + chunk 4800 + speed 1.05 라 자막 hold 시간이 길어
        체감 지연이 컸지만, 지금은 chunk 1920 + speed 1.0 + 짧은 텍스트로
        합성이 1~2초라 자막 hold 도 거의 안 느껴짐.
        """

        def __init__(self):
            super().__init__()
            self._pending_reply: str | None = None
            self._sent_for_current: bool = False

        def queue_reply(self, text: str):
            self._pending_reply = text
            self._sent_for_current = False

        def clear(self):
            self._pending_reply = None
            self._sent_for_current = False

        async def process_frame(self, frame, direction):  # type: ignore[override]
            from pipecat.frames.frames import TTSAudioRawFrame as _TTSAudioRawFrame

            await super().process_frame(frame, direction)

            if (
                not self._sent_for_current
                and self._pending_reply
                and isinstance(frame, _TTSAudioRawFrame)
            ):
                await _send_caption("reply", self._pending_reply)
                self._sent_for_current = True
                self._pending_reply = None

            await self.push_frame(frame, direction)

    class JeonbukRAGProcessor(FrameProcessor):
        """음성 STT 결과를 텍스트 챗봇과 같은 RAG/LLM 흐름으로 처리.

        자막 reply 는 직접 push 하지 않고 _AudioStartCaptionObserver 에
        queue 만 한다. observer 가 TTS 첫 audio chunk 시점에 자막을 push 해서
        음성-자막 동시 시작을 보장.

        멀티턴: 단일 WebSocket 세션 동안 history 누적.
        """

        def __init__(
            self,
            owi_request: Request,
            caption_observer: "_AudioStartCaptionObserver",
        ):
            super().__init__()
            self._owi_request = owi_request
            self._caption_observer = caption_observer
            self._history: list[dict] = []
            self._generation_id: int = 0
            self._generation_task: asyncio.Task | None = None
            self._pending_user_segments: list[str] = []
            self._active_user_segments: list[str] = []
            self._turn_debounce_secs: float = 0.85
            # 멀티턴 요약 filler — "그러니까 ...말씀이군요" 식 응답 직전 1줄.
            # 슬롯 누적 (지역·대상·나이·상태·의도) 으로 trigger 조건 판정.
            self._slots: dict[str, list[str]] = {}
            self._last_summary_filler_ts: float = 0.0
            self._summary_filler_cooldown_secs: float = 15.0
            self._min_chars_for_summary: int = 25
            self._min_turns_for_summary: int = 2

        def _is_current_generation(self, generation_id: int) -> bool:
            return generation_id == self._generation_id

        def _merge_turn_segments(self, segments: list[str]) -> list[str]:
            merged: list[str] = []
            for segment in segments:
                segment = (segment or "").strip()
                if segment and (not merged or merged[-1] != segment):
                    merged.append(segment)
            return merged

        async def _stop_current_generation(self):
            self._caption_observer.clear()
            await _send_caption("clear", "")
            try:
                await self.broadcast_interruption()
            except Exception as e:
                log.debug(f"[voice_ws] generation interruption broadcast failed: {e}")

            task = self._generation_task
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        async def _restart_generation(self, user_text: str):
            # 직전 턴이 이미 done 까지 가서 history 에 박혀 있으면, 그 응답을 무효화하고
            # 사용자 발화는 회수해 이번 합친 응답에 포함시킨다 (사용자 요구: 새 턴이 오면
            # 직전 응답 삭제, 두 발화 모두 합쳐 한 응답으로).
            prev_user_for_merge: list[str] = []
            if not self._active_user_segments and self._history:
                if self._history and self._history[-1].get("role") == "assistant":
                    dropped = self._history.pop()
                    log.info(
                        "[voice_ws] dropping completed assistant reply len=%s due to new turn",
                        len(dropped.get("content") or ""),
                    )
                if self._history and self._history[-1].get("role") == "user":
                    prev = self._history.pop()
                    text = (prev.get("content") or "").strip()
                    if text:
                        prev_user_for_merge.append(text)

            self._pending_user_segments = self._merge_turn_segments(
                [
                    *prev_user_for_merge,
                    *self._active_user_segments,
                    *self._pending_user_segments,
                    user_text,
                ]
            )
            self._generation_id += 1
            generation_id = self._generation_id
            log.info(
                "[voice_ws] restart generation id=%s pending=%s",
                generation_id,
                self._pending_user_segments,
            )

            await self._stop_current_generation()
            self._generation_task = asyncio.create_task(
                self._generate_after_debounce(generation_id)
            )

        async def _generate_after_debounce(self, generation_id: int):
            try:
                await asyncio.sleep(self._turn_debounce_secs)
                if not self._is_current_generation(generation_id):
                    return

                user_segments = [s for s in self._pending_user_segments if s.strip()]
                self._pending_user_segments = []
                self._active_user_segments = user_segments
                user_text = " ".join(user_segments).strip()
                if not user_text:
                    self._active_user_segments = []
                    return

                await _send_caption("phase", "understanding")
                # 멀티턴 누적 슬롯 갱신 + delta 계산 → 요약 filler 발동 여부 결정.
                # filler-only 발화 (추임새 단독) 는 슬롯 변화 0 으로 떨어져 자동 제외됨.
                new_slots = extract_slots(user_text)
                delta = compute_delta(self._slots, new_slots)
                self._slots = merge_slots(self._slots, new_slots)
                # 슬롯 chip frontend 로 push — 새로 추가된 게 있을 때만
                if delta.get("new_slot_count", 0) > 0:
                    import json as _json
                    flat = flatten_slots_for_display(self._slots)
                    await _send_caption("slots", _json.dumps(flat, ensure_ascii=False))
                if self._should_emit_summary_filler(user_text, delta):
                    await self._emit_summary_filler(generation_id, user_text)

                await _send_caption("phase", "searching")
                await self._generate_reply(generation_id, user_text)
            except asyncio.CancelledError:
                log.info("[voice_ws] generation task cancelled id=%s", generation_id)
                raise
            finally:
                if self._is_current_generation(generation_id):
                    self._active_user_segments = []

        def _should_emit_summary_filler(self, merged_user_text: str, delta: dict) -> bool:
            """요약 filler 발동 조건 — slot 변화 + 충분한 길이 + 쿨다운."""
            if delta.get("new_slot_count", 0) < 1:
                return False
            if len(merged_user_text) < self._min_chars_for_summary:
                return False
            if len(self._active_user_segments) < self._min_turns_for_summary:
                return False
            if time.time() - self._last_summary_filler_ts < self._summary_filler_cooldown_secs:
                return False
            return True

        async def _compose_summary_filler(self, user_text: str) -> str | None:
            """mini-LLM 으로 1줄 요약. 실패 시 룰 fallback."""
            slot_terms = flatten_slots_for_display(self._slots)
            fallback = (
                f"네, {', '.join(slot_terms[:4])} 관련해서 찾아볼게요."
                if slot_terms
                else "네, 잠시만요. 확인해볼게요."
            )
            try:
                from open_webui.utils.chat import generate_chat_completion
                from open_webui.routers.public_chatbot import _get_public_user

                cfg = self._owi_request.app.state.config
                model_id = (
                    getattr(cfg, "PUBLIC_CHATBOT_MODEL_ID", None) or "jeonbuk-public-chatbot"
                )
                prompt = (
                    "다음 사용자 발화를 친절한 한국어 안내원 톤으로 1문장으로 요약해."
                    " '그러니까, ...말씀이군요' 또는 '...이시군요' 식으로 자연스럽게."
                    " 60자 이내. 따옴표나 마크다운 없이.\n\n"
                    f"발화: {user_text}"
                )
                user_obj = _get_public_user(self._owi_request)
                resp = await asyncio.wait_for(
                    generate_chat_completion(
                        self._owi_request,
                        {
                            "model": model_id,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                        },
                        user_obj,
                    ),
                    timeout=1.5,
                )
                # generate_chat_completion 의 응답 dict 구조: choices[0].message.content
                text = (
                    (resp or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
                    or ""
                ).strip()
                # 따옴표 제거 + 너무 길면 자름
                text = text.strip("\"'`").strip()
                if not text:
                    return fallback
                if len(text) > 80:
                    text = text[:80].rstrip() + "."
                return text
            except asyncio.TimeoutError:
                log.info("[voice_ws] summary filler LLM timeout, falling back")
                return fallback
            except Exception as e:
                log.warning(f"[voice_ws] summary filler failed: {e}")
                return fallback

        async def _emit_summary_filler(self, generation_id: int, user_text: str):
            """요약 filler 1줄 생성 후 TTS push. generation_id 가드로 stale 자동 무시."""
            filler_text = await self._compose_summary_filler(user_text)
            if not filler_text:
                return
            if not self._is_current_generation(generation_id):
                return
            log.info(
                "[voice_ws] summary filler emitted id=%s text=%r",
                generation_id,
                filler_text[:80],
            )
            await self._push_tts_if_current(generation_id, _tts_text_postprocess(filler_text))
            self._last_summary_filler_ts = time.time()

        async def _push_tts_if_current(self, generation_id: int, text: str):
            if not self._is_current_generation(generation_id):
                return
            await self.push_frame(TTSSpeakFrame(text=text))

        async def _generate_reply(self, generation_id: int, user_text: str):
            import re as _re
            import uuid as _uuid

            _sentence_pat = _re.compile(
                r"(?<!\d)\.\s|(?<!\d)\.$|[!?]\s|[!?]$|다\s|요\s|니다\s|에요\s|어요\s"
            )
            sentence_buffer = ""
            full_reply = ""
            sentence_count = 0
            _stream_t0 = None
            MIN_SENT_LEN = 20

            try:
                from open_webui.routers.public_chatbot import (
                    _stream_public_llm_reply,
                    _get_public_user,
                    _humanize_reply as _hum,
                )

                user_obj = _get_public_user(self._owi_request)
                session_id = str(_uuid.uuid4())

                import time as _time

                stream_failed = False
                delta_count = 0
                async for kind, payload in _stream_public_llm_reply(
                    self._owi_request,
                    user_obj,
                    user_text,
                    self._history,
                    session_id,
                    voice_mode=True,
                ):
                    if not self._is_current_generation(generation_id):
                        log.info("[voice_ws] stale stream ignored id=%s", generation_id)
                        return

                    if kind == "delta":
                        now = _time.time()
                        if _stream_t0 is None:
                            _stream_t0 = now
                            log.info("[voice_ws] FIRST delta arrived id=%s", generation_id)
                        delta_count += 1
                        if delta_count <= 5 or delta_count % 20 == 0:
                            log.info(
                                f"[voice_ws] delta #{delta_count} +{(now - _stream_t0)*1000:.0f}ms len={len(payload)} id={generation_id}"
                            )
                        sentence_buffer += payload
                        full_reply += payload
                        while True:
                            m = _sentence_pat.search(sentence_buffer)
                            if not m:
                                break
                            end = m.end()
                            sentence = sentence_buffer[:end].strip()
                            if len(sentence) < MIN_SENT_LEN:
                                break
                            sentence_buffer = sentence_buffer[end:]
                            sentence_clean = _hum(sentence)
                            if sentence_clean.strip():
                                sentence_count += 1
                                tts_sentence = _tts_text_postprocess(sentence_clean)
                                log.info(
                                    f"[voice_ws] stream sentence #{sentence_count}: {tts_sentence[:50]!r} id={generation_id}"
                                )
                                await self._push_tts_if_current(generation_id, tts_sentence)
                                # 자막 sentence 단위 push — frontend 가 rotate 형태로 표시.
                                # 첫 sentence 시 speaking phase 도 같이 알림.
                                if self._is_current_generation(generation_id):
                                    if sentence_count == 1:
                                        await _send_caption("phase", "speaking")
                                    await _send_caption("caption_segment", sentence_clean)
                    elif kind == "done":
                        final_text = payload or _hum(full_reply)
                        if sentence_buffer.strip():
                            tail = _hum(sentence_buffer.strip())
                            if tail and len(tail) >= 5:
                                log.info(
                                    f"[voice_ws] stream tail: {tail[:40]!r} id={generation_id}"
                                )
                                await self._push_tts_if_current(
                                    generation_id, _tts_text_postprocess(tail)
                                )
                                if self._is_current_generation(generation_id):
                                    await _send_caption("caption_segment", tail)
                        if not self._is_current_generation(generation_id):
                            return
                        self._caption_observer.queue_reply(final_text)
                        self._history.append({"role": "user", "content": user_text})
                        self._history.append({"role": "assistant", "content": final_text})
                        log.info(
                            f"[voice_ws] stream done deltas={delta_count} len={len(final_text)} id={generation_id}"
                        )
                    elif kind == "error":
                        log.warning(f"[voice_ws] stream error: {payload}")
                        stream_failed = True

                if stream_failed and not full_reply:
                    raise RuntimeError("LLM stream produced no output")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._is_current_generation(generation_id):
                    return
                log.exception(f"voice_ws streaming failed, falling back to non-stream: {e}")
                try:
                    from open_webui.routers.public_chatbot import _run_chat_internal

                    reply_text, _sid, _src = await _run_chat_internal(
                        self._owi_request, user_text, self._history
                    )
                except Exception as e2:
                    log.exception(f"voice_ws fallback also failed: {e2}")
                    reply_text = "죄송해요. 답변 준비 중에 문제가 생겼어요."

                if not self._is_current_generation(generation_id):
                    return

                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply_text})
                self._caption_observer.queue_reply(reply_text)
                parts = _re.split(
                    r"(?<=[.!?])\s+|(?<=다)\s+|(?<=요)\s+", reply_text
                )
                parts = [p.strip() for p in parts if p.strip()] or [reply_text]
                for s in parts:
                    if not self._is_current_generation(generation_id):
                        return
                    if s.strip():
                        await self._push_tts_if_current(
                            generation_id, _tts_text_postprocess(s)
                        )

        async def process_frame(self, frame, direction):  # type: ignore[override]
            await super().process_frame(frame, direction)

            # 음성 흐름 추적용 로깅 (anomaly 발견 시 빠르게 봄)
            if isinstance(frame, VADUserStartedSpeakingFrame):
                log.info("[voice_ws] VAD: user started speaking")
                # 사용자가 끼어들면 STT 결과 도착 전에 진행 중 generation 을 먼저 멈춘다.
                # 짧은 발화 (LLM stream 이 1.5s 내 done) 케이스에서 cancel 못 잡는 문제 보강.
                # 단 history pop 은 _restart_generation 에서 — 여기선 in-flight 만 정리.
                if self._generation_task and not self._generation_task.done():
                    self._generation_id += 1  # in-flight stream 의 stale 체크가 즉시 False
                    log.info(
                        "[voice_ws] barge-in detected, bumping generation_id=%s and stopping",
                        self._generation_id,
                    )
                    await self._stop_current_generation()
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                log.info("[voice_ws] VAD: user stopped speaking")

            if isinstance(frame, TranscriptionFrame):
                user_text = (frame.text or "").strip()
                log.info(f"[voice_ws] STT transcript: {user_text!r}")
                if not user_text:
                    return

                # 사용자 발화 자막 즉시 push
                await _send_caption("transcription", user_text)
                await self._restart_generation(user_text)
                return

                # ── 진짜 LLM streaming + sentence-by-sentence TTS ──
                # _stream_public_llm_reply 가 SSE delta 별로 yield. LLM 첫 토큰이
                # 도착하는 순간부터 sentence buffer 에 누적, 종결 어미 감지 즉시
                # TTSSpeakFrame push → 첫 문장 음성 합성을 LLM 응답 완료 전에 시작.
                # 사용자 체감 응답 시간 큰 폭 단축.
                import re as _re
                import uuid as _uuid

                # 종결 매칭 — 소수점/약어 (예: "1.5%", "A.B.C") 잘못 끊지 않도록
                # "." 뒤에 숫자/영문 오면 무시. 한국어 종결어미 + 문장부호.
                _sentence_pat = _re.compile(
                    r"(?<!\d)\.\s|(?<!\d)\.$|[!?。…]|다\s|요\s|니다\s|에요\s|이에요\s|예요\s"
                )
                sentence_buffer = ""
                full_reply = ""
                sentence_count = 0
                _stream_t0 = None  # 첫 delta 도착 시점 — streaming 동작 진단
                # sentence 최소 길이 — 너무 짧은 단편은 다음과 합쳐서 push (악센트 깨짐 방지)
                MIN_SENT_LEN = 20

                try:
                    from open_webui.routers.public_chatbot import (
                        _stream_public_llm_reply,
                        _get_public_user,
                        _humanize_reply as _hum,
                    )

                    user_obj = _get_public_user(self._owi_request)
                    session_id = str(_uuid.uuid4())

                    import time as _time

                    stream_failed = False
                    delta_count = 0
                    async for kind, payload in _stream_public_llm_reply(
                        self._owi_request, user_obj, user_text, self._history,
                        session_id, voice_mode=True,
                    ):
                        if kind == "delta":
                            now = _time.time()
                            if _stream_t0 is None:
                                _stream_t0 = now
                                log.info("[voice_ws] FIRST delta arrived")
                            delta_count += 1
                            if delta_count <= 5 or delta_count % 20 == 0:
                                # 처음 5개 + 20개마다 delta 도착 시점 로깅
                                log.info(
                                    f"[voice_ws] delta #{delta_count} +{(now - _stream_t0)*1000:.0f}ms len={len(payload)}"
                                )
                            sentence_buffer += payload
                            full_reply += payload
                            # 문장 경계 감지 → 즉시 TTS 합성 시작
                            while True:
                                m = _sentence_pat.search(sentence_buffer)
                                if not m:
                                    break
                                end = m.end()
                                sentence = sentence_buffer[:end].strip()
                                # 20자 미만이면 buffer 그대로 두고 다음 종결까지 누적
                                if len(sentence) < MIN_SENT_LEN:
                                    break
                                sentence_buffer = sentence_buffer[end:]
                                sentence_clean = _hum(sentence)
                                if sentence_clean.strip():
                                    sentence_count += 1
                                    # TTS 만 한국어 발음 (자막은 원본)
                                    tts_sentence = _tts_text_postprocess(sentence_clean)
                                    log.info(
                                        f"[voice_ws] stream sentence #{sentence_count}: "
                                        f"{tts_sentence[:50]!r}"
                                    )
                                    await self.push_frame(TTSSpeakFrame(text=tts_sentence))
                        elif kind == "done":
                            final_text = payload or _hum(full_reply)
                            # 남은 buffer 도 한 문장으로 합성
                            if sentence_buffer.strip():
                                tail = _hum(sentence_buffer.strip())
                                if tail and len(tail) >= 5:
                                    log.info(f"[voice_ws] stream tail: {tail[:40]!r}")
                                    await self.push_frame(
                                        TTSSpeakFrame(text=_tts_text_postprocess(tail))
                                    )
                            # 자막은 humanized 원본 (숫자 그대로)
                            self._caption_observer.queue_reply(final_text)
                            self._history.append({"role": "user", "content": user_text})
                            self._history.append({"role": "assistant", "content": final_text})
                            log.info(
                                f"[voice_ws] stream done deltas={delta_count} len={len(final_text)}"
                            )
                        elif kind == "error":
                            log.warning(f"[voice_ws] stream error: {payload}")
                            stream_failed = True

                    if stream_failed and not full_reply:
                        raise RuntimeError("LLM stream produced no output")

                except Exception as e:
                    log.exception(f"voice_ws streaming failed, falling back to non-stream: {e}")
                    # 폴백: 기존 non-streaming 경로
                    try:
                        from open_webui.routers.public_chatbot import _run_chat_internal

                        reply_text, _sid, _src = await _run_chat_internal(
                            self._owi_request, user_text, self._history
                        )
                    except Exception as e2:
                        log.exception(f"voice_ws fallback also failed: {e2}")
                        reply_text = "죄송해요. 답변 준비 중에 문제가 생겼어요."
                    self._history.append({"role": "user", "content": user_text})
                    self._history.append({"role": "assistant", "content": reply_text})
                    self._caption_observer.queue_reply(reply_text)
                    # 문장 분할 후 push (제한 없음)
                    parts = _re.split(
                        r"(?<=[.!?。…])\s+|(?<=다)\s+|(?<=요)\s+", reply_text
                    )
                    parts = [p.strip() for p in parts if p.strip()] or [reply_text]
                    for s in parts:
                        if s.strip():
                            await self.push_frame(
                                TTSSpeakFrame(text=_tts_text_postprocess(s))
                            )
                return

            # 그 외 frame (오디오/제어) 은 그대로 다음 노드로
            await self.push_frame(frame, direction)

    caption_observer = _AudioStartCaptionObserver()
    rag = JeonbukRAGProcessor(request, caption_observer)
    barge_in = _BargeInBroadcaster()
    return rag, caption_observer, barge_in


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

    rag, caption_observer, barge_in = _build_rag_processor(owi_request_proxy, websocket=websocket)

    # Pipecat OpenAITTSService 는 voice 를 OpenAI 표준 화이트리스트
    # (alloy/echo/nova/...) 로 강제 검증해서 "Sohee" 입력 시 KeyError 가 난다.
    # 우리는 Qwen3-TTS endpoint 라 임의 voice 이름 통과시켜야 하므로 subclass 로
    # run_tts 만 override 해서 voice 를 그대로 endpoint 에 보낸다.
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

    # TTS 톤·말투 초기 세팅 — Qwen3-TTS 에 OpenAI 호환 instructions 인자로 전달.
    # 사용자 피드백: 디폴트 Sohee 가 "지친 말투" 같음 → 아나운서 톤으로 또박또박,
    # 명확하게, 약간 밝게. 도청 도민 안내원 컨셉이라 정중하고 신뢰감 있는 어조.
    _TTS_INSTRUCTIONS = (
        "정중하고 또박또박한 한국어 아나운서 톤으로 말씀해 주세요. "
        "표준어 억양으로 분명하게, 너무 느리지 않게, 신뢰감 있는 어조로 발음해 주세요. "
        "도청 도민 안내원이 시민에게 친절하게 설명한다고 생각하고 자연스럽게 읽어 주세요."
    )
    # speed 디폴트 1.0 (endpoint 표준 동작). 1.05 는 약간 빠르긴 하지만 endpoint
    # 측 합성 시간이 약간 늘어나고 톤이 어색해지는 부작용이 보여 디폴트로 복원.
    _TTS_SPEED = 1.0

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


async def _compose_summary_filler_for_sim(
    owi_request_proxy: Any, user_text: str, slots: dict[str, list[str]]
) -> str | None:
    """STT 시뮬레이터용 요약 filler 1줄. PipeCat 외부에서도 동작하도록 module-level."""
    slot_terms = flatten_slots_for_display(slots)
    fallback = (
        f"네, {', '.join(slot_terms[:4])} 관련해서 찾아볼게요."
        if slot_terms
        else "네, 잠시만요. 확인해볼게요."
    )
    try:
        from open_webui.utils.chat import generate_chat_completion
        from open_webui.routers.public_chatbot import _get_public_user

        cfg = owi_request_proxy.app.state.config
        model_id = getattr(cfg, "PUBLIC_CHATBOT_MODEL_ID", None) or "jeonbuk-public-chatbot"
        prompt = (
            "다음 사용자 발화를 친절한 한국어 안내원 톤으로 1문장으로 요약해."
            " '그러니까, ...말씀이군요' 또는 '...이시군요' 식으로 자연스럽게."
            " 60자 이내. 따옴표나 마크다운 없이.\n\n"
            f"발화: {user_text}"
        )
        user_obj = _get_public_user(owi_request_proxy)
        resp = await asyncio.wait_for(
            generate_chat_completion(
                owi_request_proxy,
                {
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                user_obj,
            ),
            timeout=1.5,
        )
        text = (
            (resp or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
            or ""
        ).strip()
        text = text.strip("\"'`").strip()
        if not text:
            return fallback
        if len(text) > 80:
            text = text[:80].rstrip() + "."
        return text
    except asyncio.TimeoutError:
        log.info("[voice_sim] summary filler LLM timeout, falling back")
        return fallback
    except Exception as e:
        log.warning(f"[voice_sim] summary filler failed: {e}")
        return fallback


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
        "slots": {},
        "last_summary_filler_ts": 0.0,
        "summary_filler_cooldown_secs": 15.0,
        "min_chars_for_summary": 25,
        "min_turns_for_summary": 2,
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

            full_reply = ""
            stream_failed = False
            async for kind, payload in _stream_public_llm_reply(
                owi_request_proxy, user_obj, user_text, state["history"],
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
                    state["history"].append({"role": "user", "content": user_text})
                    state["history"].append({"role": "assistant", "content": final_text})
                    await send("reply", final_text)
                    await debug(f"reply id={gid} len={len(final_text)}")
                elif kind == "error":
                    stream_failed = True
                    await debug(f"stream error: {payload}")
            if stream_failed and not full_reply:
                await send("reply", "죄송해요. 답변 준비 중에 문제가 생겼어요.")
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

            # 슬롯 누적 + 요약 filler 발동 결정 — JeonbukRAGProcessor 와 동일 로직
            new_slots = extract_slots(user_text)
            delta = compute_delta(state["slots"], new_slots)
            state["slots"] = merge_slots(state["slots"], new_slots)
            if delta.get("new_slot_count", 0) > 0:
                flat = flatten_slots_for_display(state["slots"])
                await send("slots", _json.dumps(flat, ensure_ascii=False))
            if (
                delta.get("new_slot_count", 0) >= 1
                and len(user_text) >= state["min_chars_for_summary"]
                and len(state["active"]) >= state["min_turns_for_summary"]
                and time.time() - state["last_summary_filler_ts"]
                >= state["summary_filler_cooldown_secs"]
            ):
                filler_text = await _compose_summary_filler_for_sim(
                    owi_request_proxy, user_text, state["slots"]
                )
                if filler_text and is_current(gid):
                    await send("summary_filler", filler_text)
                    await debug(f"summary_filler id={gid} text={filler_text[:60]!r}")
                    state["last_summary_filler_ts"] = time.time()

            await debug(f"debounce passed, calling LLM id={gid} text={user_text[:60]!r}")
            await generate_reply(gid, user_text)
        except asyncio.CancelledError:
            await debug(f"generation cancelled id={gid}")
            raise
        finally:
            if is_current(gid):
                state["active"] = []

    async def restart_generation(user_text: str):
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
