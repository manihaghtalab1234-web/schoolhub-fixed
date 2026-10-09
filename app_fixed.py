import os
import sqlite3
import json
import uuid
from pathlib import Path
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "schoolhub.db"))

app = Flask(__name__)
app.config["SCHOOL_NAME"] = "دبیرستان استعدادهای درخشان علامه حلی 2 همدان"
app.secret_key = os.environ.get("SECRET_KEY", "schoolhub-change-this-secret-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
app.config["TURN_URL"] = os.environ.get("TURN_URL", "")
app.config["TURN_USERNAME"] = os.environ.get("TURN_USERNAME", "")
app.config["TURN_PASSWORD"] = os.environ.get("TURN_PASSWORD", "")

@app.after_request
def add_media_permissions(response):
    response.headers["Permissions-Policy"] = "microphone=*, camera=*, display-capture=*"
    response.headers["Feature-Policy"] = "microphone *; camera *; display-capture *"
    return response

ROLE_NAMES = {
    "admin": "ادمین",
    "manager": "مدیر",
    "teacher": "دبیر",
    "student": "دانش‌آموز",
}
STAFF_ROLES = {"admin", "manager", "teacher"}


def _upload_dir():
    candidates = []
    configured = os.environ.get("UPLOAD_DIR")
    if configured:
        candidates.append(configured)
    candidates.extend([os.path.join(BASE_DIR, "uploads"), "/tmp/schoolhub_uploads"])
    for path in candidates:
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            test = Path(path) / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return Path(path)
        except Exception:
            continue
    raise RuntimeError("No writable upload directory is available")


def _db_path():
    configured = os.environ.get("DATABASE_PATH")
    candidates = []
    if configured:
        candidates.append(configured)
    candidates.append(os.path.join(BASE_DIR, "schoolhub.db"))
    candidates.append("/tmp/schoolhub.db")
    for path in candidates:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
            return path
        except Exception:
            continue
    raise RuntimeError("No writable location is available for the SQLite database")


def db():
    if "db" not in g:
        path = _db_path()
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            create_schema(conn)
            migrate_users(conn)
            migrate_extra_columns(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            fallback = os.path.join("/tmp", "schoolhub_clean.db")
            conn = sqlite3.connect(fallback, timeout=30)
            conn.row_factory = sqlite3.Row
            create_schema(conn)
            conn.commit()
        g.db = conn
    return g.db


def create_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','manager','teacher','student')),
        class_id INTEGER,
        subject TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        teacher_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        subject TEXT NOT NULL,
        description TEXT,
        due_date TEXT,
        teacher_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS assignment_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER NOT NULL,
        uploader_id INTEGER NOT NULL, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
        uploaded_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS grades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        title TEXT NOT NULL,
        score REAL NOT NULL,
        created_at TEXT NOT NULL,
        teacher_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        weekday TEXT NOT NULL,
        period INTEGER NOT NULL,
        subject TEXT NOT NULL,
        teacher TEXT NOT NULL,
        class_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        present INTEGER NOT NULL DEFAULT 0,
        UNIQUE(student_id, date)
    );

    CREATE TABLE IF NOT EXISTS virtual_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        teacher_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        chat_enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS virtual_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        class_id INTEGER,
        session_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS virtual_peers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL, last_seen TEXT NOT NULL, UNIQUE(session_id,user_id)
    );
    CREATE TABLE IF NOT EXISTS virtual_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL, signal_type TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS teacher_classes (
        teacher_id INTEGER NOT NULL, class_id INTEGER NOT NULL,
        PRIMARY KEY (teacher_id, class_id)
    );
    CREATE TABLE IF NOT EXISTS virtual_permissions (
        session_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
        granted_at TEXT NOT NULL, PRIMARY KEY(session_id, student_id)
    );
    CREATE TABLE IF NOT EXISTS virtual_permission_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, UNIQUE(session_id, student_id)
    );
    CREATE TABLE IF NOT EXISTS virtual_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, uploader_id INTEGER NOT NULL,
        original_name TEXT NOT NULL, stored_name TEXT NOT NULL, uploaded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vmsg_session_id ON virtual_messages(session_id,id);
    CREATE INDEX IF NOT EXISTS idx_vsig_recipient ON virtual_signals(session_id,recipient_id,delivered,id);
    CREATE INDEX IF NOT EXISTS idx_vpeer_session_seen ON virtual_peers(session_id,last_seen);
    """)


def migrate_extra_columns(conn):
    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "subject" not in user_cols:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN subject TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    for table, column in [("schedule", "class_id"), ("virtual_messages", "class_id"), ("virtual_messages", "session_id"), ("virtual_sessions", "chat_enabled")]:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
            except sqlite3.OperationalError:
                pass
    if "chat_enabled" in {r["name"] for r in conn.execute("PRAGMA table_info(virtual_sessions)").fetchall()}:
        conn.execute("UPDATE virtual_sessions SET chat_enabled=1 WHERE chat_enabled IS NULL")


def migrate_users(conn):
    info = conn.execute("PRAGMA table_info(users)").fetchall()
    if not info:
        return
    columns = {row["name"] for row in info}
    if "class_id" not in columns:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN class_id INTEGER")
        except sqlite3.OperationalError:
            pass
    role_sql = next((row["sql"] or "" for row in info if row["name"] == "role"), "")
    if "admin" not in role_sql or "manager" not in role_sql:
        conn.execute("ALTER TABLE users RENAME TO users_legacy")
        conn.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','manager','teacher','student')),
                class_id INTEGER
            )
        """)
        legacy = conn.execute(
            "SELECT id,username,password,full_name,role FROM users_legacy ORDER BY id"
        ).fetchall()
        for row in legacy:
            role = "teacher" if row["role"] == "teacher" else "student"
            password = row["password"]
            if not password.startswith(("pbkdf2:", "scrypt:")):
                password = generate_password_hash(password)
            conn.execute(
                "INSERT INTO users(id,username,password,full_name,role,class_id) VALUES(?,?,?,?,?,?)",
                (row["id"], row["username"], password, row["full_name"], role, row["class_id"])
            )
        conn.execute("DROP TABLE users_legacy")
    else:
        for row in conn.execute("SELECT id,password FROM users").fetchall():
            pw = row["password"]
            if not pw.startswith(("pbkdf2:", "scrypt:")):
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(pw), row["id"])
                )


