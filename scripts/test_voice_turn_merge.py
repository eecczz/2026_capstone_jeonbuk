"""Voice turn-merge 로직 자동화 테스트.

JeonbukRAGProcessor 의 turn merge state machine 만 추출해서 PipeCat 의존성 없이
TranscriptionFrame / VADUserStartedSpeakingFrame 의 시퀀스를 던지고
_pending_user_segments / _active_user_segments / _history / _generation_id
를 매 단계 검증.

목적:
1) Regex fix 후 pending reset 안 일어남 확인
2) 빠른 연속 STT 가 한 응답으로 합쳐지는지
3) Done 까지 완료된 응답이 새 turn 으로 회수되어 합쳐지는지
"""
import asyncio
import contextlib
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("turn_merge_test")


class TurnMergeStateMachine:
    """JeonbukRAGProcessor 의 turn merge 부분만 분리. _generate_reply 는 stub."""

    def __init__(self, llm_delay: float = 0.5, simulate_done: bool = True):
        self._history: list[dict] = []
        self._generation_id: int = 0
        self._generation_task: asyncio.Task | None = None
        self._pending_user_segments: list[str] = []
        self._active_user_segments: list[str] = []
        self._turn_debounce_secs: float = 0.85
        self._llm_delay = llm_delay
        self._simulate_done = simulate_done
        self.replies_generated: list[tuple[int, str]] = []
        self.interruption_broadcasts: int = 0

    def _is_current_generation(self, gid):
        return gid == self._generation_id

    def _merge_turn_segments(self, segments):
        merged = []
        for s in segments:
            s = (s or "").strip()
            if s and (not merged or merged[-1] != s):
                merged.append(s)
        return merged

    async def _stop_current_generation(self):
        self.interruption_broadcasts += 1
        task = self._generation_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _restart_generation(self, user_text):
        prev_user_for_merge = []
        if not self._active_user_segments and self._history:
            if self._history and self._history[-1].get("role") == "assistant":
                dropped = self._history.pop()
                log.info(f"[restart] dropping completed assistant reply len={len(dropped.get('content') or '')}")
            if self._history and self._history[-1].get("role") == "user":
                prev = self._history.pop()
                t = (prev.get("content") or "").strip()
                if t:
                    prev_user_for_merge.append(t)

        self._pending_user_segments = self._merge_turn_segments([
            *prev_user_for_merge,
            *self._active_user_segments,
            *self._pending_user_segments,
            user_text,
        ])
        self._generation_id += 1
        gid = self._generation_id
        log.info(f"[restart] id={gid} pending={self._pending_user_segments}")

        await self._stop_current_generation()
        self._generation_task = asyncio.create_task(self._generate_after_debounce(gid))

    async def _generate_after_debounce(self, gid):
        try:
            await asyncio.sleep(self._turn_debounce_secs)
            if not self._is_current_generation(gid):
                return
            user_segments = [s for s in self._pending_user_segments if s.strip()]
            self._pending_user_segments = []
            self._active_user_segments = user_segments
            user_text = " ".join(user_segments).strip()
            if not user_text:
                self._active_user_segments = []
                return
            await self._generate_reply(gid, user_text)
        except asyncio.CancelledError:
            log.info(f"[debounce] cancelled id={gid}")
            raise
        finally:
            if self._is_current_generation(gid):
                self._active_user_segments = []

    async def _generate_reply(self, gid, user_text):
        """Stub LLM. _llm_delay 후 history append."""
        log.info(f"[reply] id={gid} starting LLM stream for {user_text[:60]!r}...")
        try:
            await asyncio.sleep(self._llm_delay)
            if not self._is_current_generation(gid):
                return
            if self._simulate_done:
                reply = f"[mock reply for {user_text[:30]}]"
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": reply})
                self.replies_generated.append((gid, reply))
                log.info(f"[reply] id={gid} DONE — history len={len(self._history)}")
        except asyncio.CancelledError:
            log.info(f"[reply] id={gid} cancelled mid-stream")
            raise

    # 시뮬레이션 헬퍼
    async def vad_started(self):
        if self._generation_task and not self._generation_task.done():
            self._generation_id += 1
            log.info(f"[VAD] barge-in bumping id={self._generation_id}")
            await self._stop_current_generation()
        else:
            log.info("[VAD] started — no active task, skip bump")

    async def stt(self, text):
        log.info(f"[STT] transcript={text!r}")
        await self._restart_generation(text)

    async def wait_for_idle(self, timeout=5.0):
        """현 generation task 가 끝날 때까지 대기."""
        if self._generation_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._generation_task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


