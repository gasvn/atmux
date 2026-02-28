#!/usr/bin/env python3
import subprocess
import concurrent.futures
import sys
import os
import curses
import time
import json
import threading
import hashlib
import urllib.request
import multiprocessing
import queue


NOTES_FILE = os.path.expanduser("~/.autotmux_notes.json")
SNAPSHOTS_FILE = os.path.expanduser("~/.autotmux_snapshots.json")
CONFIG_FILE = os.path.expanduser("~/.autotmux_config.json")
CACHE_FILE = os.path.expanduser("~/.autotmux_cache.json")

def fetch_nodes(user):
    node_infos = {}
    if not user: return node_infos
    try:
        # Fetch detailed info: NodeList, TimeLeft, JobID, JobName, Partition, User, State, TimeUsed, NumDes, Reason
        cmd = ['squeue', '-u', user, '-h', '-o', '%N|%L|%j|%i|%P|%u|%T|%M|%D|%R']
        result = subprocess.check_output(cmd, universal_newlines=True, timeout=15)
        
        for line in result.splitlines():
            line = line.strip()
            if not line: continue
            parts = line.split('|')
            if len(parts) < 10: continue
            
            node_part = parts[0]
            time_left = parts[1]
            job_name = parts[2]
            job_id = parts[3]
            partition = parts[4]
            user_name = parts[5]
            state = parts[6]
            time_used = parts[7]
            num_nodes = parts[8]
            reason = parts[9] # This is typically the last one

            # Construct detailed string for top of preview
            details = f"JobId={job_id} Name={job_name} User={user_name} Partition={partition} State={state} Time={time_used} TimeLeft={time_left} Nodes={num_nodes} NodeList={node_part} Reason={reason}"

            info = {
                'time': time_left,
                'job_name': job_name,
                'job_id': job_id,
                'details': details
            }

            if '[' in node_part or ',' in node_part:
                try:
                    expanded = subprocess.check_output(['scontrol', 'show', 'hostnames', node_part], universal_newlines=True)
                    for node in expanded.splitlines():
                        if node.strip(): 
                            node_infos[node.strip()] = info
                except: 
                    node_infos[node_part] = info
            else:
                node_infos[node_part] = info
    except: pass
    return node_infos

def fetch_sessions_on_node(node):
    sessions = []
    error = None
    try:
        cmd = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=2', node, "tmux list-sessions -F '#{session_name}:#{session_windows}'"]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        stdout, stderr = process.communicate(timeout=10)
        if process.returncode == 0:
            for line in stdout.splitlines():
                if line.strip():
                    if ':' in line:
                        parts = line.split(':')
                        sessions.append((node, parts[0], parts[1] if len(parts)>1 else "?"))
                    else:
                        sessions.append((node, line.strip(), "?"))
        else:
            if "Connection timed out" in stderr or "Permission denied" in stderr or "Could not resolve hostname" in stderr:
                error = f"{node}: {stderr.strip()}"
    except subprocess.TimeoutExpired:
        error = f"{node}: Connection timed out (subprocess)"
    except Exception as e:
        error = f"{node}: {str(e)}"
    return sessions, error

def fetch_snapshot(node, session):
    if session == "<Start Shell>": return (node, session, ["(Shell - No Active Session)"])
    try:
        cmd = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=3', node, f"tmux capture-pane -pt {session} -S -10"]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, universal_newlines=True)

        return (node, session, output.splitlines())
    except Exception as e:
        return (node, session, [f"Error fetching snapshot: {str(e)}"])

def background_result_worker(result_queue, user):
    try:
        # 1. Fetch Nodes
        node_infos = fetch_nodes(user)
        nodes = list(node_infos.keys())
        
        # Send partial result to unblock UI
        result_queue.put({
            'node_infos': node_infos,
            'sessions': [],
            'errors': [],
            'snapshots': {}
        })
        
        # 2. Fetch Sessions
        new_sessions = []
        new_errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_node = {executor.submit(fetch_sessions_on_node, node): node for node in nodes}

            for future in concurrent.futures.as_completed(future_to_node):
                try:
                    s, e = future.result()
                    new_sessions.extend(s)
                    if e: new_errors.append(e)
                except: pass
        
        # 3. Fetch Snapshots
        new_snapshots = {}
        if new_sessions:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = []

                for node, session, _ in new_sessions:
                     if session != "<Start Shell>":
                         futures.append(executor.submit(fetch_snapshot, node, session))
                for future in concurrent.futures.as_completed(futures):
                    try:
                        sn, ss, lines = future.result()
                        new_snapshots[f"{sn}:{ss}"] = lines
                    except: pass
        
        # 4. Return Data
        result = {
            'node_infos': node_infos,
            'sessions': new_sessions,
            'errors': new_errors,
            'snapshots': new_snapshots
        }
        result_queue.put(result)
    except Exception as e:
        result_queue.put({'error': str(e)})

