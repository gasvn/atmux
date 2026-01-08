#!/usr/bin/env python3
import subprocess
import concurrent.futures
import sys
import os
import curses
import time
import json

NOTES_FILE = os.path.expanduser("~/.autotmux_notes.json")

def load_notes():
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_notes(notes):
    try:
        with open(NOTES_FILE, 'w') as f:
            json.dump(notes, f, indent=2)
    except Exception as e:
        pass

def get_nodes():
    """
    Parses `squeue -u $USER -l` to get a list of nodes and their remaining time.
    Returns a dict: {node_name: time_left_string}
    """
    node_times = {}
    user = os.environ.get('USER')
    if not user:
        print("Error: USER environment variable not set.")
        return node_times

    try:
        # Run squeue command
        # -o "%N|%L" gives "NodeList|TimeLeft"
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
                
    except subprocess.CalledProcessError as e:
        print(f"Error running squeue: {e}")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: squeue command not found.")
        sys.exit(1)

    return node_times

def check_node_sessions(node):
    """
    Checks for tmux sessions on a given node via SSH.
    Returns a tuple: (list_of_sessions, error_message)
    """
    sessions = []
    error = None
    try:
        # BatchMode=yes fails if host key is unknown.
        # strictHostKeyChecking=no will auto-add new keys to known_hosts and proceed.
        # This solves the "first time login" prompt issue.
        # ConnectTimeout=2 prevents hanging on dead nodes.
        cmd = [
            'ssh', '-o', 'BatchMode=yes', 
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ConnectTimeout=2', 
            node, 
            "tmux list-sessions -F '#{session_name}'"
        ]
        # redirect stderr to pipe to capture errors
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            for line in stdout.splitlines():
                session_name = line.strip()
                if session_name:
                    sessions.append((node, session_name))
        else:
            # If return code is non-zero, it could be ssh error or tmux error (no sessions = error in some versions?)
            # tmux list-sessions returns 1 if no sessions found.
            if "no server running on" in stderr or "failed to connect to server" in stderr or not stdout.strip():
                # likely just no sessions
                pass
            else:
                 # Real connection error or other issue
                 # Only capture relevant SSH errors
                 if "Connection timed out" in stderr or "Permission denied" in stderr or "Could not resolve hostname" in stderr:
                     error = f"{node}: {stderr.strip()}"

    except Exception as e:
        error = f"{node}: {str(e)}"
        
    return sessions, error

def get_all_sessions(nodes):
    """
    Concurrency wrapper to check all nodes.
    Returns (all_sessions, errors)
    """
    all_sessions = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_node = {executor.submit(check_node_sessions, node): node for node in nodes}
        for future in concurrent.futures.as_completed(future_to_node):
            node = future_to_node[future]
            try:
                node_sessions, error = future.result()
                all_sessions.extend(node_sessions)
                if error:
                    errors.append(error)
            except Exception as e:
                pass 
    return all_sessions, errors

def main():
    print("Fetching active nodes from squeue...")
    node_times = get_nodes()
    nodes = list(node_times.keys())
    print(f"Found {len(nodes)} nodes. Checking for tmux sessions...")
    
    sessions, errors = get_all_sessions(nodes)
    
    if errors:
        print("\nWarnings (connection failures):")
        for err in errors:
            print(f"  - {err}")
        print("\n")
        # Give user a moment to see errors before menu or exit
        if sessions:
            print("Starting menu in 2 seconds...")
            time.sleep(2)
    
    # Identify nodes with no sessions
    nodes_with_sessions = set(node for node, _ in sessions)
    empty_nodes = [node for node in nodes if node not in nodes_with_sessions]
    
    # Add placeholders for empty nodes
    for node in empty_nodes:
        # We use a special session name to indicate empty/shell
        sessions.append((node, "<Start Shell>"))
        
    sessions.sort() # Sort by node name

    if not sessions:
        print("No active nodes found.")
        return
    
    notes = load_notes()

    while True:
        # Calculate stale sessions (notes that don't match active sessions)
        active_keys = set(f"{node}:{session}" for node, session in sessions)
        stale_items = []
        for key in notes:
            if key not in active_keys:
                parts = key.split(':')
                if len(parts) >= 2:
                    node = parts[0]
                    session = ':'.join(parts[1:]) 
                    stale_items.append((node, session))
        
        # Initialize curses for menu
        selection = curses.wrapper(lambda stdscr: setup_curses_and_run(stdscr, sessions, stale_items, notes, node_times))
        
        if selection:
            # Check if it's a "shell" request tuple or standard return
            # We can modify draw_menu to return (node, session, is_stale, action)
            # Or just assume if session is <Start Shell> it is shell.
            # BUT user wants 's' key on ANY session to open shell.
            # So let's standarize return: (node, session, is_stale, action_type)
            # action_type: 'attach', 'shell', 'delete_n', 'edit_n' ... actually menu handles notes.
            # active_type: 'attach', 'shell'
            
            # Since I can't easily change signature in replace_file_content without changing everything, 
            # I will check the returned tuple length or type if I change it.
            # Let's change draw_menu to return a dict or named tuple or just larger tuple.
            # (node, session, is_stale, 'shell') or (node, session, is_stale, 'attach')
            
            node, session, is_stale, action = selection
            
            if is_stale:
                pass
            else:
                if action == 'shell' or session == "<Start Shell>":
                    print(f"Opening shell on node '{node}'...")
                    ssh_cmd = ['ssh', '-t', node] # Plain SSH
                else:
                    print(f"Attaching to session '{session}' on node '{node}'...")
                    ssh_cmd = ['ssh', '-t', node, 'tmux', 'attach', '-t', session]
                
                try:
                    subprocess.call(ssh_cmd)
                except KeyboardInterrupt:
                    pass
                except Exception as e:
                    print(f"Error: {e}")
                    time.sleep(2)
        else:
            break

