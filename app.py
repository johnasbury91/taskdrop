"""
Simple Task Server - Serve unique tasks to microtask workers
Supports multiple projects (dharmis, vpns, etc.)
"""

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from datetime import datetime, timedelta
import json
import os
import secrets
import hashlib

app = FastAPI(title="Task Server")
security = HTTPBasic()

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
TASK_EXPIRY_MINUTES = 30
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

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

# --- Data Layer ---

def load_data():
    try:
        # Ensure directory exists
        data_dir = os.path.dirname(DATA_FILE)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        return {"projects": {}, "assignments": {}, "submissions": []}
    except Exception as e:
        print(f"ERROR loading data: {e}")
        return {"projects": {}, "assignments": {}, "submissions": []}

def save_data(data):
    # Ensure directory exists
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

# --- Worker Routes ---

@app.get("/", response_class=HTMLResponse)
async def home():
    """Landing page - list available projects"""
    data = load_data()
    projects = list(data["projects"].keys())

    # Count only available tasks (not completed, not assigned)
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
            .footer { margin-top: 24px; padding-top: 16px; border-top: 1px solid #eee; text-align: center; }
            .footer a { color: #999; font-size: 13px; text-decoration: none; }
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
        # Check if any projects have tasks
        if not any(get_available_count(p) > 0 for p in projects):
            html += '<div class="empty">😴 No tasks right now.<br>Check back soon!</div>'
    else:
        html += '<div class="empty">😴 No tasks right now.<br>Check back soon!</div>'

    html += """
            <div class="footer">
                <a href="/admin">Admin</a>
            </div>
        </div>
    </body>
    </html>
    """
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

    # Check if worker already has ANY active assignment (across all projects)
    for key, assignment in data["assignments"].items():
        if key.endswith(f":{worker_id}") and not assignment.get("completed"):
            # Worker has an active task - redirect to it
            active_project = key.split(":")[0]
            task = next((t for t in data["projects"].get(active_project, []) if t["id"] == assignment["task_id"]), None)
            if task:
                assigned_at = datetime.fromisoformat(assignment["assigned_at"])
                expires_in = TASK_EXPIRY_MINUTES * 60 - (datetime.now() - assigned_at).seconds
                if expires_in > 0:
                    return render_task_page(task, assignment["code"], expires_in, active_project)

    # No active assignment - check if this specific project assignment exists
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
                    .btn { display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 12px; text-decoration: none; font-weight: 600; transition: all 0.2s; }
                    .btn:hover { background: #5a6fd6; transform: translateY(-2px); }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="emoji">🎉</div>
                    <h1>All tasks claimed!</h1>
                    <p>Other workers are on it. New tasks are added regularly, so check back soon.</p>
                    <a href="/" class="btn">← Back to projects</a>
                </div>
            </body>
            </html>
        """)
    
    # Assign first available task
    task = available[0]
    code = secrets.token_hex(3).upper()  # e.g., "A7B3C2"
    
    data["assignments"][assignment_key] = {
        "task_id": task["id"],
        "code": code,
        "assigned_at": datetime.now().isoformat(),
        "completed": False
    }
    save_data(data)
    
    return render_task_page(task, code, TASK_EXPIRY_MINUTES * 60, project)

def render_task_page(task, code, expires_in, project):
    """Render the task page HTML - optimized for low-skilled workers"""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Your Task</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 500px; margin: 0 auto; padding: 16px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }}
            .card {{ background: white; border-radius: 16px; padding: 20px; margin-bottom: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); }}
            .header {{ text-align: center; padding: 24px 20px; }}
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
            .help {{ color: #888; font-size: 13px; margin-top: 6px; }}
            ol {{ margin: 8px 0; padding-left: 20px; color: #4a5568; }}
            ol li {{ margin-bottom: 6px; font-size: 14px; line-height: 1.5; }}
            .note {{ background: #fffbeb; color: #92400e; padding: 12px; border-radius: 10px; font-size: 13px; margin-top: 14px; text-align: center; }}
            .save-note {{ background: #f0fff4; color: #276749; padding: 10px; border-radius: 8px; font-size: 13px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="card header">
            <div class="code">{code}</div>
            <div class="save-note">📌 Save this code - you need it for payment</div>
            <p class="timer">⏱️ Time left: <span id="timer">{expires_in // 60}:{expires_in % 60:02d}</span></p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">1</span>
                <span class="step-title">Open this link</span>
            </div>
            <a href="{task['url']}" target="_blank" class="link">🔗 {task['url'][:50]}{'...' if len(task['url']) > 50 else ''}</a>
            <p class="help">Opens in new tab. Keep this page open!</p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">2</span>
                <span class="step-title">Copy this comment</span>
            </div>
            <div class="comment-box" id="comment">{task['comment']}</div>
            <button class="btn btn-copy" onclick="copyComment()" id="copy-btn">📋 Tap to copy</button>
            <p class="help" id="copy-status"></p>
        </div>

        <div class="card">
            <div class="step-header">
                <span class="step-num">3</span>
                <span class="step-title">Post it</span>
            </div>
            <ol>
                <li>Go to the page you opened</li>
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
                <input type="text" name="proof_url" placeholder="Paste your comment link here..." required>
                <button type="submit" class="btn btn-submit">✅ Submit & get paid</button>
            </form>
            <div class="note">⚠️ We check all submissions - only submit real proof!</div>
        </div>

        <script>
            function copyComment() {{
                const comment = document.getElementById('comment').innerText;
                navigator.clipboard.writeText(comment);
                document.getElementById('copy-status').innerHTML = '✅ Copied! Now paste it on the other page.';
                document.getElementById('copy-status').style.color = '#48bb78';
                document.getElementById('copy-btn').innerHTML = '✅ Copied!';
                document.getElementById('copy-btn').style.background = '#c6f6d5';
                document.getElementById('copy-btn').style.color = '#276749';
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
async def submit_proof(task_id: str, request: Request, project: str = Form(...), code: str = Form(...), proof_url: str = Form(...)):
    """Worker submits proof"""
    
    data = load_data()
    worker_id = get_worker_id(request)
    assignment_key = f"{project}:{worker_id}"
    
    # Verify assignment
    if assignment_key not in data["assignments"]:
        raise HTTPException(400, "No active assignment found")
    
    assignment = data["assignments"][assignment_key]
    if assignment["task_id"] != task_id or assignment["code"] != code:
        raise HTTPException(400, "Invalid task or code")
    
    # Mark completed
    assignment["completed"] = True
    assignment["proof_url"] = proof_url
    assignment["submitted_at"] = datetime.now().isoformat()
    
    # Also mark task as completed
    for task in data["projects"].get(project, []):
        if task["id"] == task_id:
            task["completed"] = True
            task["proof_url"] = proof_url
            task["completed_at"] = datetime.now().isoformat()
            break
    
    # Add to submissions list
    data["submissions"].append({
        "project": project,
        "task_id": task_id,
        "code": code,
        "proof_url": proof_url,
        "submitted_at": datetime.now().isoformat()
    })
    
    save_data(data)
    
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
                .btn {{ display: inline-block; background: #667eea; color: white; padding: 14px 32px; border-radius: 12px; text-decoration: none; font-weight: 600; transition: all 0.2s; }}
                .btn:hover {{ background: #5a6fd6; transform: translateY(-2px); }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="emoji">🎉</div>
                <h1>Nice work!</h1>
                <p class="subtitle">Your proof has been submitted</p>
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
    """Simple admin dashboard"""
    data = load_data()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin - Task Server</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; }
            body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
            .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            h1, h2 { margin-top: 0; }
            input, textarea, select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 6px; margin-bottom: 10px; font-size: 14px; }
            textarea { min-height: 100px; font-family: inherit; }
            button { background: #2563eb; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; }
            button:hover { background: #1d4ed8; }
            table { width: 100%; border-collapse: collapse; font-size: 14px; }
            th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #f9fafb; }
            .status { padding: 4px 8px; border-radius: 4px; font-size: 12px; }
            .status.open { background: #dcfce7; color: #166534; }
            .status.assigned { background: #fef3c7; color: #92400e; }
            .status.completed { background: #dbeafe; color: #1e40af; }
            .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
            .tab { padding: 8px 16px; background: #e5e7eb; border-radius: 6px; cursor: pointer; }
            .tab.active { background: #2563eb; color: white; }
        </style>
    </head>
    <body>
        <h1>🔧 Task Admin</h1>
    """
    
    # Add Task Form
    html += """
        <div class="card">
            <h2>➕ Add Tasks</h2>
            <form action="/admin/add" method="POST">
                <label>Project:</label>
                <input type="text" name="project" placeholder="e.g., dharmis, vpns" required>
                
                <label>Tasks (one per line: URL | Comment):</label>
                <textarea name="tasks" placeholder="https://reddit.com/r/example/post1 | Great comment here
https://reddit.com/r/example/post2 | Another comment" required></textarea>
                
                <button type="submit">Add Tasks</button>
            </form>
        </div>
    """
    
    # Projects Overview
    for project, tasks in data["projects"].items():
        completed = len([t for t in tasks if t.get("completed")])
        total = len(tasks)
        
        html += f"""
        <div class="card">
            <h2>📁 {project} <small style="color:#666">({completed}/{total} done)</small></h2>
            <table>
                <tr><th>ID</th><th>URL</th><th>Status</th><th>Proof</th></tr>
        """
        
        # Get assigned task IDs for this project
        assigned_task_ids = {
            a["task_id"] for key, a in data["assignments"].items()
            if key.startswith(f"{project}:") and not a.get("completed")
        }

        for task in tasks:
            if task.get("completed"):
                status = "completed"
                status_label = "✅ Done"
            elif task["id"] in assigned_task_ids:
                status = "assigned"
                status_label = "🔒 Locked"
            else:
                status = "open"
                status_label = "🟢 Open"
            proof = f'<a href="{task.get("proof_url", "#")}" target="_blank">View</a>' if task.get("proof_url") else "-"
            short_url = task["url"][:40] + "..." if len(task["url"]) > 40 else task["url"]
            
            html += f"""
                <tr>
                    <td><code>{task['id'][:8]}</code></td>
                    <td><a href="{task['url']}" target="_blank">{short_url}</a></td>
                    <td><span class="status {status}">{status_label}</span></td>
                    <td>{proof}</td>
                </tr>
            """
        
        html += """
            </table>
        </div>
        """
    
    # Recent Submissions
    if data["submissions"]:
        html += """
        <div class="card">
            <h2>📥 Recent Submissions</h2>
            <table>
                <tr><th>Project</th><th>Code</th><th>Proof</th><th>Time</th></tr>
        """
        for sub in reversed(data["submissions"][-20:]):
            html += f"""
                <tr>
                    <td>{sub['project']}</td>
                    <td><code>{sub['code']}</code></td>
                    <td><a href="{sub['proof_url']}" target="_blank">View</a></td>
                    <td>{sub['submitted_at'][:16]}</td>
                </tr>
            """
        html += "</table></div>"
    
    html += """
        <div class="card">
            <p><a href="/admin/export">📥 Export all data as JSON</a></p>
        </div>
    </body>
    </html>
    """
    
    return html

@app.post("/admin/add")
async def admin_add_tasks(project: str = Form(...), tasks: str = Form(...), admin: str = Depends(verify_admin)):
    """Add tasks from form"""
    data = load_data()
    
    if project not in data["projects"]:
        data["projects"][project] = []
    
    for line in tasks.strip().split("\n"):
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        url = parts[0].strip()
        comment = parts[1].strip()
        
        task_id = secrets.token_hex(6)
        data["projects"][project].append({
            "id": task_id,
            "url": url,
            "comment": comment,
            "created_at": datetime.now().isoformat(),
            "completed": False
        })
    
    save_data(data)
    return RedirectResponse("/admin", status_code=303)

@app.get("/admin/export")
async def admin_export(admin: str = Depends(verify_admin)):
    """Export all data"""
    data = load_data()
    return JSONResponse(data)

# --- Health Check ---

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/debug")
async def debug():
    """Debug endpoint to check data file status"""
    data_dir = os.path.dirname(DATA_FILE) if os.path.dirname(DATA_FILE) else "."
    return {
        "DATA_FILE": DATA_FILE,
        "data_dir": data_dir,
        "dir_exists": os.path.isdir(data_dir),
        "file_exists": os.path.exists(DATA_FILE),
        "dir_contents": os.listdir(data_dir) if os.path.isdir(data_dir) else "N/A"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
