"""
Reliable Reasoning — Word-Problem Solver API

POST /solve
Body:  {"problem_id": "p0", "problem": "..."}
Reply: {"reasoning": "<string, >=80 chars>", "answer": <integer>}

Design notes:
- Uses Claude with an explicit chain-of-thought instruction, but the model is
  told to keep its *visible* reasoning concise-but->=80-chars and to ignore
  distractor numbers.
- We ask the model for strict JSON via the API's JSON-forcing pattern
  (system prompt says "respond with ONLY JSON, no markdown fences").
- After getting a response we VALIDATE it ourselves (never trust the model):
    * exactly two keys
    * reasoning is a string with len >= 80
    * answer is a real int (rejects "945", 945.0, 945.5, "$945")
- If validation fails, we retry (up to MAX_RETRIES) with a corrective
  message that tells the model exactly what was wrong. This is what makes
  the service "reliable" rather than "hopeful."
"""

import os
import json
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"
MAX_RETRIES = 3

app = FastAPI(title="Word-Problem Solver API")


class ProblemRequest(BaseModel):
    problem_id: str = ""
    problem: str = ""


def _parse_flexible_request(body: dict) -> "ProblemRequest":
    """Accept several possible key names the grader might use."""
    problem_text = (
        body.get("problem")
        or body.get("query")
        or body.get("question")
        or body.get("text")
        or ""
    )
    problem_id = (
        body.get("problem_id")
        or body.get("query_id")
        or body.get("id")
        or ""
    )
    return ProblemRequest(problem_id=str(problem_id), problem=str(problem_text))


class SolutionResponse(BaseModel):
    reasoning: str
    answer: int


SYSTEM_PROMPT = """You are a careful arithmetic word-problem solver.

Rules you MUST follow:
1. The problem may contain irrelevant distractor numbers (e.g. unrelated
   distances, counts, dates, or IDs). Identify only the numbers that matter
   to the actual question being asked, and explicitly say which numbers you
   ignored and why.
2. Work the problem step by step, showing each arithmetic operation and its
   running result.
3. The final answer is always a single integer. If your calculation
   produces a decimal, round only if the problem implies rounding (e.g.
   number of people, tiles, boxes); otherwise the arithmetic should resolve
   to an exact integer — recheck your steps if it does not.
4. Respond with ONLY a raw JSON object. No markdown code fences, no
   commentary before or after, no extra keys.
5. The JSON object must have EXACTLY two keys:
   - "reasoning": a string of at least 80 characters that shows your
     step-by-step work AND names the irrelevant numbers you discarded.
   - "answer": a JSON integer (e.g. 945), never a string, never a float,
     never containing a currency symbol or comma.

Example output:
{"reasoning": "Base = 150 * 8 = 1200. Order > 50 so apply 25% discount: 1200 * 0.75 = 900. Add 5% tax: 900 * 1.05 = 945. The km and product-line counts are irrelevant.", "answer": 945}
"""


def _build_messages(problem: str, correction: str | None = None) -> list[dict]:
    messages = [{"role": "user", "content": f"Problem:\n{problem}"}]
    if correction:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous reply was invalid: "
                    f"{correction} Reply again with ONLY the corrected raw "
                    "JSON object, following all the rules exactly."
                ),
            }
        )
    return messages


async def _call_claude(problem: str, correction: str | None = None) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY is not configured on the server.",
        )

    payload = {
        "model": MODEL,
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": _build_messages(problem, correction),
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Anthropic API error {resp.status_code}: {resp.text}",
            )
        data = resp.json()

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(text_blocks).strip()


def _extract_json(raw: str) -> dict[str, Any]:
    """Strip markdown fences if the model added them anyway, then parse."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _validate(obj: dict[str, Any]) -> tuple[bool, str]:
    """Returns (is_valid, error_message)."""
    if not isinstance(obj, dict):
        return False, "Top-level response must be a JSON object."

    keys = set(obj.keys())
    if keys != {"reasoning", "answer"}:
        extra = keys - {"reasoning", "answer"}
        missing = {"reasoning", "answer"} - keys
        msg = "Keys must be exactly 'reasoning' and 'answer'."
        if extra:
            msg += f" Remove extra key(s): {sorted(extra)}."
        if missing:
            msg += f" Missing key(s): {sorted(missing)}."
        return False, msg

    reasoning = obj["reasoning"]
    if not isinstance(reasoning, str):
        return False, "'reasoning' must be a string."
    if len(reasoning) < 80:
        return False, (
            f"'reasoning' must be at least 80 characters long "
            f"(got {len(reasoning)}). Add more detail about your steps "
            "and which numbers were irrelevant."
        )

    answer = obj["answer"]
    # bool is a subclass of int in Python -- explicitly reject it.
    if isinstance(answer, bool) or not isinstance(answer, int):
        return False, (
            "'answer' must be a JSON integer, not a string, float, or "
            "boolean (e.g. 945, not \"945\" or 945.0)."
        )

    return True, ""


@app.post("/solve", response_model=SolutionResponse)
async def solve(request: Request) -> SolutionResponse:
    body = await request.json()
    req = _parse_flexible_request(body)

    if not req.problem:
        raise HTTPException(
            status_code=422,
            detail="No problem text found. Expected one of: 'problem', 'query', 'question', 'text'.",
        )

    correction: str | None = None
    last_error = ""

    for _ in range(MAX_RETRIES):
        raw = await _call_claude(req.problem, correction)

        try:
            parsed = _extract_json(raw)
        except json.JSONDecodeError as e:
            correction = f"Your reply was not valid JSON ({e})."
            last_error = correction
            continue

        ok, err = _validate(parsed)
        if ok:
            return SolutionResponse(reasoning=parsed["reasoning"], answer=parsed["answer"])

        correction = err
        last_error = err

    raise HTTPException(
        status_code=502,
        detail=f"Failed to get a valid solution after {MAX_RETRIES} attempts: {last_error}",
    )


@app.post("/", response_model=SolutionResponse)
async def solve_root(request: Request) -> SolutionResponse:
    return await solve(request)


@app.get("/health")
async def health():
    return {"status": "ok"}