def setup_curses_and_run(stdscr, sessions, stale_items, notes, node_times):
    # Modern Color Scheme
    curses.start_color()
    curses.use_default_colors()
    
    # Define colors
    # 1: Selected Item (Black on Cyan)
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN) 
    # 2: Offline/Stale (Red on Default)
    curses.init_pair(2, curses.COLOR_RED, -1)
    # 3: Active Node/Session (Green on Default)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    # 4: Header/Footer (White on Blue)
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
    # 5: Time/Info (Yellow on Default)
    curses.init_pair(5, curses.COLOR_YELLOW, -1)
    # 6: Notes (Magenta on Default)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)
    # 7: Column Headers (Cyan on Default)
    curses.init_pair(7, curses.COLOR_CYAN, -1)

    return draw_menu(stdscr, sessions, stale_items, notes, node_times)

def draw_menu(stdscr, active_items, stale_items, notes, node_times):
    curses.curs_set(0)
    current_row = 0
    
    # Combined items
    nav_items = []
    for node, session in active_items:
        nav_items.append((node, session, False))
    
    if stale_items:
        stale_items.sort()
        for node, session in stale_items:
            nav_items.append((node, session, True))
            
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        
        # --- Header ---
        header_text = f" AutoTmux v0.1 | User: {os.environ.get('USER', 'unknown')} | Active: {len(active_items)} | Offline: {len(stale_items)} "
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(0, 0, header_text.ljust(width))
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
        
        # --- Column Headers ---
        # Fixed widths: Time(14), Node(20), Session(30), Note(Rest)
        col_fmt = "{:<14} {:<20} {:<30} {}"
        col_header = col_fmt.format("TIME LEFT", "NODE", "SESSION", "NOTES")
        
        stdscr.attron(curses.color_pair(7) | curses.A_BOLD)
        stdscr.addstr(1, 1, col_header)
        stdscr.attroff(curses.color_pair(7) | curses.A_BOLD)
        
        stdscr.hline(2, 0, curses.ACS_HLINE, width)

        # --- Footer ---
        footer_text = " ENTER: Connect | s: Shell | S: Snapshot | n: Note | d: Del Note | q: Quit "
        stdscr.attron(curses.color_pair(4))
        try:
            stdscr.addstr(height-1, 0, footer_text.ljust(width))
        except:
            pass # Ignore if resize causes error
        stdscr.attroff(curses.color_pair(4))

        # --- List Area ---
        list_height = height - 4 # Top 3 lines, Bottom 1 line
        start_y = 3
        
        # Scroll Logic
        if current_row < 0: current_row = 0
        if current_row >= len(nav_items): current_row = len(nav_items) - 1
            
        scroll_offset = 0
        if current_row >= list_height:
            scroll_offset = current_row - list_height + 1
            
        display_end = min(len(nav_items), scroll_offset + list_height)
        
        for i in range(scroll_offset, display_end):
            idx = i - scroll_offset
            y = start_y + idx
            
            node, session, is_stale = nav_items[i]
            key = f"{node}:{session}"
            note = notes.get(key, "")
            
            # Formatting
            if is_stale:
                time_disp = "[OFFLINE]"
                sess_disp = session
                node_disp = node
                item_attr = curses.color_pair(2) # Red
            else:
                time_left = node_times.get(node, "N/A")
                time_disp = f"[{time_left}]"
                sess_disp = session if session != "<Start Shell>" else "<Start Shell>"
                node_disp = node
                item_attr = curses.color_pair(3) # Green

            # Truncate strings to fit columns
            time_disp = time_disp[:13]
            node_disp = node_disp[:19]
            sess_disp = sess_disp[:29]
            note_disp = note[:max(10, width - 66)]
            
            line_str = col_fmt.format(time_disp, node_disp, sess_disp, note_disp)
            
            # Render
            if i == current_row:
                # Selected
                stdscr.attron(curses.color_pair(1))
                stdscr.addstr(y, 1, line_str) # Padded x=1
                # Fill remaining line
                stdscr.addstr(y, 1 + len(line_str), " " * (width - 2 - len(line_str)))
                stdscr.attroff(curses.color_pair(1))
            else:
                # Normal
                stdscr.attron(item_attr)
                stdscr.addstr(y, 1, "{:<14}".format(time_disp))
                stdscr.attroff(item_attr)

                # Node & Session in normal/bold?
                # If specific shell item, maybe distinctive?
                if sess_disp == "<Start Shell>":
                     stdscr.attron(curses.A_DIM)
                
                stdscr.addstr(y, 16, "{:<20}".format(node_disp))
                
                if sess_disp == "<Start Shell>":
                     stdscr.attroff(curses.A_DIM)
                     stdscr.attron(curses.color_pair(7)) # Cyan for action
                     
                stdscr.addstr(y, 37, "{:<30}".format(sess_disp))
                
                if sess_disp == "<Start Shell>":
                     stdscr.attroff(curses.color_pair(7))
                     
                # Note
                if note:
                    stdscr.attron(curses.color_pair(6)) # Magenta
                    stdscr.addstr(y, 68, note_disp)
                    stdscr.attroff(curses.color_pair(6))

        stdscr.refresh()
        
        # --- Input ---
        key_input = stdscr.getch()
        
        if key_input == curses.KEY_UP:
            if current_row > 0: current_row -= 1
        elif key_input == curses.KEY_DOWN:
             if current_row < len(nav_items) - 1: current_row += 1
        elif key_input == ord('\n'):
            msg_node, msg_session, msg_stale = nav_items[current_row]
            return (msg_node, msg_session, msg_stale, 'attach') 
        elif key_input == ord('s'):
            msg_node, msg_session, msg_stale = nav_items[current_row]
            return (msg_node, msg_session, msg_stale, 'shell')
        elif key_input == ord('S'):
            # Snapshot Mode
            # draw_snapshot_mode sets its own timeout and handles fetching
            draw_snapshot_mode(stdscr, active_items)
            # Upon return, loop continues and redraws menu
        elif key_input == ord('d'):
            msg_node, msg_session, _ = nav_items[current_row]
            note_key = f"{msg_node}:{msg_session}"
            if note_key in notes:
                del notes[note_key]
                save_notes(notes)
            if nav_items[current_row][2]:
                del nav_items[current_row]
                if current_row >= len(nav_items):
                     current_row = max(0, len(nav_items) - 1)
        elif key_input == ord('n'):
            msg_node, msg_session, _ = nav_items[current_row]
            note_key = f"{msg_node}:{msg_session}"
            current_note = notes.get(note_key, "")
            new_note = get_note_input(stdscr, f"Note: {msg_node}:{msg_session}", current_note)
            if new_note is not None:
                notes[note_key] = new_note
                save_notes(notes)
        elif key_input == ord('q'):
            return None

