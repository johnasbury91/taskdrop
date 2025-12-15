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
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Task Server</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; }
            body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
            .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            h1 { margin-top: 0; }
            a { color: #2563eb; text-decoration: none; }
            a:hover { text-decoration: underline; }
            .project-link { display: block; padding: 12px; background: #f0f7ff; border-radius: 8px; margin-bottom: 8px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🎯 Task Server</h1>
            <p>Select a project to get your task:</p>
    """
    
    if projects:
        for project in projects:
            task_count = len([t for t in data["projects"].get(project, []) if not t.get("completed")])
            html += f'<a class="project-link" href="/task?project={project}"><strong>{project}</strong> - {task_count} tasks available</a>'
    else:
        html += "<p><em>No projects available yet.</em></p>"
    
    html += """
            <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
            <p><small><a href="/admin">Admin →</a></small></p>
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
    
    # Check if worker already has an active assignment for this project
    assignment_key = f"{project}:{worker_id}"
    
    if assignment_key in data["assignments"]:
        assignment = data["assignments"][assignment_key]
        if not assignment.get("completed"):
            # Return existing task
            task = next((t for t in data["projects"][project] if t["id"] == assignment["task_id"]), None)
            if task:
                assigned_at = datetime.fromisoformat(assignment["assigned_at"])
                expires_in = TASK_EXPIRY_MINUTES * 60 - (datetime.now() - assigned_at).seconds
                return render_task_page(task, assignment["code"], expires_in, project)
    
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
            <html>
            <head>
                <title>No Tasks</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; text-align: center; }
                    .card { background: white; border-radius: 12px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>😅 No Tasks Available</h1>
                    <p>All tasks have been claimed. Check back later!</p>
                    <p><a href="/">← Back</a></p>
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
        <title>Your Task - {code}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 12px; background: #f5f5f5; }}
            .card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            .step {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); border-left: 5px solid #2563eb; }}
            .step-number {{ background: #2563eb; color: white; width: 36px; height: 36px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-weight: bold; font-size: 18px; margin-right: 12px; }}
            .step-title {{ font-size: 20px; font-weight: bold; display: inline; }}
            h1 {{ margin-top: 0; font-size: 1.4em; }}
            .code {{ background: #fef3c7; padding: 10px 18px; border-radius: 8px; font-family: monospace; font-size: 1.4em; display: inline-block; font-weight: bold; }}
            .timer {{ color: #dc2626; font-weight: bold; font-size: 1.1em; }}
            .big-btn {{ display: block; width: 100%; padding: 16px 24px; border-radius: 10px; font-size: 18px; font-weight: bold; text-align: center; cursor: pointer; border: none; margin-top: 12px; text-decoration: none; }}
            .btn-blue {{ background: #2563eb; color: white; }}
            .btn-blue:hover {{ background: #1d4ed8; }}
            .btn-green {{ background: #059669; color: white; }}
            .btn-green:hover {{ background: #047857; }}
            .btn-orange {{ background: #ea580c; color: white; }}
            .btn-orange:hover {{ background: #c2410c; }}
            .comment-box {{ background: #f0f7ff; padding: 16px; border-radius: 8px; font-size: 15px; white-space: pre-wrap; margin: 12px 0; border: 2px dashed #2563eb; }}
            input[type="text"] {{ width: 100%; padding: 14px; border: 2px solid #ddd; border-radius: 8px; font-size: 16px; margin-top: 8px; }}
            input[type="text"]:focus {{ border-color: #2563eb; outline: none; }}
            .warning {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 12px; border-radius: 8px; font-size: 14px; margin-top: 12px; }}
            .help-text {{ color: #666; font-size: 14px; margin-top: 8px; }}
            .save-code {{ background: #fefce8; border: 2px solid #fde047; padding: 12px; border-radius: 8px; margin-top: 12px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>YOUR TASK</h1>
            <p>Your Task Code: <span class="code">{code}</span></p>
            <div class="save-code">
                <strong>SAVE THIS CODE!</strong> You need it for payment.
            </div>
            <p style="margin-top: 12px;">⏱️ Time left: <span class="timer" id="timer">{expires_in // 60}:{expires_in % 60:02d}</span></p>
        </div>

        <div class="step">
            <span class="step-number">1</span>
            <span class="step-title">CLICK THIS BUTTON TO OPEN THE PAGE</span>
            <a href="{task['url']}" target="_blank" class="big-btn btn-blue">OPEN PAGE IN NEW TAB</a>
            <p class="help-text">A new tab will open. Keep this tab open too!</p>
        </div>

        <div class="step">
            <span class="step-number">2</span>
            <span class="step-title">COPY THIS COMMENT</span>
            <div class="comment-box" id="comment">{task['comment']}</div>
            <button class="big-btn btn-orange" onclick="copyComment()">TAP TO COPY COMMENT</button>
            <p class="help-text" id="copy-status">Tap the button, then paste in the other tab</p>
        </div>

        <div class="step">
            <span class="step-number">3</span>
            <span class="step-title">POST THE COMMENT</span>
            <p>Go to the tab you opened and:</p>
            <ol style="font-size: 16px; line-height: 1.8;">
                <li>Scroll down to the comment box</li>
                <li>Click in the comment box</li>
                <li>Paste the comment (Ctrl+V or long-press → Paste)</li>
                <li>Click the Post/Submit button</li>
            </ol>
        </div>

        <div class="step">
            <span class="step-number">4</span>
            <span class="step-title">GET YOUR PROOF LINK</span>
            <p>After posting, get the link to YOUR comment:</p>
            <ol style="font-size: 16px; line-height: 1.8;">
                <li>Find your comment on the page</li>
                <li>Click <strong>"Share"</strong> under your comment</li>
                <li>Click <strong>"Copy Link"</strong></li>
            </ol>
            <p class="help-text">The link should look like: reddit.com/r/something/comments/...</p>
        </div>

        <div class="step">
            <span class="step-number">5</span>
            <span class="step-title">PASTE YOUR PROOF LINK HERE</span>
            <form action="/task/{task['id']}/submit" method="POST">
                <input type="hidden" name="project" value="{project}">
                <input type="hidden" name="code" value="{code}">
                <input type="text" name="proof_url" placeholder="Paste your comment link here..." required>
                <button type="submit" class="big-btn btn-green">SUBMIT AND GET PAID</button>
            </form>
            <div class="warning">
                <strong>DO NOT</strong> submit without posting the comment first. We check all submissions!
            </div>
        </div>

        <script>
            function copyComment() {{
                const comment = document.getElementById('comment').innerText;
                navigator.clipboard.writeText(comment);
                document.getElementById('copy-status').innerHTML = '<strong style="color: green;">✓ COPIED! Now paste it in the other tab</strong>';
                event.target.innerText = '✓ COPIED!';
                event.target.style.background = '#16a34a';
            }}

            let seconds = {expires_in};
            setInterval(() => {{
                seconds--;
                if (seconds <= 0) {{
                    document.getElementById('timer').innerText = 'EXPIRED - Refresh page';
                    return;
                }}
                const m = Math.floor(seconds / 60);
                const s = seconds % 60;
                document.getElementById('timer').innerText = m + ':' + s.toString().padStart(2, '0');
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
    
    return HTMLResponse("""
        <html>
        <head>
            <title>Task Completed!</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
                .card { background: white; border-radius: 12px; padding: 40px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
                .success { color: #059669; font-size: 3em; }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="success">✅</div>
                <h1>Task Completed!</h1>
                <p>Your proof has been submitted successfully.</p>
                <p>Include your <strong>Task Code</strong> in your platform proof.</p>
                <p><a href="/">Get another task</a></p>
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
