"""
Half Sword Online — Host Dashboard

An overlay/separate window the host sees showing:
- Connected players, their teams, ready status, ping
- Per-player stream stats (resolution, bitrate, FPS, latency)
- Controls: kick, change max players, lock teams, force start
- Overall server stats

This runs as a lightweight tkinter window alongside the game,
since the game uses the GPU/fullscreen and we don't want to fight it.
Tkinter is in Python stdlib — no extra deps.

The host can also use hotkeys (registered via the Lua mod):
    Ctrl+N = spawn player slot
    Ctrl+U = remove player slot
    Ctrl+D = dismiss death screen
"""

import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class HostDashboard:
    """
    Tkinter-based host dashboard window.

    Shows server state and provides controls for managing the game session.
    Runs in its own thread so it doesn't block the game or server.
    """

    def __init__(self, server_port: int = 8080, max_players: int = 8,
                 on_kick: Optional[Callable[[int], None]] = None,
                 on_force_start: Optional[Callable[[], None]] = None,
                 on_set_max_players: Optional[Callable[[int], None]] = None,
                 on_lock_teams: Optional[Callable[[bool], None]] = None,
                 on_set_mode: Optional[Callable[[str], None]] = None):
        self.server_port = server_port
        self.max_players = max_players

        # Callbacks
        self.on_kick = on_kick
        self.on_force_start = on_force_start
        self.on_set_max_players = on_set_max_players
        self.on_lock_teams = on_lock_teams
        self.on_set_mode = on_set_mode

        # State (updated by server thread)
        self.players: list[dict] = []
        self.server_stats: dict = {}
        self.teams_locked = False

        self._root: Optional[tk.Tk] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Widget refs
        self._player_tree: Optional[ttk.Treeview] = None
        self._status_label: Optional[tk.Label] = None
        self._stats_labels: dict[str, tk.Label] = {}

    def start(self):
        """Start the dashboard in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run_ui, daemon=True, name="dashboard")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception:
                pass

    def update_players(self, players: list[dict]):
        """
        Update the player list. Each dict has:
        {slot, name, team, ready, ping_ms, resolution, bitrate_kbps, fps, bytes_sent}
        """
        self.players = players
        if self._root and self._running:
            try:
                self._root.after(0, self._refresh_player_list)
            except Exception:
                pass

    def update_stats(self, stats: dict):
        """Update overall server stats."""
        self.server_stats = stats
        if self._root and self._running:
            try:
                self._root.after(0, self._refresh_stats)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _run_ui(self):
        self._root = tk.Tk()
        self._root.title("Half Sword Online — Host Dashboard")
        self._root.geometry("650x500")
        self._root.configure(bg="#1a1a2e")
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background="#1a1a2e")
        style.configure("Dark.TLabel", background="#1a1a2e", foreground="#ddd", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#1a1a2e", foreground="#c8b464",
                         font=("Segoe UI", 16, "bold"))
        style.configure("Treeview", background="#262640", foreground="#ddd",
                         fieldbackground="#262640", font=("Segoe UI", 10), rowheight=28)
        style.configure("Treeview.Heading", background="#1e1e36", foreground="#aaa",
                         font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#3a3a6a")])

        main = ttk.Frame(self._root, style="Dark.TFrame")
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Title
        ttk.Label(main, text="⚔  HALF SWORD ONLINE — HOST",
                  style="Title.TLabel").pack(anchor=tk.W)

        # Status bar
        status_frame = ttk.Frame(main, style="Dark.TFrame")
        status_frame.pack(fill=tk.X, pady=(5, 10))

        self._status_label = ttk.Label(status_frame,
            text=f"Serving on UDP port {self.server_port}  •  Max {self.max_players} players",
            style="Dark.TLabel")
        self._status_label.pack(side=tk.LEFT)

        # Player list
        ttk.Label(main, text="Connected Players", style="Dark.TLabel").pack(anchor=tk.W)

        tree_frame = ttk.Frame(main, style="Dark.TFrame")
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 5))

        columns = ("slot", "name", "team", "ready", "ping", "resolution", "bitrate", "fps")
        self._player_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)

        self._player_tree.heading("slot", text="#")
        self._player_tree.heading("name", text="Player")
        self._player_tree.heading("team", text="Team")
        self._player_tree.heading("ready", text="Ready")
        self._player_tree.heading("ping", text="Ping")
        self._player_tree.heading("resolution", text="Resolution")
        self._player_tree.heading("bitrate", text="Bitrate")
        self._player_tree.heading("fps", text="FPS")

        self._player_tree.column("slot", width=30, anchor=tk.CENTER)
        self._player_tree.column("name", width=120)
        self._player_tree.column("team", width=80, anchor=tk.CENTER)
        self._player_tree.column("ready", width=50, anchor=tk.CENTER)
        self._player_tree.column("ping", width=50, anchor=tk.CENTER)
        self._player_tree.column("resolution", width=90, anchor=tk.CENTER)
        self._player_tree.column("bitrate", width=70, anchor=tk.CENTER)
        self._player_tree.column("fps", width=40, anchor=tk.CENTER)

        self._player_tree.pack(fill=tk.BOTH, expand=True)

        # Controls
        ctrl_frame = ttk.Frame(main, style="Dark.TFrame")
        ctrl_frame.pack(fill=tk.X, pady=(5, 5))

        btn_style = {"bg": "#464670", "fg": "#ddd", "activebackground": "#5a5a90",
                     "font": ("Segoe UI", 10), "relief": tk.FLAT, "padx": 10, "pady": 4}

        tk.Button(ctrl_frame, text="Kick Selected", command=self._kick_selected,
                  **btn_style).pack(side=tk.LEFT, padx=2)

        tk.Button(ctrl_frame, text="Force Start", command=self._force_start,
                  bg="#2a8a4a", fg="#fff", activebackground="#3ab05a",
                  font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                  padx=10, pady=4).pack(side=tk.LEFT, padx=2)

        self._lock_btn = tk.Button(ctrl_frame, text="Lock Teams",
                                    command=self._toggle_lock_teams, **btn_style)
        self._lock_btn.pack(side=tk.LEFT, padx=2)

        # Max players spinner
        tk.Label(ctrl_frame, text="Max:", bg="#1a1a2e", fg="#aaa",
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(10, 2))

        self._max_var = tk.StringVar(value=str(self.max_players))
        max_spin = tk.Spinbox(ctrl_frame, from_=2, to=8, width=3,
                              textvariable=self._max_var,
                              command=self._change_max_players,
                              bg="#262640", fg="#ddd", font=("Segoe UI", 10),
                              buttonbackground="#464670")
        max_spin.pack(side=tk.LEFT)

        # Server stats
        stats_frame = ttk.Frame(main, style="Dark.TFrame")
        stats_frame.pack(fill=tk.X, pady=(5, 0))

        for stat_name in ["uptime", "total_sent", "total_recv", "active_streams"]:
            lbl = ttk.Label(stats_frame, text=f"{stat_name}: --", style="Dark.TLabel")
            lbl.pack(side=tk.LEFT, padx=8)
            self._stats_labels[stat_name] = lbl

        # Start periodic refresh
        self._root.after(500, self._periodic_refresh)

        # Run
        self._root.mainloop()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh_player_list(self):
        if not self._player_tree:
            return

        # Clear existing
        for item in self._player_tree.get_children():
            self._player_tree.delete(item)

        # Add current players
        for p in self.players:
            ready_str = "✓" if p.get("ready") else ""
            self._player_tree.insert("", tk.END, values=(
                p.get("slot", "?"),
                p.get("name", "Unknown"),
                p.get("team", "?"),
                ready_str,
                f"{p.get('ping_ms', 0)}ms",
                p.get("resolution", "?"),
                f"{p.get('bitrate_kbps', 0)}k",
                p.get("fps", "?"),
            ))

    def _refresh_stats(self):
        for name, label in self._stats_labels.items():
            value = self.server_stats.get(name, "--")
            label.configure(text=f"{name}: {value}")

    def _periodic_refresh(self):
        if not self._running:
            return
        # Auto-refresh (in case update_players wasn't called)
        self._refresh_player_list()
        self._refresh_stats()
        self._root.after(1000, self._periodic_refresh)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _kick_selected(self):
        if not self._player_tree:
            return
        selected = self._player_tree.selection()
        if not selected:
            return
        item = self._player_tree.item(selected[0])
        slot = int(item["values"][0])
        name = item["values"][1]

        if messagebox.askyesno("Kick Player", f"Kick {name} (slot {slot})?"):
            if self.on_kick:
                self.on_kick(slot)

    def _force_start(self):
        if self.on_force_start:
            self.on_force_start()

    def _toggle_lock_teams(self):
        self.teams_locked = not self.teams_locked
        self._lock_btn.configure(
            text="Unlock Teams" if self.teams_locked else "Lock Teams",
            bg="#8a2a2a" if self.teams_locked else "#464670")
        if self.on_lock_teams:
            self.on_lock_teams(self.teams_locked)

    def _change_max_players(self):
        try:
            new_max = int(self._max_var.get())
            self.max_players = new_max
            if self.on_set_max_players:
                self.on_set_max_players(new_max)
        except ValueError:
            pass

    def _on_close(self):
        self._running = False
        self._root.destroy()
