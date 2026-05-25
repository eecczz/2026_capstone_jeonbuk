"""클라이언트 ↔ 서버 raw PCM 양방향 매핑 serializer.

⚠️ PipeCat 1.1 의 FastAPIWebsocketTransport 는 serializer 가 None 이면
`_receive_messages` 에서 모든 WS 메시지를 그냥 skip 한다
(transport/websocket/fastapi.py:305-306). 즉 serializer 안 주면 클라이언트가
보낸 PCM 이 VAD 까지 도달조차 못 함.

이 모듈은 가장 단순한 protocol 로 양방향 매핑:
  binary message ↔ raw PCM bytes (InputAudioRawFrame / OutputAudioRawFrame)

text 메시지 (자막 ack 등) 는 _send_caption 별도 채널을 쓰므로 여기선 무시.

사용처: voice_ws.py 의 voice-ws endpoint 가 FastAPIWebsocketTransport 의
serializer 인자로 이 함수의 반환을 넘김.
"""
from __future__ import annotations


def make_raw_pcm_serializer(sample_rate: int = 16000, channels: int = 1):
    """raw int16 LE mono PCM 양방향 serializer 인스턴스 반환.

    Lazy import — pipecat 의존성을 import time 에 끌어오지 않음 (main.py
    로딩 시 사이클 회피).
    """
    from pipecat.frames.frames import (
        Frame,
        InputAudioRawFrame,
        OutputAudioRawFrame,
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
