#!/bin/bash
# AutoSub Mac Launcher
cd "$(dirname "$0")"
# Launch via .app bundle so macOS identifies the process as AutoSub GUI
# (not Python.app) for correct Dock and Cmd+Tab icon
open "$(cd "$(dirname "$0")/../../../" && pwd)/AutoSub GUI.app"
