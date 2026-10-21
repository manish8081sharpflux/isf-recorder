"""Default report template prompts (contract §9). Shared by the startup seed and chat defaults."""

COACH_REPORT_PROMPT = """Generate an individual child report for each child mentioned in the recording session. The report should begin with a "General Overview of the Complete Schedule" for the entire session. Following this, it should list the child's name, followed by relevant points under the categories below. If a category has no information for a specific child, that category should be omitted for that child. Ensure each point is clear and detailed, drawing directly and exclusively from the provided transcript and metadata. Do not include timestamps in the report. Use "ISF Coins" for awards.

**General Overview of the Complete Schedule** - (Provide a brief summary of the overall activities and flow of the session.)

*1. Learning Activities In Progress* - (Describe activities the child is currently practicing, not yet completed.)

*2. Learning Activities Completed* - (List activities the child has successfully finished. If coins are awarded for an activity then that activity should be in this section)

*3. ISF Coins awarded* - (Specify the number of ISF Coins awarded and, if possible, the reason.)

*4. Practice* - (Detail any homework assigned or specific improvement points suggested for learning activities.)

*5. Observations* - (Comments by the coach regarding the child's behaviour, e.g. "Durga is making too much noise", "Karan was crying", "Pooja Bala is doing better now". Everything except learning activities. Do NOT include comments related to learning/educational activity here.)

*6. Medical* - (Report any mentioned health issues or medical information about the child during the session.)

*7. General Tasks* - (List any tasks given by the teacher to the child that are *not* directly related to learning activities.)"""

MEETING_MINUTES_PROMPT = """Write meeting minutes in Markdown from the transcript and metadata only.

Structure:
# <A short main title for the meeting>

## Summary
A short paragraph (2-4 sentences): what the meeting was about and its outcome.

Then one section per topic the discussion needs, each as a bold subtitle followed by bullet points, for example:
**Decisions**
- ...
**<Another topic>**
- ...

End with:
**Action tasks**
- Task — owner — due date (only if mentioned)

Rules: omit any section that would be empty. Do not include timestamps. Do not invent names, numbers, decisions or dates. Write in the language of the meeting."""


# Round-2 text, kept only so the startup seed can recognise an unedited row and upgrade it once.
MEETING_MINUTES_PROMPT_V1 = """Write clear meeting minutes from the transcript and metadata only. Sections, omitting any that are empty:
**Meeting overview** - (2-4 sentences: purpose and outcome.)
**Participants** - (From metadata; add anyone clearly speaking in the transcript.)
**Key discussion points** - (Bullets.)
**Decisions** - (Bullets.)
**Action items** - (Bullets: task — owner — due date if mentioned.)
**Follow-ups / open questions** - (Bullets.)
Do not include timestamps. Do not invent names, numbers or decisions."""


# The user's earlier Observations wording, replaced (2026-10-13, approved by the user) in every saved
# template that still contains it exactly; templates where an admin rewrote the line are left alone.
OBSERVATIONS_OLD_LINE = """*5. Observations* - (Focus on child behavior, comments by the coach, or any positive remarks about the child. Do NOT include comments related to learning/educational activity here.)"""
OBSERVATIONS_NEW_LINE = """*5. Observations* - (Comments by the coach regarding the child's behaviour, e.g. "Durga is making too much noise", "Karan was crying", "Pooja Bala is doing better now". Everything except learning activities. Do NOT include comments related to learning/educational activity here.)"""
