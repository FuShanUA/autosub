#!/bin/bash
# AutoSub Launcher App Wrapper (Mac Terminal Version)

# Launch via .app bundle so macOS identifies the process as AutoSub Launcher
# (not Python.app) for correct Dock and Cmd+Tab icon
open "$(cd "$(dirname "$0")/../../../" && pwd)/AutoSub Launcher.app"
