# Word-Problem Solver API

A FastAPI microservice that solves multi-step arithmetic word problems and
returns a strictly validated JSON contract:

```json
{"reasoning": "<string, >= 80 chars>", "answer": <integer>}
```

## How it works

1. `POST /solve` receives `{"problem_id": ..., "problem": "..."}`.
2. The server prompts Claude with a system prompt that forces raw JSON,
   chain-of-thought reasoning, and explicit handling of distractor numbers.
3. Before replying to the caller, the server **independently validates**
   the model's JSON:
   - exactly the keys `reasoning` and `answer`
   - `reasoning` is a string of at least 80 characters
   - `answer` is a real Python `int` (rejects `"945"`, `945.0`, `True`)
4. If validation fails, the server sends the model a corrective follow-up
   message describing exactly what was wrong and retries (up to 3 times),
   so transient formatting slips don't reach the grader.

This means the contract is enforced by code, not just by prompting — the
model can misbehave and the service still won't leak a malformed response.

## Deploy it (you need a public URL for the grader)

I can't host a live public endpoint myself, but this deploys in a few
minutes on any of these free/cheap options:

### Option A — Render.com (recommended, free tier)
1. Push this folder to a new GitHub repo.
2. On render.com: New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add environment variable `ANTHROPIC_API_KEY` = your Anthropic API key.
6. Deploy. Your endpoint will be:
   `https://<your-service>.onrender.com/solve`

### Option B — Railway.app
1. `railway init` in this folder, `railway up`.
2. Set the `ANTHROPIC_API_KEY` variable in the Railway dashboard.
3. Railway auto-detects the `Procfile`.

### Option C — Fly.io
```
fly launch          # accept defaults, it detects Python/FastAPI
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly deploy
```

### Local test
```
export ANTHROPIC_API_KEY=sk-ant-...
pip install -r requirements.txt
uvicorn main:app --reload
curl -X POST http://localhost:8000/solve \
  -H "Content-Type: application/json" \
  -d '{"problem_id":"p0","problem":"A workshop orders 150 tiles at 8 dollars each. Any order of more than 50 units earns a 25% bulk discount, and a 5% tax is added afterward. The workshop is 12 km away and has 3 product lines. What is the final cost?"}'
```

Expected response:
```json
{"reasoning": "Base = 150 * 8 = 1200. Order > 50 so apply 25% discount: 1200 * 0.75 = 900. Add 5% tax: 900 * 1.05 = 945. The km and product-line counts are irrelevant.", "answer": 945}
```

Once deployed, submit `https://<your-domain>/solve` as your endpoint URL.
