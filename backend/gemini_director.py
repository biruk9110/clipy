import json
import re
import os
import warnings
warnings.filterwarnings("ignore")
import google.generativeai as genai
from .config import get_settings

MOMENTS_PROMPT = """You are a master viral social media editor for TikTok, YouTube Shorts, and Instagram Reels.
You are given a timestamped transcript of a video or livestream, with annotations for audio loudness jumps [LOUD +NdB].

Your goal: Find the {want} STRONGEST, most engaging standalone moments for 9:16 vertical shorts (30 to 60 seconds long).
Viral categories:
- first_reaction: First impressions, genuine shock or awe
- discovery: Finding something crazy, unexpected, or hidden
- reaction: High emotion, yelling, intense laughter, disbelief
- funny: Fails, chaotic moments, funny commentary
- take: Controversial or passionate opinions

For EACH moment, write:
1. "start" and "end" in seconds (duration between {min_d} and {max_d} seconds).
2. "hook_text": A punchy 4-10 word title overlay on top of the video to stop people from scrolling. CAPITALISE the 2-3 most dramatic words. Add 1-2 fitting emojis (e.g. 😭, 💀, 😱).
3. "caption": A short, viral caption driving comments (under 150 characters).
4. "hashtags": 4-7 relevant viral hashtags including trending tags (e.g. #gaming, #shorts, #viral, #funny).
5. "score": Retention score from 1-100.
6. "reason": Why this moment will go viral.

Return ONLY a valid JSON array, with no markdown code blocks and no surrounding prose:
[
  {{
    "start": 120.5,
    "end": 165.0,
    "kind": "reaction",
    "title": "Insane moment",
    "hook_text": "He COULD NOT BELIEVE this happened 💀",
    "caption": "Ain't no way this is real... drop your thoughts below 👇",
    "hashtags": ["#viral", "#shorts", "#trending", "#fyp"],
    "score": 92,
    "reason": "Sudden scream and high energy reaction makes viewers stay until the end."
  }}
]

TRANSCRIPT:
{transcript}
"""

SHORT_POLISH_PROMPT = """You are a viral social media manager for TikTok and Instagram Reels.
You are given a transcript of a short video clip (under 2 minutes).
Your job is to polish this clip for maximum virality on TikTok, YouTube Shorts, and Instagram Reels:
1. Write a catchy "hook_text" (4-10 words, 2-3 dramatic words CAPITALISED, with 1-2 emojis) to overlay at the top.
2. Write a viral "caption" that drives comments and shares (under 150 characters).
3. Generate 5-8 trending "hashtags".
4. Estimate a virality "score" (1-100).

Return ONLY a valid JSON object, no markdown code fences, no extra text:
{{
  "hook_text": "Wait for the ENDING... 😭💀",
  "caption": "Tag someone who needs to see this! What would you do? 👇",
  "hashtags": ["#viral", "#shorts", "#trending", "#reels", "#fyp"],
  "score": 90,
  "reason": "Strong curiosity hook that forces viewers to rewatch."
}}

TRANSCRIPT:
{transcript}
"""

def get_gemini_model():
    settings = get_settings()
    api_key = settings.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None
    genai.configure(api_key=api_key)
    for m in ["gemini-2.5-flash", "gemini-flash-latest", "gemini-3.5-flash", "gemini-2.0-flash"]:
        try:
            return genai.GenerativeModel(m)
        except Exception:
            continue
    return genai.GenerativeModel("gemini-2.5-flash")

def clean_json_response(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()

def pick_stream_moments(transcript_text: str, duration: float, count: int = 3, min_d: int = 25, max_d: int = 60):
    model = get_gemini_model()
    if not model:
        # Fallback heuristic if API key is not yet set
        return fallback_stream_moments(duration, count)
    
    prompt = MOMENTS_PROMPT.format(
        want=count,
        min_d=min_d,
        max_d=max_d,
        transcript=transcript_text[:12000] # Fit in standard context
    )
    try:
        resp = model.generate_content(prompt)
        cleaned = clean_json_response(resp.text)
        data = json.loads(cleaned)
        if isinstance(data, list) and len(data) > 0:
            return data
    except Exception as e:
        print(f"[Gemini] Error picking moments: {e}")
    
    return fallback_stream_moments(duration, count)

def polish_short_video(transcript_text: str, duration: float):
    model = get_gemini_model()
    if not model:
        return fallback_short_polish(duration)
    
    prompt = SHORT_POLISH_PROMPT.format(transcript=transcript_text[:4000])
    try:
        resp = model.generate_content(prompt)
        cleaned = clean_json_response(resp.text)
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[Gemini] Error polishing short: {e}")
    
    return fallback_short_polish(duration)

def fallback_stream_moments(duration: float, count: int = 3):
    """Fallback when Gemini API key is missing or fails."""
    moments = []
    chunk = duration / (count + 1)
    for i in range(1, count + 1):
        start = max(0, chunk * i - 15)
        end = min(duration, start + 35)
        moments.append({
            "start": round(start, 1),
            "end": round(end, 1),
            "kind": "reaction",
            "title": f"Highlight {i}",
            "hook_text": f"Wait TILL YOU SEE What Happened Here! 😱",
            "caption": "You won't believe this moment! What are your thoughts? 👇",
            "hashtags": ["#gaming", "#shorts", "#viral", "#trending", "#fyp"],
            "score": 88,
            "reason": "High activity segment picked from stream."
        })
    return moments

def fallback_short_polish(duration: float):
    return {
        "hook_text": "Wait for the ENDING... 😭💀",
        "caption": "Tag someone who needs to see this! 👇",
        "hashtags": ["#shorts", "#viral", "#trending", "#fyp", "#reels"],
        "score": 85,
        "reason": "Attention grabbing hook for short video."
    }
