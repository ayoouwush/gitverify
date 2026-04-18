import os
import sqlite3
import requests
import random
from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash, g
from dotenv import load_dotenv
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.units import cm
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")

DATABASE = "gitverify.db"

# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user'
            );
            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                full_name TEXT,
                dob TEXT,
                email TEXT,
                phone TEXT,
                bio TEXT,
                skills TEXT,
                experience TEXT,
                education TEXT,
                projects TEXT,
                github_link TEXT,
                linkedin TEXT,
                github_score INTEGER DEFAULT 0,
                github_status TEXT DEFAULT 'Unverified'
            );
        """)
        db.commit()

# ─── Ads ──────────────────────────────────────────────────────────────────────

ADS = [
    {"title": "Coursera", "text": "Learn from world-class universities online. Get certified today.", "btn": "Explore Courses", "url": "https://www.coursera.org"},
    {"title": "Internshala", "text": "Find internships & fresher jobs. 40,000+ opportunities waiting.", "btn": "Find Internships", "url": "https://www.internshala.com"},
    {"title": "Udemy", "text": "Top courses from ₹449. Learn Python, ML, Web Dev & more.", "btn": "Browse Udemy", "url": "https://www.udemy.com"},
    {"title": "LinkedIn Learning", "text": "Upskill with 16,000+ expert-led courses. Free for 1 month.", "btn": "Start Free Trial", "url": "https://www.linkedin.com/learning"},
]

def get_ads(n=2):
    return random.sample(ADS, min(n, len(ADS)))

# ─── GitHub Verification ──────────────────────────────────────────────────────

def verify_github_repo(repo_url):
    """Parse a github.com URL and verify the repo."""
    try:
        # Accept full URL or owner/repo
        repo_url = repo_url.strip().rstrip("/")
        if "github.com" in repo_url:
            parts = repo_url.split("github.com/")[-1].split("/")
        else:
            parts = repo_url.split("/")

        if len(parts) < 2:
            return {"error": "Invalid GitHub repo URL. Use: https://github.com/owner/repo"}

        owner, repo = parts[0], parts[1]
        token = os.getenv("GITHUB_TOKEN", "")
        headers = {"Authorization": f"token {token}"} if token else {}

        base = f"https://api.github.com/repos/{owner}/{repo}"

        repo_data = requests.get(base, headers=headers, timeout=10).json()
        if "message" in repo_data and repo_data["message"] == "Not Found":
            return {"error": "Repository not found. Check the URL."}

        # Gather metrics
        stars = repo_data.get("stargazers_count", 0)
        forks = repo_data.get("forks_count", 0)
        open_issues = repo_data.get("open_issues_count", 0)

        # Commits (last page trick)
        commits_resp = requests.get(f"{base}/commits?per_page=1", headers=headers, timeout=10)
        commits = 1
        if "Link" in commits_resp.headers:
            import re
            match = re.search(r'page=(\d+)>; rel="last"', commits_resp.headers["Link"])
            if match:
                commits = int(match.group(1))

        # Contributors
        contrib_resp = requests.get(f"{base}/contributors?per_page=50&anon=true", headers=headers, timeout=10)
        contributors = len(contrib_resp.json()) if contrib_resp.status_code == 200 and isinstance(contrib_resp.json(), list) else 1

        # Score calculation (0–100)
        score = 0
        flags = []

        # Stars (up to 25)
        if stars >= 50:
            score += 25
        elif stars >= 10:
            score += 15
        elif stars >= 1:
            score += 8

        # Commits (up to 35)
        if commits >= 100:
            score += 35
        elif commits >= 30:
            score += 25
        elif commits >= 10:
            score += 15
        elif commits >= 3:
            score += 8
        else:
            flags.append("Very few commits")

        # Contributors (up to 20)
        if contributors >= 5:
            score += 20
        elif contributors >= 2:
            score += 12
        elif contributors == 1:
            score += 5
            flags.append("Single contributor")

        # Forks (up to 10)
        if forks >= 10:
            score += 10
        elif forks >= 2:
            score += 5

        # Issues (up to 10)
        if open_issues >= 5:
            score += 10
        elif open_issues >= 1:
            score += 5

        score = min(score, 100)

        # Status
        if score >= 65:
            status = "Verified"
        elif score >= 35:
            status = "Low Activity"
        else:
            status = "Suspicious"
            flags.append("Insufficient activity")

        return {
            "score": score,
            "status": status,
            "flags": flags,
            "stars": stars,
            "commits": commits,
            "contributors": contributors,
            "forks": forks,
            "repo": f"{owner}/{repo}",
        }

    except requests.exceptions.Timeout:
        return {"error": "GitHub API timed out. Try again."}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", ads=get_ads(), user_name=session.get("user_name"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            flash("Email already registered.", "error")
            return redirect(url_for("signup"))
        hashed = generate_password_hash(password)
        db.execute("INSERT INTO users (name, email, password) VALUES (?,?,?)", (name, email, hashed))
        db.commit()
        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            return redirect(url_for("index"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── GitHub Verification ──────────────────────────────────────────────────────

@app.route("/github-verification", methods=["GET", "POST"])
def github_verification():
    if "user_id" not in session:
        return redirect(url_for("login"))
    result = None
    repo_url = ""
    if request.method == "POST":
        repo_url = request.form.get("repo_url", "").strip()
        result = verify_github_repo(repo_url)
    return render_template("github_verification.html", result=result, repo_url=repo_url, ads=get_ads())

# ─── Portfolio Builder ────────────────────────────────────────────────────────

@app.route("/portfolio-builder", methods=["GET", "POST"])
def portfolio_builder():
    if "user_id" not in session:
        return redirect(url_for("login"))

    github_result = None
    form_data = {}

    if request.method == "POST":
        form_data = request.form.to_dict()
        github_link = form_data.get("github_link", "").strip()

        # Verify GitHub if link given
        github_score = 0
        github_status = "Unverified"
        if github_link:
            github_result = verify_github_repo(github_link)
            if "error" not in github_result:
                github_score = github_result["score"]
                github_status = github_result["status"]

        if "action" in request.form and request.form["action"] == "verify":
            # Just show score, don't save yet
            return render_template("portfolio_builder.html",
                                   form_data=form_data,
                                   github_result=github_result,
                                   ads=get_ads())

        # Save portfolio
        db = get_db()
        db.execute("""
            INSERT INTO portfolios
            (user_id, full_name, dob, email, phone, bio, skills, experience,
             education, projects, github_link, linkedin, github_score, github_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session["user_id"],
            form_data.get("full_name"),
            form_data.get("dob"),
            form_data.get("email"),
            form_data.get("phone"),
            form_data.get("bio"),
            form_data.get("skills"),
            form_data.get("experience"),
            form_data.get("education"),
            form_data.get("projects"),
            github_link,
            form_data.get("linkedin"),
            github_score,
            github_status,
        ))
        db.commit()
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return redirect(url_for("portfolio_preview", pid=pid))

    return render_template("portfolio_builder.html", form_data=form_data, github_result=github_result, ads=get_ads())

