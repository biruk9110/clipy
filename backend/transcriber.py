import os
from pathlib import Path
from faster_whisper import WhisperModel
import numpy as np
import subprocess
import json

_whisper_cache = {}

def get_whisper(model_size="tiny"):
    if model_size not in _whisper_cache:
        # device="cpu", compute_type="int8" is fast and works on all PCs and cloud instances
        _whisper_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
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
    """Measures audio energy per second to find screams, laughter, loud spikes."""
    try:
        import wave
        with wave.open(str(audio_path), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
            
        sec_samples = rate
        num_secs = int(len(samples) / sec_samples)
        if num_secs == 0:
            return []
        
        loudness = []
        for i in range(num_secs):
            chunk = samples[i * sec_samples : (i + 1) * sec_samples]
            rms = np.sqrt(np.mean(chunk**2)) if len(chunk) > 0 else 0
            db = 20 * np.log10(rms + 1e-6)
            loudness.append(float(db))
        return loudness
    except Exception as e:
        print(f"[Transcriber] Loudness check error: {e}")
        return []

def transcribe_audio(audio_path: Path, model_size="tiny"):
    model = get_whisper(model_size)
    segments, info = model.transcribe(str(audio_path), word_timestamps=True)
    
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
        "language": info.language,
        "duration": info.duration
    }

