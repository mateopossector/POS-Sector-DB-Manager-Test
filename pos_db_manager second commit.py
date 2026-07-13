#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POS Sector DB Manager
---------------------
Mali GUI alat za backup i restore SQL Server baze (POS Sector) bez SSMS-a.

Funkcije:
  - Spoji se na SQL Server (Windows ili SQL auth)
  - Napravi backup baze u .bak fajl
  - Restoraj bazu iz .bak fajla (automatski sredi logicke/fizicke nazive)

Zavisnost:  pip install pyodbc
            + ODBC Driver 17 ili 18 for SQL Server (Microsoft, besplatno)
"""

import os
import threading
import datetime
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import pyodbc
except ImportError:
    pyodbc = None


# ----------------------------------------------------------------------------
# DB sloj
# ----------------------------------------------------------------------------
class SqlServer:
    """Tanak omotac oko pyodbc konekcije. Sve operacije idu u 'master' kontekstu."""

    def __init__(self):
        self.driver = None
        self.server = None
        self.use_windows_auth = True
        self.user = None
        self.password = None

    @staticmethod
    def available_drivers():
        if pyodbc is None:
            return []
        # Preferiraj noviji driver
        order = ["ODBC Driver 18 for SQL Server",
                 "ODBC Driver 17 for SQL Server",
                 "SQL Server Native Client 11.0",
                 "SQL Server"]
        installed = [d for d in pyodbc.drivers()]
        ordered = [d for d in order if d in installed]
        # dodaj sve ostale sto nisu u listi
        ordered += [d for d in installed if d not in ordered]
        return ordered

    def _conn_str(self, database="master"):
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={database}",
        ]
        if self.use_windows_auth:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.user}")
            parts.append(f"PWD={self.password}")
        # Driver 18 podrazumijevano trazi enkripciju -> za lokalni server vjeruj certifikatu
        if "18" in (self.driver or ""):
            parts.append("Encrypt=optional")
        parts.append("TrustServerCertificate=yes")
        return ";".join(parts) + ";"

    def connect(self, database="master", autocommit=True):
        # autocommit=True je OBAVEZNO za BACKUP/RESTORE
        # (ne smiju se izvrsavati unutar transakcije)
        return pyodbc.connect(self._conn_str(database), autocommit=autocommit, timeout=10)

    def test(self):
        with self.connect() as c:
            cur = c.cursor()
            cur.execute("SELECT @@VERSION, SERVERPROPERTY('Edition')")
            row = cur.fetchone()
            return row[0].splitlines()[0], row[1]

    def list_databases(self):
        with self.connect() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT name FROM sys.databases "
                "WHERE database_id > 4 ORDER BY name"  # preskoci system baze
            )
            return [r[0] for r in cur.fetchall()]

    def default_paths(self):
        with self.connect() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT "
                "CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(512)), "
                "CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS NVARCHAR(512))"
            )
            data_p, log_p = cur.fetchone()
            return data_p, log_p

    def db_exists(self, db):
        with self.connect() as c:
            cur = c.cursor()
            cur.execute("SELECT 1 FROM sys.databases WHERE name = ?", db)
            return cur.fetchone() is not None

    # --- BACKUP -----------------------------------------------------------
    def backup(self, db, bak_path, compression, log):
        comp = ", COMPRESSION" if compression else ""
        sql = (
            f"BACKUP DATABASE [{db}] TO DISK = N'{bak_path}' "
            f"WITH INIT, FORMAT, STATS = 5{comp}"
        )
        log(f"-> {sql}")
        with self.connect() as c:
            cur = c.cursor()
            cur.execute(sql)
            # procitaj poruke (STATS) iz servera
            self._drain_messages(cur, log)
        log("Backup gotov.")

    # --- RESTORE ----------------------------------------------------------
    def filelist(self, bak_path):
        """RESTORE FILELISTONLY -> [(LogicalName, Type 'D'/'L'), ...]"""
        with self.connect() as c:
            cur = c.cursor()
            cur.execute(f"RESTORE FILELISTONLY FROM DISK = N'{bak_path}'")
            cols = [d[0] for d in cur.description]
            li = cols.index("LogicalName")
            ti = cols.index("Type")
            return [(r[li], r[ti]) for r in cur.fetchall()]

    def restore(self, bak_path, target_db, log):
        data_p, log_p = self.default_paths()
        if not data_p:
            data_p = os.path.dirname(bak_path)
        if not log_p:
            log_p = data_p

        files = self.filelist(bak_path)
        moves = []
        data_count = 0
        for logical, ftype in files:
            if ftype == "L":
                phys = os.path.join(log_p, f"{target_db}_log.ldf")
            else:
                ext = "mdf" if data_count == 0 else f"ndf{data_count}"
                if data_count == 0:
                    phys = os.path.join(data_p, f"{target_db}.mdf")
                else:
                    phys = os.path.join(data_p, f"{target_db}_{data_count}.ndf")
                data_count += 1
            moves.append(f"MOVE N'{logical}' TO N'{phys}'")
            log(f"   {logical} ({ftype}) -> {phys}")

        # ako baza vec postoji, izbaci sve konekcije
        if self.db_exists(target_db):
            log(f"Baza '{target_db}' postoji -> SINGLE_USER (izbacujem konekcije)...")
            with self.connect() as c:
                c.cursor().execute(
                    f"ALTER DATABASE [{target_db}] "
                    f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
                )

        move_sql = ", ".join(moves)
        sql = (
            f"RESTORE DATABASE [{target_db}] FROM DISK = N'{bak_path}' "
            f"WITH REPLACE, RECOVERY, STATS = 5, {move_sql}"
        )
        log(f"-> RESTORE DATABASE [{target_db}] ...")
        try:
            with self.connect() as c:
                cur = c.cursor()
                cur.execute(sql)
                self._drain_messages(cur, log)
        finally:
            # vrati u multi_user (ako je baza nastala)
            if self.db_exists(target_db):
                try:
                    with self.connect() as c:
                        c.cursor().execute(
                            f"ALTER DATABASE [{target_db}] SET MULTI_USER"
                        )
                except Exception as e:
                    log(f"(upozorenje pri MULTI_USER: {e})")
        log("Restore gotov.")

    @staticmethod
    def _drain_messages(cursor, log):
        # SQL Server salje progres (STATS) i info poruke; pokupi ih
        try:
            while cursor.nextset():
                pass
        except pyodbc.Error:
            pass

    # --- KORISNICI / LOGINI ----------------------------------------------
    # Fiksne server role u SQL Serveru (bez 'public' - njega ima svako)
    SERVER_ROLES = ["sysadmin", "serveradmin", "securityadmin", "processadmin",
                    "setupadmin", "bulkadmin", "diskadmin", "dbcreator"]

    @staticmethod
    def _q(ident):
        # sigurno umetanje identifikatora u [ ]
        return "[" + ident.replace("]", "]]") + "]"

    @staticmethod
    def _lit(text):
        # sigurno umetanje string literala u ' '
        return "N'" + text.replace("'", "''") + "'"

    def login_exists(self, name):
        with self.connect() as c:
            cur = c.cursor()
            cur.execute("SELECT 1 FROM sys.server_principals WHERE name = ?", name)
            return cur.fetchone() is not None

    def create_or_update_login(self, name, password, enforce_policy, roles, log):
        exists = self.login_exists(name)
        policy = "ON" if enforce_policy else "OFF"
        with self.connect() as c:
            cur = c.cursor()
            if exists:
                log(f"Login '{name}' vec postoji -> azuriram lozinku/postavke.")
                cur.execute(
                    f"ALTER LOGIN {self._q(name)} WITH PASSWORD = {self._lit(password)}")
                cur.execute(
                    f"ALTER LOGIN {self._q(name)} WITH "
                    f"CHECK_POLICY = {policy}"
                    + ("" if enforce_policy else ", CHECK_EXPIRATION = OFF"))
            else:
                # kad je policy OFF, mora i expiration OFF
                extra = "" if enforce_policy else ", CHECK_EXPIRATION = OFF"
                cur.execute(
                    f"CREATE LOGIN {self._q(name)} WITH PASSWORD = {self._lit(password)}, "
                    f"CHECK_POLICY = {policy}{extra}")
                log(f"Login '{name}' kreiran.")
            # osiguraj da je login omogucen
            try:
                cur.execute(f"ALTER LOGIN {self._q(name)} ENABLE")
            except pyodbc.Error:
                pass
            # dodaj role
            for role in roles:
                try:
                    cur.execute(
                        f"ALTER SERVER ROLE {self._q(role)} ADD MEMBER {self._q(name)}")
                    log(f"   + role: {role}")
                except pyodbc.Error as e:
                    log(f"   (role '{role}' preskocena: {e})")
        log("Korisnik spreman.")

    def set_mixed_mode(self, log):
        # LoginMode = 2 -> SQL + Windows (Mixed). Treba restart servera da proradi.
        sql = (
            "EXEC xp_instance_regwrite N'HKEY_LOCAL_MACHINE', "
            "N'Software\\Microsoft\\MSSQLServer\\MSSQLServer', "
            "N'LoginMode', REG_DWORD, 2")
        with self.connect() as c:
            c.cursor().execute(sql)
        log("Mixed Mode (SQL + Windows auth) ukljucen. "
            "POTREBAN je restart SQL Servera da proradi.")

    def service_name(self):
        # vrati Windows naziv servisa za ovaj instance
        with self.connect() as c:
            cur = c.cursor()
            cur.execute("SELECT CAST(SERVERPROPERTY('InstanceName') AS NVARCHAR(128))")
            inst = cur.fetchone()[0]
        if inst:
            return f"MSSQL${inst}"
        return "MSSQLSERVER"


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App(tk.Tk):
    BG = "#eef1f6"        # svijetlo siva pozadina
    PANEL = "#ffffff"     # bijeli paneli
    BLUE = "#1565c0"      # plava (primarna)
    BLUE_DK = "#0d47a1"
    ORANGE = "#f57c00"    # narancasta (akcija)
    ORANGE_DK = "#e65100"
    TEXT = "#1b2330"
    MUTED = "#5b6470"

    def __init__(self):
        super().__init__()
        self.title("POS Sector DB Manager")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.configure(bg=self.BG)
        self.db = SqlServer()
        self._busy = False

        self._setup_style()
        self._build_connection()
        self._build_tabs()
        self._build_log()

        if pyodbc is None:
            self.log("GRESKA: pyodbc nije instaliran.  Pokreni:  pip install pyodbc")
        else:
            drivers = SqlServer.available_drivers()
            if drivers:
                self.driver_cb["values"] = drivers
                self.driver_cb.current(0)
            else:
                self.log("Nije nadjen nijedan ODBC driver za SQL Server. "
                         "Instaliraj 'ODBC Driver 18 for SQL Server'.")

    # --- style ---
    def _setup_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=self.BG, foreground=self.TEXT,
                     fieldbackground="#ffffff", font=("Segoe UI", 10))
        st.configure("TFrame", background=self.BG)
        st.configure("Panel.TFrame", background=self.PANEL, relief="solid",
                     borderwidth=1, bordercolor="#d6dde6")
        st.configure("TLabel", background=self.BG, foreground=self.TEXT)
        st.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT)
        # sekundarni gumb (plavi okvir)
        st.configure("TButton", background="#e8eef7", foreground=self.BLUE_DK,
                     borderwidth=1, padding=6)
        st.map("TButton", background=[("active", "#d4e0f2")])
        # primarni / akcijski gumb (narancasti)
        st.configure("Accent.TButton", background=self.ORANGE, foreground="#ffffff",
                     font=("Segoe UI", 10, "bold"), borderwidth=0, padding=8)
        st.map("Accent.TButton",
               background=[("active", self.ORANGE_DK), ("pressed", self.ORANGE_DK)])
        # plavi gumb (npr. Spoji se)
        st.configure("Blue.TButton", background=self.BLUE, foreground="#ffffff",
                     font=("Segoe UI", 10, "bold"), borderwidth=0, padding=8)
        st.map("Blue.TButton",
               background=[("active", self.BLUE_DK), ("pressed", self.BLUE_DK)])
        st.configure("TNotebook", background=self.BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#dce3ec", foreground=self.MUTED,
                     padding=(16, 8))
        st.map("TNotebook.Tab", background=[("selected", self.BLUE)],
               foreground=[("selected", "#ffffff")])
        st.configure("TEntry", fieldbackground="#ffffff", foreground=self.TEXT)
        st.configure("TCombobox", fieldbackground="#ffffff", foreground=self.TEXT)
        st.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        st.map("TCheckbutton", background=[("active", self.PANEL)])

    # --- konekcija ---
    def _build_connection(self):
        f = ttk.Frame(self, style="Panel.TFrame", padding=12)
        f.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(f, text="SQL Server konekcija", style="Panel.TLabel",
                  foreground=self.BLUE,
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=4,
                                                      sticky="w", pady=(0, 8))

        ttk.Label(f, text="Server:", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.server_var = tk.StringVar(value=r".\SQLEXPRESS")
        ttk.Entry(f, textvariable=self.server_var, width=28).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Driver:", style="Panel.TLabel").grid(row=1, column=2, sticky="w")
        self.driver_cb = ttk.Combobox(f, width=28, state="readonly")
        self.driver_cb.grid(row=1, column=3, sticky="w", padx=6)

        self.win_auth = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Windows autentikacija", variable=self.win_auth,
                        command=self._toggle_auth).grid(row=2, column=0, columnspan=2,
                                                        sticky="w", pady=(8, 0))

        ttk.Label(f, text="User:", style="Panel.TLabel").grid(row=3, column=0, sticky="w")
        self.user_var = tk.StringVar(value="sa")
        self.user_entry = ttk.Entry(f, textvariable=self.user_var, width=28, state="disabled")
        self.user_entry.grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(f, text="Lozinka:", style="Panel.TLabel").grid(row=3, column=2, sticky="w")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(f, textvariable=self.pass_var, width=28,
                                    show="*", state="disabled")
        self.pass_entry.grid(row=3, column=3, sticky="w", padx=6)

        ttk.Button(f, text="Spoji se / Test", style="Blue.TButton",
                   command=self.on_connect).grid(row=4, column=0, columnspan=2,
                                                 sticky="w", pady=(10, 0))
        self.status_lbl = ttk.Label(f, text="Nije spojeno", style="Panel.TLabel",
                                    foreground="#c62828")
        self.status_lbl.grid(row=4, column=2, columnspan=2, sticky="w", pady=(10, 0))

    def _toggle_auth(self):
        state = "disabled" if self.win_auth.get() else "normal"
        self.user_entry.configure(state=state)
        self.pass_entry.configure(state=state)

    # --- tabovi ---
    def _build_tabs(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=12, pady=6)

        # BACKUP
        bf = ttk.Frame(nb, style="Panel.TFrame", padding=14)
        nb.add(bf, text="  Backup  ")
        ttk.Label(bf, text="Baza:", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.backup_db = ttk.Combobox(bf, width=34, state="readonly")
        self.backup_db.grid(row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(bf, text="Spasi u:", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.backup_path = tk.StringVar()
        ttk.Entry(bf, textvariable=self.backup_path, width=44).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(bf, text="...", width=3, command=self._pick_backup_path).grid(row=1, column=2)

        self.compress = tk.BooleanVar(value=False)
        ttk.Checkbutton(bf, text="Kompresija (ne radi na Express editionu)",
                        variable=self.compress).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Button(bf, text="Napravi backup", style="Accent.TButton",
                   command=self.on_backup).grid(row=3, column=1, sticky="w", pady=(8, 0))

        # RESTORE
        rf = ttk.Frame(nb, style="Panel.TFrame", padding=14)
        nb.add(rf, text="  Restore  ")
        ttk.Label(rf, text=".bak fajl:", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.restore_path = tk.StringVar()
        ttk.Entry(rf, textvariable=self.restore_path, width=44).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(rf, text="...", width=3, command=self._pick_restore_path).grid(row=0, column=2)

        ttk.Label(rf, text="Naziv baze:", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.restore_db = tk.StringVar()
        ttk.Entry(rf, textvariable=self.restore_db, width=34).grid(row=1, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(rf, text="(postojeca baza s tim imenom ce biti PREPISANA)",
                  style="Panel.TLabel", foreground="#c62828").grid(row=2, column=1, sticky="w")

        ttk.Button(rf, text="Restoraj bazu", style="Accent.TButton",
                   command=self.on_restore).grid(row=3, column=1, sticky="w", pady=(8, 0))

        # KORISNICI (LOGINI)
        uf = ttk.Frame(nb, style="Panel.TFrame", padding=14)
        nb.add(uf, text="  Korisnici  ")
        ttk.Label(uf, text="Korisnik:", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.new_user = tk.StringVar()
        ttk.Entry(uf, textvariable=self.new_user, width=28).grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(uf, text="Lozinka:", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.new_pass = tk.StringVar()
        ttk.Entry(uf, textvariable=self.new_pass, width=28, show="*").grid(row=1, column=1, sticky="w", padx=6, pady=3)

        self.enforce_policy = tk.BooleanVar(value=False)
        ttk.Checkbutton(uf, text="Enforce Password Policy (ostavi iskljuceno za slabe lozinke)",
                        variable=self.enforce_policy).grid(row=2, column=0, columnspan=3,
                                                           sticky="w", pady=(2, 6))

        ttk.Label(uf, text="Server role:", style="Panel.TLabel",
                  foreground=self.BLUE).grid(row=3, column=0, columnspan=3, sticky="w")
        self.role_vars = {}
        roles_frame = ttk.Frame(uf, style="Panel.TFrame")
        roles_frame.grid(row=4, column=0, columnspan=3, sticky="w", pady=2)
        for i, role in enumerate(SqlServer.SERVER_ROLES):
            v = tk.BooleanVar(value=(role in ("sysadmin", "dbcreator")))
            self.role_vars[role] = v
            ttk.Checkbutton(roles_frame, text=role, variable=v).grid(
                row=i // 4, column=i % 4, sticky="w", padx=(0, 14), pady=2)

        ttk.Button(uf, text="Sve role", style="TButton",
                   command=self._toggle_all_roles).grid(row=5, column=0, sticky="w", pady=(4, 0))
        ttk.Button(uf, text="Kreiraj / azuriraj korisnika", style="Accent.TButton",
                   command=self.on_create_login).grid(row=5, column=1, sticky="w", pady=(4, 0))

        # SERVER
        sf = ttk.Frame(nb, style="Panel.TFrame", padding=14)
        nb.add(sf, text="  Server  ")
        ttk.Label(sf, text="Autentikacija i servis", style="Panel.TLabel",
                  foreground=self.BLUE, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Button(sf, text="Omoguci SQL + Windows auth (Mixed Mode)", style="Blue.TButton",
                   command=self.on_enable_mixed).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(sf, text="(treba restart servera da proradi)", style="Panel.TLabel",
                  foreground=self.MUTED).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Button(sf, text="Restartaj SQL Server", style="Accent.TButton",
                   command=self.on_restart_server).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Label(sf, text="(app mora biti pokrenut kao Administrator)",
                  style="Panel.TLabel", foreground="#c62828").grid(row=2, column=1, sticky="w", padx=8)

    def _toggle_all_roles(self):
        # ako su sve ukljucene -> iskljuci sve, inace ukljuci sve
        all_on = all(v.get() for v in self.role_vars.values())
        for v in self.role_vars.values():
            v.set(not all_on)

    # --- log ---
    def _build_log(self):
        f = ttk.Frame(self, padding=(12, 6, 12, 12))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Log").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            f, height=12, bg="#f4f7fb", fg="#243042",
            insertbackground=self.TEXT, relief="solid", borderwidth=1,
            font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))

    # ------------------------------------------------------------------
    def log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.update_idletasks()

    def _sync_db_settings(self):
        self.db.driver = self.driver_cb.get()
        self.db.server = self.server_var.get().strip()
        self.db.use_windows_auth = self.win_auth.get()
        self.db.user = self.user_var.get().strip()
        self.db.password = self.pass_var.get()

    def _pick_backup_path(self):
        db = self.backup_db.get() or "baza"
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        p = filedialog.asksaveasfilename(
            defaultextension=".bak", filetypes=[("SQL backup", "*.bak")],
            initialfile=f"{db}_{ts}.bak")
        if p:
            self.backup_path.set(p)

    def _pick_restore_path(self):
        p = filedialog.askopenfilename(filetypes=[("SQL backup", "*.bak"), ("Svi", "*.*")])
        if p:
            self.restore_path.set(p)
            if not self.restore_db.get():
                base = os.path.splitext(os.path.basename(p))[0]
                self.restore_db.set(base.split("_")[0])

    # --- async runner ---
    def _run_async(self, fn):
        if self._busy:
            messagebox.showinfo("Cekaj", "Operacija je vec u toku.")
            return
        if pyodbc is None:
            self.log("pyodbc nije instaliran.")
            return
        self._busy = True

        def worker():
            try:
                fn()
            except Exception as e:
                self.log(f"GRESKA: {e}")
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    # --- akcije ---
    def on_connect(self):
        self._sync_db_settings()

        def job():
            ver, edition = self.db.test()
            self.log(f"Spojeno. {ver}  | Edition: {edition}")
            dbs = self.db.list_databases()
            self.backup_db["values"] = dbs
            if dbs:
                self.backup_db.current(0)
            self.status_lbl.configure(text="Spojeno", foreground="#2e7d32")
            self.log(f"Baze: {', '.join(dbs) if dbs else '(nema korisnickih)'}")
        self._run_async(job)

    def on_backup(self):
        self._sync_db_settings()
        db = self.backup_db.get()
        path = self.backup_path.get().strip()
        if not db or not path:
            messagebox.showwarning("Fali podatak", "Izaberi bazu i putanju za .bak.")
            return

        def job():
            self.log(f"Backup baze '{db}' -> {path}")
            self.db.backup(db, path, self.compress.get(), self.log)
        self._run_async(job)

    def on_restore(self):
        self._sync_db_settings()
        path = self.restore_path.get().strip()
        target = self.restore_db.get().strip()
        if not path or not target:
            messagebox.showwarning("Fali podatak", "Izaberi .bak fajl i naziv baze.")
            return
        if not messagebox.askyesno(
                "Potvrda",
                f"Baza '{target}' ce biti restorana iz:\n{path}\n\n"
                f"Ako postoji, bit ce PREPISANA. Nastaviti?"):
            return

        def job():
            self.log(f"Restore '{target}' iz {path}")
            self.db.restore(path, target, self.log)
        self._run_async(job)

    def on_create_login(self):
        self._sync_db_settings()
        name = self.new_user.get().strip()
        pwd = self.new_pass.get()
        if not name or not pwd:
            messagebox.showwarning("Fali podatak", "Upisi korisnicko ime i lozinku.")
            return
        roles = [r for r, v in self.role_vars.items() if v.get()]

        def job():
            self.log(f"Kreiram/azuriram korisnika '{name}' (role: {', '.join(roles) or 'nema'})")
            self.db.create_or_update_login(
                name, pwd, self.enforce_policy.get(), roles, self.log)
        self._run_async(job)

    def on_enable_mixed(self):
        self._sync_db_settings()

        def job():
            self.db.set_mixed_mode(self.log)
        self._run_async(job)

    def on_restart_server(self):
        self._sync_db_settings()
        if not messagebox.askyesno(
                "Restart servera",
                "SQL Server ce biti restartovan. Sve aktivne konekcije "
                "(ukljucujuci POS) ce pasti na par sekundi.\n\nNastaviti?"):
            return

        def job():
            try:
                svc = self.db.service_name()
            except Exception as e:
                self.log(f"Ne mogu utvrditi naziv servisa: {e}")
                return
            self.log(f"Restartujem servis '{svc}' ...")
            self.status_lbl.configure(text="Restart...", foreground="#e65100")
            try:
                for action in ("stop", "start"):
                    r = subprocess.run(
                        ["net", action, svc],
                        capture_output=True, text=True, shell=False)
                    out = (r.stdout + r.stderr).strip()
                    if out:
                        self.log(out.splitlines()[-1])
                    if r.returncode != 0 and action == "stop" and "5" in out:
                        self.log("GRESKA: Access denied. Pokreni aplikaciju "
                                 "DESNI KLIK -> Run as administrator.")
                        return
                self.log("Server restartovan. Klikni 'Spoji se' za ponovno povezivanje.")
                self.status_lbl.configure(text="Nije spojeno", foreground="#c62828")
            except Exception as e:
                self.log(f"GRESKA pri restartu: {e}")
        self._run_async(job)


if __name__ == "__main__":
    App().mainloop()
