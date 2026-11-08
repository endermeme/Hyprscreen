#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo 'python3 is required' >&2; exit 1; }
python3 -c 'import curses' || { echo 'Python curses is required' >&2; exit 1; }
install -d "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 hyprscreen.py "$HOME/.local/bin/hyprscreen"
cat > "$HOME/.local/share/applications/hyprscreen.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Hyprscreen
Comment=Display layout, refresh rates and workspaces for Hyprland
Exec="$HOME/.local/bin/hyprscreen"
Icon=preferences-desktop-display
Terminal=true
Categories=Settings;HardwareSettings;
EOF
printf '%s\n' 'Installed ~/.local/bin/hyprscreen' 'Next: hyprscreen init (inside a Hyprland session).' 'Ensure ~/.local/bin is in PATH.'
