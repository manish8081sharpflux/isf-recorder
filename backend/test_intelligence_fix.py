import asyncio
import os
import sys

# Test extract_intelligence
from backend.transcription.intelligence import extract_intelligence

async def test():
    sample_transcript = """
    Coach: Aaj humne Rahul ke saath Math aur Hindi practice ki. Rahul ne table of 4 aur 5 pura kiya.
    Coach: Uske baad Hindi reading ki, 2 mistakes hue the jo correct kiye.
    Coach: Rahul ko 10 ISF Coins award kiye gaye for completing the math activity.
    Coach: Homework: Table of 6 learn karna hai kal tak.
    Coach: Observation: Rahul bahut attentive tha aur jaldi seekh raha hai.
    """
    print("Testing extract_intelligence...")
    result = await extract_intelligence(sample_transcript)
    print("Title:", result.get("title"))
    print("Overview:", result.get("overview"))
    print("Action items:", result.get("action_items"))
    print("Key points:", result.get("key_points"))
    assert "encountered an error" not in result.get("overview", "").lower(), "Error found in overview!"
    print("SUCCESS: Intelligence extracted without error!")

if __name__ == "__main__":
    asyncio.run(test())
