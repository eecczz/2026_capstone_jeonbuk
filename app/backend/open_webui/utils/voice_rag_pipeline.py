"""음성 챗봇 PipeCat FrameProcessor 집합 + RAG 파이프라인 빌더.

voice_ws.py 의 voice-ws endpoint 가 PipeCat Pipeline 을 조립할 때, RAG/
LLM/TTS/자막 흐름을 담당하는 3개의 FrameProcessor 와 그 closure 헬퍼들.

원래 voice_ws.py 안에 nested 로 있던 620줄짜리 _build_rag_processor 를
모듈 함수 build_rag_processor 로 옮긴 것. signature 와 closure 구조는
그대로 유지 — 호출 사이트 변경 없음.

────────────────────────────────────────────────────────────────────────
호출 그래프 (코드 리뷰 시 따라가기 좋은 순서)
────────────────────────────────────────────────────────────────────────
  build_rag_processor(request, websocket)
    │
    ├─ _send_caption(kind, text)    ← closure, websocket text JSON push
    │     호출 kind: transcription / reply / clear / phase_label /
    │                phase_overlay / vad / caption_segment / slots
    │
    ├─ class _BargeInBroadcaster (FrameProcessor)
    │     봇 발화 중 사용자 VAD 시작 감지 → broadcast_interruption.
    │     PipeCat 정통 barge-in 패턴. STT/RAG 와 무관 — VAD 위에 배치.
    │
    ├─ class _AudioStartCaptionObserver (FrameProcessor)
    │     첫 TTSAudioRawFrame 도착 시점에 자막 reply 메시지 push.
    │     자막-음성 동시 시작 보장.
    │
    └─ class JeonbukRAGProcessor (FrameProcessor)   ← 음성 흐름의 두뇌
          멀티턴 turn-merge + barge-in cancel + history pop + post-reply-reset.
          process_frame(frame, dir)
            ├─ BotStartedSpeakingFrame → _reply_audio_started = True
            ├─ VADUserStartedSpeakingFrame → audio_pause/orb listening
            ├─ VADUserStoppedSpeakingFrame → phase_label / orb thinking
            └─ TranscriptionFrame → _looks_like_korean → _restart_generation
                  │
                  ├─ _restart_generation(user_text)
                  │     ├─ _reply_audio_started True 면 reset 분기
                  │     │    (history 유지, post_reply_reset = True)
                  │     └─ 아니면 history pop + active merge
                  │
                  ├─ _generate_after_debounce(gid)
                  │     0.85s 디바운스 → user_text 합치기 → _generate_reply
                  │
                  └─ _generate_reply(gid, user_text)
                        ├─ post_reply_reset → effective_user_text + history 에
                        │     시스템 안내 inject
                        ├─ async for delta in _stream_public_llm_reply(...)
                        │     sentence 단위 _push_tts_if_current 호출
                        └─ done → _caption_observer.queue_reply + history 누적
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from fastapi import Request

from open_webui.utils.voice_tts_text import (
    tts_text_postprocess as _tts_text_postprocess,
)

log = logging.getLogger(__name__)


def build_rag_processor(request: Request, websocket=None):
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
                # reply 메시지는 frontend 의 pcAcceptAudio 를 true 로 켜는 핵심 트리거.
                # 이걸 안 보내면 모든 audio chunk 가 drop 되어 음성이 안 들린다.
                # 자막은 frontend setVoiceCaption noop + char-bubble 박스에 표시.
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
            # 봇 응답 음성이 한 번이라도 재생되기 시작했는지. True 이면 그 turn 은
            # 종결된 것으로 보고, 다음 _restart_generation 진입 시 history pop /
            # active merge 를 안 함 — 새 질문의 "합치는 시작점" 으로 리셋.
            # barge-in 으로 끝까지 재생 안 됐어도 True 면 그 응답은 사용자가 들은 것.
            self._reply_audio_started: bool = False
            # 직전 turn 의 응답이 재생된 후 reset 분기로 들어왔다는 표시. LLM 호출
            # 시점에 사용자 발화 앞에 안내 prefix 를 붙여 "이전 질문은 답변 완료,
            # 이번 발화에만 답해라" 명시. history (LLM 맥락) 는 보존되므로 정정/
            # 후속 질문은 자연 처리되되 이전 질문의 재답변은 막힌다.
            self._post_reply_reset: bool = False
            # ── Force commit timer (EMS-interpret-ui 패턴 응용) ──
            # 사용자가 끝없이 발화 (소음/말 계속) 해서 매 새 STT 가 진행 중 generation
            # 을 cancel 시켜 답변이 영원히 안 나오는 무한 지연 버그 차단.
            # 첫 STT 도착 시점 (pending 비어있던 상태에서 시작) 부터 절대 시간 카운트.
            # _pending_force_commit_secs 도달 후엔 새 STT 가 들어와도 진행 중
            # generation 을 cancel 하지 않고, 새 STT 는 다음 turn 의 pending 으로
            # 누적. LLM 호출 stream done + finally 시 None 으로 리셋.
            self._pending_started_at: float | None = None
            self._pending_force_commit_secs: float = 6.0
            # ── STT empty-result timeout ──
            # VAD stopped 후 N초 안에 TranscriptionFrame 이 안 오면 STT 가 빈 결과
            # 또는 noise 만 인식한 것 — frontend 의 "발화 정리 중" phase_label 이
            # 영원 남는 무한 지연 차단. clear 신호로 listening 모드 복귀.
            self._stt_timeout_task: asyncio.Task | None = None
            self._stt_timeout_secs: float = 3.0

        def _is_current_generation(self, generation_id: int) -> bool:
            return generation_id == self._generation_id

        def _cancel_stt_timeout(self):
            """STT timeout task 가 있으면 cancel — TranscriptionFrame 도착/새 VAD started 시 호출."""
            if self._stt_timeout_task and not self._stt_timeout_task.done():
                self._stt_timeout_task.cancel()
            self._stt_timeout_task = None

        async def _compose_summary_overlay(self, user_text: str) -> tuple[str, str] | None:
            """mini-LLM 한 호출로 두 결과 동시 추출 — STT noise 제거된 의미 일관.

            반환: (keyword, overlay_sentence) 튜플
              1) keyword — RAG retrieval query 용. 30자 이내 명사구. 조사/문장부호 X
                 (BGE M3 embedding 의 정확도 ↑)
              2) overlay_sentence — phase_overlay 화면 자막용 자연 한국어
                 "질문은 …입니다." 형식. (1) 과 의미 일관.

            예: 추임새·반복이 섞인 누적 발화 → 핵심 명사구 + "질문은 …입니다" 한 줄

            timeout 1.5s. 실패 시 None — caller 가 원문 자르기 fallback.
            """
            try:
                from open_webui.utils.chat import (
                    generate_chat_completion as _gen_chat,
                )
                from open_webui.routers.public_chatbot import (
                    _get_public_user as _get_pu,
                )

                cfg = self._owi_request.app.state.config
                # cleaning 은 짧은 JSON 출력만 필요하므로 fallback (mini 모델) 우선.
                # public_chatbot_model 은 RAG 답변용 큰 모델이라 응답 시간 4~11s 걸려
                # timeout 1.5~5s 안에 못 들어옴 → cleaning fail → noise query → 못 찾음.
                fallback_model = getattr(cfg, "PUBLIC_CHATBOT_BASE_MODEL", "Qwen3.5-4B")
                main_model = getattr(cfg, "PUBLIC_CHATBOT_MODEL_ID", "jeonbuk-public-chatbot")
                models = getattr(self._owi_request.app.state, "MODELS", None) or {}
                model_id = fallback_model if fallback_model in models else (
                    main_model if main_model in models else fallback_model
                )

                user_obj = _get_pu(self._owi_request)

                # 이전 대화 컨텍스트 — 지시대명사 ("거기서", "그것", "저거") 치환에 사용.
                # 가장 최근 user/assistant turn 만 (전체 history 는 prompt 길이 부담).
                _ctx_lines: list[str] = []
                if self._history:
                    # 최근 2~3 턴 (assistant ↔ user)
                    for turn in self._history[-4:]:
                        role = turn.get("role")
                        content = (turn.get("content") or "").strip()
                        if not content:
                            continue
                        # assistant 답변은 길 수 있으니 처음 80자만
                        content_short = content if len(content) <= 80 else content[:78] + "…"
                        _ctx_lines.append(f"  {role}: {content_short}")
                context_block = (
                    "\n이전 대화 (지시대명사 치환용 컨텍스트):\n" + "\n".join(_ctx_lines) + "\n"
                ) if _ctx_lines else ""

                prompt = (
                    "다음은 사용자가 음성으로 말한 **여러 발화가 누적되어 한 문자열로 합쳐진** "
                    "텍스트입니다. 한 사람이 같은 의도를 여러 번 다른 식으로 시도한 흐름이 "
                    "이어붙어 있고, STT 오인식·추임새·반복도 섞여 있어요.\n\n"
                    "두 결과를 JSON 한 줄로 출력해 주세요:\n"
                    " - keyword: 모든 발화의 의도를 통합한 RAG 검색용 키워드. 30자 이내. "
                    "명사구. 조사·문장부호 빼고.\n"
                    " - summary: 모든 발화의 의도를 합쳐 화면에 보여줄 자연 한국어 한 문장. "
                    "'질문은 …입니다.' 종결. 60자 이내.\n\n"
                    "원칙:\n"
                    "1) 마지막 발화만 보지 말 것 — 누적된 모든 시도를 모아 한 의도로 통합.\n"
                    "2) 추임새 ('어', '음', '아', '잠깐', '아니', '그게', '네' 등) 와 명백한 STT 노이즈 무시.\n"
                    "3) **원문 보존이 기본**. 특정 어휘로 임의 변환·끼워맞춤 금지. "
                    "원문이 무의미해 보여도 그대로 keyword 로 보내라.\n"
                    "4) 정정·구체화 흐름이면 마지막 정착된 의도를 우선.\n"
                    "5) keyword 와 summary 는 의미 일관.\n"
                    "6) 단답·추임새만 들어왔으면 keyword 를 빈 문자열 `\"\"` 로 출력. "
                    "임의 키워드로 추측하지 말 것. 예: '네' / '음' → {\"keyword\":\"\", \"summary\":\"네\"}.\n"
                    "7) **지시대명사·생략 치환 — 이전 대화 컨텍스트가 있으면 활용**:\n"
                    "   - '거기서', '그것', '저거', '그건' → 이전 답변/질문의 핵심 주제로 치환.\n"
                    "   - 주어 생략 ('알려줘', '뭐 있어?') → 이전 turn 의 주제 보충.\n"
                    "   - 예: 이전 user='답례품 뭐 있어?' → 현재 '거기서 농산물' → "
                    "     summary='질문은 답례품 중 농산물이 무엇인지에 대한 것입니다.'\n"
                    "   - 예: 이전 assistant='고향사랑기부제는…' → 현재 '신청 어떻게' → "
                    "     summary='질문은 고향사랑기부제 신청 방법입니다.'\n"
                    "   - 컨텍스트 무관해 보이면 치환하지 말고 현재 발화 그대로.\n\n"
                    "출력 형식 (정확히 이 JSON, 추가 설명 금지):\n"
                    '{"keyword": "<치환·정리된 명사구>", "summary": "질문은 …입니다."}\n'
                    f"{context_block}\n"
                    f"누적 발화 (공백 구분): {user_text}"
                )
                form_data = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
                try:
                    self._owi_request.state.metadata = {
                        "user_id": user_obj.id,
                        "chat_id": "",
                        "message_id": "summary-overlay",
                        "session_id": "summary-overlay",
                        "params": {},
                        "features": {},
                        "variables": {},
                        "filter_ids": [],
                        "tool_ids": None,
                        "tool_servers": None,
                        "files": None,
                        "model": models.get(model_id),
                        "direct": False,
                        "public_chatbot": True,
                    }
                except Exception:
                    pass

                # timeout 5s — mini 모델이 평소 1~2s, 가끔 3~4s 걸려서 1.5s 너무 빡빡.
                # cleaning fail 시 retrieval 이 raw STT noise 로 검색해 정답 chunk miss.
                resp = await asyncio.wait_for(
                    _gen_chat(self._owi_request, form_data, user=user_obj, bypass_filter=True),
                    timeout=5.0,
                )
                if isinstance(resp, dict):
                    choices = resp.get("choices") or []
                    if choices:
                        text = (choices[0].get("message") or {}).get("content", "").strip()
                        # mini-LLM 이 ```json ... ``` 로 감싸면 풀기
                        text = text.strip("`").strip()
                        if text.startswith("json"):
                            text = text[4:].strip()
                        # 첫 { 부터 마지막 } 까지만 (앞뒤 설명 잘라냄)
                        import json as _json
                        try:
                            i = text.index("{"); j = text.rindex("}")
                            data = _json.loads(text[i:j+1])
                            keyword = (data.get("keyword") or "").strip().strip("\"'`“”")
                            summary = (data.get("summary") or "").strip().strip("\"'`“”")
                            if keyword and summary and 3 <= len(keyword) <= 40 and 5 <= len(summary) <= 80:
                                log.info(f"[voice_ws] cleaned keyword={keyword!r} summary={summary!r}")
                                return (keyword, summary)
                        except Exception as pe:
                            log.info(f"[voice_ws] summary JSON parse fail: {pe} raw={text[:120]!r}")
                            # JSON 실패 시 한 줄 text 를 keyword 로 + wrap 으로 summary
                            line = text.split("\n")[0].strip().strip("\"'`“”.,!?")
                            if line and 3 <= len(line) <= 40:
                                return (line, f"질문은 {line}에 대한 것입니다.")
                return None
            except asyncio.TimeoutError:
                log.info("[voice_ws] summary overlay timed out (>1.5s)")
                return None
            except Exception as e:
                log.warning(f"[voice_ws] summary overlay failed: {e}")
                return None

        async def _stt_timeout_handler(self):
            """VAD stopped 후 _stt_timeout_secs 안 transcript 안 오면 발화 정리 중 phase_label 해제.

            Pipecat OpenAISTTService 가 빈/noise 인식 시 TranscriptionFrame 안 emit 해
            우리 process_frame 의 STT transcript 분기 진입 자체가 안 됨. frontend 의
            "발화 정리 중" 라벨이 영원 남는 무한 지연 버그 차단.
            """
            try:
                await asyncio.sleep(self._stt_timeout_secs)
                log.info(
                    "[voice_ws] STT empty-result timeout (%.1fs) — clearing phase_label",
                    self._stt_timeout_secs,
                )
                # clear 메시지 → frontend 의 clear 핸들러가 setVoiceCaption("") +
                # setMode("listening") 호출. 사용자 재발화 가능 상태로 복귀.
                await _send_caption("clear", "")
            except asyncio.CancelledError:
                pass

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
            # ── Force commit timer 보호 ──
            # 사용자가 끝없이 발화해서 매 새 STT 마다 진행 중 generation 이 cancel
            # 되어 답변 영원히 안 나오는 패턴 차단. pending 의 첫 STT 시점부터
            # _pending_force_commit_secs (6초) 도달 후엔 진행 중 generation 을
            # 더 이상 죽이지 않는다. 새 STT 는 다음 turn 의 pending 으로 누적만.
            # 단 reset 분기 (_reply_audio_started=True) 는 우선 — 답변 들은 후
            # 새 질문은 정상 흐름 유지.
            import time as _t
            if (
                not self._reply_audio_started
                and self._generation_task is not None
                and not self._generation_task.done()
                and self._pending_started_at is not None
                and (_t.time() - self._pending_started_at) >= self._pending_force_commit_secs
            ):
                self._pending_user_segments.append(user_text)
                # safety cap — 무한 누적 방지
                while (
                    len(self._pending_user_segments) > 6
                    or sum(len(s) for s in self._pending_user_segments) > 400
                ):
                    self._pending_user_segments.pop(0)
                log.info(
                    "[voice_ws] force-commit budget elapsed — queuing STT for next turn: %r "
                    "(elapsed=%.1fs, pending=%d items)",
                    user_text,
                    _t.time() - self._pending_started_at,
                    len(self._pending_user_segments),
                )
                return

            # 봇 응답 음성이 이미 한 번이라도 재생됐다면 그 turn 은 종결 — 다음 발화는
            # 새 질문의 시작점으로 리셋 (history 는 LLM 컨텍스트로 보존, 합치기만 안 함).
            if self._reply_audio_started:
                log.info(
                    "[voice_ws] previous reply was played — resetting merge window for new question"
                )
                self._active_user_segments = []
                self._pending_user_segments = [user_text.strip()] if user_text.strip() else []
                self._reply_audio_started = False
                self._post_reply_reset = True  # LLM 호출 시 안내 prefix 부착 신호
                # 새 turn 의 첫 STT — force_commit timer 시작
                self._pending_started_at = _t.time()
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
                return

            # 직전 턴 응답이 아직 음성 재생 시작 전 — barge-in 의도 있는 끼어들기로 보고
            # 직전 응답을 무효화하고 사용자 발화는 회수해 합친 응답으로 처리.
            #
            # 단 _post_reply_reset==True (이미 답변 끝나고 reset turn 진행 중) 면
            # 이전 답변/질문은 종결된 것 — pop 하면 안 됨. 같은 reset turn 의 후속
            # STT 가 이 분기로 빠져 이전 답변된 질문을 다시 컨텍스트에 합치는 버그
            # 가 있었다.
            prev_user_for_merge: list[str] = []
            if (
                not self._active_user_segments
                and self._history
                and not self._post_reply_reset
            ):
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
            # 첫 STT (timer 미시작) 면 시작 — 그 후 새 STT 가 와도 timer 리셋 X.
            # 6초 절대 한도 안에 LLM 호출 도달 보장.
            if self._pending_started_at is None:
                self._pending_started_at = _t.time()
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

                # 룰 기반 cleaning — keyword 만으로 자연 액션 메시지 표시 (mini-LLM polish 제거).
                # 자막은 "질문은 …입니다" 가 아니라 "지금 'XXX' 에 대해 찾아보고 있어요" 같이
                # 챗봇의 현재 행동을 보여주는 메시지로 대체. polish 호출 비용·지연 ↓.
                from open_webui.utils.voice_cleaning_rules import clean_user_text
                cleaned_keyword, _ = clean_user_text(user_text, self._history)
                log.info(f"[voice_ws] rule-cleaned keyword={cleaned_keyword!r}")

                if not cleaned_keyword:
                    # 단답·추임새 → cleaning + retrieval skip, history 만으로 LLM 답변
                    await _send_caption("phase_overlay", "다시 말씀해 주시겠어요? 잘 못 알아들었어요.")
                    await _send_caption("phase_label", "응답 준비 중")
                    await self._generate_reply(generation_id, user_text, retrieval_query="")
                    return

                # 행동 메시지 — keyword 가 짧으면 그대로, 길면 truncate
                _kw_disp = cleaned_keyword if len(cleaned_keyword) <= 30 else cleaned_keyword[:30] + "…"
                await _send_caption(
                    "phase_overlay",
                    f"지금 '{_kw_disp}'에 대해 자료를 찾아보고 있어요.",
                )
                await _send_caption("phase_label", "관련 자료 찾는 중")
                await self._generate_reply(generation_id, user_text, retrieval_query=cleaned_keyword)
            except asyncio.CancelledError:
                log.info("[voice_ws] generation task cancelled id=%s", generation_id)
                raise
            finally:
                if self._is_current_generation(generation_id):
                    self._active_user_segments = []

        async def _push_tts_if_current(self, generation_id: int, text: str):
            if not self._is_current_generation(generation_id):
                return
            await self.push_frame(TTSSpeakFrame(text=text))

        async def _generate_reply(self, generation_id: int, user_text: str, retrieval_query: str | None = None):
            import re as _re
            import uuid as _uuid

            # 문장부호 + 한국어 종결어미 결합만 매치. 단순 "다\s" / "요\s" 매치는
            # 짧은 종결 ("했다.", "이고요,") 마다 끊겨 TTS 호출이 빈번해지고,
            # 합성 시간 ≈ 재생 시간이 되어 frontend buffer 가 비는 무음 구간이 생김.
            _sentence_pat = _re.compile(
                r"(?<!\d)\.\s|(?<!\d)\.$|[!?]\s|[!?]$"
                r"|다\.\s|요\.\s|니다\.\s|에요\.\s|어요\.\s"
                r"|다\.$|요\.$|니다\.$|에요\.$|어요\.$"
            )
            sentence_buffer = ""
            full_reply = ""
            sentence_count = 0
            _stream_t0 = None
            MIN_SENT_LEN = 60

            try:
                from open_webui.routers.public_chatbot import (
                    _stream_public_llm_reply,
                    _get_public_user,
                    _humanize_reply as _hum,
                )

                user_obj = _get_public_user(self._owi_request)
                session_id = str(_uuid.uuid4())

                import time as _time

                # 직전 응답이 음성으로 사용자에게 이미 전달된 후의 새 발화라면 LLM 에
                # 안내 (1) user_text prefix + (2) history 끝 system message 둘 다
                # inject. prefix 만으로는 LLM 이 history 의 이전 질문을 다시
                # 답변하는 케이스가 잔재. system message 가 더 명확한 instruction.
                effective_user_text = user_text
                effective_history = self._history
                if self._post_reply_reset:
                    effective_user_text = (
                        "(시스템 안내: 직전 사용자 질문에 대한 답변은 이미 완료되어 "
                        "사용자가 들었습니다. 이번 발화는 새 질문이거나, 직전 답변에 "
                        "대한 정정·후속 질문일 수 있습니다. 이전 대화 맥락은 자유롭게 "
                        "참조하되, 직전 질문의 답변을 통째로 반복하지는 마세요.)\n\n"
                        + user_text
                    )
                    effective_history = self._history + [
                        {
                            "role": "system",
                            "content": (
                                "위 대화의 직전 사용자 질문은 이미 답변이 완료되어 사용자가 "
                                "음성으로 들었습니다. 다음 사용자 발화는 (a) 그 답변과 별개의 "
                                "새 질문, (b) 직전 답변에 대한 후속/심화 질문, (c) 직전 답변의 "
                                "정정 요청 중 하나입니다. 이전 대화 맥락은 그대로 활용해서 "
                                "발화 의도를 정확히 파악하되, 직전 사용자 질문 자체를 다시 "
                                "처음부터 답변하지는 마세요. 이번 새 발화의 의도에만 응답하세요."
                            ),
                        }
                    ]
                    log.info("[voice_ws] injecting post-reply-reset guidance (prefix + system msg)")
                    # NOTE: 여기서 False 로 리셋하지 않는다. 같은 reset turn 안에 새 STT
                    # 가 들어와 _restart_generation 이 다시 호출될 때도 history pop
                    # 차단 + system msg 재 inject 가 일관되게 동작해야 한다. reset
                    # 모드 종료는 새 답변 음성 도달 시점 (BotStartedSpeakingFrame).

                stream_failed = False
                delta_count = 0
                async for kind, payload in _stream_public_llm_reply(
                    self._owi_request,
                    user_obj,
                    effective_user_text,
                    effective_history,
                    session_id,
                    voice_mode=True,
                    retrieval_query=retrieval_query,
                ):
                    if not self._is_current_generation(generation_id):
                        log.info("[voice_ws] stale stream ignored id=%s", generation_id)
                        return

                    if kind == "rag_ready":
                        # RAG 검색 끝나고 hit source 도착. payload 는 OWI 의 그룹화된 sources —
                        # 각 group dict 안 "source" key 가 다시 dict 거나 "metadata" 가 list[dict].
                        # 안전하게 모든 str 후보만 추출해 phase_overlay 표시.
                        def _safe_str(v):
                            return v.strip() if isinstance(v, str) and v.strip() else ""
                        try:
                            picks = []
                            for group in (payload or [])[:3]:
                                if not isinstance(group, dict):
                                    continue
                                # 두 가지 모양 지원: group["source"] = dict 또는 group 자체가 source
                                cand = group.get("source") if isinstance(group.get("source"), dict) else group
                                if not isinstance(cand, dict):
                                    continue
                                title = _safe_str(cand.get("title")) or _safe_str(cand.get("name"))
                                inst  = _safe_str(cand.get("institution"))
                                # metadata 가 list[dict] 일 수 있음
                                metas = group.get("metadata") if isinstance(group.get("metadata"), list) else []
                                if not title and metas and isinstance(metas[0], dict):
                                    title = _safe_str(metas[0].get("title")) or _safe_str(metas[0].get("name"))
                                if not inst and metas and isinstance(metas[0], dict):
                                    inst  = _safe_str(metas[0].get("institution"))
                                if title and inst:
                                    picks.append(f"[{inst}] {title}")
                                elif title:
                                    picks.append(title)
                                elif inst:
                                    picks.append(inst)
                                if len(picks) >= 2:
                                    break
                            if picks:
                                join_txt = " · ".join(picks)
                                # 핵심 단어 + "검색중" — 동사 줄임 형태
                                msg = f"{join_txt} 검색중"
                                if len(msg) > 60:
                                    msg = msg[:58] + "…"
                                await _send_caption("phase_overlay", msg)
                                log.info(f"[voice_ws] rag_ready overlay: {msg!r}")
                        except Exception as _e:
                            log.warning(f"[voice_ws] rag_ready overlay failed: {_e}")
                        continue

                    if kind == "delta":
                        now = _time.time()
                        if _stream_t0 is None:
                            _stream_t0 = now
                            log.info("[voice_ws] FIRST delta arrived id=%s", generation_id)
                            # LLM 이 답변을 만들기 시작 — orb 는 여전히 thinking
                            await _send_caption("phase_label", "답변 작성 중")
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
                                # caption_segment 송신 롤백 — TTS sentence push 직전 caption
                                # 송신이 reply 메시지 이전에 frontend 도착 → pcAcceptAudio
                                # 미설정 상태로 audio 가 drop 되는 race. 자막 동기화는
                                # frontend 시간 추정 (showReplyChunks) fallback 으로.
                                await self._push_tts_if_current(generation_id, tts_sentence)
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
                        if not self._is_current_generation(generation_id):
                            return
                        self._caption_observer.queue_reply(final_text)
                        self._history.append({"role": "user", "content": user_text})
                        self._history.append({"role": "assistant", "content": final_text})
                        # 답변 생성 완료 — force_commit timer 종결 (다음 turn 의 첫 STT 가
                        # 새로 시작).
                        self._pending_started_at = None
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

            # 봇 응답 음성이 실제 재생 시작된 시점 마킹 — 다음 사용자 발화의 "합치는
            # 시작점" 을 리셋하는 신호. barge-in 으로 끝까지 못 들었어도 시작은 된 것.
            if isinstance(frame, BotStartedSpeakingFrame):
                if not self._reply_audio_started:
                    self._reply_audio_started = True
                    # reset turn 의 새 답변 음성이 사용자에게 도달 → reset 모드 종료.
                    # 그 후 새 STT 가 들어오면 다시 reset 분기 (이번엔 새로 시작).
                    self._post_reply_reset = False
                    # 음성 도달 시점 = turn 완전 종결. force_commit timer 도 정리
                    # (stream done 시점에서 이미 None 됐을 가능성 크지만 안전 확보).
                    self._pending_started_at = None
                    log.info("[voice_ws] reply audio started — merge window will reset on next turn")

            # 음성 흐름 추적용 로깅 (anomaly 발견 시 빠르게 봄)
            if isinstance(frame, VADUserStartedSpeakingFrame):
                log.info("[voice_ws] VAD: user started speaking")
                # 새 발화 시작 → 이전 STT timeout (있다면) 취소. 사용자 재발화 시도 중.
                self._cancel_stt_timeout()
                # 사용자 발화 시작 → frontend orb 기본 (검은 원) 으로
                await _send_caption("vad", "start")
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
                # 사용자 발화 끝 → frontend orb thinking + 단계 라벨
                await _send_caption("vad", "stop")
                await _send_caption("phase_label", "발화 정리 중")
                # STT timeout 시작 — N초 안 TranscriptionFrame 안 오면 clear (무한 지연 차단).
                self._cancel_stt_timeout()
                self._stt_timeout_task = asyncio.create_task(self._stt_timeout_handler())

            if isinstance(frame, TranscriptionFrame):
                # STT 결과 도착 → timeout task cancel.
                self._cancel_stt_timeout()
                user_text = (frame.text or "").strip()
                log.info(f"[voice_ws] STT transcript: {user_text!r}")
                if not user_text:
                    # 빈 transcript — phase_label clear (timeout 안 기다리고 즉시).
                    await _send_caption("clear", "")
                    return

                # STT 가 한국어가 아닌 외국어/영문 비스무리하게 transcribe 한 결과는
                # 노이즈로 drop — LLM 컨텍스트 오염을 막아 응답 품질과 TTS 발음
                # 안정성 보장. 도청 챗봇은 한국어 발화 가정.
                from open_webui.routers.public_chatbot import _looks_like_korean
                if not _looks_like_korean(user_text, threshold=0.5):
                    log.info(
                        f"[voice_ws] non-Korean STT transcript dropped (noise): {user_text!r}"
                    )
                    await _send_caption("clear", "")
                    return

                # 사용자 transcription 자막 비표시 — phase_overlay (cleaning 결과) 만
                # 화면에 표시해 정리된 1 문장만 깔끔히 보이게 함.
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



# voice_ws.py 호환 alias
_build_rag_processor = build_rag_processor
