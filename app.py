"""
Task Server - Serve tasks to microtask workers
Supports both Reddit comments and posts
"""

from fastapi import FastAPI, Request, Form, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from typing import Optional, List
from pydantic import BaseModel
import json
import os
import secrets
import hashlib

app = FastAPI(title="Task Server")
security = HTTPBasic()

# CORS for API access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
TASK_EXPIRY_MINUTES = 30
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
API_KEY = os.environ.get("API_KEY", "")  # For programmatic access
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # Callback when task completes
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # HMAC secret for webhook

# Rate limiting: max tasks per worker per hour
MAX_TASKS_PER_HOUR = int(os.environ.get("MAX_TASKS_PER_HOUR", "10"))
# Bad actor threshold: rejections before temp ban
MAX_REJECTIONS = int(os.environ.get("MAX_REJECTIONS", "3"))

# --- Models ---

class TaskCreate(BaseModel):
    type: str = "comment"  # "comment" or "post"
    # For comments
    url: Optional[str] = None
    comment: Optional[str] = None
    # For posts
    subreddit: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    # Metadata
    reddit_account: Optional[str] = None
    external_id: Optional[str] = None  # ID from main app

class TaskBatch(BaseModel):
    project: str
    tasks: List[TaskCreate]

# --- Auth ---

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials"""
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if credentials.username != "admin" or not correct_password:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def verify_api_key(x_api_key: str = Header(None)):
    """Verify API key for programmatic access"""
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API key not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True

# --- Data Layer ---

def load_data():
    try:
        data_dir = os.path.dirname(DATA_FILE)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        return {"projects": {}, "assignments": {}, "submissions": [], "accounts": []}
    except Exception as e:
        print(f"ERROR loading data: {e}")
        return {"projects": {}, "assignments": {}, "submissions": [], "accounts": []}

def save_data(data):
    data_dir = os.path.dirname(DATA_FILE)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def get_worker_id(request: Request):
    """Generate consistent worker ID from IP + User Agent"""
    ip = request.client.host
    ua = request.headers.get("user-agent", "")
    raw = f"{ip}:{ua}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def cleanup_expired_assignments(data):
    """Release tasks assigned more than EXPIRY minutes ago without completion"""
    now = datetime.now()
    expired = []
    for worker_id, assignment in list(data["assignments"].items()):
        assigned_at = datetime.fromisoformat(assignment["assigned_at"])
        if now - assigned_at > timedelta(minutes=TASK_EXPIRY_MINUTES):
            if not assignment.get("completed"):
                expired.append(worker_id)
    for worker_id in expired:
        del data["assignments"][worker_id]
    return data

# --- Validation & Security ---

import re
import hmac
import httpx

REDDIT_COMMENT_PATTERN = re.compile(
    r'^https?://(www\.|old\.)?reddit\.com/r/[^/]+/comments/[a-z0-9]+/[^/]*/[a-z0-9]+/?$',
    re.IGNORECASE
)
REDDIT_POST_PATTERN = re.compile(
    r'^https?://(www\.|old\.)?reddit\.com/r/[^/]+/comments/[a-z0-9]+',
    re.IGNORECASE
)

def validate_proof_url(proof_url: str, task_type: str) -> tuple[bool, str]:
    """Validate proof URL format"""
    if not proof_url:
        return False, "Proof URL is required"

    proof_url = proof_url.strip()

    # Must be reddit.com
    if not re.match(r'^https?://(www\.|old\.)?reddit\.com/', proof_url, re.IGNORECASE):
        return False, "Proof must be a Reddit URL"

    if task_type == "comment":
        # Comment URLs have an extra ID at the end
        if not REDDIT_COMMENT_PATTERN.match(proof_url):
            return False, "Invalid comment URL format. Use the 'Copy Link' from your comment."
    else:
        # Post URLs just need to be /r/subreddit/comments/id
        if not REDDIT_POST_PATTERN.match(proof_url):
            return False, "Invalid post URL format"

    return True, ""

def is_duplicate_proof(data: dict, proof_url: str) -> bool:
    """Check if proof URL was already submitted"""
    proof_url = proof_url.strip().lower().rstrip('/')

    for sub in data.get("submissions", []):
        existing = sub.get("proof_url", "").strip().lower().rstrip('/')
        if existing == proof_url:
            return True

    return False

def check_worker_rate_limit(data: dict, worker_id: str) -> tuple[bool, str]:
    """Check if worker has exceeded rate limit"""
    if "worker_stats" not in data:
        data["worker_stats"] = {}

    stats = data["worker_stats"].get(worker_id, {})

    # Check if banned
    if stats.get("banned_until"):
        banned_until = datetime.fromisoformat(stats["banned_until"])
        if datetime.now() < banned_until:
            remaining = (banned_until - datetime.now()).seconds // 60
            return False, f"Too many bad submissions. Try again in {remaining} minutes."
        else:
            # Ban expired, reset
            stats["rejections"] = 0
            stats["banned_until"] = None

    # Check hourly rate
    hour_ago = datetime.now() - timedelta(hours=1)
    recent_completions = [
        s for s in data.get("submissions", [])
        if s.get("worker_id") == worker_id and
        datetime.fromisoformat(s["submitted_at"]) > hour_ago
    ]

    if len(recent_completions) >= MAX_TASKS_PER_HOUR:
        return False, f"Rate limit: max {MAX_TASKS_PER_HOUR} tasks per hour. Try again later."

    return True, ""

def record_worker_rejection(data: dict, worker_id: str):
    """Record a rejection and potentially ban worker"""
    if "worker_stats" not in data:
        data["worker_stats"] = {}

    if worker_id not in data["worker_stats"]:
        data["worker_stats"][worker_id] = {"rejections": 0, "completions": 0}

    stats = data["worker_stats"][worker_id]
    stats["rejections"] = stats.get("rejections", 0) + 1

    if stats["rejections"] >= MAX_REJECTIONS:
        # Temp ban for 1 hour
        stats["banned_until"] = (datetime.now() + timedelta(hours=1)).isoformat()

async def send_webhook(submission: dict):
    """Send webhook notification when task completes"""
    if not WEBHOOK_URL:
        return

    try:
        payload = json.dumps(submission, default=str)

        headers = {"Content-Type": "application/json"}
        if WEBHOOK_SECRET:
            signature = hmac.new(
                WEBHOOK_SECRET.encode(),
                payload.encode(),
                hashlib.sha256
            ).hexdigest()
            headers["X-Webhook-Signature"] = signature

        async with httpx.AsyncClient() as client:
            await client.post(WEBHOOK_URL, content=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"Webhook failed: {e}")

def error_page(message: str):
    """Return a friendly error page"""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Error</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); min-height: 100vh; display: flex; align-items: center; }}
            .container {{ background: white; border-radius: 20px; padding: 40px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); text-align: center; width: 100%; }}
            .emoji {{ font-size: 64px; margin-bottom: 16px; }}
            h1 {{ margin: 0 0 12px 0; font-size: 24px; color: #1a1a2e; }}
            p {{ color: #666; margin-bottom: 24px; line-height: 1.6; }}
            .error-msg {{ background: #fef2f2; color: #dc2626; padding: 16px; border-radius: 12px; margin-bottom: 24px; font-size: 14px; }}
            .btn {{ display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 12px; text-decoration: none; font-weight: 600; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="emoji">❌</div>
            <h1>Submission Failed</h1>
            <div class="error-msg">{message}</div>
            <a href="javascript:history.back()" class="btn">← Go Back</a>
        </div>
    </body>
    </html>
    """)

