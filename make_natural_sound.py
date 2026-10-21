# Creates ava.wav with a natural-sounding Microsoft voice, in the format the bot needs.
# Needs an internet connection. Uses the free, unofficial edge-tts tool.
# To change the words or voice, edit the lines below and run this file again,
# then restart the bot so it loads the new sound.

import asyncio
import wave
from pathlib import Path

import edge_tts
import miniaudio

MESSAGE = "Time's up."
VOICE = "en-US-AvaNeural"   # run preview_voices.py to hear the options
SPEED = "-10%"              # slower than normal; use "+0%" for normal speed
PITCH = "-8Hz"              # lower than normal; more negative = deeper, "+0Hz" = normal
VOLUME = "-10%"             # quieter than normal; "+0%" = normal

folder = Path(__file__).parent
mp3_path = folder / "ava.mp3"
wav_path = folder / "ava.wav"


async def download():
    await edge_tts.Communicate(MESSAGE, VOICE, rate=SPEED, pitch=PITCH, volume=VOLUME).save(str(mp3_path))


asyncio.run(download())

# Convert to WAV: 48000 Hz, 16-bit, stereo
audio = miniaudio.decode_file(
    str(mp3_path),
    output_format=miniaudio.SampleFormat.SIGNED16,
    nchannels=2,
    sample_rate=48000,
)
with wave.open(str(wav_path), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes(audio.samples.tobytes())

mp3_path.unlink()
print("Created ava.wav")