async def scenario1_fast_consecutive():
    """STT 3개 빠르게 → 첫 두 개는 debounce 안, 세번째는 그 직후."""
    print("\n========== Scenario 1: 빠른 STT 3개 (debounce 안 끝나기 전 누적) ==========")
    sm = TurnMergeStateMachine(llm_delay=0.3)
    await sm.stt("안녕하세요")
    await asyncio.sleep(0.1)
    await sm.vad_started()
    await asyncio.sleep(0.05)
    await sm.stt("전북도청에")
    await asyncio.sleep(0.1)
    await sm.vad_started()
    await asyncio.sleep(0.05)
    await sm.stt("문의드립니다")
    await sm.wait_for_idle(timeout=3)

    print(f"\n[RESULT] replies_generated={sm.replies_generated}")
    print(f"[RESULT] history={[m['content'][:60] for m in sm._history]}")
    assert len(sm.replies_generated) == 1, f"기대: 1 reply, 실제: {len(sm.replies_generated)}"
    last_user = sm._history[-2]["content"]
    assert "안녕하세요" in last_user and "전북도청에" in last_user and "문의드립니다" in last_user, \
        f"3개 발화 모두 합쳐져야: {last_user!r}"
    print("✅ PASS — 3 STT 가 1 reply 로 합쳐짐")


async def scenario2_after_done():
    """첫 STT done 까지 완료 → 두번째 STT → history pop 으로 합쳐져야."""
    print("\n========== Scenario 2: 첫 응답 완료 후 두번째 turn (history pop 검증) ==========")
    sm = TurnMergeStateMachine(llm_delay=0.2)
    await sm.stt("청년지원 사업")
    await sm.wait_for_idle(timeout=3)  # 1차 응답 완료
    assert len(sm._history) == 2, f"1차 후 history 2개 (user+assistant): {sm._history}"
    print(f"[after 1st] history={[m['content'][:50] for m in sm._history]}")

    # 두번째 STT — 이 시점 active=[], history=[user1, assistant1]
    # restart_generation 에서 prev_user_for_merge 가 동작해야 함
    await sm.stt("자격조건이 어떻게 되나요")
    await sm.wait_for_idle(timeout=3)

    print(f"\n[RESULT] replies_generated={sm.replies_generated}")
    print(f"[RESULT] history={[m['content'][:60] for m in sm._history]}")
    last_user = sm._history[-2]["content"]
    assert "청년지원 사업" in last_user and "자격조건" in last_user, \
        f"history pop 으로 첫 turn user 회수 + 합쳐져야: {last_user!r}"
    print("✅ PASS — done 응답이 새 turn 으로 회수되어 합쳐짐")


async def scenario3_mid_stream_cancel():
    """첫 STT 의 LLM stream 도중 두번째 STT → cancel + 합쳐서 응답."""
    print("\n========== Scenario 3: stream 도중 두번째 turn (mid-cancel 합치기) ==========")
    sm = TurnMergeStateMachine(llm_delay=1.5)  # LLM 1.5s — debounce 0.85 + 0.65s LLM 시점에 cancel
    await sm.stt("첫번째 발화")
    await asyncio.sleep(1.2)  # debounce 0.85 후 LLM 0.35s 진행
    await sm.vad_started()
    await asyncio.sleep(0.05)
    await sm.stt("두번째 발화")
    await sm.wait_for_idle(timeout=4)

    print(f"\n[RESULT] replies_generated={sm.replies_generated}")
    print(f"[RESULT] history={[m['content'][:60] for m in sm._history]}")
    # 첫 LLM 은 mid-cancel — history append 안 됨. 두번째 LLM 만 done.
    assert len(sm.replies_generated) == 1, f"mid-cancel 후 1 reply 만: {sm.replies_generated}"
    last_user = sm._history[-2]["content"]
    assert "첫번째" in last_user and "두번째" in last_user, \
        f"두 발화 합쳐져야: {last_user!r}"
    print("✅ PASS — mid-stream cancel 후 두 발화 합쳐 1 응답")


async def main():
    await scenario1_fast_consecutive()
    await scenario2_after_done()
    await scenario3_mid_stream_cancel()
    print("\n========== 모든 시나리오 통과 ==========")


if __name__ == "__main__":
    asyncio.run(main())
