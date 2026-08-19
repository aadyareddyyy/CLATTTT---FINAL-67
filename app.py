"""
CLAT Preparation Docket
------------------------
A simple Flask website for tracking CLAT exam preparation.

TECHNIQUES USED IN THIS FILE (for reference):
  - Flask web framework: routes, forms, sessions, redirects, flash messages
  - sqlite3: a small file-based database, accessed with plain SQL queries
  - matplotlib: draws two PNG charts (score trend + section breakdown)
  - datetime: date math for the exam countdown and spaced revision dates
  - Plain Python: lists, dictionaries, loops, functions, f-strings

There is no advanced framework magic here on purpose - every route is a
plain function that reads the database, does some simple math, and shows
a page.
"""

import os
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, flash, g

import matplotlib
matplotlib.use("Agg")  # draw charts without needing a screen
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# Basic setup
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "clat_tracker.db")
CHARTS_DIR = os.path.join(BASE_DIR, "static", "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this-in-production")

# ---------------------------------------------------------------------------
# Syllabus data (kept as one simple Python list of dictionaries)
# Section max marks add up to 120, based on the CLAT question distribution.
# ---------------------------------------------------------------------------
SUBJECTS = [
    {
        "id": "english", "roman": "I", "name": "English Language", "max": 24,
        "topics": [
            "Reading Comprehension", "Vocabulary in Context", "Synonyms & Antonyms",
            "Grammar & Usage", "Tone & Author's Viewpoint", "Para Jumbles",
            "Summary & Main Idea", "Critical Reasoning in Passages",
            "Figures of Speech", "Inference-Based Questions",
        ],
    },
    {
        "id": "gk", "roman": "II", "name": "Current Affairs & GK", "max": 30,
        "topics": [
            "National Affairs", "International Affairs",
            "Legal & Constitutional Current Affairs", "Static GK",
            "Awards & Honours", "Sports & Miscellaneous",
            "Government Schemes & Policies", "Science & Technology News",
            "Books, Authors & Committees", "Person in the News",
        ],
    },
    {
        "id": "legal", "roman": "III", "name": "Legal Reasoning", "max": 30,
        "topics": [
            "Constitutional Law", "Law of Contracts", "Torts", "Criminal Law",
            "Legal Maxims & Principles", "Family Law Basics",
            "International Law Basics", "Intellectual Property Basics",
            "Jurisprudence & Legal Theory", "Recent Landmark Judgments",
        ],
    },
    {
        "id": "logical", "roman": "IV", "name": "Logical Reasoning", "max": 24,
        "topics": [
            "Critical Reasoning", "Analogies & Series", "Syllogisms",
            "Puzzles & Arrangements", "Statement-Assumption", "Blood Relations",
            "Coding-Decoding", "Cause & Effect", "Strengthen-Weaken Arguments",
            "Logical Sequences",
        ],
    },
    {
        "id": "quant", "roman": "V", "name": "Quantitative Techniques", "max": 12,
        "topics": [
            "Data Interpretation", "Ratio, Proportion & Averages",
            "Percentages & Profit-Loss", "Basic Algebra", "Graphs & Charts",
            "Time & Work", "Mensuration Basics", "Number Systems",
        ],
    },
]
SUBJECT_MAP = {s["id"]: s for s in SUBJECTS}

REVISION_INTERVALS = [3, 7, 15, 30]  # days, for spaced repetition of finished topics
MISTAKE_TYPES = [
    "Silly Mistake", "Conceptual Gap", "Time Management",
    "Misread Question", "Wrong Guess", "Calculation Error", "Other",
]

