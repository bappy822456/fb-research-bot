# FB Research Helper — Free Webhook Edition

An English Telegram bot intended for educational research using public profile links/IDs. It uses Telegram webhooks and is designed for a Render **Free Web Service** (not a background worker).

## Important accuracy limits

- A Facebook numeric ID is **not a reliable timestamp**. The bot does not invent an account creation date or age from an ID prefix.
- Trustworthiness, authenticity, and account risk cannot be determined from the ID alone.
- After a profile report, tap **Calculate from public Joined date** and enter the date Facebook displays (YYYY-MM-DD or YYYY-MM). The bot calculates elapsed time from that user-supplied date; it does not independently verify it.
- Or tap **Use oldest public post** and enter its date. The bot reports only a conservative minimum-age clue: the profile existed by that post date; the account may be older.
- Month-only input produces a range. A numeric ID alone never produces an age estimate.
- Username-to-ID lookup is best effort and depends on an external lookup site; it can fail or return no ID.
- No phone-number search, login bypass, or private account data is used.

## Files

- `webhook_app.py` — Flask webhook receiver, safe output, username lookup, health endpoint.
- `Dockerfile` — production Gunicorn startup; binds to Render's `$PORT`.
- `requirements.txt` — pinned Python packages.
- `render.yaml` — Render Free Web Service Blueprint (`plan: free`).
- `.env.example` — placeholder only; contains no real token.
- `.dockerignore`, `.gitignore` — prevent accidental credential uploads.

## Deploy on Render

1. Create/update a GitHub repository with these files at the repository root. Remove old polling `render.yaml` configuration; this edition is a **Web Service**.
2. In Render choose **New → Web Service** and select the repository. Do not choose Background Worker.
3. Select Docker runtime and the **Free** compute plan.
4. Add Environment Variable: `BOT_TOKEN` = the current token from BotFather. Never put the real token in GitHub or screenshots.
5. Deploy. On startup the app registers a Telegram webhook using Render's `RENDER_EXTERNAL_URL`.
6. Visit `/healthz` on the Render URL. Expect `token_configured: true` and `webhook_registered: true`.

Free Render web services can spin down after inactivity; the first request after a quiet period may be slow. This is a hosting limitation, not an account-age calculation.
