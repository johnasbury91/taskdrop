# Task Server

Simple task distribution server for microtask platforms (Microworkers, Picoworkers, etc.)

## Features

- **One task per worker** - Automatic unique assignment
- **30-min expiry** - Unfinished tasks return to pool
- **Multi-project** - Separate task pools (dharmis, vpns, etc.)
- **Proof collection** - Workers submit proof URLs
- **Simple admin** - Add tasks, view status, export data

## Quick Start

### Local
```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:8080
```

### Deploy to Railway

1. Create new project on [railway.app](https://railway.app)
2. Connect your GitHub repo (or deploy from local)
3. Railway auto-detects Python
4. Add start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Deploy!

### Deploy to Fly.io

```bash
fly launch
fly deploy
```

## Usage

### Admin: Add Tasks
1. Go to `/admin`
2. Enter project name (e.g., `dharmis`)
3. Add tasks, one per line:
   ```
   https://reddit.com/r/headphones/xyz | I've been using XM5s for 6 months, amazing noise canceling
   https://reddit.com/r/headphones/abc | Budget pick would be the HD560S, great soundstage
   ```
4. Click "Add Tasks"

### Workers
1. Go to `/task?project=dharmis` (or click from homepage)
2. See one unique task with:
   - Task code (for proof)
   - Reddit URL
   - Comment to post
3. Post comment on Reddit
4. Submit proof URL

### Platform Job Template

```
Title: Post a Comment on Reddit

Instructions:
1. Go to: https://YOUR-APP.railway.app/task?project=dharmis
2. You'll receive ONE unique task
3. Copy the comment and post it on the Reddit thread
4. Submit your comment URL on the page
5. Take a screenshot showing the completed task page

Proof required:
- Screenshot showing "Task Completed" with your Task Code
- Include Task Code in proof notes

Pay: $0.20 | Time: 10 min
```

## Data Storage

All data stored in `data.json`:
- Projects and tasks
- Assignments (with expiry)
- Submissions with proofs

For production, consider:
- SQLite for better concurrency
- Redis for assignment locking
- S3/GCS for data backup

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Homepage - list projects |
| `/task?project=X` | GET | Get unique task for worker |
| `/task/{id}/submit` | POST | Submit proof |
| `/admin` | GET | Admin dashboard |
| `/admin/add` | POST | Add tasks |
| `/admin/export` | GET | Export all data as JSON |
| `/health` | GET | Health check |
