"""
Simple Task Server - Serve unique tasks to microtask workers
Supports multiple projects (dharmis, vpns, etc.)
"""

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
import json
import os
import secrets
import hashlib

app = FastAPI(title="Task Server")

DATA_FILE = "data.json"
TASK_EXPIRY_MINUTES = 30

# --- Data Layer ---

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"projects": {}, "assignments": {}, "submissions": []}

def save_data(data):
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
    """Render the task page HTML"""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Your Task - {code}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
            .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            h1 {{ margin-top: 0; font-size: 1.5em; }}
            .code {{ background: #fef3c7; padding: 8px 16px; border-radius: 8px; font-family: monospace; font-size: 1.2em; display: inline-block; }}
            .timer {{ color: #dc2626; font-weight: bold; }}
            .url {{ background: #f0f7ff; padding: 12px; border-radius: 8px; word-break: break-all; margin: 12px 0; }}
            .comment {{ background: #f9fafb; padding: 16px; border-radius: 8px; border-left: 4px solid #2563eb; white-space: pre-wrap; }}
            .copy-btn {{ background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-top: 8px; }}
            .copy-btn:hover {{ background: #1d4ed8; }}
            input[type="text"] {{ width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }}
            .submit-btn {{ background: #059669; color: white; border: none; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-size: 16px; width: 100%; margin-top: 12px; }}
            .submit-btn:hover {{ background: #047857; }}
            .instructions {{ background: #fefce8; padding: 12px; border-radius: 8px; font-size: 0.9em; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🎯 Your Task</h1>
            <p>Task Code: <span class="code">{code}</span></p>
            <p>⏱️ Expires in: <span class="timer" id="timer">{expires_in // 60}:{expires_in % 60:02d}</span></p>
        </div>
        
        <div class="card">
            <h3>📍 Post URL</h3>
            <div class="url"><a href="{task['url']}" target="_blank">{task['url']}</a></div>
            
            <h3>💬 Comment to Post</h3>
            <div class="comment" id="comment">{task['comment']}</div>
            <button class="copy-btn" onclick="copyComment()">📋 Copy Comment</button>
        </div>
        
        <div class="card">
            <div class="instructions">
                <strong>Instructions:</strong>
                <ol style="margin: 8px 0; padding-left: 20px;">
                    <li>Click the URL above to open the Reddit thread</li>
                    <li>Copy and post the comment</li>
                    <li>Paste your comment's URL below as proof</li>
                </ol>
            </div>
            
            <form action="/task/{task['id']}/submit" method="POST" style="margin-top: 16px;">
                <input type="hidden" name="project" value="{project}">
                <input type="hidden" name="code" value="{code}">
                <label><strong>Your Comment URL (proof):</strong></label>
                <input type="text" name="proof_url" placeholder="https://reddit.com/r/.../comment/..." required style="margin-top: 8px;">
                <button type="submit" class="submit-btn">✅ Submit Proof</button>
            </form>
        </div>
        
        <script>
            function copyComment() {{
                const comment = document.getElementById('comment').innerText;
                navigator.clipboard.writeText(comment);
                event.target.innerText = '✓ Copied!';
                setTimeout(() => event.target.innerText = '📋 Copy Comment', 2000);
            }}
            
            let seconds = {expires_in};
            setInterval(() => {{
                seconds--;
                if (seconds <= 0) {{
                    document.getElementById('timer').innerText = 'EXPIRED';
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
async def admin_dashboard():
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
        
        for task in tasks:
            status = "completed" if task.get("completed") else "open"
            status_label = "✅ Done" if task.get("completed") else "🟢 Open"
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
async def admin_add_tasks(project: str = Form(...), tasks: str = Form(...)):
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
async def admin_export():
    """Export all data"""
    data = load_data()
    return JSONResponse(data)

# --- Health Check ---

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