# --- API Routes (for main app integration) ---

@app.post("/api/tasks")
async def create_tasks(batch: TaskBatch, authorized: bool = Depends(verify_api_key)):
    """Create tasks programmatically from the main app"""
    data = load_data()

    if batch.project not in data["projects"]:
        data["projects"][batch.project] = []

    created = []
    for task in batch.tasks:
        task_id = secrets.token_hex(6)
        task_data = {
            "id": task_id,
            "type": task.type,
            "created_at": datetime.now().isoformat(),
            "completed": False,
            "external_id": task.external_id,
            "reddit_account": task.reddit_account,
        }

        if task.type == "comment":
            task_data["url"] = task.url
            task_data["comment"] = task.comment
        else:  # post
            task_data["subreddit"] = task.subreddit
            task_data["title"] = task.title
            task_data["body"] = task.body

        data["projects"][batch.project].append(task_data)
        created.append({"id": task_id, "external_id": task.external_id})

    save_data(data)
    return {"success": True, "created": len(created), "tasks": created}

@app.get("/api/tasks/{project}")
async def get_tasks(project: str, status: str = None, authorized: bool = Depends(verify_api_key)):
    """Get tasks for a project"""
    data = load_data()
    tasks = data["projects"].get(project, [])

    if status == "completed":
        tasks = [t for t in tasks if t.get("completed")]
    elif status == "pending":
        tasks = [t for t in tasks if not t.get("completed")]

    return {"tasks": tasks}

@app.get("/api/submissions")
async def get_submissions(project: str = None, since: str = None, authorized: bool = Depends(verify_api_key)):
    """Get submissions, optionally filtered by project and time"""
    data = load_data()
    submissions = data.get("submissions", [])

    if project:
        submissions = [s for s in submissions if s.get("project") == project]

    if since:
        since_dt = datetime.fromisoformat(since)
        submissions = [s for s in submissions if datetime.fromisoformat(s["submitted_at"]) > since_dt]

    return {"submissions": submissions}

@app.post("/api/accounts")
async def add_account(username: str, notes: str = "", authorized: bool = Depends(verify_api_key)):
    """Add a Reddit account for workers to use"""
    data = load_data()
    if "accounts" not in data:
        data["accounts"] = []

    # Check if already exists
    if any(a["username"] == username for a in data["accounts"]):
        return {"success": False, "error": "Account already exists"}

    data["accounts"].append({
        "username": username,
        "status": "active",
        "tasks_completed": 0,
        "added_at": datetime.now().isoformat(),
        "notes": notes,
    })
    save_data(data)
    return {"success": True}

@app.get("/api/accounts")
async def get_accounts(authorized: bool = Depends(verify_api_key)):
    """Get all Reddit accounts"""
    data = load_data()
    return {"accounts": data.get("accounts", [])}

# --- Worker Routes ---

