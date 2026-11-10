import os, uuid, sqlite3
from flask import Flask, render_template, request, redirect, url_for, g, flash, session

DB = "lostfound.db"
UPLOADS = os.path.join("static", "uploads")
ADMIN_PASSWORD = "admin4747"        

app = Flask(__name__)
app.secret_key = "change-this-secret"


# ---------- DATABASE ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    os.makedirs(UPLOADS, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        type TEXT NOT NULL CHECK (type IN ('lost','found')),
        category_id INTEGER NOT NULL,
        location TEXT,
        date_reported TEXT DEFAULT CURRENT_DATE,
        contact_name TEXT NOT NULL,
        contact_info TEXT NOT NULL,
        resolved INTEGER DEFAULT 0,
        FOREIGN KEY (category_id) REFERENCES categories(id)
    );
    """)
    # NEW: add a "photo" column to the old database (skipped if it already exists)
    try:
        db.execute("ALTER TABLE items ADD COLUMN photo TEXT")
    except sqlite3.OperationalError:
        pass
    if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        for c in ["Phone", "Wallet / Cards", "Keys", "Bag", "Clothing", "Electronics", "Documents", "Other"]:
            db.execute("INSERT INTO categories (name) VALUES (?)", (c,))
    db.commit()
    db.close()


# Makes "is_admin" available in every template
@app.context_processor
def inject_admin():
    return {"is_admin": session.get("admin", False)}


# ---------- BROWSE + SEARCH ----------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    t = request.args.get("type", "")
    show_resolved = request.args.get("resolved") == "1"

    sql = """SELECT items.*, categories.name AS category
             FROM items JOIN categories ON items.category_id = categories.id
             WHERE resolved = ?"""
    params = [1 if show_resolved else 0]
    if q:
        sql += " AND (title LIKE ? OR description LIKE ? OR location LIKE ?)"
        params += [f"%{q}%"] * 3
    if t in ("lost", "found"):
        sql += " AND type = ?"
        params.append(t)
    sql += " ORDER BY date_reported DESC, items.id DESC"

    items = get_db().execute(sql, params).fetchall()
    return render_template("index.html", items=items, q=q, t=t, show_resolved=show_resolved)


# ---------- REPORT AN ITEM (now with photo) ----------
@app.route("/add", methods=["GET", "POST"])
def add():
    db = get_db()
    categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        if not f["title"].strip() or not f["contact_name"].strip() or not f["contact_info"].strip():
            flash("Please fill in all required fields.")
            return render_template("add.html", categories=categories)

        # NEW: save the photo (if any) under a random file name
        photo_name = None
        photo = request.files.get("photo")
        if photo and photo.filename and "." in photo.filename:
            ext = photo.filename.rsplit(".", 1)[-1].lower()
            if ext in ("png", "jpg", "jpeg", "gif", "webp"):
                photo_name = uuid.uuid4().hex + "." + ext
                photo.save(os.path.join(UPLOADS, photo_name))

        cur = db.execute(
            """INSERT INTO items (title, description, type, category_id, location,
               contact_name, contact_info, photo) VALUES (?,?,?,?,?,?,?,?)""",
            (f["title"], f["description"], f["type"], f["category_id"],
             f["location"], f["contact_name"], f["contact_info"], photo_name),
        )
        db.commit()
        flash("Item reported successfully!")
        return redirect(url_for("item", item_id=cur.lastrowid))
    return render_template("add.html", categories=categories)


# ---------- ITEM PAGE + POSSIBLE MATCHES ----------
@app.route("/item/<int:item_id>")
def item(item_id):
    db = get_db()
    row = db.execute(
        """SELECT items.*, categories.name AS category FROM items
           JOIN categories ON items.category_id = categories.id WHERE items.id = ?""",
        (item_id,),
    ).fetchone()
    if row is None:
        return "Item not found", 404

    # NEW: matches = opposite type (lost <-> found), same category, still open
    opposite = "found" if row["type"] == "lost" else "lost"
    matches = db.execute(
        """SELECT * FROM items WHERE type = ? AND category_id = ?
           AND resolved = 0 AND id != ? ORDER BY id DESC LIMIT 3""",
        (opposite, row["category_id"], item_id),
    ).fetchall()
    return render_template("item.html", item=row, matches=matches)


# ---------- ADMIN LOGIN ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["password"] == ADMIN_PASSWORD:
            session["admin"] = True
            flash("Logged in as admin.")
            return redirect(url_for("index"))
        flash("Wrong password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("admin", None)
    flash("Logged out.")
    return redirect(url_for("index"))


# ---------- ADMIN-ONLY ACTIONS ----------
@app.route("/resolve/<int:item_id>", methods=["POST"])
def resolve(item_id):
    if not session.get("admin"):
        return redirect(url_for("login"))
    db = get_db()
    db.execute("UPDATE items SET resolved = 1 WHERE id = ?", (item_id,))
    db.commit()
    flash("Item marked as returned/claimed.")
    return redirect(url_for("index"))


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete(item_id):
    if not session.get("admin"):
        return redirect(url_for("login"))
    db = get_db()
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    flash("Item deleted.")
    return redirect(url_for("index"))


# ---------- ADMIN DASHBOARD ----------
@app.route("/admin")
def admin():
    if not session.get("admin"):
        return redirect(url_for("login"))
    db = get_db()

    # Stat cards: each one is a simple COUNT query
    stats = {
        "total":    db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "lost":     db.execute("SELECT COUNT(*) FROM items WHERE type='lost' AND resolved=0").fetchone()[0],
        "found":    db.execute("SELECT COUNT(*) FROM items WHERE type='found' AND resolved=0").fetchone()[0],
        "returned": db.execute("SELECT COUNT(*) FROM items WHERE resolved=1").fetchone()[0],
    }

    # Table of every item
    items = db.execute(
        """SELECT items.*, categories.name AS category FROM items
           JOIN categories ON items.category_id = categories.id
           ORDER BY items.id DESC"""
    ).fetchall()
    return render_template("admin.html", stats=stats, items=items)



if __name__ == "__main__":
    init_db()
    app.run(debug=True)
