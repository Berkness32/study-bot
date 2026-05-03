"""
app/vision.py — Handwritten math transcription and error detection via LLaVA
"""

import base64
import re
from pathlib import Path
import ollama

VISION_MODEL = "llava:13b"

TRANSCRIBE_PROMPT = """You are a precise math transcription assistant.
The image shows handwritten math work. Your job is to transcribe it exactly.

Instructions:
- Transcribe every line of math exactly as written, including any errors
- Use LaTeX notation for all math (e.g. \\frac{a}{b}, x^2, \\sqrt{x})
- Preserve the student's step-by-step work in order
- Label each step as: Step 1:, Step 2:, etc.
- Do NOT correct errors — transcribe exactly what is written
- If something is illegible, write [illegible] in that spot

Return only the transcription, no commentary."""

ERROR_CHECK_PROMPT = """You are a precise math error checker.

Here is a student's handwritten work (transcribed):
{transcription}

And here is the correct solution computed by a math engine:
{correct_solution}

Your job:
1. Compare the student's work step by step against the correct solution
2. Identify every error — algebraic mistakes, arithmetic errors, sign errors, etc.
3. For each error explain: what the student wrote, what it should be, and why

Format your response as:
**Errors Found:** (or "No errors found" if correct)
For each error:
- **Step N:** [what student wrote] → should be [correct value] because [reason]

Then show the complete correct solution with LaTeX."""


def image_to_base64(image_path: str) -> str:
    """Convert image file to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def transcribe_image(image_path: str) -> str:
    """
    Use LLaVA to transcribe handwritten math from an image.
    Returns the transcribed math as a string.
    """
    try:
        b64 = image_to_base64(image_path)
        response = ollama.chat(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": TRANSCRIBE_PROMPT,
                "images": [b64],
            }]
        )
        return response["message"]["content"].strip()
    except Exception as e:
        return f"Transcription error: {e}"


def check_errors(transcription: str, correct_solution: str) -> str:
    """
    Use LLaVA to compare student transcription against correct solution
    and identify errors.
    """
    try:
        prompt = ERROR_CHECK_PROMPT.format(
            transcription=transcription,
            correct_solution=correct_solution,
        )
        response = ollama.chat(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": prompt,
            }]
        )
        return response["message"]["content"].strip()
    except Exception as e:
        return f"Error check failed: {e}"


def extract_problem_from_transcription(transcription: str) -> str:
    """
    Pull just the first line / original problem statement from
    a transcription so the agent knows what to solve.
    """
    lines = [l.strip() for l in transcription.strip().splitlines() if l.strip()]
    # Return first non-empty line as the problem
    return lines[0] if lines else transcription
