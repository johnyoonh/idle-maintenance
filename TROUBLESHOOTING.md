# Troubleshooting: "Shows on every prompt"

## Problem
The maintenance suggestion appears on EVERY prompt instead of once per tab.

## Root Cause
The `MAINT_SESSION_ID` environment variable is not set in your current shell session.

## Check Current State

```bash
echo $MAINT_SESSION_ID
```

**Expected:** `12345_1234567890` (PID_timestamp format)
**Problem:** Empty or not set

## Solutions

### Option 1: Open New iTerm2 Tab (Recommended)
Press `Cmd+T` to open a completely new tab. The new tab will have `MAINT_SESSION_ID` set.

### Option 2: Reload Shell
```bash
exec zsh
```

This restarts your shell and sources `~/.zshrc` fresh.

### Option 3: Source Manually
```bash
source ~/.zshrc
```

Note: This may not work if `MAINT_SESSION_ID` was already set.

## Verify It's Working

After opening a new tab or reloading:

```bash
# Check env var is set
echo $MAINT_SESSION_ID
# Should show: 12345_1234567890

# Press Enter a few times
# Same suggestion should appear each time
```

## How It Works

**File:** `~/.zshrc` (lines 2-5)
```bash
if [[ -z "$MAINT_SESSION_ID" ]]; then
    export MAINT_SESSION_ID="$$_$(date +%s)"
fi
```

This runs ONCE when the shell starts:
- `$$` = Current shell PID (unique per tab)
- `$(date +%s)` = Unix timestamp when shell started
- Together = Stable ID for this tab's entire lifetime

## Session Behavior

**Open new tab:**
- `.zshrc` runs → Sets `MAINT_SESSION_ID="12345_1234567890"`
- First `precmd()` → Picks random suggestion → Saves to session.json
- Every subsequent `precmd()` → Returns same suggestion

**Take action** (`maint <id> [1-4]`):
- Executes action
- Clears session.json entry for this session ID
- Next tab gets different suggestion

## Still Not Working?

### Debug: Check session.json
```bash
cat ~/Library/Application\ Support/idle-maintenance/session.json | jq
```

You should see entries like:
```json
{
  "12345_1234567890": {
    "timestamp": 1774135000,
    "suggestion": {...}
  }
}
```

### Debug: Manual test
```bash
export MAINT_SESSION_ID="test_manual_123"
~/Dropbox/Mackup/scripts/idle-maintenance/prompt-suggest.py
# Press up arrow and Enter to run again
# Should show SAME suggestion both times
```

### Reset Everything
```bash
rm ~/Library/Application\ Support/idle-maintenance/session.json
exec zsh
```

## Expected Behavior

✅ **Correct:** Same suggestion appears before every prompt in a tab
✅ **Correct:** Different suggestions in different tabs
✅ **Correct:** New suggestion after taking action
❌ **Wrong:** Different suggestion on every prompt in same tab

The "showing on every prompt" is actually a FEATURE - it's a reminder system. The issue is when it shows a DIFFERENT suggestion each time.
