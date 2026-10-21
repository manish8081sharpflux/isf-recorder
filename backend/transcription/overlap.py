"""
ISF Recorder — 3-minute window planning and overlap de-duplication
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Long audio is transcribed in windows: window k = [max(0, k·180 − 3), min((k+1)·180, total)].
Neighbouring windows share 3 s of audio, so the words spoken in that overlap usually appear twice:
at the end of window k and at the start of window k+1. merge_windows() removes the second copy:

1. Word match: normalise words (lower case, no punctuation; Devanagari kept intact) and find the
   longest run (<= 15 words) that ends the previous window's text and starts the next window's text
   — only looking at segments near the boundary. Those words are removed from the next window.
2. If no words match (e.g. one window wrote Hindi in Devanagari and the other in Roman script), a
   next-window segment that ends before the boundary is dropped only if its text fuzzily matches
   the previous window's tail. Otherwise it is kept: a duplicate is better than a lost word.
"""
import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

CHUNK_SECONDS = 180.0
OVERLAP_SECONDS = 3.0
MAX_MATCH_WORDS = 15
BOUNDARY_TOLERANCE = 2.0      # Gemini timestamps are approximate
FUZZY_SEGMENT_RATIO = 0.6


def plan_windows(total_seconds: float, chunk_seconds: float = CHUNK_SECONDS,
                 overlap_seconds: float = OVERLAP_SECONDS) -> List[Tuple[int, float, float]]:
    """[(idx, start_sec, end_sec)]. Audio <= chunk_seconds is one window."""
    total = max(0.0, float(total_seconds or 0.0))
    if total <= chunk_seconds:
        return [(0, 0.0, round(total, 3))]
    count = int(math.ceil(total / chunk_seconds - 1e-9))
    windows = []
    for k in range(count):
        start = max(0.0, k * chunk_seconds - overlap_seconds)
        end = min((k + 1) * chunk_seconds, total)
        if end - start <= 0:
            continue
        windows.append((k, round(start, 3), round(end, 3)))
    return windows


def norm_word(token: str) -> str:
    """Lower-case and drop punctuation/symbols, keeping letters, combining marks (matras) and digits."""
    out = []
    for ch in unicodedata.normalize("NFC", token or "").lower():
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "M", "N"):
            out.append(ch)
    return "".join(out)


def norm_text(text: str) -> str:
    return " ".join(w for w in (norm_word(t) for t in (text or "").split()) if w)


def _words_equal(a: str, b: str, last_prev: bool = False) -> bool:
    if a == b:
        return True
    # The word cut by the window edge may be partial in the previous window ("चले" vs "चलेंगे")
    if last_prev and len(a) >= 2 and b.startswith(a):
        return True
    if len(a) >= 4 and len(b) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True
    return False


def _tokens(segments: List[Dict[str, Any]]) -> List[Tuple[int, int, str]]:
    """(segment position, token position, normalised word) for every non-empty word, in order."""
    out = []
    for si, seg in enumerate(segments):
        for ti, tok in enumerate(str(seg.get("text") or "").split()):
            w = norm_word(tok)
            if w:
                out.append((si, ti, w))
    return out


def _longest_match(prev_words: List[str], next_words: List[str]) -> int:
    """Largest L with prev_words[-L:] ≈ next_words[:L]."""
    max_l = min(len(prev_words), len(next_words), MAX_MATCH_WORDS)
    for length in range(max_l, 0, -1):
        tail = prev_words[-length:]
        head = next_words[:length]
        if all(_words_equal(tail[i], head[i], last_prev=(i == length - 1)) for i in range(length)):
            return length
    return 0


