# AutoTmux

AutoTmux is a powerful, terminal-based tool designed to streamline the management of tmux sessions across multiple Slurm compute nodes. It automatically detects your active jobs, scans for tmux sessions on those nodes, and provides a unified dashboard to monitor, attach, and annotate your workflows.

## Features

- **Automatic Node Discovery**: Queries `squeue` to find all compute nodes currently allocated to your user.
- **Session Scanning**: Connects to each node via SSH to list active tmux sessions.
- **Split-View Dashboard**: Compact session list on the left, live snapshot preview on the right.
- **Mouse Support**: Click rows to select, use mouse wheel to scroll.
- **Live Refresh**: Sessions are automatically re-scanned in the background every 30 seconds. (Press `r` to force refresh).
- **Session Management**:
  - **Attach**: Instantly attach to any tmux session or start a new shell.
  - **Create (`c`)**: Create new named sessions on any node directly from the UI.
  - **Kill (`k`)**: Terminate sessions with a confirmation prompt.
- **Search / Filter (`/`)**: Quickly filter the session list by typing queries.
- **One-Key Connection**:
  - **Attach**: Instantly attach to any tmux session with `ENTER`.
  - **Shell**: Open a fresh shell on any active node with `s`.
- **Persistent Notes**: Add custom notes to any session (`n` key) to keep track of experiment status or to-do items. Notes are saved to `~/.autotmux_notes.json`.
- **Persistent Snapshots**: Snapshots are automatically captured and saved to `~/.autotmux_snapshots.json` so they are available instantly and across restarts.
- **Error Logging (`e`)**: View connection errors and issues directly within the tool.
- **Stale Session Tracking**: Keeps track of sessions you've noted even if they go offline (displayed in red).

## Installation

You can install AutoTmux directly from the source:

```bash
git clone https://github.com/gasvn/atmux.git
cd atmux
pip install .
```

Or run it directly without installation:

```bash
python3 autotmux.py
```

## Usage

Start the application by running:

```bash
atmux
```

### Dashboard Controls

| Key | Action |
| :--- | :--- |
| **UP / DOWN** | Navigate the list of sessions. |
| **ENTER** | **Attach** to the selected tmux session via SSH. |
| **s** | Open a raw SSH **shell** on the selected node. |
| **r** | **Refresh** the session list. |
| **k** | **Kill** the selected session (with confirmation). |
| **c** | **Create** a new session on a specific node. |
| **/** | **Search / Filter** the list. |
| **n** | **Add/Edit Note** for the selected session. |
| **d** | **Delete Note** (and remove from list if session is offline). |
| **e** | **View Error Log**. |
| **?** | **Show Help** screen. |
| **q** | **Quit** the application. |

## Requirements

- **Python 3.6+**
- **Slurm**: The tool uses `squeue` to find nodes.
- **SSH**: Must have SSH access to compute nodes (configured with keys for passwordless access recommended).
- **Tmux**: Must be installed on the remote nodes.

## Configuration

AutoTmux stores your session notes in `~/.autotmux_notes.json`. You can manually edit this file if needed, but it is managed automatically by the application.
