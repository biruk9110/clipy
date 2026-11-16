"""Gemini AI integration.

Model policy (free-tier friendly, newest first):
  1. gemini-2.5-flash       - best quality/price balance, generous free tier
  2. gemini-2.5-flash-lite  - cheapest, highest free daily quota
  3. gemini-2.0-flash       - stable legacy fallback

The chain is tried in order; models that don't exist on the account are
skipped automatically. Users can override with settings.gemini_model.
"""
import json
import re
import os
import warnings

warnings.filterwarnings("ignore")

import google.generativeai as genai

from .config import HOST_SETTINGS

MODEL_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

# Errors that mean "this model isn't available for this key" -> try the next one
_MODEL_UNAVAILABLE = ("404", "not found", "not supported", "does not exist",
                      "is not supported for use", "permission denied")

_active_model = ""


def get_api_key(user_id: int = 0) -> str:
    """Per-user key first, then optional server-wide fallback key."""
    if user_id:
        from .usersettings import get_secret
        key = get_secret(user_id, "gemini_api_key")
        if key:
            return key
    return HOST_SETTINGS.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")


def _client(api_key: str):
    genai.configure(api_key=api_key)


def _clean_json(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def gemini_generate(user_id: int, prompt: str, images: list[str] | None = None,
                    expect_json: bool = True, temperature: float = 0.7) -> dict:
    """Runs a prompt through the model chain.

    images: list of local image paths (frames) attached inline.
    Returns {'ok': bool, 'data': parsed JSON or text, 'error': str, 'model': str}.
    """
    global _active_model
    api_key = get_api_key(user_id)
    if not api_key:
        return {"ok": False, "data": None, "error": "no_api_key", "model": ""}

    override = ""
    if user_id:
        from .usersettings import get_prefs
        override = (get_prefs(user_id).get("gemini_model") or "").strip()
    chain = ([override] if override and override not in MODEL_CHAIN else []) + MODEL_CHAIN

    try:
        _client(api_key)
    except Exception as e:
        return {"ok": False, "data": None, "error": f"Gemini init failed: {e}", "model": ""}

    errors = []
    for model_name in chain:
        try:
            model = genai.GenerativeModel(model_name)
            content: list = [prompt]
            for img_path in (images or [])[:12]:
                try:
                    content.append({
                        "mime_type": "image/jpeg",
                        "data": open(img_path, "rb").read(),
                    })
                except Exception:
                    continue
            resp = model.generate_content(
                content,
                generation_config={
                    "temperature": temperature,
                    "response_mime_type": "application/json" if expect_json else "text/plain",
                },
            )
            text = getattr(resp, "text", "") or ""
            if not text:
                errors.append(f"{model_name}: empty response")
                continue
            _active_model = model_name
            if expect_json:
                try:
                    return {"ok": True, "data": json.loads(_clean_json(text)),
                            "error": "", "model": model_name}
                except json.JSONDecodeError:
                    return {"ok": True, "data": _clean_json(text), "error": "", "model": model_name}
            return {"ok": True, "data": text, "error": "", "model": model_name}
        except Exception as e:
            msg = str(e)
            errors.append(f"{model_name}: {msg[:200]}")
            if any(k in msg.lower() for k in _MODEL_UNAVAILABLE):
                continue  # model unavailable for this key -> try next in chain
            if "quota" in msg.lower() or "429" in msg:
                break  # rate limited - further attempts won't help right now
            continue

    return {"ok": False, "data": None, "error": " | ".join(errors)[:500], "model": ""}


def test_connection(user_id: int) -> dict:
    """Validates the stored API key with a tiny live request."""
    res = gemini_generate(user_id, 'Reply with exactly this JSON: {"ok": true}',
                          expect_json=True, temperature=0)
    if res["ok"]:
        return {"ok": True, "model": res.get("model", "")}
    err = res.get("error", "")
    if err == "no_api_key":
        return {"ok": False, "error": "No API key saved. Add your Gemini API key first."}
    low = err.lower()
    if "api key" in low or "api_key" in low or "invalid" in low and "credential" in low:
        return {"ok": False, "error": "Invalid API key. Double-check the key in Google AI Studio."}
    if "quota" in low or "429" in low:
        return {"ok": False, "error": "Quota exceeded for now - the key is valid, try again later."}
    return {"ok": False, "error": err[:400] or "Unknown Gemini error"}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
MOMENTS_PROMPT = """You are a master viral social media editor for TikTok, YouTube Shorts, and Instagram Reels.
You are given a timestamped transcript of a video, with annotations for audio loudness jumps [LOUD +NdB].

Your goal: Find the {want} STRONGEST, most engaging standalone moments ({min_d} to {max_d} seconds long).

IMPORTANT CRITERIA:
1. You MUST return EXACTLY {want} distinct moments.
2. If the video is short (under 2.5 minutes), divide the video into {want} logical sequential parts (e.g. Part 1 covers the setup/beginning, Part 2 covers the climax/payoff/ending).
3. Do NOT return the full video for any clip when {want} > 1. Each clip must cover a different segment/portion (e.g. first half, second half).
4. If dividing into sequential parts, prefix the "hook_text" with "Part 1 — ...", "Part 2 — ...", etc.

Editing profile from a reference video (match this style where possible):
{reference_profile}

For EACH moment, write:
1. "start" and "end" in seconds (must be inside 0 and {duration:.0f}, with end > start).
2. "hook_text": A punchy 4-10 word title overlay. CAPITALISE the 2-3 most dramatic words. Add 1-2 fitting emojis.
3. "caption": A short viral caption driving comments (under 150 characters).
4. "hashtags": 4-7 relevant hashtags.
5. "score": Retention score 1-100.
6. "reason": Why this moment will go viral.

Return ONLY a valid JSON array:
[{{"start": 0.0, "end": 28.5, "kind": "part_1", "title": "Part 1",
"hook_text": "Part 1 — He COULD NOT BELIEVE this happened 💀", "caption": "Ain't no way... 👇",
"hashtags": ["#viral", "#shorts", "#part1"], "score": 92, "reason": "..."}}]

TRANSCRIPT:
{transcript}
"""

SHORT_POLISH_PROMPT = """You are a viral social media manager for TikTok, YouTube Shorts and Instagram Reels.
Polish this short video (under 2.5 minutes) for maximum virality:
1. "hook_text": catchy 4-10 word overlay (CAPITALISE dramatic words, 1-2 emojis).
2. "caption": viral caption driving comments (under 150 characters).
3. "hashtags": 5-8 trending hashtags.
4. "score": virality estimate 1-100.

Editing profile from a reference video (match this style where possible):
{reference_profile}

Return ONLY a valid JSON object: {{"hook_text": "...", "caption": "...", "hashtags": ["#..."], "score": 90, "reason": "..."}}

TRANSCRIPT:
{transcript}
"""

REFERENCE_ANALYSIS_PROMPT = """You are an expert video editor analyzing a REFERENCE short-form video (frames shown in order, from its start to end).
Analyze its visual composition, editing patterns, pacing, captions, framing and effects.

Return ONLY a valid JSON object with these keys:
{
 "editing_style": "2-3 sentence summary of the overall style",
 "hook_structure": "how the video grabs attention in the first seconds",
 "avg_shot_duration_sec": 3.0,
 "scene_transitions": "hard cut | whip pan | zoom punch | etc.",
 "camera_movement": "static | handheld | tracking | etc.",
 "zoom_effects": "none | subtle punch-ins | aggressive zooms",
 "caption_style": "karaoke word-by-word | bold static blocks | minimal",
 "caption_position": "center | lower-third | top",
 "caption_color_accent": "#RRGGBB or description",
 "subtitle_animation": "pop-in | fade | typewriter | none",
 "music": "upbeat | emotional | none | unknown",
 "sound_effects": "list of audible effects, [] if none",
 "color_grade": "vibrant | cinematic | neutral | etc.",
 "aspect_ratio": "9:16 | 1:1 | 16:9",
 "pacing": "very fast | fast | medium | calm",
 "storytelling": "1-2 sentences about narrative flow",
 "key_moments": ["short list of what moments carry the video"],
 "ending_style": "loop bait | cliffhanger | resolution | CTA",
 "editing_plan_hint": "2-3 sentence instruction set an editor can apply to another video to achieve the same feel"
}
Be concrete and honest - if something is not visible in the frames, say "unknown".
"""

PLAN_PROMPT = """You are a professional short-form video editor. You analyzed a REFERENCE video (editing profile below)
and now must produce an EDITING PLAN for a DIFFERENT user video with this transcript:

{transcript}
(User video duration: {duration:.1f}s. Language: {language})

Reference editing profile to emulate (do NOT copy its content, only its style):
{reference_profile}

Produce {count} clip plans, each {min_d}-{max_d} seconds, ordered by strongest moment first.
If trim_silence is true, prefer segments with continuous speech and note silent gaps in "silence_gaps".
Each clip: {{"start": sec, "end": sec, "hook_text": "4-10 punchy words w/ 1-2 emojis",
"caption": "under 150 chars", "hashtags": ["#..."], "score": 1-100,
"zoom_intensity": 0-2 (0 none, 2 punchy), "transition": "hard_cut|zoom_punch",
"sfx_hint": "" or "swoosh/riser/pop", "silence_gaps": [[start,end],...]}}
Also return "global": {{"caption_style": one of karaoke|bold|minimal, "pacing": "fast|medium|calm", "hook_strategy": "1 sentence"}}.

Return ONLY valid JSON: {{"clips": [...], "global": {{...}}}}
"""


# ---------------------------------------------------------------------------
# High-level helpers used by the job pipeline
# ---------------------------------------------------------------------------
def _profile_text(project: dict) -> str:
    profile = project.get("reference_profile") if project else None
    if not profile:
        return "(no reference video - use best-practice viral editing)"
    try:
        if isinstance(profile, str):
            profile = json.loads(profile)
        return json.dumps(profile, ensure_ascii=False)[:4000]
    except Exception:
        return "(no reference video - use best-practice viral editing)"


def pick_stream_moments(transcript_text: str, duration: float, count: int = 3,
                        min_d: int = 25, max_d: int = 60, user_id: int = 0,
                        project: dict | None = None) -> list:
    count = max(1, int(count or 1))
    dur = max(float(duration or 0), 5.0)
    profile = _profile_text(project)
    prompt = MOMENTS_PROMPT.format(
        want=count, min_d=min_d, max_d=max_d, duration=dur,
        reference_profile=profile, transcript=(transcript_text or "")[:12000])
    res = gemini_generate(user_id, prompt, temperature=0.8)
    raw_clips = []
    if res["ok"] and isinstance(res["data"], list) and res["data"]:
        raw_clips = res["data"]
    elif res["ok"] and isinstance(res["data"], dict) and isinstance(res["data"].get("clips"), list):
        raw_clips = res["data"]["clips"]
    if res.get("error") not in ("no_api_key", ""):
        print(f"[Gemini] pick_stream_moments fallback: {res.get('error', '')[:200]}")

    valid_clips = []
    for c in raw_clips:
        if isinstance(c, dict) and "start" in c and "end" in c:
            try:
                s = float(c["start"])
                e = float(c["end"])
                if e > s:
                    c["start"] = max(0.0, round(s, 1))
                    c["end"] = min(dur, round(e, 1))
                    valid_clips.append(c)
            except (ValueError, TypeError):
                continue

    # If count > 1, reject clips that cover virtually the entire video (>= 90%)
    if count > 1 and dur > 12.0:
        valid_clips = [c for c in valid_clips if (c["end"] - c["start"]) < dur * 0.90]

    # If we have enough distinct clips, return exactly count
    if len(valid_clips) >= count:
        return valid_clips[:count]

    fallback = fallback_stream_moments(dur, count)
    if not valid_clips:
        return fallback

    # Pad missing moments from fallback so we always satisfy count
    for fb in fallback:
        if len(valid_clips) >= count:
            break
        valid_clips.append(fb)
    return valid_clips[:count]


def polish_short_video(transcript_text: str, duration: float, user_id: int = 0,
                       project: dict | None = None) -> dict:
    clean = (transcript_text or "").strip()
    if len(clean) < 8:
        clean = f"(High-energy visual video with background audio, duration {duration:.1f} seconds)"
    prompt = SHORT_POLISH_PROMPT.format(transcript=clean[:4000], duration=duration,
                                        reference_profile=_profile_text(project))
    res = gemini_generate(user_id, prompt, temperature=0.8)
    if res["ok"] and isinstance(res["data"], dict):
        hook = str(res["data"].get("hook_text", ""))
        if not hook or "TRANSCRIPT" in hook.upper() or "MISSING" in hook.upper():
            res["data"]["hook_text"] = "Wait FOR THE END! 😱"
        return res["data"]
    return fallback_short_polish(duration)


def build_editing_plan(transcript_text: str, duration: float, language: str,
                       count: int, min_d: int, max_d: int, trim_silence: bool,
                       user_id: int = 0, project: dict | None = None) -> dict:
    """Reference-style editing plan for the user's video. Falls back to a
    heuristic plan (even segments) when AI is unavailable."""
    count = max(1, int(count or 1))
    dur = max(float(duration or 0), 5.0)
    prompt = PLAN_PROMPT.format(
        transcript=(transcript_text or "(no speech detected - background audio only)")[:12000],
        duration=dur, language=language or "unknown", count=count,
        min_d=min_d, max_d=max_d, reference_profile=_profile_text(project),
    )
    prompt += f'\nGlobal flags: trim_silence={bool(trim_silence)}'
    res = gemini_generate(user_id, prompt, temperature=0.7)
    if res["ok"] and isinstance(res["data"], dict) and res["data"].get("clips"):
        plan = res["data"]
        plan.setdefault("global", {})
        plan["global"].setdefault("caption_style", "karaoke")
        plan["source"] = "gemini"
        plan["model"] = res.get("model", "")
        # Ensure plan has count clips
        clips = plan.get("clips") or []
        if len(clips) < count:
            fb_clips = fallback_plan(dur, count, min_d, max_d, trim_silence).get("clips", [])
            for fb in fb_clips:
                if len(clips) >= count:
                    break
                clips.append(fb)
            plan["clips"] = clips
        return plan
    return fallback_plan(dur, count, min_d, max_d, trim_silence)


def analyze_reference(frames: list[str], transcript_text: str, duration: float,
                      user_id: int = 0) -> dict:
    """Analyzes reference frames + transcript into a structured editing profile."""
    prompt = REFERENCE_ANALYSIS_PROMPT
    if transcript_text:
        prompt += f"\n\nReference audio transcript (first part):\n{transcript_text[:4000]}"
    prompt += f"\n\nReference duration: {duration:.1f} seconds."
    res = gemini_generate(user_id, prompt, images=frames, temperature=0.4)
    if res["ok"] and isinstance(res["data"], dict):
        res["data"]["source"] = "gemini"
        res["data"]["model"] = res.get("model", "")
        return {"ok": True, "profile": res["data"], "error": ""}
    err = res.get("error", "")
    if err == "no_api_key":
        return {"ok": False, "profile": None, "error": "no_api_key"}
    return {"ok": False, "profile": None, "error": err[:300]}


def fallback_stream_moments(duration: float, count: int = 3) -> list:
    count = max(1, int(count or 1))
    dur = max(float(duration or 0), 5.0)

    if dur <= 150.0 and count > 1:
        # Short video (TikTok / Shorts / Reels) sliced into sequential parts (e.g. halves or thirds)
        step = dur / count
        overlap = min(2.5, step * 0.15)
        hooks = [
            "Part 1 — Look What HAPPENED! 😱",
            "Part 2 — The CRAZIEST Part! 👀",
            "Part 3 — The FINAL Ending! 🔥",
            "Part 4 — You WON'T Believe This! 💀",
            "Part 5 — The EPIC Finale! ✨",
            "Part 6 — Watch Till THE END! 🚀",
        ]
        moments = []
        for i in range(count):
            start = 0.0 if i == 0 else max(0.0, round(i * step - overlap, 1))
            end = dur if i == count - 1 else min(dur, round((i + 1) * step + overlap, 1))
            if end - start < 4.0:
                end = min(dur, start + 4.0)
            hook = hooks[i] if i < len(hooks) else f"Part {i + 1} — Watch Till THE END! 😱"
            moments.append({
                "start": round(start, 1),
                "end": round(end, 1),
                "kind": f"part_{i + 1}",
                "title": f"Part {i + 1}",
                "hook_text": hook,
                "caption": f"Part {i + 1} of {count}! Drop your reaction below 👇",
                "hashtags": ["#shorts", "#viral", "#fyp", f"#part{i + 1}"],
                "score": 90 - (i * 2),
                "reason": f"Sequential Part {i + 1} of {count} covering this segment.",
                "zoom_intensity": 1,
            })
        return moments

    moments = []
    chunk = dur / (count + 1)
    default_hooks = [
        "Wait TILL YOU SEE What Happened! 😱",
        "Nobody Expected THIS To Happen! 👀",
        "The Most INSANE Moment Yet! 🔥",
        "He COULD NOT BELIEVE This! 💀",
        "Watch Till THE VERY END! 🚀",
        "This Turned Out SO WILD! 🤯",
    ]
    for i in range(1, count + 1):
        start = max(0.0, round(chunk * i - 15, 1))
        end = min(dur, round(start + 35, 1))
        hook = default_hooks[(i - 1) % len(default_hooks)]
        moments.append({
            "start": start,
            "end": end,
            "kind": "highlight",
            "title": f"Highlight {i}",
            "hook_text": hook,
            "caption": "You won't believe this moment! What are your thoughts? 👇",
            "hashtags": ["#shorts", "#viral", "#trending", "#fyp"],
            "score": 88,
            "reason": "Balanced segment chosen without AI analysis.",
            "zoom_intensity": 1,
        })
    return moments


def fallback_short_polish(duration: float) -> dict:
    return {
        "hook_text": "Wait for the ENDING... 😭💀",
        "caption": "Tag someone who needs to see this! 👇",
        "hashtags": ["#shorts", "#viral", "#trending", "#fyp", "#reels"],
        "score": 85, "reason": "Attention grabbing hook.", "zoom_intensity": 1,
    }


def fallback_plan(duration: float, count: int, min_d: int, max_d: int, trim_silence: bool) -> dict:
    count = max(1, int(count or 1))
    usable = max(float(duration or 0), 5.0)
    hooks = [
        "Part 1 — Look What HAPPENED! 😱",
        "Part 2 — The CRAZIEST Part! 👀",
        "Part 3 — The FINAL Ending! 🔥",
        "Part 4 — You WON'T Believe This! 💀",
    ]
    clips = []
    step = usable / count
    overlap = min(2.5, step * 0.15) if count > 1 else 0.0
    for i in range(count):
        start = 0.0 if i == 0 else max(0.0, round(i * step - overlap, 2))
        end = usable if i == count - 1 else min(usable, round((i + 1) * step + overlap, 2))
        if end - start < 4.0:
            end = min(usable, start + 4.0)
        hook = hooks[i] if i < len(hooks) else f"Part {i + 1} — You NEED to see this 👀"
        clips.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "hook_text": hook,
            "caption": f"Part {i + 1} of {count} — drop your thoughts! 👇",
            "hashtags": ["#shorts", "#viral", "#fyp", f"#part{i + 1}"],
            "score": 85 - (i * 2),
            "zoom_intensity": 1,
            "transition": "hard_cut",
            "sfx_hint": "",
            "silence_gaps": [],
        })
    return {
        "clips": clips,
        "global": {"caption_style": "karaoke", "pacing": "medium", "hook_strategy": "Sequential highlights."},
        "source": "fallback",
    }