# ─── Portfolio Preview ────────────────────────────────────────────────────────

@app.route("/portfolio-preview/<int:pid>")
def portfolio_preview(pid):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    portfolio = db.execute("SELECT * FROM portfolios WHERE id=?", (pid,)).fetchone()
    if not portfolio:
        flash("Portfolio not found.", "error")
        return redirect(url_for("portfolio_builder"))
    return render_template("portfolio_preview.html", p=portfolio, ads=get_ads())

# ─── PDF Download ─────────────────────────────────────────────────────────────

def generate_pdf(portfolio):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle("Title", parent=styles["Heading1"],
                                 fontSize=22, textColor=colors.HexColor("#1a1a2e"),
                                 spaceAfter=4)
    section_style = ParagraphStyle("Section", parent=styles["Heading2"],
                                   fontSize=13, textColor=colors.HexColor("#2563eb"),
                                   spaceBefore=14, spaceAfter=4)
    body_style = ParagraphStyle("Body", parent=styles["Normal"],
                                fontSize=10, leading=15)
    label_style = ParagraphStyle("Label", parent=styles["Normal"],
                                 fontSize=9, textColor=colors.grey)

    story = []

    # Header
    story.append(Paragraph(portfolio["full_name"] or "Portfolio", title_style))
    story.append(Paragraph(f'{portfolio["email"] or ""} &nbsp;|&nbsp; {portfolio["phone"] or ""}', label_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#2563eb"), spaceAfter=10))

    def section(title, content):
        if content:
            story.append(Paragraph(title, section_style))
            story.append(Paragraph(str(content).replace("\n", "<br/>"), body_style))
            story.append(Spacer(1, 6))

    section("About Me", portfolio["bio"])
    section("Skills", portfolio["skills"])
    section("Experience", portfolio["experience"])
    section("Education", portfolio["education"])
    section("Projects", portfolio["projects"])

    # GitHub
    story.append(Paragraph("GitHub Verification", section_style))
    story.append(Paragraph(f'Link: {portfolio["github_link"] or "N/A"}', body_style))
    story.append(Paragraph(f'Score: <b>{portfolio["github_score"]}/100</b>  &nbsp;|&nbsp;  Status: <b>{portfolio["github_status"]}</b>', body_style))
    story.append(Spacer(1, 6))

    if portfolio["linkedin"]:
        section("LinkedIn", portfolio["linkedin"])
    if portfolio["dob"]:
        section("Date of Birth", portfolio["dob"])

    doc.build(story)
    buffer.seek(0)
    return buffer

@app.route("/download-portfolio/<int:pid>")
def download_portfolio(pid):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    portfolio = db.execute("SELECT * FROM portfolios WHERE id=?", (pid,)).fetchone()
    if not portfolio:
        flash("Portfolio not found.", "error")
        return redirect(url_for("index"))
    pdf_buffer = generate_pdf(portfolio)
    name = (portfolio["full_name"] or "portfolio").replace(" ", "_")
    return send_file(pdf_buffer, as_attachment=True,
                     download_name=f"{name}_GitVerify.pdf",
                     mimetype="application/pdf")

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)