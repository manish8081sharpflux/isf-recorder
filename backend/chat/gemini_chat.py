"""
ISF Recorder — Gemini Chat Engine & Context Builders
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Core AI logic for:
1. Building session context (metadata, overview, action items, transcript with/without timestamps)
2. Building all-sessions context (compact summaries across multiple recordings)
3. Multi-turn chat with Gemini Flash, supporting admin-configured prompt templates
4. User preference learning (accumulating language, style, and topic patterns in user.settings JSONB)
5. Seeding default prompt templates for organizations
"""
import os
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config import GEMINI_INTELLIGENCE_MODEL
from backend.settings.report_prompts import COACH_REPORT_PROMPT, MEETING_MINUTES_PROMPT
from backend.transcription.gemini_stt import get_genai_client, clean_json_response

logger = logging.getLogger("isf_recorder.chat")

# Standard IST timezone for user-facing timestamps
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))

DEFAULT_TEMPLATES = [
    {
        "title": "Report template for Coach",
        "icon": "🎓",
        "prompt": COACH_REPORT_PROMPT,
        "example_output": """*Report template for Coach* -

*1. Learning Activities In Progress* -
• Child is practicing 2-digit subtraction with borrowing.

*2. Learning Activities Completed*
• Completed reading comprehension passage 3 accurately.

*3. ISF Coins awarded*
• 10 ISF Coins awarded for completing handwriting exercises without prompts.

*4. Practice*
• Practice reading paragraph 4 aloud twice at home.

*5. Observations*
• Very attentive today, smiled and shared excitement when solving the math puzzle.""",
        "sort_order": 0,
    },
    {
        "title": "Meeting minutes",
        "icon": "🗒️",
        "prompt": MEETING_MINUTES_PROMPT,
        "example_output": None,
        "sort_order": 1,
    },
    {
        "title": "Executive Brief",
        "icon": "📋",
        "prompt": "Generate a formal executive summary (3-4 concise paragraphs) covering core meeting objectives, primary discussions, key decisions reached, and high-level business impact.",
        "example_output": """**Executive Summary**\nThe morning production briefing centered on stabilizing fermentation temperatures across batch M-42 and aligning on upcoming dispatch timelines.\n\n**Core Discussion**\nTeam reviewed sensor readings from Tank 2 which exhibited a 1.2°C temperature variance overnight. corrective measures were assigned to the maintenance shift.\n\n**Key Outcomes & Impact**\nStandard operating protocol was reaffirmed for cooling jacket monitoring, ensuring zero compromise to batch flavor profile and timeline integrity.""",
        "sort_order": 1,
    },
    {
        "title": "Bullet Points",
        "icon": "📝",
        "prompt": "Generate a clean, structured bullet-point summary organized by topics discussed. Highlight critical takeaways clearly and concisely.",
        "example_output": """• **Fermentation Temperature:** Tank 2 spiked by 1.2°C overnight; sensor calibration required.\n• **Ingredient Stock:** Salt and koji supplies are sufficient for the next 14 days.\n• **Shift Handover:** Evening crew to log manual temperature checks every 2 hours.\n• **Dispatch Schedule:** Batch 38 packed and ready for Mumbai shipment by 4 PM.""",
        "sort_order": 2,
    },
    {
        "title": "Action-Focused",
        "icon": "✅",
        "prompt": "Extract and prioritize all action items, decisions, and commitments made. Organize by urgency, specifying clearly WHO is assigned, WHAT must be done, and DEADLINE if mentioned.",
        "example_output": """**High Priority / Immediate:**\n1. **Rahul** — Calibrate Tank 2 temperature probe before 2 PM today.\n2. **Pooja** — Verify packaging seals on Batch 38 before dispatch.\n\n**Medium Priority:**\n3. **Maintenance Team** — Inspect chiller water pressure by tomorrow morning.\n\n**Agreed Decisions:**\n- Any temperature deviation above 1.5°C triggers immediate WhatsApp supervisor alert.""",
        "sort_order": 3,
    },
    {
        "title": "Casual / WhatsApp",
        "icon": "💬",
        "prompt": "Summarize this session in simple, crisp, conversational language ready to be copied and pasted directly into WhatsApp for team members. Use emojis tastefully.",
        "example_output": """📢 *Team Update from Today's Meeting* 📢\n\nHey team, quick summary of our discussion:\n\n🔹 *Tank 2 Temp:* Spiked a bit last night — Rahul is calibrating the sensor by 2 PM.\n🔹 *Dispatch:* Batch 38 is ready to move at 4 PM 🚚\n🔹 *Evening Shift:* Please record manual temps every 2 hours on the sheet.\n\nGood job everyone, let's keep things smooth! 👍""",
        "sort_order": 4,
    },
    {
        "title": "Hinglish Summary",
        "icon": "🗣️",
        "prompt": "Generate the complete summary in natural conversational Hinglish (Hindi words written in Roman/English script, mixed naturally with English as spoken on factory floors). Make it clear, friendly, and practical for on-ground staff.",
        "example_output": """**Aaj ki Meeting ka Summary:**\n\n1. **Tank 2 ka Temperature Issue:**\nRaat ko Tank 2 ka temp thoda upar chala gaya tha (1.2°C). Rahul bhai aaj do baje se pehle sensor check aur calibrate karenge.\n\n2. **Dispatch Updates:**\nBatch 38 ka packing complete ho gaya hai. Pooja didi final verification karengi aur 4 baje tak dispatch niklega.\n\n3. **Night Shift ke liye Note:**\nHar 2 ghante mein register mein temperature note karna zaroori hai. Koi bhi deviation ho toh turant update karein.\n\nSabhi log action items pe dhyaan dein! 🚀""",
        "sort_order": 5,
    },
]


