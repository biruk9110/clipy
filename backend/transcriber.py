import os
from pathlib import Path
from faster_whisper import WhisperModel
import numpy as np
import subprocess
import json

_whisper_cache = {}

def get_whisper(model_size="tiny"):
    if model_size not in _whisper_cache:
        # device="cpu", compute_type="int8", cpu_threads=2 is memory-efficient and fast
        _whisper_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=2)
    return _whisper_cache[model_size]

def extract_audio(video_path: Path, output_audio_path: Path):
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(output_audio_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return output_audio_path

def get_audio_loudness(audio_path: Path):
    """Measures audio energy per second in streaming chunks to avoid memory spikes."""
    try:
        import wave
        with wave.open(str(audio_path), "rb") as wf:
            rate = wf.getframerate()
            sec_samples = max(1, rate)
            loudness = []
            while True:
                chunk_bytes = wf.readframes(sec_samples)
                if not chunk_bytes:
                    break
                chunk = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32)
                rms = np.sqrt(np.mean(chunk**2)) if len(chunk) > 0 else 0
                db = 20 * np.log10(rms + 1e-6)
                loudness.append(float(db))
            return loudness
    except Exception as e:
        print(f"[Transcriber] Loudness check error: {e}")
        return []

def decode_wav_to_float32(audio_path: Path) -> np.ndarray:
    """Reads a 16 kHz mono PCM WAV with the stdlib (no PyAV) -> float32 array."""
    import wave
    with wave.open(str(audio_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
    if sampwidth == 2:
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        samples = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        samples = np.frombuffer(frames, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def transcribe_audio(audio_path: Path, model_size="tiny"):
    try:
        model = get_whisper(model_size)

        # faster-whisper normally decodes via PyAV; some PyAV builds reject the
        # 'metadata_errors' kwarg (incompatible faster-whisper/PyAV pair), so fall
        # back to decoding the WAV ourselves and passing a numpy array.
        try:
            segments, info = model.transcribe(str(audio_path), word_timestamps=True, beam_size=1)
            segments = list(segments)  # force iteration inside the guarded block
        except TypeError as e:
            if "metadata_errors" not in str(e) and "unexpected keyword" not in str(e):
                raise
            try:
                samples, _rate = decode_wav_to_float32(audio_path)
            except Exception:
                # Re-encode any odd input to 16 kHz mono PCM, then read it back.
                import subprocess as _sp
                tmp = Path(audio_path).with_suffix(".reenc.wav")
                _sp.run(["ffmpeg", "-y", "-i", str(audio_path), "-vn", "-acodec", "pcm_s16le",
                         "-ar", "16000", "-ac", "1", str(tmp)], capture_output=True, timeout=600)
                samples, _rate = decode_wav_to_float32(tmp)
            segments, info = model.transcribe(samples, word_timestamps=True, beam_size=1)
            segments = list(segments)

        transcript_lines = []
        all_words = []
        
        loudness = get_audio_loudness(audio_path)
        median_loud = np.median(loudness) if len(loudness) > 0 else -30
        
        for seg in segments:
            seg_start = seg.start
            seg_end = seg.end
            text = seg.text.strip()
            
            # Check if loud
            is_loud = False
            if len(loudness) > int(seg_start):
                start_sec = int(seg_start)
                end_sec = min(len(loudness), int(seg_end) + 1)
                seg_max = max(loudness[start_sec:end_sec]) if start_sec < end_sec else -99
                if seg_max - median_loud > 10:
                    is_loud = True
            
            prefix = f"[{int(seg_start // 60):02d}:{int(seg_start % 60):02d}] "
            if is_loud:
                prefix += "[LOUD] "
            
            transcript_lines.append(f"{prefix}{text}")
            
            if seg.words:
                for w in seg.words:
                    all_words.append({
                        "word": w.word.strip(),
                        "start": w.start,
                        "end": w.end
                    })
                    
        full_transcript = "\n".join(transcript_lines)
        return {
            "text": full_transcript,
            "words": all_words,
            "language": getattr(info, "language", "en"),
            "duration": getattr(info, "duration", 0)
        }
    except Exception as e:
        print(f"[Transcriber] Transcription fallback: {e}")
        return {
            "text": "Audio track available",
            "words": [],
            "language": "en",
            "duration": 0
        }

