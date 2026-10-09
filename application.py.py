import os, datetime, uuid
import boto3, pymysql
from flask import (Flask, request, redirect, url_for, session,
                   render_template, flash)
from werkzeug.security import generate_password_hash, check_password_hash
 
application = Flask(__name__)              # EB looks for "application"
application.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
 
DB_HOST = os.environ.get("DB_HOST"); DB_USER = os.environ.get("DB_USER", "admin")
DB_PASSWORD = os.environ.get("DB_PASSWORD"); DB_NAME = os.environ.get("DB_NAME", "teamnotes")
BUCKET_NAME = os.environ.get("BUCKET_NAME"); DDB_TABLE = os.environ.get("DDB_TABLE")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
 
s3 = boto3.client("s3", region_name=AWS_REGION)          # creds from IAM role
ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
 
def get_db():
    return pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, port=3306, cursorclass=pymysql.cursors.DictCursor, autocommit=True)
 
def log_activity(user_id, action, note_id=None, note_title="", detail=""):
    if not DDB_TABLE: return
    try:
        ddb.Table(DDB_TABLE).put_item(Item={
            "user_id": str(user_id),
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "action": action, "note_id": str(note_id) if note_id else "",
            "note_title": note_title, "detail": detail})
    except Exception as e:
        application.logger.warning("activity log failed: %s", e)
 
def recent_activity(user_id, limit=10):
    if not DDB_TABLE: return []
    from boto3.dynamodb.conditions import Key
    r = ddb.Table(DDB_TABLE).query(KeyConditionExpression=Key("user_id").eq(str(user_id)),
        ScanIndexForward=False, Limit=limit)
    return r.get("Items", [])
 
def current_user(): return session.get("user_id")
 
@application.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        u = request.form["username"].strip(); p = request.form["password"]
        conn = get_db()
        with conn.cursor() as c:
            c.execute("SELECT id FROM users WHERE username=%s", (u,))
            if c.fetchone():
                flash("Username already taken."); return redirect(url_for("register"))
            c.execute("INSERT INTO users (username,password_hash) VALUES (%s,%s)",
                      (u, generate_password_hash(p)))
        conn.close(); flash("Registered! Please log in."); return redirect(url_for("login"))
    return render_template("register.html")
 
@application.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip(); p = request.form["password"]
        conn = get_db()
        with conn.cursor() as c:
            c.execute("SELECT * FROM users WHERE username=%s", (u,)); user = c.fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], p):
            session["user_id"] = user["id"]; session["username"] = user["username"]
            log_activity(user["id"], "LOGGED_IN", detail="User logged in")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.")
    return render_template("login.html")
 
@application.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))
 
@application.route("/")
def dashboard():
    if not current_user(): return redirect(url_for("login"))
    uid = current_user(); q = request.args.get("q","").strip(); status = request.args.get("status","").strip()
    sql = "SELECT * FROM notes WHERE user_id=%s"; params=[uid]
    if q: sql += " AND (title LIKE %s OR body LIKE %s)"; params += [f"%{q}%", f"%{q}%"]
    if status: sql += " AND status=%s"; params.append(status)
    sql += " ORDER BY created_at DESC"
    conn = get_db()
    with conn.cursor() as c:
        c.execute(sql, params); notes = c.fetchall()
        c.execute("SELECT status,COUNT(*) c FROM notes WHERE user_id=%s GROUP BY status",(uid,))
        counts = {r["status"]: r["c"] for r in c.fetchall()}
    conn.close()
    return render_template("dashboard.html", notes=notes, counts=counts,
                           activity=recent_activity(uid), q=q, status=status)
 
@application.route("/note/new", methods=["POST"])
def new_note():
    if not current_user(): return redirect(url_for("login"))
    uid = current_user(); title = request.form["title"].strip()
    body_ = request.form.get("body","").strip(); status = request.form.get("status","open")
    due = request.form.get("due_date") or None
    s3_key = ""; filename = ""; f = request.files.get("attachment")
    if f and f.filename:
        filename = f.filename; s3_key = f"user{uid}/{uuid.uuid4().hex}_{filename}"
        s3.upload_fileobj(f, BUCKET_NAME, s3_key)
    conn = get_db()
    with conn.cursor() as c:
        c.execute("""INSERT INTO notes (user_id,title,body,status,due_date,s3_key,filename)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                  (uid,title,body_,status,due,s3_key,filename))
        note_id = c.lastrowid
    conn.close(); log_activity(uid,"CREATED",note_id,title,f"Created note '{title}'")
    return redirect(url_for("dashboard"))
 
@application.route("/note/<int:note_id>/update", methods=["POST"])
def update_note(note_id):
    if not current_user(): return redirect(url_for("login"))
    uid = current_user(); status = request.form.get("status","open")
    conn = get_db()
    with conn.cursor() as c:
        c.execute("UPDATE notes SET status=%s WHERE id=%s AND user_id=%s",(status,note_id,uid))
        c.execute("SELECT title FROM notes WHERE id=%s",(note_id,)); row=c.fetchone()
    conn.close(); log_activity(uid,"UPDATED",note_id,row["title"] if row else "",f"Set status to {status}")
    return redirect(url_for("dashboard"))
 
@application.route("/note/<int:note_id>/delete", methods=["POST"])
def delete_note(note_id):
    if not current_user(): return redirect(url_for("login"))
    uid = current_user(); conn = get_db()
    with conn.cursor() as c:
        c.execute("SELECT * FROM notes WHERE id=%s AND user_id=%s",(note_id,uid)); note=c.fetchone()
        if note:
            if note.get("s3_key"):
                try: s3.delete_object(Bucket=BUCKET_NAME, Key=note["s3_key"])
                except Exception as e: application.logger.warning("s3 delete failed: %s", e)
            c.execute("DELETE FROM notes WHERE id=%s AND user_id=%s",(note_id,uid))
    conn.close()
    if note: log_activity(uid,"DELETED",note_id,note["title"],f"Deleted note '{note['title']}'")
    return redirect(url_for("dashboard"))
 
@application.route("/note/<int:note_id>/file")
def download_file(note_id):
    if not current_user(): return redirect(url_for("login"))
    conn = get_db()
    with conn.cursor() as c:
        c.execute("SELECT s3_key FROM notes WHERE id=%s AND user_id=%s",(note_id,current_user()))
        row=c.fetchone()
    conn.close()
    if not row or not row["s3_key"]:
        flash("No file."); return redirect(url_for("dashboard"))
    url = s3.generate_presigned_url("get_object",
        Params={"Bucket":BUCKET_NAME,"Key":row["s3_key"]}, ExpiresIn=300)
    return redirect(url)
 
@application.route("/health")
def health(): return "ok", 200
 
if __name__ == "__main__":
    # Port-agnostic: EB may proxy to 8080 or 5000 depending on the platform.
    port = int(os.environ.get("PORT", 8080))
    application.run(host="0.0.0.0", port=port)