def format_seconds(seconds: Optional[float]) -> str:
    """Format seconds into MM:SS or HH:MM:SS string."""
    if seconds is None:
        return "00:00"
    total_sec = int(round(seconds))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_datetime_ist(dt: Optional[datetime]) -> str:
    """Format datetime in IST friendly string e.g. '16 Sep 2026, 11:30 AM IST'."""
    if not dt:
        return "Date/time not recorded"
    # Convert to IST if timezone-aware, or treat as UTC and convert
    if dt.tzinfo is None:
        dt_ist = dt.replace(tzinfo=timezone.utc).astimezone(IST_OFFSET)
    else:
        dt_ist = dt.astimezone(IST_OFFSET)
    return dt_ist.strftime("%d %b %Y, %I:%M %p IST")


def build_session_context(
    recording: Dict[str, Any],
    segments: List[Dict[str, Any]],
    action_items: List[Dict[str, Any]],
    user_preferences: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Builds the system prompt context for per-session chat.
    Gracefully handles sessions with or without segment timestamps.
    """
    title = recording.get("title") or recording.get("note") or "Recording Session"
    recorder_name = recording.get("user_name") or recording.get("user_phone") or "Staff"
    started_at = recording.get("started_at") or recording.get("created_at")
    date_str = format_datetime_ist(started_at)
    
    duration_sec = recording.get("duration_seconds")
    if duration_sec:
        duration_str = format_seconds(duration_sec)
    else:
        duration_str = "Unknown"

    speaker_count = recording.get("speaker_count") or (len(set(s.get("speaker") for s in segments if s.get("speaker"))) if segments else "Unknown")
    language = recording.get("detected_language") or "Mixed (Hindi / English)"

    overview = recording.get("overview") or "No overview summary generated yet."

    key_points_raw = recording.get("key_points")
    if isinstance(key_points_raw, list) and key_points_raw:
        key_points_str = "\n".join(f"- {kp}" for kp in key_points_raw)
    elif isinstance(key_points_raw, str) and key_points_raw:
        key_points_str = key_points_raw
    else:
        key_points_str = "None recorded"

    decisions_raw = recording.get("decisions")
    if isinstance(decisions_raw, list) and decisions_raw:
        decisions_str = "\n".join(f"- {d}" for d in decisions_raw)
    elif isinstance(decisions_raw, str) and decisions_raw:
        decisions_str = decisions_raw
    else:
        decisions_str = "None recorded"

    # Action items formatting
    if action_items:
        ai_lines = []
        for ai in action_items:
            assigned = ai.get("assigned_to") or "Unassigned"
            urgency = ai.get("urgency") or "normal"
            due = ai.get("due_date") or "No deadline"
            desc = ai.get("description") or ""
            ai_lines.append(f"- [{urgency.upper()}] {desc} (Assigned: {assigned}, Due: {due})")
        action_items_str = "\n".join(ai_lines)
    else:
        action_items_str = "None recorded"

    # Transcript formatting — supports WITH or WITHOUT timestamps
    transcript_lines = []
    has_timestamps = False
    if segments:
        for seg in segments:
            speaker = seg.get("speaker") or "Speaker"
            text_content = seg.get("text") or ""
            start_t = seg.get("start_time")
            if start_t is not None:
                has_timestamps = True
                timestamp_label = f"[{format_seconds(float(start_t))}] "
            else:
                timestamp_label = ""
            transcript_lines.append(f"{timestamp_label}{speaker}: {text_content}")
    
    if not transcript_lines and recording.get("transcript_text"):
        transcript_str = recording["transcript_text"]
    elif transcript_lines:
        transcript_str = "\n".join(transcript_lines)
    else:
        transcript_str = "No transcript available for this recording."

    # User preferences
    prefs_str = ""
    if user_preferences:
        pref_lang = user_preferences.get("preferred_language", "default")
        pref_style = user_preferences.get("response_style", "default")
        topics = user_preferences.get("common_topics", [])
        prefs_str = f"""
USER PROFILE & LEARNED PREFERENCES:
- Preferred language: {pref_lang}
- Response style preference: {pref_style}
- Frequently discussed topics: {', '.join(topics) if topics else 'None recorded'}
(Please adapt tone and language to these preferences where appropriate.)
"""

    timestamp_instruction = (
        "Timestamps are included in the transcript above. When quoting or referring to specific statements, cite the timestamp (e.g. [02:15])."
        if has_timestamps
        else "Timestamps are not present in this transcript. Reference speakers and topics directly without timestamps."
    )

    # Order matters for cost: Gemini's implicit cache discounts a repeated PREFIX. The fixed
    # instructions and the transcript (the bulk of the tokens) come first and stay byte-identical
    # between messages; things that can change (learned preferences) go last.
    context = f"""You are an intelligent, highly helpful AI assistant for ISF Recorder, designed for factory and organizational operations.
You are chatting with a team member about a specific audio recording session.

GUIDELINES:
1. Answer questions accurately based on this recording's content and metadata.
2. {timestamp_instruction}
3. If asked about when the meeting happened or how long it took, use the Recording Date & Time and Duration below.
4. If asked about something not mentioned in the transcript or metadata, politely explain that it wasn't discussed in this session.
5. If the user prompts in Hindi, English, or Hinglish, respond in the corresponding language naturally.
6. FORMATTING RULE: Respond in clean, beautifully formatted plain Markdown text with real paragraphs and bullet points. NEVER output raw JSON objects, JSON code blocks (e.g. ```json), or escaped \n strings.

TRANSCRIPT:
{transcript_str}

RECORDING SESSION METADATA:
- Title: "{title}"
- Recorded By: {recorder_name}
- Recording Date & Time: {date_str}
- Duration: {duration_str}
- Speakers: {speaker_count}
- Language: {language}

EXECUTIVE OVERVIEW:
{overview}

KEY DISCUSSION POINTS:
{key_points_str}

DECISIONS:
{decisions_str}

ACTION ITEMS:
{action_items_str}
{prefs_str}"""
    return context.strip()


def build_all_sessions_context(
    sessions: List[Dict[str, Any]],
    user_preferences: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Builds the system prompt context for all-sessions AI chat.
    Uses compact overviews + key points (NOT full transcripts) for speed and token efficiency.
    """
    prefs_str = ""
    if user_preferences:
        pref_lang = user_preferences.get("preferred_language", "default")
        pref_style = user_preferences.get("response_style", "default")
        topics = user_preferences.get("common_topics", [])
        prefs_str = f"""
USER PROFILE & LEARNED PREFERENCES:
- Preferred language: {pref_lang}
- Response style preference: {pref_style}
- Frequently discussed topics: {', '.join(topics) if topics else 'None recorded'}
(Please adapt tone and language to these preferences where appropriate.)
"""

    session_blocks = []
    # Cap to last 20 sessions (instead of 40) for massive token savings and lower latency
    for idx, s in enumerate(sessions[:20], 1):
        title = s.get("title") or s.get("note") or f"Session {idx}"
        recorder = s.get("user_name") or s.get("user_phone") or "Staff"
        dt_str = format_datetime_ist(s.get("started_at") or s.get("created_at"))
        dur_str = format_seconds(s.get("duration_seconds")) if s.get("duration_seconds") else "Unknown"
        
        # For recent sessions (1-5), provide detailed overview. For older sessions (6-20), compact to 120 chars.
        raw_overview = s.get("overview") or "No overview available."
        if idx > 5 and len(raw_overview) > 120:
            overview = raw_overview[:120].rsplit(" ", 1)[0] + "..."
        else:
            overview = raw_overview
        
        kp = s.get("key_points")
        if isinstance(kp, list) and kp:
            kp_str = " | ".join(kp[:2])
        elif isinstance(kp, str) and kp:
            kp_str = kp[:120]
        else:
            kp_str = "None"

        dec = s.get("decisions")
        if isinstance(dec, list) and dec:
            dec_str = " | ".join(dec[:2])
        elif isinstance(dec, str) and dec:
            dec_str = dec[:80]
        else:
            dec_str = "None"

        block = (
            f"--- Session #{idx}: \"{title}\" ---\n"
            f"ID: {s.get('id')}\n"
            f"Date & Time: {dt_str} | Duration: {dur_str} | Recorded by: {recorder}\n"
            f"Overview: {overview}\n"
            f"Key Points: {kp_str}\n"
            f"Decisions: {dec_str}"
        )
        session_blocks.append(block)

    sessions_text = "\n\n".join(session_blocks) if session_blocks else "No recorded sessions found in the archive."

    # Stable instructions first, learned preferences last (implicit prompt caching works on prefixes)
    return f"""You are "AI Chat", the comprehensive organization-wide intelligence assistant for ISF Recorder.
You have access to the archive of recording sessions across the organization.

GUIDELINES:
1. You can synthesize information across multiple sessions, identify trends, track project status over time, or find specific discussions.
2. Always specify which session(s) you are referring to by Title and Date & Time.
3. If a user asks what was discussed this week, yesterday, or regarding a specific topic (e.g., miso, packaging, temperature), search across all relevant sessions.
4. If a topic was never mentioned in any session, clearly state that.
5. If the user prompts in Hinglish or Hindi, reply naturally in the same style.
6. FORMATTING RULE: Respond in clean, readable plain Markdown text with real paragraphs and bullet points. NEVER output raw JSON objects, JSON code blocks (e.g. ```json), or escaped \n strings.

TOTAL SESSIONS LOADED: {len(session_blocks)}

ARCHIVE OF RECORDINGS (Ordered newest to oldest):
{sessions_text}
{prefs_str}""".strip()


from backend.transcription.usage_tracker import track_gemini_call


def _extract_human_text_from_dict(d: dict) -> str:
    """Extracts clean human-readable Markdown text from a structured dict or report object."""
    parts = []

    title = d.get("title")
    if title and isinstance(title, str) and not title.lower().startswith("untitled") and not title.lower().startswith("audio recording"):
        parts.append(f"### {title}")

    main_body = d.get("overview") or d.get("report") or d.get("summary") or d.get("content") or d.get("response") or d.get("message")
    if main_body and isinstance(main_body, str):
        main_clean = main_body.replace("\\r\\n", "\n").replace("\\n", "\n")
        parts.append(main_clean)
    elif main_body and isinstance(main_body, dict):
        for k, v in main_body.items():
            if isinstance(v, list):
                v_str = "\n".join(f"• {x}" for x in v)
                parts.append(f"**{k}:**\n{v_str}")
            else:
                parts.append(f"**{k}:** {v}")

    # Key discussion points
    kp = d.get("key_points")
    if isinstance(kp, list) and kp:
        kp_lines = "\n".join(f"• {p}" for p in kp if p)
        if kp_lines:
            parts.append(f"**Key Discussion Points:**\n{kp_lines}")
    elif isinstance(kp, str) and kp.strip():
        parts.append(f"**Key Discussion Points:**\n{kp.strip()}")

    # Decisions
    dec = d.get("decisions")
    if isinstance(dec, list) and dec:
        dec_lines = "\n".join(f"• {x}" for x in dec if x)
        if dec_lines:
            parts.append(f"**Decisions:**\n{dec_lines}")
    elif isinstance(dec, str) and dec.strip():
        parts.append(f"**Decisions:**\n{dec.strip()}")

    # Action items
    ai = d.get("action_items")
    if isinstance(ai, list) and ai:
        ai_lines = []
        for item in ai:
            if isinstance(item, dict):
                desc = item.get("description") or ""
                assignee = item.get("assigned_to")
                assignee_str = f" (@{assignee})" if assignee else ""
                urgency = item.get("urgency", "normal")
                urgency_badge = "🔴 " if urgency in ["high", "critical"] else "🔹 "
                ai_lines.append(f"{urgency_badge}{desc}{assignee_str}")
            elif isinstance(item, str):
                ai_lines.append(f"🔹 {item}")
        if ai_lines:
            parts.append(f"**Action Items:**\n" + "\n".join(ai_lines))

    # Fallback if none of the standard keys matched
    if not parts:
        for k, v in d.items():
            k_title = k.replace("_", " ").title()
            if isinstance(v, list):
                v_lines = "\n".join(f"• {x}" for x in v)
                parts.append(f"**{k_title}:**\n{v_lines}")
            elif isinstance(v, dict):
                sub_lines = "\n".join(f"• {sk}: {sv}" for sk, sv in v.items())
                parts.append(f"**{k_title}:**\n{sub_lines}")
            else:
                parts.append(f"**{k_title}:** {v}")

    return "\n\n".join(parts)


def clean_chat_response(text: str) -> str:
    """
    Cleans an AI response or overview to eliminate raw JSON dumps and literal '\\n' escape strings,
    ensuring clean, beautifully rendered Markdown text in chat bubbles and recording screens.
    """
    if not text:
        return ""

    clean = text.strip()

    # 1. First check for JSON code block ```json ... (even if unclosed or truncated)
    if "```json" in clean or "```" in clean or clean.startswith("{"):
        m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)(?:```|$)", clean)
        json_candidate = m_fence.group(1).strip() if m_fence else clean

        extracted = None
        try:
            parsed = json.loads(json_candidate)
            if isinstance(parsed, dict):
                extracted = _extract_human_text_from_dict(parsed)
        except Exception:
            pass

        # If json.loads failed (truncated or unescaped newlines/quotes), use robust regex extraction
        if not extracted:
            m_title = re.search(r'"title"\s*:\s*"([^"\n\r]+)"', json_candidate)
            title = m_title.group(1).strip() if m_title else None

            m_body = re.search(r'"(?:overview|report|summary|content|response|message)"\s*:\s*"([\s\S]*?)(?:"\s*,\s*"\w+"|\s*"\s*\}|\s*"$|$)', json_candidate)
            if m_body:
                body = m_body.group(1).strip()
                body = re.sub(r'["\'}\s]+$', '', body)
                body = body.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
                if title and not title.lower().startswith("untitled") and not title.lower().startswith("audio recording"):
                    extracted = f"### {title}\n\n{body}"
                else:
                    extracted = body
            elif title:
                extracted = f"### {title}"

        if extracted:
            prefix = ""
            if m_fence and m_fence.start() > 0:
                prefix = clean[:m_fence.start()].strip()
            suffix = ""
            if m_fence and m_fence.end() < len(clean):
                suffix = clean[m_fence.end():].strip()
            parts = [p for p in [prefix, extracted, suffix] if p]
            return "\n\n".join(parts)

    # 2. Unescape any literal \n in regular text
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")

    return text


def chat_with_gemini(
    system_context: str,
    history: List[Dict[str, Any]],
    new_message: str,
    template: Optional[Dict[str, Any]] = None,
    recording_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> str:
    """
    Calls Gemini API with the given system context, recent message history, and user input.
    Optionally applies an admin prompt template and tracks usage.
    """
    client = get_genai_client()

    # Prepend template instruction if specified
    user_content = new_message.strip()
    if template:
        t_prompt = (template.get("prompt") or "").strip()
        t_example = (template.get("example_output") or "").strip()  # optional: admin templates may have none
        t_title = template.get("title", "Template")
        
        template_instruction = (
            f"[APPLY TEMPLATE: {t_title}]\n"
            f"Follow the template below EXACTLY: the same section names, numbering, order and *bold* markers, "
            f"and every rule written in brackets or notes inside it.\n"
            f"Use ONLY facts stated in the TRANSCRIPT of this session. Never invent names, coins, activities, "
            f"medical details or tasks. If something is not in the transcript, follow the template's rule for "
            f"missing sections (leave the section out) instead of guessing.\n\n"
            f"TEMPLATE:\n{t_prompt}"
        )
        if t_example:
            template_instruction += (f"\n\nEXAMPLE OF THE FORMAT ONLY (do NOT copy its content; it is not about "
                                     f"this session):\n{t_example}")

        if user_content:
            user_content = f"{template_instruction}\n\nUSER ADDITIONAL QUERY:\n{user_content}"
        else:
            user_content = f"{template_instruction}\n\nPlease generate the output according to the template above using the conversation context."

    # Build contents array from history + new user message
    # Gemini SDK accepts list of contents or text
    formatted_contents = []
    # Cap history to last 14 messages (so total with new message is <= 15). A template report is built
    # from the transcript alone: earlier answers (e.g. an older, wrong report) must not leak into it.
    recent_history = [] if template else (history[-14:] if len(history) > 14 else history)
    for msg in recent_history:
        role = "user" if msg.get("role") == "user" else "model"
        formatted_contents.append({
            "role": role,
            "parts": [{"text": msg.get("content", "")}]
        })

    # Add the current message
    formatted_contents.append({
        "role": "user",
        "parts": [{"text": user_content}]
    })

    try:
        from google.genai import types
        # Use admin-configured model from org settings (via model_resolver)
        from backend.settings.model_resolver import get_intelligence_model
        active_model = get_intelligence_model(org_id)

        config = types.GenerateContentConfig(
            system_instruction=system_context,
            temperature=0.1 if template else 0.3,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

        def _do_chat():
            return client.models.generate_content(
                model=active_model,
                contents=formatted_contents,
                config=config,
            )

        response = track_gemini_call(
            recording_id=recording_id,
            operation_type="CHAT",
            gemini_model=active_model,
            call_fn=_do_chat,
        )
        raw_output = (response.text or "").strip()
        return clean_chat_response(raw_output)
    except Exception as e:
        logger.error(f"Gemini chat failed: {e}")
        # Fallback to direct prompt if structured call has issue
        try:
            fallback_prompt = f"{system_context}\n\nUSER QUERY:\n{user_content}"
            def _do_fallback():
                return client.models.generate_content(
                    model=active_model,
                    contents=[fallback_prompt],
                    config=config,
                )

            fallback_resp = track_gemini_call(
                recording_id=recording_id,
                operation_type="CHAT",
                gemini_model=active_model,
                call_fn=_do_fallback,
            )
            raw_fallback = (fallback_resp.text or "I apologize, but I could not generate a response.").strip()
            return clean_chat_response(raw_fallback)
        except Exception as e2:
            logger.exception(f"Gemini fallback chat also failed: {e2}")
            return f"Error communicating with Gemini: {str(e)}"


def update_user_preferences(db: Session, user_id: str, org_id: Optional[str] = None) -> None:
    """
    Learns user preferences from recent chat history and updates user.settings JSONB.
    Runs periodically (e.g. after every 10 messages).
    """
    try:
        # Fetch last 20 messages sent by this user
        query = text("""
            SELECT m.content
            FROM isf_recorder.chat_message m
            JOIN isf_recorder.chat_conversation c ON m.conversation_id = c.id
            WHERE c.user_id = :user_id AND m.role = 'user'
            ORDER BY m.created_at DESC
            LIMIT 20
        """)
        rows = db.execute(query, {"user_id": user_id}).fetchall()
        if not org_id:
            org_id = db.execute(text("SELECT CAST(org_id AS text) FROM isf_recorder.user WHERE id = :id"),
                                {"id": user_id}).scalar()
        if len(rows) < 3:
            return  # Need at least 3 messages to deduce preferences

        sample_texts = "\n".join(f"- {r[0]}" for r in rows)

        client = get_genai_client()
        learn_prompt = f"""Analyze the following sample messages sent by a factory user in our recording app:
\"\"\"{sample_texts}\"\"\"

Task: Extract this user's communication style & preferences.
Return ONLY valid JSON matching this schema:
{{
  "preferred_language": "hinglish" | "english" | "hindi" | "mixed",
  "response_style": "concise" | "bulleted" | "detailed" | "conversational",
  "common_topics": ["topic1", "topic2", "topic3"]
}}
"""
        def _do_learn():
            return client.models.generate_content(model=GEMINI_INTELLIGENCE_MODEL, contents=[learn_prompt])

        # Logged like every other Gemini call (it used to be untracked)
        resp = track_gemini_call(
            recording_id=None,
            operation_type="CHAT",
            gemini_model=GEMINI_INTELLIGENCE_MODEL,
            call_fn=_do_learn,
            org_id=org_id,
        )
        cleaned = clean_json_response(resp.text or "{}")
        prefs = json.loads(cleaned)
        prefs["last_learned_at"] = datetime.now(timezone.utc).isoformat()

        # Update user.settings JSONB field
        db.execute(
            text("""
                UPDATE isf_recorder.user
                SET settings = jsonb_set(
                    COALESCE(settings, '{}'::jsonb),
                    '{ai_preferences}',
                    CAST(:prefs_json AS jsonb),
                    true
                )
                WHERE id = :user_id
            """),
            {"user_id": user_id, "prefs_json": json.dumps(prefs)}
        )
        db.commit()
        logger.info(f"Updated AI preferences for user {user_id}: {prefs}")
    except Exception as e:
        logger.warning(f"Failed to update user preferences for {user_id}: {e}")
        db.rollback()


def seed_default_templates(db: Session, org_id: str, admin_user_id: Optional[str]) -> None:
    """
    Seeds default templates if none have ever been seeded for this organization.
    Checks organization.settings['chat_templates_seeded'] so that if an admin deletes
    templates, they are never re-seeded automatically.
    """
    try:
        org_row = db.execute(
            text("SELECT settings FROM isf_recorder.organization WHERE id = :org_id"),
            {"org_id": org_id}
        ).fetchone()

        settings = (org_row[0] or {}) if org_row else {}
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}

        # If already marked as seeded in org settings, respect admin edits and deletions
        if settings.get("chat_templates_seeded"):
            return

        count = db.execute(
            text("SELECT COUNT(*) FROM isf_recorder.chat_template WHERE org_id = :org_id"),
            {"org_id": org_id}
        ).scalar()

        if count == 0:
            for tmpl in DEFAULT_TEMPLATES:
                db.execute(
                    text("""
                        INSERT INTO isf_recorder.chat_template 
                        (org_id, created_by, title, prompt, example_output, icon, sort_order)
                        VALUES (:org_id, :created_by, :title, :prompt, :example_output, :icon, :sort_order)
                    """),
                    {
                        "org_id": org_id,
                        "created_by": admin_user_id,
                        "title": tmpl["title"],
                        "prompt": tmpl["prompt"],
                        "example_output": tmpl.get("example_output"),
                        "icon": tmpl["icon"],
                        "sort_order": tmpl["sort_order"],
                    }
                )
            logger.info(f"Seeded {len(DEFAULT_TEMPLATES)} default chat templates for org {org_id}")

        # Mark seeded in organization settings (one key, atomic: never rewrite the whole settings blob)
        db.execute(
            text("""UPDATE isf_recorder.organization
                    SET settings = COALESCE(settings, '{}'::jsonb) || '{"chat_templates_seeded": true}'::jsonb
                    WHERE id = :org_id"""),
            {"org_id": org_id}
        )
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to seed default templates: {e}")
        db.rollback()