DEFAULT_STUDENTS = [
    ("آراد فرهمند راد", "Arad901"),
    ("آرتین محمدی", "Artin"),
    ("امیرحسن الوندی", "Alvandi1901"),
    ("امیررضا برزویی", "Amirreza"),
    ("امیرعلی زندی", "Amir"),
    ("امیرعلی سجادمنش", "Amirali1"),
    ("امیرعلی فاضلی فر", "Amirali"),
    ("امیرعلی نظری", "AmirAli29901"),
    ("امیرمحمد جعفری", "AmirMohammad3901"),
    ("امیرمحمد صادقیان", "Amirmohammad"),
    ("امیرمحمد گرجی", "Amiemohammad"),
    ("امیرمهدی ملک محمدی", "Amirmahdi26901"),
    ("دانش‌آموز 15", "student15"),
    ("دانش‌آموز 27", "student27"),
    ("دانش‌آموز 3", "student3"),
    ("دانیال عزیزی", "Danial16901"),
    ("رضا خدابنده لو", "Khodabandelo"),
    ("سامیار محمدی", "Samiar901"),
    ("سیدامیرحسین مرتضوی بیباک", "Seyed24901"),
    ("سینا سبزه ای", "Sina"),
    ("علی خوانی", "Ali"),
    ("فرزاد مالمیر", "Farzad"),
    ("فرهام خدابنده لو", "Khodabandeloo"),
    ("مانی حق طلب", "Mani6901"),
    ("محمد صالح کریمی عزت", "Mohammadsaleh"),
    ("محمد پارسا مظاهری", "Mohammadparsa"),
    ("محمدحسین قدیمی", "Ghadimi"),
    ("محمدسام حاجی بابایی", "Mohammadsam"),
    ("محمدیاسین طاهری کمالان", "Yasin"),
    ("مهراد صفری", "Safari"),
    ("پویان زینتی رحیم", "Zinati"),
    ("یونس حبیبوند", "Habibvand"),
]


def ensure_account(conn, username, password, full_name, role, class_id=None):
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    hashed = generate_password_hash(password)
    if row:
        conn.execute(
            "UPDATE users SET password=?, full_name=?, role=?, class_id=COALESCE(?, class_id) WHERE username=?",
            (hashed, full_name, role, class_id, username)
        )
    else:
        conn.execute(
            "INSERT INTO users(username,password,full_name,role,class_id) VALUES(?,?,?,?,?)",
            (username, hashed, full_name, role, class_id)
        )


