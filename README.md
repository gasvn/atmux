# AutoTmux

AutoTmux is a powerful, terminal-based tool designed to streamline the management of tmux sessions across multiple Slurm compute nodes. It automatically detects your active jobs, scans for tmux sessions on those nodes, and provides a unified dashboard to monitor, attach, and annotate your workflows.

## Features

- **Automatic Node Discovery**: Queries `squeue` to find all compute nodes currently allocated to your user.
- **Session Scanning**: Connects to each node via SSH to list active tmux sessions.
- **Interactive TUI (Text User Interface)**: A clean, color-coded dashboard built with `curses`.
- **One-Key Connection**:
  - **Attach**: Instantly attach to any tmux session with `ENTER`.
  - **Shell**: Open a fresh shell on any active node with `s`.
- **Snapshot Mode**: View a live preview (last 10 lines) of all your running tmux sessions simultaneously without attaching to them.
- **Persistent Notes**: Add custom notes to any session (`n` key) to keep track of experiment status or to-do items. Notes are saved to `~/.autotmux_notes.json`.
- **Stale Session Tracking**: Keeps track of sessions you've noted even if they go offline (displayed in red), so you don't lose context.

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
autotmux
```

### Dashboard Controls

| Key | Action |
| :--- | :--- |
| **UP / DOWN** | Navigate the list of sessions. |
| **ENTER** | **Attach** to the selected tmux session via SSH. |
| **s** | Open a raw SSH **shell** on the selected node. |
| **S** | Enter **Snapshot Mode** (view pane previews). |
| **n** | **Add/Edit Note** for the selected session. |
| **d** | **Delete Note** (and remove from list if session is offline). |
| **q** | **Quit** the application. |

### Snapshot Mode

Press `S` from the main menu to see a read-only view of all your sessions.
- **Auto-Refresh**: The view updates every 60 seconds.
- **Navigation**: Use `UP`, `DOWN`, `PageUp`, `PageDown` to scroll through the output.
- **q / Esc**: Return to the main dashboard.

## Requirements

- **Python 3.6+**
- **Slurm**: The tool uses `squeue` to find nodes.
- **SSH**: Must have SSH access to compute nodes (configured with keys for passwordless access recommended).
- **Tmux**: Must be installed on the remote nodes.

## Configuration

AutoTmux stores your session notes in `~/.autotmux_notes.json`. You can manually edit this file if needed, but it is managed automatically by the application.