def dedupe_overlap(prev_segments: List[Dict[str, Any]], next_segments: List[Dict[str, Any]],
                   boundary: float, overlap_seconds: float = OVERLAP_SECONDS) -> List[Dict[str, Any]]:
    """
    Returns next_segments with the copy of the overlap speech removed.
    boundary: absolute time where the previous window ends (= next window start + overlap).
    Segments use absolute times; neither input list is modified.
    """
    nxt = [dict(s) for s in next_segments]
    if not prev_segments or not nxt:
        return nxt

    overlap_start = boundary - overlap_seconds
    prev_near = [s for s in prev_segments
                 if float(s.get("end_time") or 0.0) >= overlap_start - BOUNDARY_TOLERANCE]
    near_idx = [i for i, s in enumerate(nxt) if float(s.get("start_time") or 0.0) <= boundary + BOUNDARY_TOLERANCE]
    # Only the leading run of next-window segments near the boundary takes part
    lead = []
    for i in near_idx:
        if i != len(lead):
            break
        lead.append(i)
    if not prev_near or not lead:
        return nxt

    prev_words = [w for _, _, w in _tokens(prev_near)][-MAX_MATCH_WORDS:]
    next_tokens = _tokens([nxt[i] for i in lead])[:MAX_MATCH_WORDS]
    matched = _longest_match(prev_words, [w for _, _, w in next_tokens])

    if matched:
        # Remove the first `matched` words (and punctuation-only tokens around them) from the next window
        cut_seg, cut_tok = next_tokens[matched - 1][0], next_tokens[matched - 1][1]
        result = []
        for pos, seg in enumerate(nxt):
            if pos < cut_seg:
                continue  # fully consumed
            if pos == cut_seg:
                rest = str(seg.get("text") or "").split()[cut_tok + 1:]
                while rest and not norm_word(rest[0]):
                    rest = rest[1:]
                if not rest:
                    continue
                seg = dict(seg)
                seg["text"] = " ".join(rest)
                st, et = float(seg.get("start_time") or 0.0), float(seg.get("end_time") or 0.0)
                if st < boundary:
                    seg["start_time"] = round(min(max(st, boundary), max(et, st)), 2)
            result.append(seg)
        return result

    # No word match: drop a segment that ends before the boundary only if it repeats the previous tail
    prev_tail = norm_text(" ".join(str(s.get("text") or "") for s in prev_near))
    result = []
    for pos, seg in enumerate(nxt):
        if pos in lead and float(seg.get("end_time") or 0.0) <= boundary + 0.5:
            seg_norm = norm_text(str(seg.get("text") or ""))
            if seg_norm and _fuzzy_in(seg_norm, prev_tail):
                continue
        result.append(seg)
    return result


def _fuzzy_in(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    if needle in haystack:
        return True
    tail = haystack[-max(len(needle) * 2, 1):]
    matcher = SequenceMatcher(None, needle, tail)
    block_total = sum(b.size for b in matcher.get_matching_blocks())
    return block_total / max(len(needle), 1) >= FUZZY_SEGMENT_RATIO and matcher.ratio() >= 0.5


def merge_windows(windows: List[Dict[str, Any]], overlap_seconds: float = OVERLAP_SECONDS) -> List[Dict[str, Any]]:
    """
    windows: [{"idx", "start", "end", "segments": [...] | None}] in any order; segments use absolute
    times; None/[] for a skipped or failed window. Overlap is only de-duplicated between windows that
    are direct neighbours (idx k and k+1) and both have segments.
    """
    ordered = sorted(windows, key=lambda w: w["idx"])
    merged: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    for w in ordered:
        segs = sorted([dict(s) for s in (w.get("segments") or [])], key=lambda s: float(s.get("start_time") or 0.0))
        if prev is not None and segs and prev.get("segments") and w["idx"] == prev["idx"] + 1:
            segs = dedupe_overlap(prev["segments"], segs, boundary=float(w["start"]) + overlap_seconds,
                                  overlap_seconds=overlap_seconds)
        merged.extend(segs)
        prev = {"idx": w["idx"], "segments": w.get("segments") or []}
    merged.sort(key=lambda s: float(s.get("start_time") or 0.0))
    return merged