class AppState:
    def __init__(self):
        self.notes = self.load_notes()
        self.snapshots = self.load_snapshots()
        
        # Load Cache
        cache = self.load_cache()
        self.node_infos = cache.get('node_infos', {})
        self.sessions = cache.get('sessions', [])
        
        self.errors = []
        self.watches = {}
        self.config = self.load_config()
        self.filter_query = ""
        self.refreshing = False
        self.last_refresh_time = 0
        self.refresh_interval = 30
        self.result_queue = multiprocessing.Queue()
        self.refresh_process = None
        self.dirty = True  # Force initial redraw

        
        self.lock = threading.Lock()
        
        # Start poller thread
        self.poller_thread = threading.Thread(target=self._poll_worker, daemon=True)
        self.poller_thread.start()


    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def load_cache(self):
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_cache(self):
        cache = {
            'node_infos': self.node_infos,
            'sessions': self.sessions
        }
        self._trigger_save(CACHE_FILE, cache)

    def _save_file_worker(self, filename, data):
        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
        except: pass

    def _trigger_save(self, filename, data):
        t = threading.Thread(target=self._save_file_worker, args=(filename, data), daemon=True)
        t.start()

    def save_config(self):
        self._trigger_save(CONFIG_FILE, self.config)
            
    def send_slack_alert(self, node, session, mins):
        url = self.config.get("slack_webhook_url", "")
        if not url: return
        
        msg = f"🚨 *AutoTmux Alert*\nSession `{session}` on node `{node}` has been inactive for *{mins:.1f} minutes*."
        payload = {
            "text": msg,
            "username": "AutoTmux",
            "icon_emoji": ":robot_face:"
        }
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as response:
                pass
        except Exception as e:
            self.errors.append(f"Slack Error: {e}")

    def load_notes(self):
        if os.path.exists(NOTES_FILE):
            try:
                with open(NOTES_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_notes(self):
        self._trigger_save(NOTES_FILE, self.notes)

    def load_snapshots(self):
        if os.path.exists(SNAPSHOTS_FILE):
            try:
                with open(SNAPSHOTS_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_snapshots(self):
        self._trigger_save(SNAPSHOTS_FILE, self.snapshots)

    def start_background_refresh(self):
        # Don't start if already running
        if self.refresh_process and self.refresh_process.is_alive():
            return
            
        self.refreshing = True
        
        # Start new process
        user = os.environ.get('USER')
        self.refresh_process = multiprocessing.Process(
            target=background_result_worker, 
            args=(self.result_queue, user)
        )
        self.refresh_process.daemon = True
        self.refresh_process.start()

    def _poll_worker(self):
        while True:
            try:
                # Blocking get with timeout to allow checking for exit if needed (though daemon thread helps)
                res = self.result_queue.get() 
                
                if 'error' in res:
                    with self.lock:
                        self.errors.append(res['error'])
                        # Must finish refresh even on error
                        self.refreshing = False
                    continue
                
                # Unpack - No lock needed yet
                node_infos = res.get('node_infos', {})
                new_sessions = res.get('sessions', [])
                errors = res.get('errors', [])
                new_snapshots = res.get('snapshots', {})

                # --- Prepare Watch Updates (Outside Lock) ---
                # We calculate what needs to change so we hold lock for less time
                updates_to_apply = []
                alerts_to_send = []

                # We need a read-copy of watches or just iterate carefully?
                # Since this is the only thread writing to watches (except main enabling/disabling),
                # we can be a bit careful. But `watches` is shared.
                # Actually, strictly speaking `watches` is read by UI thread.
                # So we should probably capture the state we need or just accept we might need lock for reads.
                # To really optimize, we can calculate hashes first.
                
                snapshot_hashes = {}
                for k, lines in new_snapshots.items():
                    content_str = "".join(lines)
                    snapshot_hashes[k] = hashlib.md5(content_str.encode('utf-8')).hexdigest()

                # --- Critical Section ---
                with self.lock:
                    self.node_infos = node_infos
                    self.sessions = new_sessions
                    self.errors.extend(errors) # Append new errors
                    
                    # Merge snapshots
                    for k, v in new_snapshots.items():
                        self.snapshots[k] = v
                    
                    # Watch Logic
                    # Now we process the pre-calculated hashes against the current state
                    curr_time = time.time()

                    for k, curr_hash in snapshot_hashes.items():
                         # Auto-enroll
                        if k not in self.watches:
                            self.watches[k] = {
                                'threshold': 300,
                                'last_change': curr_time,
                                'last_hash': '',
                                'alert_enabled': False,
                                'alert_sent': False
                            }
                        
                        w_data = self.watches[k]
                        if w_data['last_hash'] != curr_hash:
                            w_data['last_hash'] = curr_hash
                            w_data['last_change'] = curr_time
                            w_data['alert_sent'] = False
                        else:
                            # Check for alert
                            idle = curr_time - w_data.get('last_change', curr_time)
                            if idle > w_data.get('threshold', 300) and not w_data.get('alert_sent') and w_data.get('alert_enabled', False):
                                alerts_to_send.append((k, idle))
                                w_data['alert_sent'] = True
                    
                    self.save_snapshots()

                    # Add placeholders
                    nodes = list(self.node_infos.keys())
                    nodes_with_sessions = set(node for node, _, _ in new_sessions)
                    empty_nodes = [node for node in nodes if node not in nodes_with_sessions]
                    for node in empty_nodes:
                        self.sessions.append((node, "<Start Shell>", "0"))
                        
                    self.sessions.sort()
                    self.last_refresh_time = time.time()
                    self.refreshing = False
                    self.save_cache()
                    
                    # UI needs update
                    self.dirty = True

                # --- Send Alerts (Outside Lock) ---
                for k, idle_sec in alerts_to_send:
                     parts = k.split(':')
                     if len(parts) >= 2:
                         sn = parts[0]
                         ss = ":".join(parts[1:])
                         t = threading.Thread(target=self.send_slack_alert, args=(sn, ss, idle_sec/60))
                         t.start()

            except Exception as e:
                # In case of queue errors or other mess
                time.sleep(1)

    def refresh_data(self):
        # Synchronous wrapper for initial load or forced sync actions
        self.start_background_refresh()
        # Wait for refresh to complete
        while self.refreshing:
            time.sleep(0.1)


    def _action_worker(self, cmd):
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        # Trigger refresh to show update
        self.start_background_refresh()

    def kill_session_async(self, node, session):
        cmd = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', node, 'tmux', 'kill-session', '-t', session]
        t = threading.Thread(target=self._action_worker, args=(cmd,), daemon=True)
        t.start()

    def create_session_async(self, node, session_name):
        cmd = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', node, 'tmux', 'new-session', '-d', '-s', session_name]
        t = threading.Thread(target=self._action_worker, args=(cmd,), daemon=True)
        t.start()

def draw_centered_msg(stdscr, msg):
    height, width = stdscr.getmaxyx()
    y = height // 2
    x = max(0, (width - len(msg)) // 2)
    stdscr.clear()
    stdscr.addstr(y, x, msg, curses.A_BOLD)
    stdscr.refresh()

def get_input(stdscr, prompt):
    curses.echo()
    curses.curs_set(1)
    height, width = stdscr.getmaxyx()
    win = curses.newwin(5, 60, (height-5)//2, (width-60)//2)
    win.box()
    win.addstr(1, 2, prompt)
    win.refresh()
    try:
        data = win.getstr(2, 2).decode('utf-8')
    except:
        data = ""
    curses.noecho()
    curses.curs_set(0)
    return data

def confirm_action(stdscr, prompt):
    height, width = stdscr.getmaxyx()
    win = curses.newwin(5, 60, (height-5)//2, (width-60)//2)
    win.box()
    win.addstr(1, 2, prompt + " (y/n)")
    win.refresh()
    key = win.getch()
    # Handle resize or other errors gracefully?
    return key in [ord('y'), ord('Y')]

def draw_settings(stdscr, app):
    current = 0
    items = ["Slack Webhook URL", "Refresh Interval (s)", "Exit"]
    
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        
        # Draw Header
        title = " SETTINGS "
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(0, 0, title.ljust(width))
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
        
        # Draw Items
        for i, item in enumerate(items):
            y = 2 + i
            prefix = "[x]" if i == current else "[ ]"
            
            val_disp = ""
            if i == 0:
                val = app.config.get("slack_webhook_url", "")
                val_disp = f": {val[:50]}..." if val else ": (Not Set)"
            elif i == 1:
                val_disp = f": {app.refresh_interval}"
                
            line = f"{prefix} {item}{val_disp}"
            
            if i == current:
                stdscr.attron(curses.A_REVERSE)
                stdscr.addstr(y, 2, line)
                stdscr.attroff(curses.A_REVERSE)
            else:
                stdscr.addstr(y, 2, line)
                
        stdscr.refresh()
        key = stdscr.getch()
        
        if key == -1: continue
        
        if key == ord('q') or key == 27: break
        if key == curses.KEY_UP: current = max(0, current - 1)
        if key == curses.KEY_DOWN: current = min(len(items)-1, current + 1)
        if key == 10: # Enter
            if current == 0: # Slack
                curr = app.config.get("slack_webhook_url", "")
                new_val = get_input(stdscr, "Slack Webhook URL (empty to disable):")
                if new_val is not None:
                    app.config["slack_webhook_url"] = new_val.strip()
                    app.save_config()
            elif current == 1: # Refresh
                pass 
            elif current == 2: break

def draw_help(stdscr):
    height, width = stdscr.getmaxyx()
    win = curses.newwin(14, 50, (height-14)//2, (width-50)//2)
    win.box()
    win.addstr(0, 2, " Help ")
    lines = [
        "Movement: Arrow Keys / PgUp / PgDn",
        "Enter   : Attach to session",
        "s       : Open Shell on node",
        "n       : Add/Edit Note",
        "d       : Delete Note",
        "S       : Settings",
        "r       : Refresh Sessions",
        "k       : Kill Session",
        "c       : Create Session",
        "/       : Filter / Search",
        "e       : View Errors",
        "q       : Quit"
    ]
    for i, line in enumerate(lines):
        win.addstr(i+1, 2, line)
    win.addstr(12, 2, "Press any key to close...")
    win.refresh()
    while True:
        k = win.getch()
        if k != -1: break

def draw_errors(stdscr, errors):
    height, width = stdscr.getmaxyx()
    win = curses.newwin(min(20, height-4), min(100, width-4), 2, 2)
    win.box()
    win.addstr(0, 2, " Error Log ")
    scroll = 0
    while True:
        win.erase()
        win.box()
        win.addstr(0, 2, f" Error Log ({len(errors)}) - q to close ")
        max_y = win.getmaxyx()[0] - 2
        
        for i in range(max_y):
            idx = scroll + i
            if idx < len(errors):
                win.addstr(i+1, 2, errors[idx][:90])
        
        win.refresh()
        k = win.getch()
        if k == -1: continue
        
        if k == ord('q'): break
        elif k == curses.KEY_UP and scroll > 0: scroll -= 1
        elif k == curses.KEY_DOWN and scroll < len(errors) - max_y: scroll += 1

def setup_curses_and_run(stdscr, app):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN) 
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(5, curses.COLOR_YELLOW, -1)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)
    curses.init_pair(7, curses.COLOR_CYAN, -1)

    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    stdscr.timeout(50) # Faster response
    current_row = 0

    
    # Initial load
    # draw_centered_msg(stdscr, "Scanning nodes... please wait...")
    app.start_background_refresh()
    app.last_refresh_time = time.time()

    return draw_menu(stdscr, app, current_row)

def draw_menu(stdscr, app, current_row):
    while True:
        # Check for auto-refresh
        if time.time() - app.last_refresh_time > app.refresh_interval:
            app.start_background_refresh()
            app.dirty = True
        
        # Check if refreshing but process died?
        if app.refreshing and app.refresh_process:
            if not app.refresh_process.is_alive():
                # Process crashed or finished without sending done signal?
                app.refreshing = False
                app.errors.append("Refresh process died unexpectedly.")
                app.dirty = True
        
        
        # Draw Interface (only if dirty)
        if app.dirty:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            
            # --- Split Layout Calculation ---
            list_width = max(60, int(width * 0.45))
            preview_start_x = list_width + 1
            preview_width = width - preview_start_x - 1
            
            # Prepare Data
            # Capture State - Minimize lock time
            with app.lock:
                snap_sessions = list(app.sessions)
                snap_node_infos = app.node_infos # Ref safe? Yes as worker replaces object
                snap_watches = app.watches.copy()
                snap_errors = list(app.errors)
                snap_notes = app.notes.copy()
                snap_snapshots = app.snapshots # Ref safe for get()
                
            # Processing and Rendering (Unlocked)
            active_items = []
            for node, session, wins in snap_sessions:
                active_items.append((node, session, wins, False))
                
            stale_items = []
            active_keys = set(f"{node}:{session}" for node, session, _ in snap_sessions)
            for key in snap_notes:
                if key not in active_keys:
                    parts = key.split(':')
                    if len(parts) >= 2:
                        node = parts[0]
                        session = ':'.join(parts[1:])
                        stale_items.append((node, session, "?", True))
            
            stale_items.sort()
            all_items = active_items + stale_items
            
            # Filter
            if app.filter_query:
                all_items = [
                    i for i in all_items 
                    if app.filter_query.lower() in i[0].lower() or app.filter_query.lower() in i[1].lower()
                ]

            # Clamp row
            if current_row >= len(all_items): current_row = max(0, len(all_items) - 1)
            if current_row < 0: current_row = 0

            # --- Header ---
            refresh_status = " [Refreshing...]" if app.refreshing else ""
            header_text = f" AutoTmux v0.3.1 | Active: {len(active_items)} | Offline: {len(stale_items)} | Errors: {len(snap_errors)}{refresh_status} | Filter: [{app.filter_query}]"
            
            # Ensure header doesn't overflow
            header_text = header_text[:width-1]
            
            stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(0, 0, header_text.ljust(width))
            stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)

            # --- Footer ---
            footer_text = " ENTER:Conn | n:Note | d:Del | w:Watch | S:Settings | k:Kill | c:New | /:Search | r:Ref | ?:H "
            stdscr.attron(curses.color_pair(4))
            try:
                stdscr.addstr(height-1, 0, footer_text.ljust(width))
            except: pass
            stdscr.attroff(curses.color_pair(4))

            # --- Column Headers ---
            col_fmt = "{:<10} {:<10} {:<14} {:<16} {:<4} {}"
            col_header = col_fmt.format("TIME", "JOB", "NODE", "SESSION", "WIN", "NOTES")
            stdscr.attron(curses.color_pair(7) | curses.A_BOLD)
            stdscr.addstr(1, 1, col_header[:list_width-2])
            stdscr.attroff(curses.color_pair(7) | curses.A_BOLD)
            stdscr.hline(2, 0, curses.ACS_HLINE, list_width)

            # --- List ---
            list_height = height - 4
            start_y = 3
            
            scroll_offset = 0
            if current_row >= list_height:
                scroll_offset = current_row - list_height + 1
            
            display_end = min(len(all_items), scroll_offset + list_height)
            
            for i in range(scroll_offset, display_end):
                node, session, wins, is_stale = all_items[i]
                y = start_y + (i - scroll_offset)
                
                # Data
                node_info = snap_node_infos.get(node, {})
                # backward compat or if it's just a time string (old cache)
                if isinstance(node_info, str):
                    time_left = node_info
                    job_name = "?"
                else:
                    time_left = node_info.get('time', "N/A")
                    job_name = node_info.get('job_name', "?")

                note_key = f"{node}:{session}"
                note = snap_notes.get(note_key, "")

                # Colors & Watch Status
                watch_data = snap_watches.get(note_key)
                
                if is_stale:
                    attr = curses.color_pair(2)
                    time_disp = "OFFLINE"
                else:
                    attr = curses.color_pair(3)
                    time_disp = f"{time_left}"
                    
                if watch_data:
                    idle = time.time() - watch_data['last_change']
                    
                    if watch_data.get('alert_enabled', False):
                        thresh = watch_data['threshold']
                        if idle > thresh:
                            attr = curses.color_pair(2) | curses.A_BLINK | curses.A_BOLD
                            sess_disp = "🚨 " + session
                        else:
                            sess_disp = "🔔 " + session
                    else:
                        sess_disp = "👀 " + session
                else:
                    sess_disp = "   " + session

                if session == "<Start Shell>":
                    sess_disp = "🐚 <Shell>"
                    wins_disp = "-"
                else:
                    wins_disp = str(wins)

                # Truncating
                line_str = col_fmt.format(
                    time_disp[:10], job_name[:10], node[:14], sess_disp[:16], wins_disp[:4], note
                )
                # Truncate to list width
                line_str = line_str[:list_width-2]
            
                # Draw
                if i == current_row:
                    stdscr.attron(curses.color_pair(1))
                    stdscr.addstr(y, 1, line_str.ljust(list_width-2))
                    stdscr.attroff(curses.color_pair(1))
                else:
                    stdscr.attron(attr)
                    stdscr.addstr(y, 1, line_str)
                    stdscr.attroff(attr)

            # --- Separator ---
            stdscr.vline(1, list_width, curses.ACS_VLINE, height - 2)

            # --- Preview Pane (Right) ---
            if preview_width > 5:
                # Preview Header
                stdscr.attron(curses.color_pair(7) | curses.A_BOLD)
                stdscr.addstr(1, preview_start_x + 1, " PREVIEW / SNAPSHOT ")
                stdscr.attroff(curses.color_pair(7) | curses.A_BOLD)
                stdscr.hline(2, preview_start_x, curses.ACS_HLINE, preview_width)
                
                # Preview Content
                if 0 <= current_row < len(all_items):
                    p_node, p_session, _, _ = all_items[current_row]
                    if p_session == "<Start Shell>":
                            plines = ["", "  [ready to start shell]", "", "  Node: " + p_node]
                    else:
                        pkey = f"{p_node}:{p_session}"
                        plines = snap_snapshots.get(pkey, ["(Waiting for snapshot...)"])
                        
                        # Watch Details
                        pw_data = snap_watches.get(pkey)
                        if pw_data:
                            pidle = time.time() - pw_data['last_change']
                            pthr = pw_data['threshold']
                            if pw_data.get('alert_enabled', False):
                                    status = "ALERTING" if pidle > pthr else "ARMED"
                            else:
                                    status = "WATCHING"
                            info = f" [{status}] Idle: {int(pidle/60)}m / Limit: {int(pthr/60)}m"
                            plines = [info, "-"*len(info)] + plines
                    
                    # Add Job Details
                    p_info = snap_node_infos.get(p_node)
                    if p_info and isinstance(p_info, dict):
                        details = p_info.get('details', "")
                        if details:
                                # Wrap details if too long? Or just let it be truncated.
                                # Splitting by space might be nice for readability
                                # details = details.replace(" ", "  ") 
                                plines = [details, "-"*len(details)] + plines
                    
                    # Draw lines
                    for idx, line in enumerate(plines):
                        py = start_y + idx
                        if py >= height - 2: break
                        try:
                            disp = line[:preview_width-2]
                            stdscr.attron(curses.A_DIM)
                            stdscr.addstr(py, preview_start_x + 2, disp)
                            stdscr.attroff(curses.A_DIM)
                        except: pass
                else:
                    stdscr.addstr(start_y, preview_start_x + 2, "(No selection)")
            
            stdscr.refresh()
            app.dirty = False

        key = stdscr.getch()
        
        if key == -1:
            # Timeout, just loop again
            continue
            
        # Any key input makes UI dirty (selection change, scroll, etc)
        app.dirty = True

        
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                # Check for list area click
                if start_y <= my < start_y + list_height:
                    clicked_rel = my - start_y
                    clicked_idx = scroll_offset + clicked_rel
                    if 0 <= clicked_idx < len(all_items):
                        current_row = clicked_idx
                
                # Scroll Wheel
                if bstate & curses.BUTTON4_PRESSED: # Wheel Up
                    current_row -= 1
                elif bstate & 65536: # Button 4 legacy
                    current_row -= 1
                
                try:
                    if bstate & curses.BUTTON5_PRESSED: # Wheel Down
                        current_row += 1
                except: pass
            except:
                pass

        if key == curses.KEY_UP:
            current_row -= 1
        elif key == curses.KEY_DOWN:
            current_row += 1
        elif key == curses.KEY_PPAGE:
            current_row -= list_height
        elif key == curses.KEY_NPAGE:
            current_row += list_height
        elif key == ord('q'):
            return
        elif key == ord('?'):
            draw_help(stdscr)
        elif key == ord('e'):
            draw_errors(stdscr, app.errors)
        elif key == ord('r'):
            # Force background refresh
            app.start_background_refresh()
        elif key == ord('/') or key == 27: # Esc to clear filter often commonly used
             if key == 27:
                 app.filter_query = ""
             else:
                 q = get_input(stdscr, "Search Query:")
                 if q is not None: app.filter_query = q
        elif key == ord('n'):
            if all_items:
                node, session, _, _ = all_items[current_row]
                note_key = f"{node}:{session}"
                curr = app.notes.get(note_key, "")
                new_n = get_input(stdscr, f"Note for {session}:")
                if new_n is not None:
                    app.notes[note_key] = new_n
                    app.save_notes()
        elif key == ord('S'):
             draw_settings(stdscr, app)
        elif key == ord('w'):
            if all_items:
                node, session, _, is_stale = all_items[current_row]
                if not is_stale and session != "<Start Shell>":
                    key_w = f"{node}:{session}"
                    if key_w not in app.watches:
                         # Should be there, but just in case
                         app.watches[key_w] = {
                            'threshold': 300,
                            'last_change': time.time(),
                            'last_hash': '', 
                            'alert_enabled': False,
                            'alert_sent': False
                         }
                    
                    w_data = app.watches[key_w]
                    # Toggle Alert
                    current_state = w_data.get('alert_enabled', False)
                    
                    if current_state:
                        # Turn OFF
                        w_data['alert_enabled'] = False
                        w_data['alert_sent'] = False # Reset
                    else:
                        # Turn ON
                        thresh_str = get_input(stdscr, "Alert after N mins inactivity (default 5):")
                        try:
                            if not thresh_str: thresh_str = "5"
                            mins = float(thresh_str)
                            w_data['threshold'] = mins * 60
                            w_data['alert_enabled'] = True
                            w_data['alert_sent'] = False
                        except: pass
                    
                    # Trigger immediate update
                    app.start_background_refresh()
        elif key == ord('d'):
            if all_items:
                node, session, _, _ = all_items[current_row]
                note_key = f"{node}:{session}"
                if note_key in app.notes:
                    del app.notes[note_key]
                    app.save_notes()
        elif key == ord('k'):
            if all_items:
                node, session, _, is_stale = all_items[current_row]
                if not is_stale and session != "<Start Shell>":
                    if confirm_action(stdscr, f"Kill session '{session}' on {node}?"):
                        app.kill_session_async(node, session)
        elif key == ord('c'):
            # Create session
            # For simplicity, pick node from current selection or if empty list, pick from node_times
            default_node = ""
            if all_items:
                default_node = all_items[current_row][0]
            elif app.node_infos:
                default_node = list(app.node_infos.keys())[0]
            
            target_node = get_input(stdscr, f"Node (default {default_node}):")
            if not target_node and default_node: target_node = default_node
            
            if target_node:
                s_name = get_input(stdscr, "New Session Name:")
                if s_name:
                    app.create_session_async(target_node, s_name)
        elif key == ord('s'):
             if all_items:
                node, _, _, _ = all_items[current_row]
                curses.endwin()
                subprocess.call(['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', '-t', node])
                stdscr.clear()
                stdscr.refresh()
                app.last_refresh_time = time.time()
        elif key == ord('\n'):
             if all_items:
                node, session, _, is_stale = all_items[current_row]
                if not is_stale:
                    curses.endwin()
                    ssh_base = ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/ssh_mux_%u_%h_%p_%r', '-o', 'ControlPersist=600', '-t', node]
                    if session == "<Start Shell>":
                        subprocess.call(ssh_base)
                    else:
                        subprocess.call(ssh_base + ['tmux', 'attach', '-t', session])
                    stdscr.clear()
                    stdscr.refresh()
                    app.last_refresh_time = time.time()

def main():
    app = AppState()
    curses.wrapper(lambda stdscr: setup_curses_and_run(stdscr, app))

if __name__ == '__main__':
    main()
