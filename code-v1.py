
import sqlite3
import hashlib
import getpass
import json
import requests
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import time

# --- CONFIGURATION ---
DB_NAME = "school_support.db"
API_KEY = "sk_vuIaBgMibteEX3Z0gLoM4DHfE8cDfQOj"
API_URL = "https://gen.pollinations.ai/v1/chat/completions"

# Maps numeric Role IDs (used in DB/Excel) to Human-Readable Names
ROLE_MAP = {
    1: "Admin",
    2: "TSS Leader",
    3: "TSS",
    4: "Staff"
}

# --- TIMEZONE HELPER ---
def get_hk_time():
    """
    Returns current time in Hong Kong Time (UTC+8).
    Google Colab defaults to UTC, so this correction is vital for accuracy.
    """
    return datetime.utcnow() + timedelta(hours=8)

def format_time(dt_obj):
    """Formats datetime object to string for display/storage."""
    if dt_obj is None: return ""
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")

# --- DATABASE INITIALIZATION ---
def init_db():
    """
    Creates tables if they don't exist.
    Implements Data Normalization via numeric Role IDs.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Users Table
    # is_active: 0 = Deactivated , 1 = Active
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE,
                        password TEXT,
                        real_name TEXT,
                        role_id INTEGER,
                        is_first_login INTEGER DEFAULT 1,
                        is_active INTEGER DEFAULT 1)''')

    # Active Tickets Table
    # ai_summary: enable zero-request archiving later
    # status: New, Assigned, In Progress, Resolved, Cancelled, Reassign_Req
    cursor.execute('''CREATE TABLE IF NOT EXISTS tickets (
                        ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        creator_id INTEGER,
                        assigned_tss_id INTEGER,
                        main_category TEXT,
                        sub_category TEXT,
                        priority TEXT,
                        description TEXT,
                        location TEXT,
                        tss_remarks TEXT DEFAULT '',
                        ai_summary TEXT,
                        status TEXT DEFAULT 'New',
                        created_at DATETIME,
                        resolved_at DATETIME,
                        FOREIGN KEY(creator_id) REFERENCES users(user_id))''')

    # Archived Tickets
    # Drops original description to save space, keeps AI summary & metadata
    cursor.execute('''CREATE TABLE IF NOT EXISTS archived_tickets (
                        archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        original_ticket_id INTEGER,
                        ai_summary TEXT,
                        main_category TEXT,
                        sub_category TEXT,
                        year INTEGER,
                        final_status TEXT,
                        archived_date DATETIME)''')

    # Default Admin creation
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        def_pw = hashlib.sha256("24750331".encode()).hexdigest()
        cursor.execute("INSERT INTO users (username, password, real_name, role_id, is_first_login) VALUES (?, ?, ?, ?, ?)",
                      ('admin', def_pw, 'System Administrator', 1, 1))

    conn.commit()
    conn.close()

# --- UTILITY FUNCTIONS ---
def normalize_user(username):
    # lowercase and removes spaces.
    if not username: return ""
    return str(username).lower().replace(" ", "")

def hash_pw(password):
    # SHA-256 Hashing for security
    return hashlib.sha256(password.encode()).hexdigest()

def check_ticket_exists(ticket_id):
    # Verifies ticket existence before any operation to prevent crashes.
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM tickets WHERE ticket_id = ?", (ticket_id,))
    exists = cursor.fetchone()[0] > 0
    conn.close()
    if not exists:
        print(f"\n[!] Error: Ticket ID {ticket_id} not found.")
    return exists

def print_header(title):
    print(f"\n{'='*60}")
    print(f"{title.center(60)}")
    print(f"{'='*60}")

def get_choice(options):
    # Menu handler. '0' is always used for Back/Exit.
    for key, value in options.items():
        if key != "0": print(f"{key}. {value}")
    if "0" in options: print(f"0. {options['0']}")

    while True:
        choice = input("Select: ").strip()
        if choice in options: return choice
        print("Invalid selection. Try again.")

def display_table(headers, rows):
    """
    Renders a clean text-based table.
    Adjusts column width dynamically based on content.
    """
    if not rows:
        print("\n[No records found]")
        return

    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
    widths = [w + 2 for w in widths] # Add padding

    header_str = "".join(h.ljust(w) for h, w in zip(headers, widths))
    print("-" * len(header_str))
    print(header_str)
    print("-" * len(header_str))
    for row in rows:
        print("".join(str(val).ljust(w) for val, w in zip(row, widths)))
    print("-" * len(header_str))

# --- AI MODULE (WITH ERROR FALLBACK) ---
def call_ai_api(system_prompt, user_message):
    """
    [FIX-03] Calls Pollinations.ai API with Auto-Retry Logic.
    """
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
    payload = {
        "model": "openai",
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_message}]
    }

    # Retry Logic (Max 3 attempts)
    for attempt in range(3):
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content']
        except:
            time.sleep(1) # Wait 1 sec before retry

    return "AI_ERROR" # Fallback if all attempts fail

def ai_self_help_phase1(description):
    """
    Phase 1: Generates simple troubleshooting for Staff.
    Prompt ensures non-technical language (Physically check cables, etc.)
    """
    sys = "You are a helpful school IT assistant. Provide exactly ONE simple, non-technical troubleshooting step the user can try physically (e.g. check cables). Max 20 words. No jargon."
    res = call_ai_api(sys, description)
    return "Please check connections and restart device." if res == "AI_ERROR" else res

def ai_auto_tag_phase2(description):
    """
    Phase 2: 2-Layer Categorization & Priority Tagging.
    Returns JSON. Uses Default values if AI fails.
    """
    sys = """You are an IT Classifier. Return ONLY a JSON: {"main_cat": "...", "sub_cat": "...", "priority": "..."}.
    Taxonomy:
    1. Hardware: Projector, Computer, Printer, Sound, Peripherals, Other_Hardware.
    2. Software: OS, Office, Apps, Browser, Other_Software.
    3. Network: Wi-Fi, LAN, Account, Other_Network.
    4. General: Admin, Furniture, Other_General.
    Priority Rules: High (Safety/Exam/Server), Medium (Work stoppage), Low (Minor)."""

    res = call_ai_api(sys, description)

    # Fallback Defaults
    def_val = ("General", "Other_General", "Medium")

    if res == "AI_ERROR": return def_val

    try:
        # Clean JSON string (remove markdown)
        clean = res.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        return data.get("main_cat", "General"), data.get("sub_cat", "Other_General"), data.get("priority", "Medium")
    except:
        return def_val

def ai_generate_resolution_summary(description, remarks):
    """
    Phase 3: Archiving Summary.
    Generated when ticket is Resolved.
    """
    sys = "You are an IT Archivist. Summarize ticket into: '[Issue] ... [Action] ...'. Retain technical keywords. Max 30 words."
    text = f"Issue: {description}. Remarks: {remarks}"
    res = call_ai_api(sys, text)
    return "[System] AI Summary Unavailable" if res == "AI_ERROR" else res

# --- ANALYTICS MODULE (VISUALS) ---
def show_analytics():
    """Generates Pie/Bar charts using Matplotlib."""
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT main_category, priority FROM tickets WHERE status != 'Resolved'", conn)
    conn.close()

    if df.empty:
        print("Not enough active data for analytics.")
        return

    print_header("SYSTEM ANALYTICS (ACTIVE TICKETS)")

    # Create Layout
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Pie Chart (Categories)
    if not df['main_category'].empty:
        main_counts = df['main_category'].value_counts()
        ax1.pie(main_counts, labels=main_counts.index, autopct='%1.1f%%', startangle=90)
        ax1.set_title('Main Categories')

    # Bar Chart (Priority)
    if not df['priority'].empty:
        pri_counts = df['priority'].value_counts()
        ax2.bar(pri_counts.index, pri_counts.values, color=['red', 'orange', 'green'])
        ax2.set_title('Priority Distribution')

    plt.tight_layout()
    plt.show()

# --- DATA CENTER (ARCHIVING) ---
def run_zero_request_archive(months):
    """
    Scheme B Archiving Logic.
    Moves data without calling AI (since Summary is generated at Resolution).
    """
    cutoff = get_hk_time() - timedelta(days=months * 30)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Select tickets older than cutoff
    # Uses ai_summary if exists, else falls back to description
    cursor.execute("""
        SELECT ticket_id, COALESCE(ai_summary, description), main_category, sub_category, status, created_at
        FROM tickets
        WHERE status IN ('Resolved', 'Cancelled') AND created_at < ?
    """, (format_time(cutoff),))

    rows = cursor.fetchall()
    count = 0

    for r in rows:
        tid, summary, m_cat, s_cat, stat, created = r
        year = created[:4]

        # Insert to Archive
        cursor.execute("INSERT INTO archived_tickets (original_ticket_id, ai_summary, main_category, sub_category, year, final_status, archived_date) VALUES (?,?,?,?,?,?,?)",
                       (tid, summary, m_cat, s_cat, int(year), stat, format_time(get_hk_time())))

        # Delete from Active
        cursor.execute("DELETE FROM tickets WHERE ticket_id = ?", (tid,))
        count += 1

    conn.commit()
    conn.close()
    print(f"\n[Success] Archived {count} records.")

# --- SHARED WORKFLOWS ---
def open_ticket_flow(user_id):
    """
    Unified Ticket Creation Flow (Used by Staff, TSS, Leader).
    Includes AI Self-Help (Phase 1).
    """
    print("\n--- NEW TICKET ---")
    loc = input("Location: ")
    desc = input("Description: ")

    print("Analyzing...")
    # AI Suggestion (Hidden as 'System Suggestion')
    suggestion = ai_self_help_phase1(desc)
    print(f"\n[System Suggestion]: {suggestion}")

    # Self-Resolution Check
    if input("Did this solve the problem? (y/n): ").lower() == 'y':
        m_cat, s_cat, pri = ai_auto_tag_phase2(desc)
        conn = sqlite3.connect(DB_NAME)
        # Directly save as Resolved
        conn.execute("""
            INSERT INTO tickets (creator_id, main_category, sub_category, priority, description, location, status, created_at, resolved_at, ai_summary)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, m_cat, s_cat, pri, desc, loc, 'Resolved', format_time(get_hk_time()), format_time(get_hk_time()), "[Self-Help] User resolved via System Suggestion."))
        conn.commit()
        conn.close()
        print("Ticket saved as Self-Resolved.")
        return

    # Formal Creation (Phase 2)
    m_cat, s_cat, pri = ai_auto_tag_phase2(desc)
    print(f"Ticket Created -> Category: {m_cat} | Priority: {pri}")

    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO tickets (creator_id, main_category, sub_category, priority, description, location, created_at) VALUES (?,?,?,?,?,?,?)",
                 (user_id, m_cat, s_cat, pri, desc, loc, format_time(get_hk_time())))
    conn.commit()
    conn.close()