def capture_pane_content(node, session):
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
        # Filter empty lines or just keep them? Keep them.
        return (node, session, lines)
    except Exception as e:
        return (node, session, [f"Error fetching snapshot: {str(e)}"])

def get_all_snapshots(sessions):
    """
    Parallel fetch of snapshots.
    sessions: list of (node, session)
    """
    snapshots = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(capture_pane_content, node, session) for node, session in sessions]
        for future in concurrent.futures.as_completed(futures):
            snapshots.append(future.result())
    # Sort by node, session
    snapshots.sort(key=lambda x: (x[0], x[1]))
    return snapshots

def draw_snapshot_mode(stdscr, sessions):
    curses.curs_set(0)
    scroll_y = 0
    stdscr.timeout(100) # 100ms non-blocking for auto-refresh check
    
    last_refresh = 0.0
    snapshots = []
    display_lines = []
    requires_redraw = True

    def refresh_data():
        nonlocal snapshots, display_lines, last_refresh
        stdscr.clear()
        stdscr.addstr(0, 0, "Refreshing snapshots... please wait...", curses.A_BOLD)
        stdscr.refresh()
        
        snapshots = get_all_snapshots(sessions)
        
        display_lines = []
        for node, session, lines in snapshots:
            header = f"=== {node} : {session} ==="
            display_lines.append(('header', header))
            for line in lines:
                display_lines.append(('content', line))
            display_lines.append(('separator', ""))
        last_refresh = time.time()

    # Initial fetch
    refresh_data()
    
    try:
        while True:
            # Auto-refresh every 60 seconds
            if time.time() - last_refresh > 60:
                refresh_data()
                requires_redraw = True
            
            if requires_redraw:
                stdscr.clear()
                height, width = stdscr.getmaxyx()
                
                # Title Bar
                time_str = time.strftime("%H:%M:%S", time.localtime(last_refresh))
                # Add auto-refresh indicator
                title = f" SNAPSHOT MODE (Auto-refresh 60s) | Last: {time_str} | q/Esc back "
                stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
                stdscr.addstr(0, 0, title.ljust(width))
                stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
                
                # Viewport
                view_height = height - 1
                max_scroll = max(0, len(display_lines) - view_height)
                
                if scroll_y > max_scroll: scroll_y = max_scroll
                if scroll_y < 0: scroll_y = 0
                
                for i in range(view_height):
                    idx = scroll_y + i
                    if idx >= len(display_lines):
                        break
                    
                    line_type, text = display_lines[idx]
                    y = i + 1
                    
                    # Ensure y is within bounds
                    if y >= height - 1: 
                        break

                    try:
                        if line_type == 'header':
                            max_len = width - 4
                            disp_text = text[:max_len]
                            stdscr.attron(curses.color_pair(7) | curses.A_BOLD)
                            stdscr.addstr(y, 1, disp_text)
                            stdscr.attroff(curses.color_pair(7) | curses.A_BOLD)
                        elif line_type == 'content':
                            max_len = width - 5
                            disp_text = text[:max_len]
                            stdscr.attron(curses.A_DIM) 
                            stdscr.addstr(y, 2, disp_text) 
                            stdscr.attroff(curses.A_DIM)
                    except Exception:
                        pass
                
                stdscr.refresh()
                requires_redraw = False
            
            key = stdscr.getch()
            
            if key == -1: # Timeout, no input
                continue
                
            if key == ord('q') or key == 27: # Esc
                return
            elif key == curses.KEY_UP:
                scroll_y -= 1
                requires_redraw = True
            elif key == curses.KEY_DOWN:
                scroll_y += 1
                requires_redraw = True
            elif key == curses.KEY_NPAGE or key == 6: # Page Down or Ctrl+f
                scroll_y += view_height
                requires_redraw = True
            elif key == curses.KEY_PPAGE or key == 2: # Page Up or Ctrl+b
                scroll_y -= view_height
                requires_redraw = True
            elif key == ord('r'): # Manual refresh hidden shortcut?
                 refresh_data()
                 requires_redraw = True
    finally:
        stdscr.timeout(-1) # Restore blocking mode