def init_db():
    path = _db_path()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    migrate_users(conn)

    class_names = ["701","801","901","101","102","103","201","202","203","301","302","303"]
    for class_name in class_names:
        conn.execute("INSERT OR IGNORE INTO classes(name,description) VALUES(?,?)", (class_name, "کلاس مدرسه"))
    class_rows = conn.execute("SELECT id,name FROM classes ORDER BY id").fetchall()
    class_map = {r["name"]: r["id"] for r in class_rows}

    ensure_account(conn, "admin", "123", "مدیر ارشد سیستم", "admin")
    ensure_account(conn, "manager", "Manager8300@#", "مدیر مدرسه", "manager")
    ensure_account(conn, "teacher", "Teacher8300@#", "دبیر مدرسه", "teacher")

    class_901 = class_map["901"]
    default_usernames = {username for _, username in DEFAULT_STUDENTS}
    placeholders = ",".join("?" for _ in default_usernames)
    conn.execute(
        f"DELETE FROM users WHERE role='student' AND username NOT IN ({placeholders})",
        tuple(sorted(default_usernames))
    )
    for full_name, username in DEFAULT_STUDENTS:
        ensure_account(conn, username, username, full_name, "student", class_901)

    if conn.execute("SELECT COUNT(*) AS c FROM announcements").fetchone()["c"] == 0:
        announcements = [
            ("خوش آمدید", "به دبیرستان استعدادهای درخشان علامه حلی 2 همدان خوش آمدید. اطلاعیه‌های مهم در این قسمت قرار می‌گیرند."),
            ("یادآوری", "لطفاً تکالیف خود را قبل از مهلت تعیین‌شده تحویل دهید.")
        ]
        teacher_id = conn.execute(
            "SELECT id FROM users WHERE username='teacher'"
        ).fetchone()["id"]
        for title, body in announcements:
            conn.execute(
                "INSERT INTO announcements(title,body,created_at,teacher_id) VALUES(?,?,?,?)",
                (title, body, datetime.now().strftime("%Y-%m-%d %H:%M"), teacher_id)
            )

    if conn.execute("SELECT COUNT(*) AS c FROM assignments").fetchone()["c"] == 0:
        teacher_id = conn.execute(
            "SELECT id FROM users WHERE username='teacher'"
        ).fetchone()["id"]
        assignments = [
            ("تمرین فصل اول", "ریاضی", "تمرین‌های مشخص‌شده توسط دبیر را حل کنید.", "2026-10-07"),
            ("گزارش علوم", "علوم", "یک گزارش کوتاه درباره موضوع درس آماده کنید.", "2026-10-10"),
        ]
        for a in assignments:
            conn.execute(
                "INSERT INTO assignments(title,subject,description,due_date,teacher_id) VALUES(?,?,?,?,?)",
                (*a, teacher_id)
            )

    if conn.execute("SELECT COUNT(*) AS c FROM schedule").fetchone()["c"] == 0:
        schedule_data = [
            ("شنبه",1,"ریاضی","دبیر ریاضی"), ("شنبه",2,"علوم","دبیر علوم"),
            ("شنبه",3,"فارسی","دبیر فارسی"), ("شنبه",4,"زبان","دبیر زبان"),
            ("یکشنبه",1,"فیزیک","دبیر فیزیک"), ("یکشنبه",2,"ریاضی","دبیر ریاضی"),
            ("یکشنبه",3,"ورزش","دبیر ورز��"), ("یکشنبه",4,"فارسی","دبیر فارسی"),
            ("دوشنبه",1,"علوم","دبیر علوم"), ("دوشنبه",2,"زبان","دبیر زبان"),
            ("دوشنبه",3,"ریاضی","دبیر ریاضی"), ("دوشنبه",4,"مطالعات","دبیر مطالعات"),
            ("سه‌شنبه",1,"فارسی","دبیر فارسی"), ("سه‌شنبه",2,"فیزیک","دبیر فیزیک"),
            ("سه‌شنبه",3,"ریاضی","دبیر ریاضی"), ("سه‌شنبه",4,"هنر","دبیر هنر"),
            ("چهارشنبه",1,"علوم","دبیر علوم"), ("چهارشنبه",2,"زبان","دبیر زبان"),
            ("چهارشنبه",3,"ورزش","دبیر ورزش"), ("چهارشنبه",4,"ریاضی","دبیر ریاضی"),
        ]
        expanded = []
        for idx, item in enumerate(schedule_data):
            expanded.append((*item, class_rows[idx % len(class_rows)]["id"]))
        conn.executemany(
            "INSERT INTO schedule(weekday,period,subject,teacher,class_id) VALUES(?,?,?,?,?)",
            expanded
        )

    if conn.execute("SELECT COUNT(*) AS c FROM grades").fetchone()["c"] == 0:
        teacher_id = conn.execute(
            "SELECT id FROM users WHERE username='teacher'"
        ).fetchone()["id"]
        student_ids = conn.execute(
            "SELECT id FROM users WHERE role='student' ORDER BY id LIMIT 5"
        ).fetchall()
        for student in student_ids:
            for subject, score in [("ریاضی", 18.5), ("علوم", 19), ("فارسی", 17.75)]:
                conn.execute(
                    "INSERT INTO grades(student_id,subject,title,score,created_at,teacher_id) VALUES(?,?,?,?,?,?)",
                    (student["id"], subject, "نمره کلاسی", score,
                     datetime.now().strftime("%Y-%m-%d"), teacher_id)
                )

    conn.commit()
    conn.close()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") not in roles:
                flash("شما اجازه دسترسی به این بخش را ندارید.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


teacher_required = role_required("admin", "manager", "teacher")


@app.context_processor
def inject_user():
    return {"current_user": session, "role_names": ROLE_NAMES}


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/healthz")
def healthz():
    try:
        db().execute("SELECT 1").fetchone()
        return {"status": "ok", "service": app.config["SCHOOL_NAME"]}, 200
    except Exception:
        app.logger.exception("Health check failed")
        return {"status": "error"}, 503


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            conn = db()
            admin = conn.execute("SELECT id FROM users WHERE username=?", ("admin",)).fetchone()
            if not admin:
                ensure_account(conn, "admin", "123", "مدیر ارشد سیستم", "admin")
                conn.commit()

            user = conn.execute(
                "SELECT id, username, password, full_name, role, class_id FROM users WHERE username=?",
                (username,)
            ).fetchone()
            valid = False
            if user:
                try:
                    valid = check_password_hash(user["password"], password)
                except Exception:
                    valid = (user["password"] == password)

            if valid:
                session.clear()
                session.update({
                    "user_id": user["id"],
                    "full_name": user["full_name"],
                    "role": user["role"],
                    "username": user["username"],
                    "class_id": user["class_id"],
                })
                return redirect(url_for("dashboard"))

            flash("نام کاربری یا رمز عبور اشتباه است.", "error")
        except Exception:
            app.logger.exception("Login failed")
            flash("خطای داخلی هنگام ورود. لطفاً دوباره تلاش کنید.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    announcements = conn.execute(
        "SELECT * FROM announcements ORDER BY id DESC LIMIT 5"
    ).fetchall()
    assignments = conn.execute(
        "SELECT * FROM assignments ORDER BY due_date ASC LIMIT 5"
    ).fetchall()

    if session["role"] == "student":
        grades = conn.execute(
            "SELECT * FROM grades WHERE student_id=? ORDER BY id DESC LIMIT 8",
            (session["user_id"],)
        ).fetchall()
        avg = conn.execute(
            "SELECT AVG(score) AS a FROM grades WHERE student_id=?",
            (session["user_id"],)
        ).fetchone()["a"]
    else:
        grades = []
        avg = None

    return render_template(
        "dashboard.html",
        announcements=announcements,
        assignments=assignments,
        grades=grades,
        average=round(avg, 2) if avg is not None else None
    )


@app.route("/virtual-class")
@login_required
def virtual_class():
    conn=db(); role=session.get("role")
    if role=="student":
        active_sessions=conn.execute("""SELECT vs.*,c.name AS class_name,u.full_name AS teacher_name FROM virtual_sessions vs JOIN classes c ON c.id=vs.class_id JOIN users u ON u.id=vs.teacher_id WHERE vs.active=1 AND vs.class_id=? ORDER BY vs.id DESC""",(session.get("class_id"),)).fetchall()
    else:
        active_sessions=conn.execute("""SELECT vs.*,c.name AS class_name,u.full_name AS teacher_name FROM virtual_sessions vs JOIN classes c ON c.id=vs.class_id JOIN users u ON u.id=vs.teacher_id WHERE vs.active=1 ORDER BY vs.id DESC""").fetchall()
    selected_id=request.args.get("session_id",type=int); selected=None; messages=[]
    if selected_id:
        selected=conn.execute("""SELECT vs.*,c.name AS class_name,u.full_name AS teacher_name FROM virtual_sessions vs JOIN classes c ON c.id=vs.class_id JOIN users u ON u.id=vs.teacher_id WHERE vs.id=? AND vs.active=1""",(selected_id,)).fetchone()
        if selected and not can_access_virtual_session(selected): selected=None; flash("این کلاس مجازی برای حساب شما مجاز نیست.","error")
        if selected:
            messages=conn.execute("SELECT vm.*,users.full_name,users.role FROM virtual_messages vm JOIN users ON users.id=vm.sender_id WHERE vm.session_id=? ORDER BY vm.id ASC",(selected_id,)).fetchall(); touch_virtual_peer(selected_id,session["user_id"])
    can_start=role in STAFF_ROLES; classes=teacher_classes_for(session["user_id"]) if role=="teacher" else (conn.execute("SELECT * FROM classes ORDER BY name").fetchall() if can_start else [])
    files=conn.execute("SELECT vf.*,u.full_name AS uploader_name FROM virtual_files vf JOIN users u ON u.id=vf.uploader_id WHERE vf.session_id=? ORDER BY vf.id DESC", (selected_id,)).fetchall() if selected else []
    permission_requests=conn.execute("SELECT r.*,u.full_name FROM virtual_permission_requests r JOIN users u ON u.id=r.student_id WHERE r.session_id=? AND r.status='pending' ORDER BY r.id", (selected_id,)).fetchall() if selected and role in STAFF_ROLES else []
    granted=conn.execute("SELECT p.*,u.full_name FROM virtual_permissions p JOIN users u ON u.id=p.student_id WHERE p.session_id=? ORDER BY u.full_name", (selected_id,)).fetchall() if selected and role in STAFF_ROLES else []
    return render_template("virtual_class.html",active_sessions=active_sessions,selected=selected,messages=messages,can_start=can_start,classes=classes,files=files,permission_requests=permission_requests,granted=granted)

def can_access_virtual_session(row):
    return session.get("role") in STAFF_ROLES or (session.get("role")=="student" and session.get("class_id")==row["class_id"])

def touch_virtual_peer(session_id,user_id):
    now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"); c=db()
    c.execute("""INSERT INTO virtual_peers(session_id,user_id,joined_at,last_seen) VALUES(?,?,?,?) ON CONFLICT(session_id,user_id) DO UPDATE SET last_seen=excluded.last_seen""",(session_id,user_id,now,now)); c.commit()


def teacher_allowed_class(class_id):
    role=session.get("role")
    if role in {"admin", "manager"}: return True
    if role != "teacher": return False
    return db().execute("SELECT 1 FROM teacher_classes WHERE teacher_id=? AND class_id=?", (session["user_id"], class_id)).fetchone() is not None

def teacher_classes_for(user_id):
    return db().execute("SELECT c.* FROM classes c JOIN teacher_classes tc ON tc.class_id=c.id WHERE tc.teacher_id=? ORDER BY c.name", (user_id,)).fetchall()

@app.route("/virtual-class/start",methods=["POST"])
@login_required
@teacher_required
def start_virtual_class():
    class_id=request.form.get("class_id",type=int); title=request.form.get("title","").strip()
    if not class_id or not title: flash("کلاس و عنوان را وارد کنید.","error"); return redirect(url_for("virtual_class"))
    if not db().execute("SELECT id FROM classes WHERE id=?",(class_id,)).fetchone(): flash("کلاس انتخاب‌شده وجود ندارد.","error"); return redirect(url_for("virtual_class"))
    if not teacher_allowed_class(class_id): flash("این کلاس در حوزه تدریس شما نیست.","error"); return redirect(url_for("virtual_class"))
    active=db().execute("SELECT id FROM virtual_sessions WHERE class_id=? AND active=1",(class_id,)).fetchone()
    if active: flash("برای این کلاس یک کلاس مجازی فعال است.","error"); return redirect(url_for("virtual_class",session_id=active["id"]))
    db().execute("INSERT INTO virtual_sessions(class_id,title,teacher_id,created_at,active) VALUES(?,?,?,?,1)",(class_id,title,session["user_id"],datetime.now().strftime("%Y-%m-%d %H:%M"))); db().commit()
    sid=db().execute("SELECT last_insert_rowid() id").fetchone()["id"]; flash("کلاس مجازی شروع شد.","success"); return redirect(url_for("virtual_class",session_id=sid))

@app.route("/virtual-class/<int:session_id>/stop",methods=["POST"])
@login_required
@teacher_required
def stop_virtual_class(session_id):
    row=db().execute("SELECT teacher_id FROM virtual_sessions WHERE id=?",(session_id,)).fetchone()
    if not row: flash("کلاس پیدا نشد.","error")
    elif session.get("role")=="teacher" and row["teacher_id"]!=session["user_id"]: flash("فقط دبیر برگزارکننده می‌تواند کلاس را پایان دهد.","error")
    else: db().execute("UPDATE virtual_sessions SET active=0 WHERE id=?",(session_id,)); db().commit(); flash("کلاس مجازی پایان یافت.","success")
    return redirect(url_for("virtual_class"))

@app.route("/virtual-class/send",methods=["POST"])
@login_required
def send_virtual_message():
    is_json = request.is_json
    data = request.get_json(silent=True) or {} if is_json else request.form
    body = str(data.get("body","")).strip()
    sid = data.get("session_id")
    try: sid = int(sid)
    except (TypeError, ValueError): sid = None
    room = db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(sid,)).fetchone() if sid else None
    if not room or not can_access_virtual_session(room):
        return ({"error":"forbidden"},403) if is_json else redirect(url_for("virtual_class"))
    if not room["chat_enabled"] and session.get("role") not in STAFF_ROLES:
        return ({"error":"chat_disabled"},403) if is_json else redirect(url_for("virtual_class",session_id=sid))
    touch_virtual_peer(sid,session["user_id"])
    if not body:
        return ({"error":"empty"},400) if is_json else redirect(url_for("virtual_class",session_id=sid))
    created = datetime.now().strftime("%H:%M:%S")
    c=db()
    cur=c.execute("INSERT INTO virtual_messages(sender_id,body,created_at,class_id,session_id) VALUES(?,?,?,?,?)",
                  (session["user_id"],body,created,room["class_id"],sid))
    c.commit()
    msg_id=cur.lastrowid
    if is_json:
        return {"ok":True,"message":{"id":msg_id,"sender_id":session["user_id"],
                "sender_name":db().execute("SELECT full_name FROM users WHERE id=?",(session["user_id"],)).fetchone()["full_name"],
                "body":body,"created_at":created}}
    return redirect(url_for("virtual_class",session_id=sid))