def solve_ticket_flow(user_id, role_id):
    """
    Unified Resolution Flow (Used by TSS & Leader).
    Includes Reassignment Logic.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Filter tickets based on Role
    if role_id == 2: # Leader sees ALL pending
        query = "SELECT ticket_id, status, description FROM tickets WHERE status != 'Resolved'"
        params = ()
    else: # TSS sees only assigned
        query = "SELECT ticket_id, status, description FROM tickets WHERE assigned_tss_id = ? AND status != 'Resolved'"
        params = (user_id,)

    rows = cursor.execute(query, params).fetchall()
    display_table(["ID", "Status", "Desc"], rows)

    tid = input("Enter Ticket ID: ")
    if not check_ticket_exists(tid): conn.close(); return

    # Fetch current remarks
    curr = cursor.execute("SELECT tss_remarks, description FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
    old_rem, desc = curr

    # Menu Options
    opts = {"1": "Update Status / Solve", "2": "Add Remark Only"}
    if role_id == 3: opts["3"] = "REQUEST REASSIGNMENT" # Only TSS needs to request this

    action = get_choice(opts)

    if action == "3": # Reassignment Request
        reason = input("Reason for dropping ticket: ")
        timestamp = format_time(get_hk_time())
        new_rem = f"{old_rem}\n[{timestamp}] REASSIGN REQ: {reason}"

        # Unassign user and set special status
        cursor.execute("UPDATE tickets SET status='Reassign_Req', assigned_tss_id=NULL, tss_remarks=? WHERE ticket_id=?", (new_rem, tid))
        print("Ticket returned to Leader Pool.")

    elif action == "1": # Solve / Progress
        stat_choice = get_choice({"1": "In Progress", "2": "Resolved"})
        note = input("Remark: ")
        timestamp = format_time(get_hk_time())
        new_rem = f"{old_rem}\n[{timestamp}] {ROLE_MAP[role_id]}: {note}"

        if stat_choice == "1":
            cursor.execute("UPDATE tickets SET status='In Progress', tss_remarks=? WHERE ticket_id=?", (new_rem, tid))
        else:
            # Resolution -> Generate Summary
            print("Generating Closing Summary...")
            summary = ai_generate_resolution_summary(desc, new_rem)
            cursor.execute("UPDATE tickets SET status='Resolved', tss_remarks=?, ai_summary=?, resolved_at=? WHERE ticket_id=?",
                           (new_rem, summary, format_time(get_hk_time()), tid))

    else: # Remark Only
        note = input("Remark: ")
        timestamp = format_time(get_hk_time())
        new_rem = f"{old_rem}\n[{timestamp}] {ROLE_MAP[role_id]}: {note}"
        cursor.execute("UPDATE tickets SET tss_remarks=? WHERE ticket_id=?", (new_rem, tid))

    conn.commit()
    conn.close()
    print("Update Saved.")

# --- MENUS: ADMIN ---
def admin_menu():
    while True:
        print_header("ADMIN PANEL")
        choice = get_choice({
            "1": "Add New User",
            "2": "Bulk Import Users (Excel)",
            "3": "Search User / List All",
            "4": "Delete User (Safe Mode)",
            "5": "Reset Password",
            "6": "View All Tickets",
            "7": "Data Management Center",
            "8": "System Statistics (Visual)",
            "0": "Logout"
        })

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if choice == "1":
            u = normalize_user(input("Username: "))
            r = input("Real Name: ")
            print("Roles: 1=Admin, 2=Leader, 3=TSS, 4=Staff")
            rid = input("Role ID: ")
            try:
                cursor.execute("INSERT INTO users (username, password, real_name, role_id) VALUES (?,?,?,?)",
                               (u, hash_pw("24750331"), r, rid))
                conn.commit()
                print(f"User {u} created.")
            except: print("Error: Username already exists.")

        elif choice == "2":
            # Bulk Import with Confirmation
            path = input("Enter Excel filename: ")
            if os.path.exists(path):
                try:
                    df = pd.read_excel(path)
                    print("\n--- PREVIEW DATA ---")
                    print(df[['username', 'real_name', 'role_id']].head(10)) # Show first 10

                    if input("\nConfirm Import? (y/n): ").lower() == 'y':
                        count = 0
                        skipped = 0
                        for _, row in df.iterrows():
                            try:
                                u_norm = normalize_user(row['username'])
                                cursor.execute("INSERT INTO users (username, password, real_name, role_id) VALUES (?,?,?,?)",
                                               (u_norm, hash_pw("24750331"), row['real_name'], row['role_id']))
                                count += 1
                            except: skipped += 1
                        conn.commit()
                        print(f"Imported: {count} | Skipped (Duplicates): {skipped}")
                    else: print("Import Cancelled.")
                except Exception as e: print(f"Error reading file: {e}")
            else: print("File not found.")

        elif choice == "3":
            # Search / List All
            key = input("Search Name (Enter for All): ").lower()
            query = "SELECT username, real_name, role_id, is_active FROM users"
            params = ()

            if key:
                query += " WHERE username LIKE ? OR real_name LIKE ?"
                params = (f"%{key}%", f"%{key}%")

            rows = cursor.execute(query, params).fetchall()
            # Convert Role ID to Name
            disp = [[r[0], r[1], ROLE_MAP.get(r[2]), "Active" if r[3] else "Inactive"] for r in rows]
            display_table(["Username", "Real Name", "Role", "Status"], disp)

        elif choice == "4":
            # Safe Delete (Display List First)
            print("\n--- USER LIST ---")
            rows = cursor.execute("SELECT username, real_name, role_id FROM users WHERE is_active=1").fetchall()
            disp = [[r[0], r[1], ROLE_MAP.get(r[2])] for r in rows]
            display_table(["Username", "Real Name", "Role"], disp)

            tgt = normalize_user(input("Username to delete: "))
            user = cursor.execute("SELECT user_id, real_name, role_id FROM users WHERE username=?", (tgt,)).fetchone()

            if user:
                print(f"\nTARGET: {user[1]} ({ROLE_MAP.get(user[2])})")
                # Check Dependencies
                act_tix = cursor.execute("SELECT count(*) FROM tickets WHERE (creator_id=? OR assigned_tss_id=?) AND status!='Resolved'", (user[0], user[0])).fetchone()[0]

                if act_tix > 0:
                    print(f"Cannot delete: User linked to {act_tix} active tickets.")
                else:
                    if input("Type 'CONFIRM' to deactivate: ") == "CONFIRM":
                        cursor.execute("UPDATE users SET is_active=0 WHERE user_id=?", (user[0],))
                        conn.commit()
                        print("User Deactivated.")
            else: print("User not found.")

        elif choice == "5":
            tgt = normalize_user(input("Username: "))
            cursor.execute("UPDATE users SET password=?, is_first_login=1 WHERE username=?", (hash_pw("24750331"), tgt))
            conn.commit()
            print("Password Reset.")

        elif choice == "6":
            # View All Tickets (Left Join for Real Names)
            q = """
                SELECT t.ticket_id, u1.real_name as Creator, u2.real_name as Tech, t.status, t.description
                FROM tickets t
                LEFT JOIN users u1 ON t.creator_id = u1.user_id
                LEFT JOIN users u2 ON t.assigned_tss_id = u2.user_id
            """
            display_table(["ID", "Creator", "Tech", "Status", "Desc"], cursor.execute(q).fetchall())

        elif choice == "7": conn.close(); data_center_menu(); conn = sqlite3.connect(DB_NAME)
        elif choice == "8": conn.close(); show_analytics(); conn = sqlite3.connect(DB_NAME)
        elif choice == "0": break
        conn.close()

# --- DATA CENTER ---
def data_center_menu():
    while True:
        print_header("DATA MANAGEMENT CENTER")
        choice = get_choice({
            "1": "Run 6-Month Auto-Archive",
            "2": "Run 1-Year Auto-Archive",
            "3": "Manual Full Archive",
            "4": "View & Search Archives",
            "5": "Export Data (Excel)",
            "0": "Back"
        })

        if choice == "1": run_zero_request_archive(6)
        elif choice == "2": run_zero_request_archive(12)
        elif choice == "3": run_zero_request_archive(0)
        elif choice == "4":
            key = input("Keyword: ").strip()
            conn = sqlite3.connect(DB_NAME)
            df = pd.read_sql_query(f"SELECT year, main_category, final_status, ai_summary FROM archived_tickets WHERE ai_summary LIKE '%{key}%'", conn)
            conn.close()
            print(df if not df.empty else "No records.")
        elif choice == "5":
            conn = sqlite3.connect(DB_NAME)
            # Export with Real Names
            q_act = """SELECT t.ticket_id, u.real_name as creator, t.status, t.description FROM tickets t JOIN users u ON t.creator_id=u.user_id"""
            try:
                with pd.ExcelWriter('school_report.xlsx') as writer:
                    pd.read_sql_query(q_act, conn).to_excel(writer, sheet_name='Active')
                    pd.read_sql_query("SELECT * FROM archived_tickets", conn).to_excel(writer, sheet_name='Archive')
                print("Exported to 'school_report.xlsx'")
            except: print("Export Error.")
            conn.close()
        elif choice == "0": break

# --- MENUS: LEADER & TSS ---
def leader_menu(user_id):
    while True:
        print_header("TSS LEADER DASHBOARD")
        choice = get_choice({
            "1": "View Unassigned Pool",
            "2": "Assign Ticket",
            "3": "Update / Solve Ticket",
            "4": "Open New Ticket",
            "5": "View Team Performance",
            "0": "Logout"
        })

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if choice == "1":
            # Shows 'New' and 'Reassign_Req' (Returned tickets)
            q = "SELECT ticket_id, status, priority, description FROM tickets WHERE status IN ('New', 'Reassign_Req')"
            display_table(["ID", "Status", "Pri", "Desc"], cursor.execute(q).fetchall())

        elif choice == "2":
            tid = input("Ticket ID: ")
            if not check_ticket_exists(tid): continue

            # Show Active Techs
            print("\n--- ACTIVE STAFF ---")
            techs = cursor.execute("SELECT user_id, real_name FROM users WHERE role_id IN (2,3) AND is_active=1").fetchall()
            display_table(["ID", "Name"], techs)

            assignee = input("Assign to ID: ")
            cursor.execute("UPDATE tickets SET status='Assigned', assigned_tss_id=? WHERE ticket_id=?", (assignee, tid))
            conn.commit()
            print("Assigned.")

        elif choice == "3": conn.close(); solve_ticket_flow(user_id, 2); conn = sqlite3.connect(DB_NAME)
        elif choice == "4": conn.close(); open_ticket_flow(user_id); conn = sqlite3.connect(DB_NAME)

        elif choice == "5":
            # Leader Stats: Completed jobs count
            q = """
                SELECT u.real_name, COUNT(t.ticket_id)
                FROM users u
                LEFT JOIN tickets t ON u.user_id = t.assigned_tss_id
                WHERE t.status = 'Resolved' AND u.role_id IN (2,3)
                GROUP BY u.user_id
            """
            rows = cursor.execute(q).fetchall()
            display_table(["Tech Name", "Tickets Solved"], rows)

        elif choice == "0": break
        conn.close()

def tss_menu(user_id):
    while True:
        print_header("TSS WORKSPACE")
        choice = get_choice({
            "1": "My Assigned Tickets",
            "2": "Update / Solve Ticket",
            "3": "Request Reassignment",
            "4": "Open New Ticket",
            "0": "Logout"
        })

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if choice == "1":
            q = "SELECT ticket_id, priority, status, description FROM tickets WHERE assigned_tss_id=? AND status!='Resolved'"
            display_table(["ID", "Pri", "Stat", "Desc"], cursor.execute(q, (user_id,)).fetchall())

        elif choice == "2": conn.close(); solve_ticket_flow(user_id, 3); conn = sqlite3.connect(DB_NAME)

        elif choice == "3":
            # Reassign Logic
            tid = input("Ticket ID to drop: ")
            if not check_ticket_exists(tid): continue

            # Verify ownership
            own = cursor.execute("SELECT assigned_tss_id FROM tickets WHERE ticket_id=?", (tid,)).fetchone()
            if own and own[0] == user_id:
                reason = input("Reason: ")
                note = f"[{format_time(get_hk_time())}] REASSIGN REQ: {reason}"
                cursor.execute("UPDATE tickets SET status='Reassign_Req', assigned_tss_id=NULL, tss_remarks=tss_remarks || ? WHERE ticket_id=?",
                               ("\n" + note, tid))
                conn.commit()
                print("Ticket returned to pool.")
            else: print("You are not assigned to this ticket.")

        elif choice == "4": conn.close(); open_ticket_flow(user_id); conn = sqlite3.connect(DB_NAME)
        elif choice == "0": break
        conn.close()

def staff_menu(user_id, real_name):
    while True:
        print_header(f"STAFF: {real_name}")
        choice = get_choice({
            "1": "Report Issue (AI Assisted)",
            "2": "View My Tickets",
            "3": "Cancel Ticket",
            "0": "Logout"
        })

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if choice == "1": conn.close(); open_ticket_flow(user_id); conn = sqlite3.connect(DB_NAME)
        elif choice == "2":
            q = "SELECT ticket_id, status, description, tss_remarks FROM tickets WHERE creator_id=?"
            rows = cursor.execute(q, (user_id,)).fetchall()
            for r in rows:
                print(f"\nID: {r[0]} | Stat: {r[1]}\nDesc: {r[2]}")
                if r[3]: print(f"Notes: {r[3]}")
                print("-" * 40)
        elif choice == "3":
            tid = input("Cancel ID: ")
            if not check_ticket_exists(tid): continue
            chk = cursor.execute("SELECT status FROM tickets WHERE ticket_id=? AND creator_id=?", (tid, user_id)).fetchone()
            if chk and chk[0] not in ['Resolved', 'Cancelled']:
                cursor.execute("UPDATE tickets SET status='Cancelled' WHERE ticket_id=?", (tid,))
                conn.commit()
                print("Cancelled.")
            else: print("Cannot cancel.")
        elif choice == "0": break
        conn.close()

# --- MAIN SYSTEM LOOP ---
def main():
    init_db()
    while True:
        print_header("SCHOOL TECHNICAL SUPPORT SYSTEM")
        print("Login to continue (or type '0' to exit)")

        u_in = input("Username: ").strip()
        if u_in == '0': print("Shutdown."); break

        # Login Logic
        u_norm = normalize_user(u_in)
        p_in = getpass.getpass("Password: ")

        conn = sqlite3.connect(DB_NAME)
        # Check active status
        user = conn.execute("SELECT user_id, role_id, is_first_login, real_name FROM users WHERE username=? AND password=? AND is_active=1",
                            (u_norm, hash_pw(p_in))).fetchone()
        conn.close()

        if user:
            uid, rid, first, name = user

            # First Login Force Change
            if first:
                print_header(f"WELCOME, {name}")
                print("Security Notice: First login requires password change.")
                while True:
                    p1 = getpass.getpass("New Password (min 8 chars): ")
                    if len(p1) < 8: print("Too short."); continue
                    if p1 == getpass.getpass("Confirm: "): break
                    print("Mismatch.")

                conn = sqlite3.connect(DB_NAME)
                conn.execute("UPDATE users SET password=?, is_first_login=0 WHERE user_id=?", (hash_pw(p1), uid))
                conn.commit()
                conn.close()
                print("Password Updated.")

            # Routing
            if rid == 1: admin_menu()
            elif rid == 2: leader_menu(uid)
            elif rid == 3: tss_menu(uid)
            elif rid == 4: staff_menu(uid, name)
        else:
            print("Invalid credentials.")

if __name__ == "__main__":
    main()
