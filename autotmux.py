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

NOTES_FILE = os.path.expanduser("~/.autotmux_notes.json")
SNAPSHOTS_FILE = os.path.expanduser("~/.autotmux_snapshots.json")
CONFIG_FILE = os.path.expanduser("~/.autotmux_config.json")

class AppState:
    def __init__(self):
        self.notes = self.load_notes()
        self.snapshots = self.load_snapshots()
        self.node_times = {}
        self.sessions = []
        self.errors = []
        self.errors = []
        self.watches = {}
        self.config = self.load_config()
        self.filter_query = ""
        self.refreshing = False
        self.last_refresh_time = 0
        self.refresh_interval = 30

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_config(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except:
            pass
            
    def send_slack_alert(self, node, session, mins):
        url = self.config.get("slack_webhook_url", "")
        if not url: return
        
        msg = f"🚨 *AutoTmux Alert*\nSession `{session}` on node `{node}` has been inactive for *{mins:.1f} minutes*."
        payload = {"text": msg}
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
        try:
            with open(NOTES_FILE, 'w') as f:
                json.dump(self.notes, f, indent=2)
        except Exception as e:
            pass

    def load_snapshots(self):
        if os.path.exists(SNAPSHOTS_FILE):
            try:
                with open(SNAPSHOTS_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_snapshots(self):
        try:
            with open(SNAPSHOTS_FILE, 'w') as f:
                json.dump(self.snapshots, f, indent=2)
        except Exception as e:
            pass

    def get_nodes(self):
        """
        Parses `squeue -u $USER -l` to get a list of nodes and their remaining time.
        Returns a dict: {node_name: time_left_string}
        """
        node_times = {}
        user = os.environ.get('USER')
        if not user:
            return node_times

        try:
            cmd = ['squeue', '-u', user, '-h', '-o', '%N|%L']
            result = subprocess.check_output(cmd, universal_newlines=True)
            
            for line in result.splitlines():
                line = line.strip()
                if not line:
                    continue
                
                parts = line.split('|')
                if len(parts) != 2:
                    continue
                    
                node_part = parts[0]
                time_left = parts[1]
                
                # Slurm sometimes outputs nodelists like node[1-3]. 
                if '[' in node_part or ',' in node_part:
                     try:
                         expanded = subprocess.check_output(['scontrol', 'show', 'hostnames', node_part], universal_newlines=True)
                         for node in expanded.splitlines():
                             if node.strip():
                                 node_times[node.strip()] = time_left
                     except subprocess.CalledProcessError:
                         node_times[node_part] = time_left
                else:
                    node_times[node_part] = time_left
                    
        except Exception:
            pass

        return node_times

    def check_node_sessions(self, node):
        """
        Checks for tmux sessions on a given node via SSH.
        Returns a tuple: (list_of_sessions, error_message)
        """
        sessions = []
        error = None
        try:
            # ConnectTimeout=2 prevents hanging on dead nodes.
            cmd = [
                'ssh', '-o', 'BatchMode=yes', 
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout=2', 
                node, 
                "tmux list-sessions -F '#{session_name}:#{session_windows}'"
            ]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stdout, stderr = process.communicate()
            
            if process.returncode == 0:
                for line in stdout.splitlines():
                    line = line.strip()
                    if line:
                        if ':' in line:
                            parts = line.split(':')
                            s_name = parts[0]
                            s_wins = parts[1] if len(parts) > 1 else "?"
                            sessions.append((node, s_name, s_wins))
                        else:
                            sessions.append((node, line, "?"))
            else:
                if "no server running on" in stderr or "failed to connect to server" in stderr or not stdout.strip():
                    pass
                else:
                     if "Connection timed out" in stderr or "Permission denied" in stderr or "Could not resolve hostname" in stderr:
                         error = f"{node}: {stderr.strip()}"

        except Exception as e:
            error = f"{node}: {str(e)}"
            
        return sessions, error

    def capture_pane_content(self, node, session):
        """
        Captures last 10 lines of the active pane for a session.
        Returns (node, session, content_list)
        """
        if session == "<Start Shell>":
            return (node, session, ["(Shell - No Active Session)"])
            
        try:
            cmd = [
                'ssh', '-o', 'BatchMode=yes', 
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout=3', 
                node, 
                f"tmux capture-pane -pt {session} -S -10"
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, universal_newlines=True)
            lines = output.splitlines()
            return (node, session, lines)
        except Exception as e:
            return (node, session, [f"Error fetching snapshot: {str(e)}"])

    def start_background_refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        t = threading.Thread(target=self._refresh_worker, daemon=True)
        t.start()
        
    def _refresh_worker(self):
        try:
            self.errors = []
            self.node_times = self.get_nodes()
            nodes = list(self.node_times.keys())
            
            new_sessions = []
            new_errors = []
            
            # 1. Fetch sessions
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                future_to_node = {executor.submit(self.check_node_sessions, node): node for node in nodes}
                for future in concurrent.futures.as_completed(future_to_node):
                    try:
                        node_sessions, error = future.result()
                        new_sessions.extend(node_sessions)
                        if error:
                            new_errors.append(error)
                    except Exception:
                        pass 
            
            # 2. Fetch snapshots for active sessions
            if new_sessions:
                 with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                    futures = []
                    for node, session, _ in new_sessions:
                        if session != "<Start Shell>":
                             futures.append(executor.submit(self.capture_pane_content, node, session))
                    
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            s_node, s_sess, lines = future.result()
                            key = f"{s_node}:{s_sess}"
                            self.snapshots[key] = lines
                            
                            # Watch Mode Logic
                            if key in self.watches:
                                content_str = "".join(lines)
                                curr_hash = hashlib.md5(content_str.encode('utf-8')).hexdigest()
                                w_data = self.watches[key]
                                
                                if w_data['last_hash'] != curr_hash:
                                    w_data['last_hash'] = curr_hash
                                    w_data['last_change'] = time.time()
                                    w_data['alert_sent'] = False
                                else:
                                    # Check for alert
                                    idle = time.time() - w_data.get('last_change', time.time())
                                    if idle > w_data.get('threshold', 300) and not w_data.get('alert_sent'):
                                        self.send_slack_alert(s_node, s_sess, idle/60)
                                        w_data['alert_sent'] = True
                        except:
                            pass
            
            self.save_snapshots()

            # Add placeholders for empty nodes
            nodes_with_sessions = set(node for node, _, _ in new_sessions)
            empty_nodes = [node for node in nodes if node not in nodes_with_sessions]
            for node in empty_nodes:
                new_sessions.append((node, "<Start Shell>", "0"))
                
            new_sessions.sort()
            
            self.sessions = new_sessions
            self.errors = new_errors
        except Exception:
            pass
        finally:
            self.last_refresh_time = time.time()
            self.refreshing = False

    def refresh_data(self):
        # Synchronous wrapper for initial load or forced sync actions
        self.start_background_refresh()
        while self.refreshing:
            time.sleep(0.1)

    def kill_session(self, node, session):
        try:
            cmd = ['ssh', node, 'tmux', 'kill-session', '-t', session]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except:
            return False

    def create_session(self, node, session_name):
        try:
            cmd = ['ssh', node, 'tmux', 'new-session', '-d', '-s', session_name]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except:
            return False

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
        stdscr.clear()
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
    win.getch()

def draw_errors(stdscr, errors):
    height, width = stdscr.getmaxyx()
    win = curses.newwin(min(20, height-4), min(100, width-4), 2, 2)
    win.box()
    win.addstr(0, 2, " Error Log ")
    scroll = 0
    while True:
        win.clear()
        win.box()
        win.addstr(0, 2, f" Error Log ({len(errors)}) - q to close ")
        max_y = win.getmaxyx()[0] - 2
        
        for i in range(max_y):
            idx = scroll + i
            if idx < len(errors):
                win.addstr(i+1, 2, errors[idx][:90])
        
        win.refresh()
        k = win.getch()
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
    stdscr.timeout(100) # Non-blocking input
    current_row = 0
    
    # Initial load
    draw_centered_msg(stdscr, "Scanning nodes... please wait...")
    app.refresh_data()
    app.last_refresh_time = time.time()

    return draw_menu(stdscr, app, current_row)

def draw_menu(stdscr, app, current_row):
    while True:
        # Check for auto-refresh
        if time.time() - app.last_refresh_time > app.refresh_interval:
            app.start_background_refresh()
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        
        # --- Split Layout Calculation ---
        list_width = max(45, int(width * 0.4))
        preview_start_x = list_width + 1
        preview_width = width - preview_start_x - 1
        
        # Prepare Data
        active_items = []
        for node, session, wins in app.sessions:
            active_items.append((node, session, wins, False))
            
        stale_items = []
        active_keys = set(f"{node}:{session}" for node, session, _ in app.sessions)
        for key in app.notes:
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
        header_text = f" AutoTmux v0.3.0 | Active: {len(active_items)} | Offline: {len(stale_items)} | Errors: {len(app.errors)}{refresh_status} | Filter: [{app.filter_query}]"
        
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
        col_fmt = "{:<10} {:<14} {:<16} {:<4} {}"
        col_header = col_fmt.format("TIME", "NODE", "SESSION", "WIN", "NOTES")
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
            time_left = app.node_times.get(node, "N/A")
            note_key = f"{node}:{session}"
            note = app.notes.get(note_key, "")

            # Colors & Watch Status
            watch_data = app.watches.get(note_key)
            
            if is_stale:
                attr = curses.color_pair(2)
                time_disp = "[OFFLINE]"
            else:
                attr = curses.color_pair(3)
                time_disp = f"[{time_left}]"
                
            if watch_data:
                idle = time.time() - watch_data['last_change']
                thresh = watch_data['threshold']
                if idle > thresh:
                    attr = curses.color_pair(2) | curses.A_BLINK | curses.A_BOLD
                    time_disp = f"[ALRT {int(idle/60)}m]"
                else:
                    time_disp = f"[W {int(idle/60)}m]"

            if session == "<Start Shell>":
                sess_disp = "<Start Shell>"
                wins_disp = "-"
            else:
                sess_disp = session
                wins_disp = str(wins)

            # Truncating
            line_str = col_fmt.format(
                time_disp[:10], node[:14], sess_disp[:16], wins_disp[:4], note
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
                    plines = app.snapshots.get(pkey, ["(Waiting for snapshot...)"])
                
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

        key = stdscr.getch()
        
        if key == -1:
            # Timeout, just loop again
            continue
        
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
                    if key_w in app.watches:
                        del app.watches[key_w]
                    else:
                        thresh_str = get_input(stdscr, "Alert after N mins inactivity (default 5):")
                        try:
                            if not thresh_str: thresh_str = "5"
                            mins = float(thresh_str)
                            app.watches[key_w] = {
                                'threshold': mins * 60,
                                'last_change': time.time(),
                                'last_hash': '' # Will update on next refresh
                            }
                        except:
                            pass
                    # Trigger immediate update to register watch state
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
                        if app.kill_session(node, session):
                            app.start_background_refresh()
        elif key == ord('c'):
            # Create session
            # For simplicity, pick node from current selection or if empty list, pick from node_times
            default_node = ""
            if all_items:
                default_node = all_items[current_row][0]
            elif app.node_times:
                default_node = list(app.node_times.keys())[0]
            
            target_node = get_input(stdscr, f"Node (default {default_node}):")
            if not target_node and default_node: target_node = default_node
            
            if target_node:
                s_name = get_input(stdscr, "New Session Name:")
                if s_name:
                    if app.create_session(target_node, s_name):
                         app.start_background_refresh()
                    else:
                         time.sleep(1)
        elif key == ord('s'):
             if all_items:
                node, _, _, _ = all_items[current_row]
                curses.endwin()
                subprocess.call(['ssh', '-t', node])
        elif key == ord('S'):
            draw_snapshot_mode(stdscr, app)
        elif key == ord('\n'):
             if all_items:
                node, session, _, is_stale = all_items[current_row]
                if not is_stale:
                    curses.endwin()
                    if session == "<Start Shell>":
                        subprocess.call(['ssh', '-t', node])
                    else:
                        subprocess.call(['ssh', '-t', node, 'tmux', 'attach', '-t', session])


        
        key = stdscr.getch()
        
        if key == -1:
            if time.time() - app.last_refresh_time > app.refresh_interval:
                app.start_background_refresh()
            continue
            
        if key == ord('q') or key == 27: # Esc
            return
        elif key == curses.KEY_UP:
            scroll_y -= 1
        elif key == curses.KEY_DOWN:
            scroll_y += 1
        elif key == curses.KEY_NPAGE or key == 6: 
            scroll_y += view_height
        elif key == curses.KEY_PPAGE or key == 2: 
            scroll_y -= view_height
        elif key == ord('r'): 
             app.start_background_refresh()

def main():
    app = AppState()
    curses.wrapper(lambda stdscr: setup_curses_and_run(stdscr, app))

if __name__ == '__main__':
    main()
