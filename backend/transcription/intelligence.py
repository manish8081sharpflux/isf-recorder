"""
ISF Recorder — Intelligence & Action Item Extraction
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extracts executive summaries, key bullet points, decisions made,
and actionable tasks from conversation transcripts using Gemini.
"""
import os
import json
import logging
import re
from typing import Dict, Any, List, Optional

from backend.config import GEMINI_API_KEY, GEMINI_INTELLIGENCE_MODEL
from backend.transcription.gemini_stt import get_genai_client, clean_json_response

logger = logging.getLogger("isf_recorder.intelligence")

LEGACY_OVERVIEW_INSTRUCTIONS = """2. Write a structured overview:
   - IF THIS IS A CHILD COACHING / LEARNING SESSION (ISF Foundation context), write the overview following the exact *Report template for Coach* format:
     *Report template for Coach* -
     *1. Learning Activities In Progress* - (The child is practicing it - not yet completed - in progress)
     *2. Learning Activities Completed* (If coins are awarded, list in completed, or mention that the child did this successfully)
     *3. ISF Coins awarded* (List specific coins awarded)
     *4. Practice* (Homework given or suggested improvement points regarding learning activities)
     *5. Observations* (Do not mention learning comments. Mention specifically child behaviour, coach remarks, or good remarks)
     *6. Medical* (If any child is unwell or medical info mentioned)
     *7. General Tasks* (Tasks given other than learning activities)
     *Note:* If there is no medical update, coins, tasks, or observations, omit those sections from the report.

   - OTHERWISE (FOR GENERAL BUSINESS / TEAM MEETINGS), write a structured executive overview with these labeled sections:
     - **Context**: What this meeting/recording is about (1-2 sentences)
     - **Key Outcomes**: The main results or conclusions (2-3 bullet points)
     - **Next Steps**: What needs to happen next (1-2 sentences)

"""

# Recordings made in Schedule / Meeting mode get their full report separately (reports module), so
# the overview is a short summary and no longer guesses the kind of session from the transcript.
OVERVIEW_INSTRUCTIONS = {
    "schedule": """2. Write a short overview (3-5 sentences) of this child coaching / learning session: who took part,
   which learning activities happened and anything notable. Plain text, no headings.
""",
    "meeting": """2. Write a structured executive overview with these labeled sections:
     - **Context**: What this meeting is about (1-2 sentences)
     - **Key Outcomes**: The main results or conclusions (2-3 bullet points)
     - **Next Steps**: What needs to happen next (1-2 sentences)
""",
}

INTELLIGENCE_PROMPT = """You are an expert executive meeting assistant and educational intelligence analyst.
Analyze the provided meeting / audio transcript and extract structured intelligence.

TRANSCRIPT:
\"\"\"{transcript}\"\"\"

TASK:
1. Extract a punchy, descriptive title for this recording capturing the core subject or session purpose (max 8 words, e.g. "Math & Hindi Activity Session", "Rahul Learning & Coins Review", "Morning Shift Team Briefing"). NEVER return "Untitled Recording" or "Audio Recording" if speech is present.
{overview_instructions}3. Extract key discussion points (bullet list).
4. Identify any decisions agreed upon during the conversation.
5. Extract all action items and tasks assigned to individuals. Include:
   - description: clear, actionable description of the task
   - assigned_to: name of person assigned (or child / coach name or null)
   - urgency: "low" | "normal" | "high" | "critical"
   - due_date: YYYY-MM-DD if explicitly mentioned or relative, otherwise null

OUTPUT FORMAT:
Return ONLY valid JSON matching this schema exactly (no markdown code blocks, no preamble):
{
  "title": "Short descriptive title",
  "overview": "Concise summary or Coach Report...",
  "key_points": [
    "Point 1...",
    "Point 2..."
  ],
  "decisions": [
    "Decision 1..."
  ],
  "action_items": [
    {
      "description": "Task description",
      "assigned_to": "Person Name or Team",
      "urgency": "normal",
      "due_date": null
    }
  ]
}
"""


from backend.transcription.usage_tracker import track_gemini_call


async def extract_intelligence(transcript: str, recording_id: Optional[str] = None, org_id: Optional[str] = None,
                               mode: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyzes conversation transcript using Gemini and returns structured summary + action items with usage tracking.
    """
    if not transcript or not transcript.strip():
        return {
            "title": "No Speech Detected",
            "overview": "No speech detected in this recording.",
            "key_points": [],
            "decisions": [],
            "action_items": []
        }

    # Token Optimization: Strip timestamp boilerplate [MM:SS] from transcript before sending to Gemini.
    # The summarizer needs the spoken words and speaker labels, not timestamps on every single line.
    # This saves ~15-20% input tokens on long audio recordings.
    clean_transcript = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?\]\s*', '', transcript)
    overview_instructions = OVERVIEW_INSTRUCTIONS.get(mode or "", LEGACY_OVERVIEW_INSTRUCTIONS)
    prompt = (INTELLIGENCE_PROMPT.replace("{overview_instructions}", overview_instructions)
              .replace("{transcript}", clean_transcript[:120000]))

    from google.genai import types
    intel_config = types.GenerateContentConfig(
        temperature=0.3,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    # Use admin-configured model from org settings (via model_resolver)
    from backend.settings.model_resolver import get_intelligence_model
    active_model = get_intelligence_model(org_id)

    try:
        client = get_genai_client()

        def _do_generate():
            return client.models.generate_content(
                model=active_model,
                contents=[prompt],
                config=intel_config,
            )

        response = track_gemini_call(
            recording_id=recording_id,
            operation_type="SUMMARY",
            gemini_model=active_model,
            call_fn=_do_generate,
        )

        response_text = response.text or ""
        cleaned = clean_json_response(response_text)

        data = None
        try:
            data = json.loads(cleaned)
        except Exception:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    pass

        if not isinstance(data, dict):
            data = {
                "title": "Audio Recording",
                "overview": response_text[:500] if response_text else "Meeting recorded and transcribed.",
                "key_points": [],
                "decisions": [],
                "action_items": []
            }

        # Normalize overview to clean string if Gemini returned a dict or list
        ov = data.get("overview")
        if isinstance(ov, dict):
            formatted_ov_parts = []
            for k, v in ov.items():
                if isinstance(v, list):
                    v_str = "\n".join(f"• {x}" for x in v)
                    formatted_ov_parts.append(f"{k}:\n{v_str}")
                else:
                    formatted_ov_parts.append(f"{k}: {v}")
            data["overview"] = "\n\n".join(formatted_ov_parts)
        elif isinstance(ov, list):
            data["overview"] = "\n".join(f"• {x}" if isinstance(x, str) else str(x) for x in ov)
        elif not isinstance(ov, str):
            data["overview"] = str(ov) if ov is not None else ""

        if isinstance(data.get("overview"), str):
            data["overview"] = data["overview"].replace("\\r\\n", "\n").replace("\\n", "\n")

        logger.info(f"Intelligence extracted: title='{data.get('title')}', {len(data.get('action_items', []))} action items")
        return data

    except Exception as e:
        logger.error(f"Intelligence extraction failed: {e}")
        return {
            "title": "Audio Recording",
            "overview": "Automated intelligence extraction encountered an error.",
            "key_points": [],
            "decisions": [],
            "action_items": []
        }
