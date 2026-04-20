"""
Compression module for Caveman-style token optimization.
Implements intensity levels (lite, full, ultra) for natural language compression.
"""

import re

# Lite mode: regex-based removal of common filler words and hedging
LITE_REMOVALS = [
    r"\b(simply|basically|actually|really|just|of course|certainly|definitely|absolutely)\b",
    r"\b(I think|I believe|it seems that|it appears that|it's worth noting that)\b",
    r"\b(I'd be happy to|surely|certainly)\b",
]

def compress_lite(text: str) -> str:
    """Lite compression: remove fillers and hedging while keeping grammar."""
    result = text
    for pattern in LITE_REMOVALS:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    
    # Clean up double spaces and leading/trailing whitespace
    result = re.sub(r"\s+", " ", result).strip()
    return result

def compress_full_regex(text: str) -> str:
    """Full compression (regex fallback): Lite + remove articles."""
    result = compress_lite(text)
    # Remove articles: the, a, an
    result = re.sub(r"\b(the|a|an)\b", "", result, flags=re.IGNORECASE)
    # Clean up
    result = re.sub(r"\s+", " ", result).strip()
    return result

def get_caveman_prompt(text: str, level: str = "full") -> str:
    """
    Generate an LLM prompt to compress text using Caveman rules.
    Used by agents during indexing or transcript storage.
    """
    instructions = {
        "lite": "Remove filler words and hedging. Keep articles and full sentences. Professional but tight.",
        "full": "Drop articles (a/an/the), use fragments, short synonyms. Speak like smart caveman. Keep technical terms exact.",
        "ultra": "Abbreviate (DB/auth/config/fn/impl). Strip conjunctions. Use arrows for causality (X -> Y). One word when enough."
    }
    
    instr = instructions.get(level, instructions["full"])
    
    return f"""Compress this text into Caveman format (level: {level}).
STRICT RULES:
- {instr}
- Keep ALL technical terms (function names, variables, URLs, paths) UNCHANGED.
- Keep ALL numbers and dates UNCHANGED.
- Do NOT add any preamble or explanation.

TEXT:
{text}
"""

def compress_caveman(text: str, level: str = "full") -> str:
    """
    Entry point for compression. 
    In 'lite' mode, uses regex. 
    In 'full' or 'ultra' mode, it currently returns the original text 
    with a flag indicating it should be compressed by the LLM 
    (or it can be integrated with an LLM call if available).
    """
    if level == "lite":
        return compress_lite(text)
    if level == "full":
        return compress_full_regex(text)
    
    # For ultra, we still return the text as-is if no LLM available, 
    # but we can try to apply full_regex as a baseline.
    return compress_full_regex(text)