@app.route("/virtual-class/<int:session_id>/messages")
@login_required
def virtual_messages_api(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    try: after=int(request.args.get("after",0))
    except (TypeError,ValueError): after=0
    rows=db().execute("""SELECT vm.id,vm.sender_id,vm.body,vm.created_at,u.full_name AS sender_name
                         FROM virtual_messages vm JOIN users u ON u.id=vm.sender_id
                         WHERE vm.session_id=? AND vm.id>? ORDER BY vm.id ASC LIMIT 100""",(session_id,after)).fetchall()
    return {"chat_enabled":bool(room["chat_enabled"]), "messages":[dict(r) for r in rows]}

@app.route("/virtual-class/<int:session_id>/settings", methods=["POST"])
@login_required
def virtual_settings(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    if session.get("role") not in STAFF_ROLES: return {"error":"staff only"},403
    if session.get("role")=="teacher" and room["teacher_id"]!=session["user_id"]:
        return {"error":"only host teacher"},403
    value=request.get_json(silent=True) or request.form
    enabled=1 if str(value.get("chat_enabled","1")).lower() in {"1","true","on","yes"} else 0
    db().execute("UPDATE virtual_sessions SET chat_enabled=? WHERE id=?",(enabled,session_id)); db().commit()
    return {"ok":True,"chat_enabled":bool(enabled)}

@app.route("/virtual-class/<int:session_id>/participants")
@login_required
def virtual_participants(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    touch_virtual_peer(session_id,session["user_id"]); rows=db().execute("SELECT vp.user_id,vp.last_seen,u.full_name,u.role FROM virtual_peers vp JOIN users u ON u.id=vp.user_id WHERE vp.session_id=?",(session_id,)).fetchall(); now=datetime.now(); out=[]
    for r in rows:
        try: age=(now-datetime.strptime(r["last_seen"],"%Y-%m-%d %H:%M:%S")).total_seconds()
        except Exception: age=999
        if age<=25: out.append({"id":r["user_id"],"name":r["full_name"],"role":r["role"]})
    return {"participants":out}

@app.route("/virtual-class/<int:session_id>/signal",methods=["POST"])
@login_required
def virtual_signal_send(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    d=request.get_json(silent=True) or {}; recipient=d.get("recipient_id"); typ=d.get("type"); payload=d.get("payload")
    if not isinstance(recipient,int) or typ not in {"offer","answer","candidate","leave","revoke_media","grant_media","whiteboard","screen_start","screen_stop"} or payload is None: return {"error":"invalid signal"},400
    u=db().execute("SELECT id,role,class_id FROM users WHERE id=?",(recipient,)).fetchone()
    if not u or (u["role"]=="student" and u["class_id"]!=room["class_id"]): return {"error":"recipient not in class"},403
    if typ == "offer" and session.get("role")=="student":
        allowed=db().execute("SELECT 1 FROM virtual_permissions WHERE session_id=? AND student_id=?",(session_id,session["user_id"])).fetchone()
        if not allowed: return {"error":"microphone/camera permission not granted"},403
    if typ in {"grant_media","revoke_media","screen_start","screen_stop","whiteboard"} and session.get("role") not in STAFF_ROLES: return {"error":"staff only"},403
    if typ in {"screen_start","screen_stop","whiteboard"} and session.get("role") == "teacher" and room["teacher_id"] != session["user_id"]: return {"error":"only host teacher"},403
    touch_virtual_peer(session_id,session["user_id"]); db().execute("INSERT INTO virtual_signals(session_id,sender_id,recipient_id,signal_type,payload,created_at) VALUES(?,?,?,?,?,?)",(session_id,session["user_id"],recipient,typ,json.dumps(payload,ensure_ascii=False),datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); db().commit(); return {"ok":True}

@app.route("/virtual-class/<int:session_id>/signals")
@login_required
def virtual_signal_poll(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    touch_virtual_peer(session_id,session["user_id"]); rows=db().execute("SELECT id,sender_id,signal_type,payload FROM virtual_signals WHERE session_id=? AND recipient_id=? AND delivered=0 ORDER BY id LIMIT 100",(session_id,session["user_id"])).fetchall()
    if rows:
        ids=[r["id"] for r in rows]; db().execute(f"UPDATE virtual_signals SET delivered=1 WHERE id IN ({','.join('?'*len(ids))})",ids); db().commit()
    return {"signals":[{"id":r["id"],"sender_id":r["sender_id"],"type":r["signal_type"],"payload":json.loads(r["payload"])} for r in rows]}

@app.route("/virtual-class/<int:session_id>/heartbeat",methods=["POST"])
@login_required
def virtual_heartbeat(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    touch_virtual_peer(session_id,session["user_id"]); return {"ok":True}

@app.route("/virtual-class/<int:session_id>/permission-request", methods=["POST"])
@login_required
def virtual_permission_request(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or session.get("role")!="student" or not can_access_virtual_session(room): return {"error":"forbidden"},403
    now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"); c=db(); c.execute("INSERT OR REPLACE INTO virtual_permission_requests(session_id,student_id,status,created_at) VALUES(?,?,?,?)",(session_id,session["user_id"],"pending",now)); c.commit(); return {"ok":True}

@app.route("/virtual-class/<int:session_id>/permission/<int:student_id>/<action>", methods=["POST"])
@login_required
@teacher_required
def virtual_permission_action(session_id,student_id,action):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    if session.get("role")=="teacher" and room["teacher_id"]!=session["user_id"]: return {"error":"only host teacher"},403
    c=db(); now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if action=="grant": c.execute("INSERT OR REPLACE INTO virtual_permissions(session_id,student_id,granted_at) VALUES(?,?,?)",(session_id,student_id,now)); c.execute("UPDATE virtual_permission_requests SET status='granted' WHERE session_id=? AND student_id=?",(session_id,student_id));
    elif action=="revoke": c.execute("DELETE FROM virtual_permissions WHERE session_id=? AND student_id=?",(session_id,student_id)); c.execute("UPDATE virtual_permission_requests SET status='revoked' WHERE session_id=? AND student_id=?",(session_id,student_id));
    else: return {"error":"bad action"},400
    c.commit(); return {"ok":True}

@app.route("/virtual-class/<int:session_id>/permissions")
@login_required
def virtual_permissions(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    if session.get("role") in STAFF_ROLES:
        req=db().execute("SELECT r.student_id,u.full_name,r.status FROM virtual_permission_requests r JOIN users u ON u.id=r.student_id WHERE r.session_id=? AND r.status='pending'",(session_id,)).fetchall()
        gr=db().execute("SELECT p.student_id,u.full_name FROM virtual_permissions p JOIN users u ON u.id=p.student_id WHERE p.session_id=?",(session_id,)).fetchall()
        return {"requests":[dict(x) for x in req],"granted":[dict(x) for x in gr]}
    g=db().execute("SELECT 1 FROM virtual_permissions WHERE session_id=? AND student_id=?",(session_id,session["user_id"])).fetchone(); return {"granted":bool(g)}

@app.route("/virtual-class/<int:session_id>/upload", methods=["POST"])
@login_required
@teacher_required
def virtual_upload(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    if session.get("role")=="teacher" and room["teacher_id"]!=session["user_id"]: return {"error":"only host teacher"},403
    f=request.files.get("file")
    if not f or not f.filename:
        return ({"error":"no_file"},400) if request.is_json else redirect(url_for("virtual_class",session_id=session_id))
    try:
        upload_dir=_upload_dir()
        old=db().execute("SELECT stored_name FROM virtual_files WHERE session_id=?",(session_id,)).fetchall()
        for row in old:
            try: (upload_dir / row["stored_name"]).unlink(missing_ok=True)
            except Exception: pass
        db().execute("DELETE FROM virtual_files WHERE session_id=?",(session_id,))
        safe_name=Path(f.filename).name
        stored=f"{uuid.uuid4().hex}_{safe_name}"
        f.save(upload_dir/stored)
        c=db(); c.execute("INSERT INTO virtual_files(session_id,uploader_id,original_name,stored_name,uploaded_at) VALUES(?,?,?,?,?)",
                           (session_id,session["user_id"],safe_name,stored,datetime.now().strftime("%Y-%m-%d %H:%M:%S"))); c.commit()
    except Exception as e:
        app.logger.exception("virtual upload failed")
        return {"error":"upload_failed","detail":str(e)},500
    if request.is_json or request.headers.get("X-Requested-With")=="XMLHttpRequest":
        row=db().execute("SELECT id,original_name,uploaded_at FROM virtual_files WHERE session_id=? ORDER BY id DESC LIMIT 1",(session_id,)).fetchone()
        return {"ok":True,"file":{"id":row["id"],"original_name":row["original_name"],"url":url_for("virtual_file",file_id=row["id"])}} 
    return redirect(url_for("virtual_class",session_id=session_id))

@app.route("/virtual-class/<int:session_id>/files")
@login_required
def virtual_files_api(session_id):
    room=db().execute("SELECT * FROM virtual_sessions WHERE id=? AND active=1",(session_id,)).fetchone()
    if not room or not can_access_virtual_session(room): return {"error":"forbidden"},403
    rows=db().execute("SELECT id,original_name,uploaded_at FROM virtual_files WHERE session_id=? ORDER BY id DESC",(session_id,)).fetchall()
    return {"files":[{"id":r["id"],"original_name":r["original_name"],"uploaded_at":r["uploaded_at"],
                     "url":url_for("virtual_file",file_id=r["id"])} for r in rows]}

@app.route("/virtual-class/file/<int:file_id>")
@login_required
def virtual_file(file_id):
    from flask import send_from_directory
    row=db().execute("SELECT vf.*,vs.class_id FROM virtual_files vf JOIN virtual_sessions vs ON vs.id=vf.session_id WHERE vf.id=?",(file_id,)).fetchone()
    if not row or not can_access_virtual_session(row): return "Forbidden",403
    return send_from_directory(_upload_dir(),row["stored_name"],as_attachment=False,download_name=row["original_name"])


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="صفحه پیدا نشد."), 404


@app.errorhandler(500)
def server_error(e):
    app.logger.exception("Unhandled application error", exc_info=e)
    return render_template("error.html", code=500, message="خطای داخلی سرور. جزئیات خطا در لاگ برنامه ثبت شده است."), 500


try:
    init_db()
except Exception:
    app.logger.exception("Database initialization deferred; will retry lazily on request")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
