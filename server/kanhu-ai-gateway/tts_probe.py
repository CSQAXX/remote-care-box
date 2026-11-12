"""TTS 采样率体检。

要查的事: edge-tts 出来的 mp3 是多少 Hz, 网关转出来的 PCM 是不是真的 16k。

背景:
  edge-tts 默认返回 24kHz 单声道 mp3。
  pydub 的 set_frame_rate() 【只改元数据, 不重采样】——
  如果代码里用它把 24k 标成 16k, 板子按 16k 播, 声音会又快又尖。
  真正重采样得靠 ffmpeg (-ar) 或者自己做插值。

用法:  python tts_probe.py
"""
import asyncio
import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as gw  # noqa: E402

TEXT = "你好呀, 现在是下午三点多。今天过得怎么样?"


def mp3_rate():
    import edge_tts

    async def run():
        comm = edge_tts.Communicate(TEXT, gw.TTS_VOICE)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    data = asyncio.run(run())
    print("edge-tts mp3 大小: %d 字节" % len(data))
    return data


def probe_pydub(data):
    """pydub 那条路: 看 set_frame_rate 有没有真重采样。"""
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(io.BytesIO(data), format="mp3")
        print("pydub 原始: %d Hz, %d 通道, %d 样本, 时长 %.2f 秒"
              % (seg.frame_rate, seg.channels, len(seg),
                 len(seg) / 1000.0 / seg.frame_rate))
        out = seg.set_frame_rate(gw.SAMPLE_RATE).set_channels(1).set_sample_width(2)
        print("pydub set_frame_rate 之后: 样本 %d, 按 %d Hz 算时长 %.2f 秒"
              % (len(out), gw.SAMPLE_RATE, len(out) / 2 / gw.SAMPLE_RATE / 1000.0))
        print("  -> 如果这里样本数没变, 说明 set_frame_rate 根本没重采样 (元数据骗人)")
    except Exception as e:
        print("pydub 这条路失败: %s: %s" % (type(e).__name__, e))


def probe_final():
    """最终输出: 网关真正返回给板子的 PCM。"""
    pcm = gw.tts_synthesize(TEXT)
    print("网关最终 PCM: %d 字节 -> %.2f 秒 @%d Hz"
          % (len(pcm), len(pcm) / 2 / gw.SAMPLE_RATE, gw.SAMPLE_RATE))
    # 存成 wav 方便下载试听
    with wave.open("/app/tts_probe.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(gw.SAMPLE_RATE)
        w.writeframes(pcm)
    print("已存 /app/tts_probe.wav")


if __name__ == "__main__":
    data = mp3_rate()
    probe_pydub(data)
    probe_final()