# Chart colours - simple black/white/grey palette to match the website
INK = "#111111"
GREY = "#9a9a9a"
LIGHT_GREY = "#e5e5e5"
PAPER = "#ffffff"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    """Open one database connection per request and reuse it."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            exam_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS topic_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            subject_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            percent INTEGER NOT NULL DEFAULT 0,
            UNIQUE(profile_id, subject_id, topic)
        );

        CREATE TABLE IF NOT EXISTS mock_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            date TEXT NOT NULL,
            english_correct INTEGER, english_incorrect INTEGER,
            gk_correct INTEGER, gk_incorrect INTEGER,
            legal_correct INTEGER, legal_incorrect INTEGER,
            logical_correct INTEGER, logical_incorrect INTEGER,
            quant_correct INTEGER, quant_incorrect INTEGER
        );

        CREATE TABLE IF NOT EXISTS study_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            date TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            hours REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS revision_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            topic_key TEXT NOT NULL,
            stage INTEGER NOT NULL DEFAULT 0,
            next_date TEXT NOT NULL,
            retired INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            date TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            mistake_type TEXT NOT NULL,
            note TEXT,
            reviewed INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()

    # If an older version of the database is on disk (with different mock
    # test columns), add the new columns instead of crashing. Old columns
    # are simply left unused.
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(mock_tests)")]
    for s in SUBJECTS:
        for suffix in ("correct", "incorrect"):
            col = f"{s['id']}_{suffix}"
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE mock_tests ADD COLUMN {col} INTEGER")
    conn.commit()
    conn.close()


def get_or_create_profile(name):
    db = get_db()
    row = db.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()
    if row:
        return row
    db.execute("INSERT INTO profiles (name) VALUES (?)", (name,))
    db.commit()
    profile_id = db.execute("SELECT id FROM profiles WHERE name = ?", (name,)).fetchone()["id"]
    rows = [(profile_id, s["id"], t) for s in SUBJECTS for t in s["topics"]]
    db.executemany(
        "INSERT OR IGNORE INTO topic_status (profile_id, subject_id, topic) VALUES (?, ?, ?)",
        rows,
    )
    db.commit()
    return db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def today_iso():
    return datetime.now().strftime("%Y-%m-%d")


def add_days(date_str, n):
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def fmt_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")


def roll_number(profile_id):
    year = datetime.now().year + 1
    return f"CLT{year}{profile_id:05d}"


def section_score(correct, incorrect):
    """CLAT marking: +1 for each correct answer, -0.25 for each incorrect answer."""
    correct = correct or 0
    incorrect = incorrect or 0
    return correct * 1 - incorrect * 0.25


def mock_total_score(row):
    """Add up the computed score of all five sections for one mock test row."""
    return sum(section_score(row[f"{s['id']}_correct"], row[f"{s['id']}_incorrect"]) for s in SUBJECTS)


def mock_total_questions(row):
    """Total number of questions attempted (correct + incorrect) across all sections."""
    total = 0
    for s in SUBJECTS:
        total += (row[f"{s['id']}_correct"] or 0) + (row[f"{s['id']}_incorrect"] or 0)
    return total


def mock_section_breakdown(row):
    """Build a per-section breakdown list, plus an overall row, for one mock test."""
    breakdown = []
    overall_correct = overall_incorrect = overall_max = 0
    for s in SUBJECTS:
        correct = row[f"{s['id']}_correct"] or 0
        incorrect = row[f"{s['id']}_incorrect"] or 0
        marks_correct = correct * 1
        marks_incorrect = incorrect * 0.25
        breakdown.append({
            "name": s["name"],
            "attempted": correct + incorrect,
            "max": s["max"],
            "correct": correct,
            "incorrect": incorrect,
            "marks_correct": marks_correct,
            "marks_incorrect": -marks_incorrect,
            "actual_score": round(marks_correct - marks_incorrect, 2),
        })
        overall_correct += correct
        overall_incorrect += incorrect
        overall_max += s["max"]
    breakdown.append({
        "name": "Overall",
        "attempted": overall_correct + overall_incorrect,
        "max": overall_max,
        "correct": overall_correct,
        "incorrect": overall_incorrect,
        "marks_correct": overall_correct * 1,
        "marks_incorrect": -(overall_incorrect * 0.25),
        "actual_score": round(overall_correct * 1 - overall_incorrect * 0.25, 2),
    })
    return breakdown


# ---------------------------------------------------------------------------
# Login guard - every page except /login needs a profile in the session
# ---------------------------------------------------------------------------
@app.before_request
def require_login():
    open_endpoints = {"login", "static"}
    if request.endpoint not in open_endpoints and "profile_id" not in session:
        return redirect(url_for("login"))


@app.context_processor
def inject_profile():
    if "profile_id" in session:
        return {
            "current_profile_name": session.get("profile_name"),
            "current_roll_no": roll_number(session["profile_id"]),
        }
    return {}


# ---------------------------------------------------------------------------
# Routes - login / logout
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip().lower()
        if not name:
            flash("Enter a profile name to continue.")
            return redirect(url_for("login"))
        profile = get_or_create_profile(name)
        session["profile_id"] = profile["id"]
        session["profile_name"] = profile["name"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes - dashboard
# ---------------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    db = get_db()
    profile_id = session["profile_id"]
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()

    percents = [r["percent"] for r in db.execute(
        "SELECT percent FROM topic_status WHERE profile_id = ?", (profile_id,)
    ).fetchall()]
    overall_pct = round(sum(percents) / len(percents)) if percents else 0

    latest_mock_row = db.execute(
        "SELECT * FROM mock_tests WHERE profile_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (profile_id,),
    ).fetchone()
    latest_mock_score = round(mock_total_score(latest_mock_row), 2) if latest_mock_row else None

    today_hours_row = db.execute(
        "SELECT COALESCE(SUM(hours), 0) total FROM study_logs WHERE profile_id = ? AND date = ?",
        (profile_id, today_iso()),
    ).fetchone()
    today_hours = today_hours_row["total"]

    due_revisions = db.execute(
        "SELECT COUNT(*) c FROM revision_items WHERE profile_id = ? AND retired = 0 AND next_date <= ?",
        (profile_id, today_iso()),
    ).fetchone()["c"]

    open_mistakes = db.execute(
        "SELECT COUNT(*) c FROM mistakes WHERE profile_id = ? AND reviewed = 0",
        (profile_id,),
    ).fetchone()["c"]

    days_left = None
    if profile["exam_date"]:
        days_left = (datetime.strptime(profile["exam_date"], "%Y-%m-%d") - datetime.now()).days

    trend_chart = f"charts/trend_{profile_id}.png"
    has_trend = os.path.exists(os.path.join(BASE_DIR, "static", trend_chart))

    return render_template(
        "dashboard.html",
        overall_pct=overall_pct,
        latest_mock=latest_mock_score,
        today_hours=today_hours,
        due_revisions=due_revisions,
        open_mistakes=open_mistakes,
        exam_date=profile["exam_date"],
        days_left=days_left,
        fmt_date=fmt_date,
        trend_chart=trend_chart if has_trend else None,
    )


@app.route("/set-exam-date", methods=["POST"])
def set_exam_date():
    db = get_db()
    exam_date = request.form.get("exam_date") or None
    db.execute("UPDATE profiles SET exam_date = ? WHERE id = ?", (exam_date, session["profile_id"]))
    db.commit()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes - subjects (percent complete per topic)
# ---------------------------------------------------------------------------
@app.route("/subjects")
def subjects():
    db = get_db()
    profile_id = session["profile_id"]

    subject_view = []
    for s in SUBJECTS:
        topic_rows = [
            dict(db.execute(
                "SELECT * FROM topic_status WHERE profile_id = ? AND subject_id = ? AND topic = ?",
                (profile_id, s["id"], t),
            ).fetchone())
            for t in s["topics"]
        ]
        avg_pct = round(sum(t["percent"] for t in topic_rows) / len(topic_rows)) if topic_rows else 0
        subject_view.append({**s, "topic_rows": topic_rows, "avg_pct": avg_pct})

    return render_template("subjects.html", subject_view=subject_view)


@app.route("/subjects/update", methods=["POST"])
def update_topic():
    db = get_db()
    profile_id = session["profile_id"]
    subject_id = request.form["subject_id"]
    topic = request.form["topic"]
    percent = request.form.get("percent", type=int) or 0
    percent = max(0, min(100, percent))  # keep it inside 0-100

    db.execute(
        "UPDATE topic_status SET percent = ? WHERE profile_id = ? AND subject_id = ? AND topic = ?",
        (percent, profile_id, subject_id, topic),
    )

    if percent == 100:
        key = f"{subject_id}::{topic}"
        existing = db.execute(
            "SELECT id FROM revision_items WHERE profile_id = ? AND topic_key = ? AND retired = 0",
            (profile_id, key),
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO revision_items (profile_id, topic_key, stage, next_date, retired) VALUES (?, ?, 0, ?, 0)",
                (profile_id, key, add_days(today_iso(), REVISION_INTERVALS[0])),
            )
    db.commit()
    return redirect(url_for("subjects"))


# ---------------------------------------------------------------------------
# Routes - mock tests
# ---------------------------------------------------------------------------
@app.route("/mocks", methods=["GET", "POST"])
def mocks():
    db = get_db()
    profile_id = session["profile_id"]

    if request.method == "POST":
        date = request.form.get("date") or today_iso()

        # Read correct/incorrect counts for every section (blank = 0).
        counts = {}
        for s in SUBJECTS:
            correct = request.form.get(f"{s['id']}_correct", type=int) or 0
            incorrect = request.form.get(f"{s['id']}_incorrect", type=int) or 0
            counts[s["id"]] = (correct, incorrect)

        # Validate: each section's attempted questions must fit inside that
        # section's question count, and the grand total must not pass 120.
        errors = []
        total_attempted = 0
        for s in SUBJECTS:
            correct, incorrect = counts[s["id"]]
            attempted = correct + incorrect
            total_attempted += attempted
            if attempted > s["max"]:
                errors.append(
                    f"{s['name']}: {attempted} questions entered, but this section only has {s['max']}."
                )
        if total_attempted > 120:
            errors.append(f"Total questions entered ({total_attempted}) go beyond 120.")

        if errors:
            for e in errors:
                flash(e)
            return redirect(url_for("mocks"))

        values = [profile_id, date]
        for s in SUBJECTS:
            correct, incorrect = counts[s["id"]]
            values.append(correct)
            values.append(incorrect)
        db.execute(
            """INSERT INTO mock_tests
               (profile_id, date, english_correct, english_incorrect, gk_correct, gk_incorrect,
                legal_correct, legal_incorrect, logical_correct, logical_incorrect,
                quant_correct, quant_incorrect)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
        db.commit()
        generate_charts(profile_id)
        flash("Mock test saved.")
        return redirect(url_for("mocks"))

    rows = db.execute(
        "SELECT * FROM mock_tests WHERE profile_id = ? ORDER BY date DESC, id DESC", (profile_id,)
    ).fetchall()
    mock_view = [
        {
            "date": r["date"],
            "total_questions": mock_total_questions(r),
            "total_score": round(mock_total_score(r), 2),
        }
        for r in rows
    ]

    latest_breakdown = mock_section_breakdown(rows[0]) if rows else None

    trend_chart = f"charts/trend_{profile_id}.png"
    section_chart = f"charts/sections_{profile_id}.png"
    pie_chart = f"charts/pie_{profile_id}.png"
    has_trend = os.path.exists(os.path.join(BASE_DIR, "static", trend_chart))
    has_section = os.path.exists(os.path.join(BASE_DIR, "static", section_chart))
    has_pie = os.path.exists(os.path.join(BASE_DIR, "static", pie_chart))

    return render_template(
        "mocks.html", mocks=mock_view, subjects=SUBJECTS, fmt_date=fmt_date, today=today_iso(),
        latest_breakdown=latest_breakdown,
        trend_chart=trend_chart if has_trend else None,
        section_chart=section_chart if has_section else None,
        pie_chart=pie_chart if has_pie else None,
    )


