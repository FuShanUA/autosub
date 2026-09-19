#!/bin/bash
# AutoSub Launcher App Wrapper (Mac Terminal Version)
# TOOLS_DIR is auto-detected by the Python code via os.path.realpath,
# so no manual AUTOSUB_TOOLS_DIR export is needed here.
TOOLDIR="/Users/shanfu/cc/Library/Tools/autosub"

# Launch a new Terminal window and run the Batch Pro directly
osascript -e "tell application \"Terminal\"
    activate
   do script \"cd '$TOOLDIR' && source .venv/bin/activate && python3 autosub_batch_pro.py\"
end tell"
