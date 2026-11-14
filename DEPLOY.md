# Publishing FireLink so judges can try it

Judges will not install Python. They need a **link that opens in a browser**.

## Option 1 - Render (free, permanent) - recommended

1. Push the project to GitHub. Two things matter:
   - **Never commit `.env`** (it is already git-ignored). The key goes into Render instead.
   - Ship the starting data so the map is not empty on first load:
     ```bash
     git add -f firelink_output.geojson
     ```
2. Go to <https://render.com>, sign in with GitHub, then **New -> Blueprint** and pick the repository.
   Render reads `render.yaml` automatically.
   (Manual alternative: **New -> Web Service**, Build `pip install -r requirements-deploy.txt`,
   Start `python run_all.py --no-fetch --host 0.0.0.0`.)
3. Under **Environment** add `FIRMS_MAP_KEY` (and optionally `OPENWEATHER_API_KEY`).
4. **Create**, wait for the build, and you get a URL such as `https://firelink-xxxx.onrender.com`.

The free plan sleeps after 15 minutes without traffic, so the first visit afterwards takes 30-60 s.
Open the link yourself before submitting and add a line to the description:
"first load may take up to a minute".

## Option 2 - any Python host (Railway, Fly.io, Heroku, a VPS)

The app reads the port from the `PORT` environment variable:

```bash
PORT=8080 python run_all.py --no-fetch --host 0.0.0.0
```

A `Procfile` is included (`web: python run_all.py --no-fetch --host 0.0.0.0`). There is no database
and no state to migrate, so any Python 3.10+ host works.

## Option 3 - temporary link from a running machine

```bash
python run_all.py --no-fetch --host 0.0.0.0 --port 8005
```

This exposes the app on the machine's own address. It is fine for a live demo or for recording, but it
dies with the machine, so do not submit it as the project link.

## Check the deployment

| Check | Expected |
|---|---|
| `https://<your-url>/map.html` | the map opens with fires already drawn |
| `https://<your-url>/api/query?bbox=-17,-18,42,38&days=1` | JSON containing `counts` |
| `https://<your-url>/.env` | **404** (only three files are served) |
| Press "Show fires" on a small box | results in a few seconds |

## Protecting the key on a public URL

- Only `map.html` and the two generated output files are served; every other path returns 404.
- Query results are cached for 10 minutes (`FIRELINK_QUERY_TTL`), so repeated requests do not consume
  the NASA quota twice.
- If a link spreads too far, rotate the key at NASA and update `FIRMS_MAP_KEY` in the host's settings.