def get_note_input(stdscr, title, initial_text):
    curses.echo()
    curses.curs_set(1)
    height, width = stdscr.getmaxyx()
    
    # Wider centered box
    box_height = 5
    box_width = min(width - 4, 120) # Use up to 120 chars or screen width
    start_y = (height - box_height) // 2
    start_x = (width - box_width) // 2
    
    win = curses.newwin(box_height, box_width, start_y, start_x)
    win.keypad(True)
    win.bkgd(' ', curses.color_pair(4)) # Blue background style for dialog
    win.box()
    
    # Title
    win.attron(curses.A_BOLD)
    win.addstr(0, 2, f" {title} ")
    win.attroff(curses.A_BOLD)
    
    # Show current text (truncated if needed for display)
    disp_current = "Current: " + initial_text
    if len(disp_current) > box_width - 4:
        disp_current = disp_current[:box_width-7] + "..."
    win.addstr(1, 2, disp_current)
    
    win.addstr(2, 2, "New Note: ")
    
    win.refresh()
    
    # Input field
    try:
        # Allow input up to box_width - 15 (padding for "New Note: ")
        max_len = box_width - 14
        input_bytes = win.getstr(2, 12, max_len) 
        return input_bytes.decode('utf-8')
    except:
        return None
    finally:
        curses.noecho()
        curses.curs_set(0)

if __name__ == '__main__':
    main()