def generate_charts(profile_id):
    """Redraw the score-trend and latest-section-breakdown PNGs for a profile."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM mock_tests WHERE profile_id = ? ORDER BY date ASC, id ASC", (profile_id,)
    ).fetchall()
    if not rows:
        return

    plt.rcParams["font.family"] = "sans-serif"

    # --- Score trend line chart ---
    dates = [r["date"][5:] for r in rows]
    scores = [mock_total_score(r) for r in rows]
    fig, ax = plt.subplots(figsize=(6, 2.6), dpi=150)
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    ax.plot(dates, scores, color=INK, linewidth=2, marker="o", markersize=5,
             markerfacecolor=INK, markeredgecolor=PAPER)
    ax.set_title("Mock Test Score Trend", color=INK, fontsize=11, loc="left", fontweight="bold")
    ax.grid(color=LIGHT_GREY, linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(LIGHT_GREY)
    ax.tick_params(colors=GREY, labelsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(CHARTS_DIR, f"trend_{profile_id}.png"), facecolor=PAPER)
    plt.close(fig)

    # --- Latest section breakdown bar chart ---
    latest = rows[-1]
    labels = [s["roman"] for s in SUBJECTS]
    values = [section_score(latest[f"{s['id']}_correct"], latest[f"{s['id']}_incorrect"]) for s in SUBJECTS]
    fig2, ax2 = plt.subplots(figsize=(6, 2.6), dpi=150)
    fig2.patch.set_facecolor(PAPER)
    ax2.set_facecolor(PAPER)
    bars = ax2.bar(labels, values, color=INK, width=0.55)
    for b in bars:
        ax2.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.3, f"{b.get_height():.2f}",
                  ha="center", fontsize=8, color=INK, fontweight="bold")
    ax2.set_title("Latest Mock - Section-wise Score", color=INK, fontsize=11, loc="left", fontweight="bold")
    ax2.grid(color=LIGHT_GREY, linewidth=0.7, axis="y")
    ax2.spines[["top", "right"]].set_visible(False)
    ax2.spines[["left", "bottom"]].set_color(LIGHT_GREY)
    ax2.tick_params(colors=GREY, labelsize=8)
    fig2.tight_layout()
    fig2.savefig(os.path.join(CHARTS_DIR, f"sections_{profile_id}.png"), facecolor=PAPER)
    plt.close(fig2)

    # --- Latest mock pie chart: correct vs incorrect vs not attempted ---
    total_correct = sum(latest[f"{s['id']}_correct"] or 0 for s in SUBJECTS)
    total_incorrect = sum(latest[f"{s['id']}_incorrect"] or 0 for s in SUBJECTS)
    total_max = sum(s["max"] for s in SUBJECTS)
    not_attempted = max(total_max - total_correct - total_incorrect, 0)

    fig3, ax3 = plt.subplots(figsize=(4.2, 4.2), dpi=150)
    fig3.patch.set_facecolor(PAPER)
    pie_values = [total_correct, total_incorrect, not_attempted]
    pie_labels = [f"Correct ({total_correct})", f"Incorrect ({total_incorrect})", f"Not attempted ({not_attempted})"]
    pie_colors = ["#2e7d32", "#c62828", "#9a9a9a"]
    # Drop any zero-value slices so the pie chart doesn't show empty labels.
    filtered = [(v, l, c) for v, l, c in zip(pie_values, pie_labels, pie_colors) if v > 0]
    if filtered:
        vals, labs, cols = zip(*filtered)
        ax3.pie(vals, labels=labs, colors=cols, autopct="%1.0f%%",
                textprops={"color": INK, "fontsize": 9})
    ax3.set_title("Latest Mock - Correct / Incorrect / Not Attempted", color=INK, fontsize=10, fontweight="bold")
    fig3.tight_layout()
    fig3.savefig(os.path.join(CHARTS_DIR, f"pie_{profile_id}.png"), facecolor=PAPER)
    plt.close(fig3)


# ---------------------------------------------------------------------------
# Routes - study log
# ---------------------------------------------------------------------------
@app.route("/study", methods=["GET", "POST"])
def study():
    db = get_db()
    profile_id = session["profile_id"]

    if request.method == "POST":
        date = request.form.get("date") or today_iso()
        subject_id = request.form.get("subject_id")
        hours = request.form.get("hours", type=float)
        if subject_id and hours:
            db.execute(
                "INSERT INTO study_logs (profile_id, date, subject_id, hours) VALUES (?, ?, ?, ?)",
                (profile_id, date, subject_id, hours),
            )
            db.commit()
        return redirect(url_for("study"))

    totals = db.execute(
        """SELECT subject_id, COALESCE(SUM(hours), 0) total
           FROM study_logs WHERE profile_id = ? GROUP BY subject_id""",
        (profile_id,),
    ).fetchall()
    totals_map = {r["subject_id"]: r["total"] for r in totals}
    subject_totals = [{**s, "total": totals_map.get(s["id"], 0)} for s in SUBJECTS]

    return render_template("study.html", subjects=SUBJECTS, subject_totals=subject_totals, today=today_iso())


# ---------------------------------------------------------------------------
# Routes - revision queue (spaced repetition for finished topics)
# ---------------------------------------------------------------------------
@app.route("/revision")
def revision():
    db = get_db()
    profile_id = session["profile_id"]
    rows = db.execute(
        "SELECT * FROM revision_items WHERE profile_id = ? AND retired = 0 ORDER BY next_date ASC",
        (profile_id,),
    ).fetchall()

    items = []
    for r in rows:
        subject_id, topic = r["topic_key"].split("::", 1)
        subject_name = SUBJECT_MAP[subject_id]["name"]
        items.append({
            "id": r["id"], "label": f"{subject_name} - {topic}",
            "next_date": r["next_date"], "due": r["next_date"] <= today_iso(),
        })

    return render_template("revision.html", items=items, fmt_date=fmt_date)


@app.route("/revision/mark/<int:item_id>", methods=["POST"])
def mark_revised(item_id):
    db = get_db()
    profile_id = session["profile_id"]
    row = db.execute(
        "SELECT * FROM revision_items WHERE id = ? AND profile_id = ?", (item_id, profile_id)
    ).fetchone()
    if row:
        next_stage = min(row["stage"] + 1, len(REVISION_INTERVALS) - 1)
        retired = 1 if row["stage"] >= len(REVISION_INTERVALS) - 1 else 0
        db.execute(
            "UPDATE revision_items SET stage = ?, next_date = ?, retired = ? WHERE id = ?",
            (next_stage, add_days(today_iso(), REVISION_INTERVALS[next_stage]), retired, item_id),
        )
        db.commit()
    return redirect(url_for("revision"))


# ---------------------------------------------------------------------------
# Routes - mistakes log + revise mistakes
# ---------------------------------------------------------------------------
@app.route("/mistakes", methods=["GET", "POST"])
def mistakes():
    db = get_db()
    profile_id = session["profile_id"]

    if request.method == "POST":
        date = request.form.get("date") or today_iso()
        subject_id = request.form.get("subject_id")
        topic = request.form.get("topic", "").strip()
        mistake_type = request.form.get("mistake_type")
        note = request.form.get("note", "").strip()
        if subject_id and topic and mistake_type:
            db.execute(
                """INSERT INTO mistakes (profile_id, date, subject_id, topic, mistake_type, note, reviewed)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (profile_id, date, subject_id, topic, mistake_type, note),
            )
            db.commit()
        return redirect(url_for("mistakes"))

    rows = db.execute(
        "SELECT * FROM mistakes WHERE profile_id = ? ORDER BY reviewed ASC, date DESC", (profile_id,)
    ).fetchall()
    mistake_view = [
        {**dict(r), "subject_name": SUBJECT_MAP[r["subject_id"]]["name"]}
        for r in rows
    ]
    to_revise = [m for m in mistake_view if not m["reviewed"]]
    reviewed = [m for m in mistake_view if m["reviewed"]]

    return render_template(
        "mistakes.html", subjects=SUBJECTS, mistake_types=MISTAKE_TYPES,
        to_revise=to_revise, reviewed=reviewed, fmt_date=fmt_date, today=today_iso(),
    )


@app.route("/mistakes/mark/<int:mistake_id>", methods=["POST"])
def mark_mistake_reviewed(mistake_id):
    db = get_db()
    profile_id = session["profile_id"]
    db.execute(
        "UPDATE mistakes SET reviewed = 1 WHERE id = ? AND profile_id = ?",
        (mistake_id, profile_id),
    )
    db.commit()
    return redirect(url_for("mistakes"))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