@app.get("/", response_class=HTMLResponse)
async def home():
    """Landing page - list available projects"""
    data = load_data()
    projects = list(data["projects"].keys())

    def get_available_count(project):
        assigned_ids = {a["task_id"] for key, a in data["assignments"].items()
                       if key.startswith(f"{project}:") and not a.get("completed")}
        return len([t for t in data["projects"].get(project, [])
                   if not t.get("completed") and t["id"] not in assigned_ids])

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Get Paid for Simple Tasks</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
            .container { background: white; border-radius: 20px; padding: 32px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); }
            h1 { margin: 0 0 8px 0; font-size: 28px; color: #1a1a2e; }
            .subtitle { color: #666; margin-bottom: 24px; font-size: 16px; }
            .project { display: flex; align-items: center; justify-content: space-between; padding: 16px; background: #f8fafc; border-radius: 12px; margin-bottom: 12px; text-decoration: none; color: inherit; transition: all 0.2s; border: 2px solid transparent; }
            .project:hover { background: #f0f7ff; border-color: #667eea; transform: translateY(-2px); }
            .project-name { font-weight: 600; font-size: 17px; color: #1a1a2e; }
            .project-count { background: #667eea; color: white; padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; }
            .empty { text-align: center; padding: 40px 20px; color: #666; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>👋 Ready to earn?</h1>
            <p class="subtitle">Pick a project and start your task</p>
    """

    if projects:
        for project in projects:
            count = get_available_count(project)
            if count > 0:
                html += f'''
                <a class="project" href="/task?project={project}">
                    <span class="project-name">📋 {project}</span>
                    <span class="project-count">{count} available</span>
                </a>'''
        if not any(get_available_count(p) > 0 for p in projects):
            html += '<div class="empty">😴 No tasks right now.<br>Check back soon!</div>'
    else:
        html += '<div class="empty">😴 No tasks right now.<br>Check back soon!</div>'

    html += "</div></body></html>"
    return html

@app.get("/task", response_class=HTMLResponse)
async def get_task(request: Request, project: str = None):
    """Serve one unique task to worker"""
    if not project:
        return RedirectResponse("/")

    data = load_data()
    data = cleanup_expired_assignments(data)

    if project not in data["projects"]:
        return HTMLResponse("<h1>Project not found</h1><p><a href='/'>Back</a></p>")

    worker_id = get_worker_id(request)

    # Check if worker already has an active assignment
    for key, assignment in data["assignments"].items():
        if key.endswith(f":{worker_id}") and not assignment.get("completed"):
            active_project = key.split(":")[0]
            task = next((t for t in data["projects"].get(active_project, []) if t["id"] == assignment["task_id"]), None)
            if task:
                assigned_at = datetime.fromisoformat(assignment["assigned_at"])
                expires_in = TASK_EXPIRY_MINUTES * 60 - (datetime.now() - assigned_at).seconds
                if expires_in > 0:
                    return render_task_page(task, assignment["code"], expires_in, active_project, data.get("accounts", []))

    assignment_key = f"{project}:{worker_id}"

    # Find unassigned task
    assigned_task_ids = {
        a["task_id"] for key, a in data["assignments"].items()
        if key.startswith(f"{project}:") and not a.get("completed")
    }
    completed_task_ids = {t["id"] for t in data["projects"][project] if t.get("completed")}

    available = [
        t for t in data["projects"][project]
        if t["id"] not in assigned_task_ids and t["id"] not in completed_task_ids
    ]

    if not available:
        return HTMLResponse("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>All Done!</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    * { box-sizing: border-box; }
                    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; }
                    .container { background: white; border-radius: 20px; padding: 40px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); text-align: center; width: 100%; }
                    .emoji { font-size: 64px; margin-bottom: 16px; }
                    h1 { margin: 0 0 12px 0; font-size: 24px; color: #1a1a2e; }
                    p { color: #666; margin-bottom: 24px; line-height: 1.6; }
                    .btn { display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 12px; text-decoration: none; font-weight: 600; }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="emoji">🎉</div>
                    <h1>All tasks claimed!</h1>
                    <p>Other workers are on it. Check back soon.</p>
                    <a href="/" class="btn">← Back to projects</a>
                </div>
            </body>
            </html>
        """)

    # Assign first available task
    task = available[0]
    code = secrets.token_hex(3).upper()

    data["assignments"][assignment_key] = {
        "task_id": task["id"],
        "code": code,
        "assigned_at": datetime.now().isoformat(),
        "completed": False
    }
    save_data(data)

    return render_task_page(task, code, TASK_EXPIRY_MINUTES * 60, project, data.get("accounts", []))

def render_task_page(task, code, expires_in, project, accounts):
    """Render task page - different UI for comments vs posts"""
    task_type = task.get("type", "comment")

    # Account selection HTML
    account_html = ""
    if accounts:
        active_accounts = [a for a in accounts if a.get("status") == "active"]
        if active_accounts:
            account_options = "".join([f'<option value="{a["username"]}">{a["username"]}</option>' for a in active_accounts])
            account_html = f'''
            <div class="card">
                <div class="step-header">
                    <span class="step-num">0</span>
                    <span class="step-title">Select Reddit account</span>
                </div>
                <select name="reddit_account" id="reddit_account" class="account-select">
                    <option value="">-- Select account --</option>
                    {account_options}
                </select>
                <p class="help">Use this account to post</p>
            </div>
            '''

    if task_type == "post":
        return render_post_task(task, code, expires_in, project, account_html)
    else:
        return render_comment_task(task, code, expires_in, project, account_html)

def render_comment_task(task, code, expires_in, project, account_html):
    """Render comment task UI"""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Your Task - Comment</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 16px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }}
            .card {{ background: white; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); }}
            .header {{ text-align: center; padding: 24px 20px; }}
            .type-badge {{ background: #3b82f6; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; display: inline-block; margin-bottom: 12px; }}
            .code {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 14px 24px; border-radius: 12px; font-family: monospace; font-size: 1.6em; display: inline-block; font-weight: bold; letter-spacing: 3px; }}
            .timer {{ color: #666; font-size: 14px; margin-top: 12px; }}
            .timer span {{ color: #e53e3e; font-weight: 600; }}
            .step-num {{ background: #667eea; color: white; width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-weight: 600; font-size: 14px; margin-right: 10px; flex-shrink: 0; }}
            .step-title {{ font-size: 15px; font-weight: 600; color: #1a1a2e; display: inline; }}
            .step-header {{ margin-bottom: 12px; }}
            .link {{ color: #667eea; word-break: break-all; font-size: 14px; }}
            .comment-box {{ background: #f7f8fc; padding: 14px; border-radius: 10px; font-size: 14px; white-space: pre-wrap; margin: 10px 0; border: 1px dashed #d0d5e3; line-height: 1.5; }}
            .btn {{ display: block; width: 100%; padding: 14px 20px; border-radius: 10px; font-size: 15px; font-weight: 600; text-align: center; cursor: pointer; border: none; margin-top: 10px; text-decoration: none; transition: all 0.2s; }}
            .btn-copy {{ background: #f0f2f8; color: #4a5568; }}
            .btn-copy:hover {{ background: #e2e6f0; }}
            .btn-submit {{ background: #48bb78; color: white; }}
            .btn-submit:hover {{ background: #38a169; }}
            input[type="text"] {{ width: 100%; padding: 14px 16px; border: 2px solid #e2e6f0; border-radius: 10px; font-size: 16px; margin-top: 8px; }}
            input[type="text"]:focus {{ border-color: #667eea; outline: none; }}
            .account-select {{ width: 100%; padding: 12px; border: 2px solid #e2e6f0; border-radius: 10px; font-size: 16px; background: white; }}
            .help {{ color: #888; font-size: 13px; margin-top: 6px; }}
            ol {{ margin: 8px 0; padding-left: 20px; color: #4a5568; }}
            ol li {{ margin-bottom: 6px; font-size: 14px; line-height: 1.5; }}
            .note {{ background: #fffbeb; color: #92400e; padding: 12px; border-radius: 10px; font-size: 13px; margin-top: 14px; text-align: center; }}
            .save-note {{ background: #f0fff4; color: #276749; padding: 10px; border-radius: 8px; font-size: 13px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="card header">
            <div class="type-badge">💬 COMMENT</div>
            <div class="code">{code}</div>
            <div class="save-note">📌 Save this code - you need it for payment</div>
            <p class="timer">⏱️ Time left: <span id="timer">{expires_in // 60}:{expires_in % 60:02d}</span></p>
        </div>

        {account_html}

        <div class="card">
            <div class="step-header">
                <span class="step-num">1</span>
                <span class="step-title">Open this thread</span>
            </div>
            <a href="{task['url']}" target="_blank" class="link">🔗 {task['url'][:50]}{'...' if len(task['url']) > 50 else ''}</a>
            <p class="help">Opens in new tab. Keep this page open!</p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">2</span>
                <span class="step-title">Copy this comment</span>
            </div>
            <div class="comment-box" id="content">{task['comment']}</div>
            <button class="btn btn-copy" onclick="copyContent()" id="copy-btn">📋 Tap to copy</button>
            <p class="help" id="copy-status"></p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">3</span>
                <span class="step-title">Post the comment</span>
            </div>
            <ol>
                <li>Go to the thread you opened</li>
                <li>Find the comment box</li>
                <li>Paste and submit</li>
            </ol>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">4</span>
                <span class="step-title">Get your proof link</span>
            </div>
            <ol>
                <li>Click <strong>Share</strong> on your comment</li>
                <li>Click <strong>Copy Link</strong></li>
            </ol>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">5</span>
                <span class="step-title">Paste proof & submit</span>
            </div>
            <form action="/task/{task['id']}/submit" method="POST">
                <input type="hidden" name="project" value="{project}">
                <input type="hidden" name="code" value="{code}">
                <input type="hidden" name="task_type" value="comment">
                <input type="hidden" name="reddit_account" id="reddit_account_hidden" value="">
                <input type="text" name="proof_url" placeholder="Paste your comment link here..." required>
                <button type="submit" class="btn btn-submit">✅ Submit & get paid</button>
            </form>
            <div class="note">⚠️ We verify all submissions - only submit real proof!</div>
        </div>

        <script>
            function copyContent() {{
                const content = document.getElementById('content').innerText;
                navigator.clipboard.writeText(content);
                document.getElementById('copy-status').innerHTML = '✅ Copied!';
                document.getElementById('copy-status').style.color = '#48bb78';
                document.getElementById('copy-btn').innerHTML = '✅ Copied!';
                document.getElementById('copy-btn').style.background = '#c6f6d5';
                document.getElementById('copy-btn').style.color = '#276749';
            }}

            // Sync account selection
            const accountSelect = document.getElementById('reddit_account');
            if (accountSelect) {{
                accountSelect.addEventListener('change', function() {{
                    document.getElementById('reddit_account_hidden').value = this.value;
                }});
            }}

            let seconds = {expires_in};
            setInterval(() => {{
                seconds--;
                if (seconds <= 0) {{
                    document.getElementById('timer').innerHTML = '<span style="color:#e53e3e">Expired - refresh page</span>';
                    return;
                }}
                const m = Math.floor(seconds / 60);
                const s = seconds % 60;
                document.getElementById('timer').innerHTML = '<span>' + m + ':' + s.toString().padStart(2, '0') + '</span>';
            }}, 1000);
        </script>
    </body>
    </html>
    """)

def render_post_task(task, code, expires_in, project, account_html):
    """Render post task UI"""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Your Task - Post</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 16px; background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); min-height: 100vh; }}
            .card {{ background: white; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); }}
            .header {{ text-align: center; padding: 24px 20px; }}
            .type-badge {{ background: #f59e0b; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; display: inline-block; margin-bottom: 12px; }}
            .code {{ background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); color: white; padding: 14px 24px; border-radius: 12px; font-family: monospace; font-size: 1.6em; display: inline-block; font-weight: bold; letter-spacing: 3px; }}
            .timer {{ color: #666; font-size: 14px; margin-top: 12px; }}
            .timer span {{ color: #e53e3e; font-weight: 600; }}
            .step-num {{ background: #f59e0b; color: white; width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-weight: 600; font-size: 14px; margin-right: 10px; flex-shrink: 0; }}
            .step-title {{ font-size: 15px; font-weight: 600; color: #1a1a2e; display: inline; }}
            .step-header {{ margin-bottom: 12px; }}
            .subreddit {{ background: #fef3c7; color: #92400e; padding: 8px 16px; border-radius: 8px; font-weight: 600; display: inline-block; margin-bottom: 12px; }}
            .content-box {{ background: #f7f8fc; padding: 14px; border-radius: 10px; font-size: 14px; margin: 10px 0; border: 1px dashed #d0d5e3; }}
            .content-label {{ font-size: 12px; color: #666; margin-bottom: 4px; font-weight: 600; }}
            .content-text {{ white-space: pre-wrap; line-height: 1.5; }}
            .btn {{ display: block; width: 100%; padding: 14px 20px; border-radius: 10px; font-size: 15px; font-weight: 600; text-align: center; cursor: pointer; border: none; margin-top: 10px; text-decoration: none; transition: all 0.2s; }}
            .btn-copy {{ background: #f0f2f8; color: #4a5568; }}
            .btn-copy:hover {{ background: #e2e6f0; }}
            .btn-submit {{ background: #48bb78; color: white; }}
            .btn-submit:hover {{ background: #38a169; }}
            input[type="text"] {{ width: 100%; padding: 14px 16px; border: 2px solid #e2e6f0; border-radius: 10px; font-size: 16px; margin-top: 8px; }}
            input[type="text"]:focus {{ border-color: #f59e0b; outline: none; }}
            .account-select {{ width: 100%; padding: 12px; border: 2px solid #e2e6f0; border-radius: 10px; font-size: 16px; background: white; }}
            .help {{ color: #888; font-size: 13px; margin-top: 6px; }}
            ol {{ margin: 8px 0; padding-left: 20px; color: #4a5568; }}
            ol li {{ margin-bottom: 6px; font-size: 14px; line-height: 1.5; }}
            .note {{ background: #fffbeb; color: #92400e; padding: 12px; border-radius: 10px; font-size: 13px; margin-top: 14px; text-align: center; }}
            .save-note {{ background: #f0fff4; color: #276749; padding: 10px; border-radius: 8px; font-size: 13px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="card header">
            <div class="type-badge">📝 NEW POST</div>
            <div class="code">{code}</div>
            <div class="save-note">📌 Save this code - you need it for payment</div>
            <p class="timer">⏱️ Time left: <span id="timer">{expires_in // 60}:{expires_in % 60:02d}</span></p>
        </div>

        {account_html}

        <div class="card">
            <div class="step-header">
                <span class="step-num">1</span>
                <span class="step-title">Go to this subreddit</span>
            </div>
            <div class="subreddit">r/{task['subreddit']}</div>
            <a href="https://reddit.com/r/{task['subreddit']}/submit" target="_blank" class="btn btn-copy">🔗 Open r/{task['subreddit']} submit page</a>
            <p class="help">Click to open the submit page in a new tab</p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">2</span>
                <span class="step-title">Copy the title</span>
            </div>
            <div class="content-box">
                <div class="content-label">POST TITLE:</div>
                <div class="content-text" id="title">{task['title']}</div>
            </div>
            <button class="btn btn-copy" onclick="copyTitle()" id="copy-title-btn">📋 Copy title</button>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">3</span>
                <span class="step-title">Copy the body</span>
            </div>
            <div class="content-box">
                <div class="content-label">POST BODY:</div>
                <div class="content-text" id="body">{task['body']}</div>
            </div>
            <button class="btn btn-copy" onclick="copyBody()" id="copy-body-btn">📋 Copy body</button>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">4</span>
                <span class="step-title">Create the post</span>
            </div>
            <ol>
                <li>Paste the title in the title field</li>
                <li>Paste the body in the body field</li>
                <li>Click "Post" to submit</li>
            </ol>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">5</span>
                <span class="step-title">Get your proof link</span>
            </div>
            <ol>
                <li>After posting, copy the URL of your new post</li>
                <li>It should look like: reddit.com/r/{task['subreddit']}/comments/...</li>
            </ol>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">6</span>
                <span class="step-title">Paste proof & submit</span>
            </div>
            <form action="/task/{task['id']}/submit" method="POST">
                <input type="hidden" name="project" value="{project}">
                <input type="hidden" name="code" value="{code}">
                <input type="hidden" name="task_type" value="post">
                <input type="hidden" name="reddit_account" id="reddit_account_hidden" value="">
                <input type="text" name="proof_url" placeholder="Paste your post URL here..." required>
                <button type="submit" class="btn btn-submit">✅ Submit & get paid</button>
            </form>
            <div class="note">⚠️ We verify all submissions - only submit real proof!</div>
        </div>

        <script>
            function copyTitle() {{
                navigator.clipboard.writeText(document.getElementById('title').innerText);
                document.getElementById('copy-title-btn').innerHTML = '✅ Title copied!';
                document.getElementById('copy-title-btn').style.background = '#c6f6d5';
                document.getElementById('copy-title-btn').style.color = '#276749';
            }}

            function copyBody() {{
                navigator.clipboard.writeText(document.getElementById('body').innerText);
                document.getElementById('copy-body-btn').innerHTML = '✅ Body copied!';
                document.getElementById('copy-body-btn').style.background = '#c6f6d5';
                document.getElementById('copy-body-btn').style.color = '#276749';
            }}

            // Sync account selection
            const accountSelect = document.getElementById('reddit_account');
            if (accountSelect) {{
                accountSelect.addEventListener('change', function() {{
                    document.getElementById('reddit_account_hidden').value = this.value;
                }});
            }}

            let seconds = {expires_in};
            setInterval(() => {{
                seconds--;
                if (seconds <= 0) {{
                    document.getElementById('timer').innerHTML = '<span style="color:#e53e3e">Expired - refresh page</span>';
                    return;
                }}
                const m = Math.floor(seconds / 60);
                const s = seconds % 60;
                document.getElementById('timer').innerHTML = '<span>' + m + ':' + s.toString().padStart(2, '0') + '</span>';
            }}, 1000);
        </script>
    </body>
    </html>
    """)

@app.post("/task/{task_id}/submit")
async def submit_proof(
    task_id: str,
    request: Request,
    project: str = Form(...),
    code: str = Form(...),
    proof_url: str = Form(...),
    task_type: str = Form("comment"),
    reddit_account: str = Form("")
):
    """Worker submits proof with validation"""
    data = load_data()
    worker_id = get_worker_id(request)
    assignment_key = f"{project}:{worker_id}"

    # Check rate limit
    rate_ok, rate_error = check_worker_rate_limit(data, worker_id)
    if not rate_ok:
        return error_page(rate_error)

    if assignment_key not in data["assignments"]:
        return error_page("No active assignment found. Your session may have expired.")

    assignment = data["assignments"][assignment_key]
    if assignment["task_id"] != task_id or assignment["code"] != code:
        return error_page("Invalid task or code. Please refresh and try again.")

    # Validate proof URL format
    valid, error_msg = validate_proof_url(proof_url, task_type)
    if not valid:
        record_worker_rejection(data, worker_id)
        save_data(data)
        return error_page(error_msg)

    # Check for duplicate proof
    if is_duplicate_proof(data, proof_url):
        record_worker_rejection(data, worker_id)
        save_data(data)
        return error_page("This proof URL was already submitted. Submit your own unique proof.")

    # Mark completed
    assignment["completed"] = True
    assignment["proof_url"] = proof_url
    assignment["submitted_at"] = datetime.now().isoformat()
    assignment["reddit_account"] = reddit_account

    # Find and update task
    task_data = None
    for task in data["projects"].get(project, []):
        if task["id"] == task_id:
            task["completed"] = True
            task["proof_url"] = proof_url
            task["completed_at"] = datetime.now().isoformat()
            task["reddit_account"] = reddit_account
            task_data = task
            break

    # Build submission record
    submission = {
        "project": project,
        "task_id": task_id,
        "external_id": task_data.get("external_id") if task_data else None,
        "type": task_type,
        "code": code,
        "proof_url": proof_url,
        "reddit_account": reddit_account,
        "worker_id": worker_id,
        "submitted_at": datetime.now().isoformat()
    }

    data["submissions"].append(submission)

    # Update account stats
    if reddit_account:
        for account in data.get("accounts", []):
            if account["username"] == reddit_account:
                account["tasks_completed"] = account.get("tasks_completed", 0) + 1
                account["last_used"] = datetime.now().isoformat()
                break

    # Update worker stats
    if "worker_stats" not in data:
        data["worker_stats"] = {}
    if worker_id not in data["worker_stats"]:
        data["worker_stats"][worker_id] = {"rejections": 0, "completions": 0}
    data["worker_stats"][worker_id]["completions"] = data["worker_stats"][worker_id].get("completions", 0) + 1

    save_data(data)

    # Send webhook async (don't wait)
    import asyncio
    asyncio.create_task(send_webhook(submission))

    return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Nice work!</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                * {{ box-sizing: border-box; }}
                body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; }}
                .container {{ background: white; border-radius: 20px; padding: 40px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); text-align: center; width: 100%; }}
                .emoji {{ font-size: 72px; margin-bottom: 16px; }}
                h1 {{ margin: 0 0 8px 0; font-size: 26px; color: #1a1a2e; }}
                .subtitle {{ color: #666; margin-bottom: 24px; font-size: 16px; }}
                .code-box {{ background: #f0fff4; border: 2px solid #9ae6b4; padding: 16px; border-radius: 12px; margin-bottom: 24px; }}
                .code-label {{ font-size: 13px; color: #276749; margin-bottom: 6px; }}
                .code {{ font-family: monospace; font-size: 24px; font-weight: bold; color: #276749; letter-spacing: 2px; }}
                .btn {{ display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 12px; text-decoration: none; font-weight: 600; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="emoji">🎉</div>
                <h1>Nice work!</h1>
                <p class="subtitle">Your {task_type} has been submitted</p>
                <div class="code-box">
                    <div class="code-label">Your task code (for payment)</div>
                    <div class="code">{code}</div>
                </div>
                <a href="/" class="btn">Get another task →</a>
            </div>
        </body>
        </html>
    """)

# --- Admin Routes ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(admin: str = Depends(verify_admin)):
    """Admin dashboard"""
    data = load_data()

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin - Task Server</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; }
            body { font-family: -apple-system, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
            .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            h1, h2 { margin-top: 0; }
            input, textarea, select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 10px; font-size: 14px; }
            textarea { min-height: 100px; font-family: inherit; }
            button { background: #2563eb; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; }
            button:hover { background: #1d4ed8; }
            .btn-sm { padding: 4px 10px; font-size: 12px; margin-right: 4px; }
            .btn-delete { background: #ef4444; }
            .btn-delete:hover { background: #dc2626; }
            table { width: 100%; border-collapse: collapse; font-size: 14px; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #f9fafb; }
            .status { padding: 4px 8px; border-radius: 4px; font-size: 12px; }
            .status.comment { background: #dbeafe; color: #1e40af; }
            .status.post { background: #fef3c7; color: #92400e; }
            .status.completed { background: #dcfce7; color: #166534; }
            .status.pending { background: #f3f4f6; color: #374151; }
            .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
            .tab { padding: 8px 16px; background: #e5e7eb; border-radius: 6px; cursor: pointer; text-decoration: none; color: inherit; }
            .tab.active { background: #2563eb; color: white; }
        </style>
    </head>
    <body>
        <h1>🔧 Task Admin</h1>
    """

    # Add Task Form - now supports both types
    html += """
        <div class="card">
            <h2>➕ Add Tasks</h2>

            <div style="display:flex; gap:8px; margin-bottom:16px;">
                <button type="button" class="tab active" id="tab-comment" onclick="showTab('comment')">💬 Add Comments</button>
                <button type="button" class="tab" id="tab-post" onclick="showTab('post')">📝 Add Post</button>
            </div>

            <!-- Comment Form (bulk) -->
            <form action="/admin/add" method="POST" id="form-comment">
                <input type="hidden" name="task_type" value="comment">
                <label>Project:</label>
                <input type="text" name="project" placeholder="e.g., myproject" required>

                <label>Comment Tasks (one per line: URL | Comment):</label>
                <textarea name="comment_tasks" style="min-height:150px" placeholder="https://reddit.com/r/example/comments/abc123 | Your comment here
https://reddit.com/r/example/comments/def456 | Another comment"></textarea>

                <button type="submit">Add Comments</button>
            </form>

            <!-- Post Form (single, with proper fields) -->
            <form action="/admin/add-post" method="POST" id="form-post" style="display:none">
                <label>Project:</label>
                <input type="text" name="project" placeholder="e.g., myproject" required>

                <label>Subreddit (without r/):</label>
                <input type="text" name="subreddit" placeholder="e.g., AskReddit" required>

                <label>Post Title:</label>
                <input type="text" name="title" placeholder="Your post title" required style="font-size:16px; font-weight:500;">

                <label>Post Body:</label>
                <textarea name="body" style="min-height:250px; font-family:inherit; line-height:1.6;" placeholder="Write your post content here...

You can use multiple paragraphs.

- Bullet points work too
- Like this

**Bold** and *italic* work on Reddit."></textarea>

                <div style="display:flex; gap:10px; align-items:center; margin-top:10px;">
                    <button type="submit">Add Post</button>
                    <label style="display:flex; align-items:center; gap:6px; font-size:14px; color:#666;">
                        <input type="checkbox" name="add_another" value="1"> Add another after this
                    </label>
                </div>
            </form>

            <script>
                function showTab(type) {
                    document.getElementById('form-comment').style.display = type === 'comment' ? 'block' : 'none';
                    document.getElementById('form-post').style.display = type === 'post' ? 'block' : 'none';
                    document.getElementById('tab-comment').className = type === 'comment' ? 'tab active' : 'tab';
                    document.getElementById('tab-post').className = type === 'post' ? 'tab active' : 'tab';
                }
            </script>
        </div>
    """

    # Projects Overview
    for project, tasks in data["projects"].items():
        completed = len([t for t in tasks if t.get("completed")])
        total = len(tasks)
        comments = len([t for t in tasks if t.get("type", "comment") == "comment"])
        posts = len([t for t in tasks if t.get("type") == "post"])

        html += f"""
        <div class="card">
            <h2>📁 {project} <small style="color:#666">({completed}/{total} done | {comments} comments, {posts} posts)</small></h2>
            <table>
                <tr><th>Type</th><th>Target</th><th>Content</th><th>Status</th><th>Proof</th></tr>
        """

        for task in tasks[-20:]:  # Last 20 tasks
            task_type = task.get("type", "comment")
            if task_type == "comment":
                target = task.get("url", "")[:30] + "..."
                content = task.get("comment", "")[:40] + "..."
            else:
                target = f"r/{task.get('subreddit', '')}"
                content = task.get("title", "")[:40] + "..."

            status_class = "completed" if task.get("completed") else "pending"
            status_text = "✅ Done" if task.get("completed") else "⏳ Pending"
            proof = f'<a href="{task.get("proof_url")}" target="_blank">View</a>' if task.get("proof_url") else "—"

            html += f"""
                <tr>
                    <td><span class="status {task_type}">{task_type}</span></td>
                    <td style="font-size:12px">{target}</td>
                    <td style="font-size:12px">{content}</td>
                    <td><span class="status {status_class}">{status_text}</span></td>
                    <td>{proof}</td>
                </tr>
            """

        html += "</table></div>"

    # Recent Submissions
    if data.get("submissions"):
        html += """
        <div class="card">
            <h2>📥 Recent Submissions</h2>
            <table>
                <tr><th>Project</th><th>Type</th><th>Code</th><th>Account</th><th>Proof</th><th>Time</th></tr>
        """
        for sub in reversed(data["submissions"][-20:]):
            html += f"""
                <tr>
                    <td>{sub.get('project', '')}</td>
                    <td><span class="status {sub.get('type', 'comment')}">{sub.get('type', 'comment')}</span></td>
                    <td><code>{sub.get('code', '')}</code></td>
                    <td>{sub.get('reddit_account', '—')}</td>
                    <td><a href="{sub.get('proof_url', '')}" target="_blank">View</a></td>
                    <td>{sub.get('submitted_at', '')[:16]}</td>
                </tr>
            """
        html += "</table></div>"

    # Accounts
    html += """
        <div class="card">
            <h2>👤 Reddit Accounts</h2>
            <form action="/admin/account" method="POST" style="display:flex; gap:10px; margin-bottom:16px;">
                <input type="text" name="username" placeholder="Reddit username" style="flex:1" required>
                <button type="submit">Add Account</button>
            </form>
    """

    if data.get("accounts"):
        html += "<table><tr><th>Username</th><th>Status</th><th>Tasks Done</th><th>Last Used</th></tr>"
        for acc in data["accounts"]:
            html += f"""
                <tr>
                    <td>{acc.get('username', '')}</td>
                    <td>{acc.get('status', 'active')}</td>
                    <td>{acc.get('tasks_completed', 0)}</td>
                    <td>{acc.get('last_used', '—')[:16] if acc.get('last_used') else '—'}</td>
                </tr>
            """
        html += "</table>"

    html += """
        </div>
        <div class="card">
            <p><a href="/admin/export">📥 Export all data as JSON</a></p>
        </div>
    </body>
    </html>
    """

    return html

@app.post("/admin/add")
async def admin_add_tasks(
    project: str = Form(...),
    task_type: str = Form("comment"),
    comment_tasks: str = Form(""),
    post_tasks: str = Form(""),
    admin: str = Depends(verify_admin)
):
    """Add tasks from admin form"""
    data = load_data()

    if project not in data["projects"]:
        data["projects"][project] = []

    if task_type == "comment" and comment_tasks:
        for line in comment_tasks.strip().split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 1)
            url = parts[0].strip()
            comment = parts[1].strip()

            data["projects"][project].append({
                "id": secrets.token_hex(6),
                "type": "comment",
                "url": url,
                "comment": comment,
                "created_at": datetime.now().isoformat(),
                "completed": False
            })

    elif task_type == "post" and post_tasks:
        for line in post_tasks.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 3:
                continue
            subreddit = parts[0].strip()
            title = parts[1].strip()
            body = parts[2].strip()

            data["projects"][project].append({
                "id": secrets.token_hex(6),
                "type": "post",
                "subreddit": subreddit,
                "title": title,
                "body": body,
                "created_at": datetime.now().isoformat(),
                "completed": False
            })

    save_data(data)
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/add-post")
async def admin_add_single_post(
    project: str = Form(...),
    subreddit: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
    add_another: str = Form(""),
    admin: str = Depends(verify_admin)
):
    """Add a single post with proper formatting"""
    data = load_data()

    if project not in data["projects"]:
        data["projects"][project] = []

    data["projects"][project].append({
        "id": secrets.token_hex(6),
        "type": "post",
        "subreddit": subreddit.strip().lstrip('r/'),
        "title": title.strip(),
        "body": body,  # Keep formatting intact
        "created_at": datetime.now().isoformat(),
        "completed": False
    })

    save_data(data)

    # If "add another" checked, redirect back to admin with post tab open
    if add_another == "1":
        return HTMLResponse(f"""
            <html>
            <head><meta http-equiv="refresh" content="0;url=/admin#add-post"></head>
            <body>
                <script>
                    localStorage.setItem('lastProject', '{project}');
                    window.location.href = '/admin';
                    setTimeout(() => {{ if(typeof showTab === 'function') showTab('post'); }}, 100);
                </script>
                <p>Post added! Redirecting...</p>
            </body>
            </html>
        """)

    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/account")
async def admin_add_account(username: str = Form(...), admin: str = Depends(verify_admin)):
    """Add Reddit account from admin"""
    data = load_data()
    if "accounts" not in data:
        data["accounts"] = []

    if not any(a["username"] == username for a in data["accounts"]):
        data["accounts"].append({
            "username": username,
            "status": "active",
            "tasks_completed": 0,
            "added_at": datetime.now().isoformat(),
        })
        save_data(data)

    return RedirectResponse("/admin", status_code=303)

@app.get("/admin/export")
async def admin_export(admin: str = Depends(verify_admin)):
    """Export all data"""
    return JSONResponse(load_data())

# --- Health ---

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
