# Choixx / Song2Choir Render All-in-One

This is the fixed Render-only version. The `Dockerfile` is in the repository root, so Render can find it.

## Correct GitHub layout

Your GitHub repo must show these files immediately when you open the repo:

```text
Dockerfile
main.py
requirements.txt
render.yaml
song2choir_engine.py
static/
```

Do **not** upload this project inside another folder like `song2choir-render-all-in-one/` unless you set that folder as Render's Root Directory.

## Render settings

Create a new Render Web Service with:

```text
Environment: Docker
Root Directory: leave blank
Dockerfile Path: Dockerfile
Health Check Path: /api/health
```

Then deploy. The website will open at:

```text
https://your-render-app.onrender.com
```

Health check:

```text
https://your-render-app.onrender.com/api/health
```

## Local run

```bash
docker build -t choixx .
docker run -p 10000:10000 choixx
```

Then open http://localhost:10000
