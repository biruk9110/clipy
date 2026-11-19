"""Clipy Plugin Manager & Host Integration.

Supports Clips Kitty / Clips Studio plugins, including the Grand Theft Auto 6
highlight detection pipeline (colingpt9/gta-vi-highlights).
"""
from __future__ import annotations

import logging
import os
import math
import re
from pathlib import Path

log = logging.getLogger(__name__)

AVAILABLE_PIPELINES = [
    {
        "id": "default",
        "name": "Standard AI Director",
        "description": "General viral clipper with hook generation, smart pacing, and captions.",
        "icon": "ph-magic-wand",
        "type": "builtin",
    },
    {
        "id": "gta6",
        "name": "GTA 6 & Gaming Clipper (Clips Kitty Plugin)",
        "description": "GTA VI / gaming pipeline detecting chases, crashes, stunts, combat, and funny moments.",
        "icon": "ph-game-controller",
        "type": "plugin",
        "plugin_id": "colingpt9/gta-vi-highlights",
        "version": "0.4.1",
    },
]

GAMING_HOOK_TEMPLATES = {
    "chase": [
        "COPS WON'T STOP CHASING ME! 🚨😱",
        "5-Star Wanted Level Escape! 🚔💨",
        "Fastest Police Pursuit Ever! 🏎️💨",
    ],
    "crash": [
        "TOTAL WIPEOUT CRASH! 💥🚗",
        "He Didn't See That Coming! 💀🚙",
        "Worst Car Crash In The Game! 💥🏎️",
    ],
    "stunt": [
        "INSANE STUNT LANDED! 🛹🤯",
        "How Did He Survive This Jump?! 🚀🔥",
        "1 in a Million Stunt Jump! 🏆🎮",
    ],
    "combat": [
        "INTENSE SHOOTOUT CHAOS! 🔫💥",
        "Surviving Against All Odds! 🪖🔥",
        "Unbelievable Combat Reflexes! ⚡🎯",
    ],
    "npc_chaos": [
        "NPCs IN THIS GAME ARE WILD! 😂💀",
        "The Weirdest NPC Interaction Ever! 🤖🤣",
        "GTA NPC Chaos At Its Finest! 🚶💥",
    ],
    "funny_dialogue": [
        "CANNOT STOP LAUGHING AT THIS! 🤣💀",
        "The Funniest Dialogue Moment! 😂🎤",
        "Streamer Broke Character Laughing! 😭🔥",
    ],
    "reaction": [
        "HIS REACTION SAYS IT ALL! 😱🔥",
        "BRO LOST HIS MIND OVER THIS! 🤯💀",
        "I Was NOT Expecting That! 🤯👀",
    ],
    "milestone": [
        "MISSION PASSED RESPECT! 🏆🔥",
        "Finally Beat The Hardest Part! 🎮✨",
    ],
}

def get_pipelines() -> list[dict]:
    return AVAILABLE_PIPELINES

def format_gaming_hook(category: str, title: str = "", default_hook: str = "") -> str:
    templates = GAMING_HOOK_TEMPLATES.get(category, [])
    if templates:
        import random
        return random.choice(templates)
    if title:
        return f"{title.upper()}! 🔥🎮"
    return default_hook or "UNBELIEVABLE GAMING MOMENT! 🎮🔥"

def detect_gta_moments(
    transcript_segments: list[dict],
    duration: float,
    num_clips: int = 3,
    clip_duration: int = 45,
    min_score: float = 0.25,
    min_confidence: float = 0.20,
) -> list[dict]:
    """
    Executes the GTA 6 highlight detection engine on transcript and timing cues.
    Returns moments formatted for Clipy editing pipeline.
    """
    try:
        from gta_highlights.detector import Config, detect
        from gta_highlights.scoring import Evidence
    except ImportError as e:
        log.warning("GTA highlights plugin package not imported: %s", e)
        return []

    evidence = []
    for s in transcript_segments or []:
        start = float(s.get("start", 0))
        end = float(s.get("end", 0))
        text = str(s.get("text", "")).strip()
        conf = float(s.get("confidence", 0.9))
        if text and end > start:
            evidence.append(Evidence(start, end, "transcript", conf, text, source="transcript"))

    min_d = max(10.0, float(clip_duration * 0.6))
    max_d = min(max(min_d + 15.0, float(clip_duration * 1.4)), float(duration))
    
    cfg = Config(
        min_score=min_score,
        min_confidence=min_confidence,
        min_duration=min_d,
        max_duration=max_d,
        lead_in=6.0,
        payoff=8.0,
        max_highlights=max(10, num_clips * 3),
        roleplay_enabled=True,
    )

    highlights = detect(evidence, duration, cfg)
    out = []
    for h in highlights:
        category = h.category
        hook = format_gaming_hook(category, h.title)
        out.append({
            "start": round(float(h.start), 2),
            "end": round(float(h.end), 2),
            "duration": round(float(h.end - h.start), 2),
            "title": h.title or f"GTA {category.replace('_', ' ').capitalize()}",
            "hook_text": hook,
            "hook_summary": h.reason or f"Detected GTA {category} event",
            "category": category,
            "score": int(round(float(h.score) * 100)),
            "reason": h.reason,
            "virality_score": int(round(float(h.score) * 100)),
            "caption": f"Watch this insane GTA {category.replace('_', ' ')}! #gta #gta6 #gaming #clip",
            "hashtags": ["#gta", "#gta6", "#gaming", f"#{category}", "#viral"],
            "source_type": "gta_plugin",
        })

    # Sort descending by score
    out.sort(key=lambda m: m.get("score", 0), reverse=True)
    return out[:num_clips]
