# Idle Maintenance System

Shows one maintenance suggestion when you open a terminal tab.

## Display Format

```
Clean Homebrew cache • brew cleanup | 1=Run 2=Del 3=Try 4=Skip
```

Clean, single line with golden command name.

## Quick Usage

After seeing a suggestion:

```bash
m1    # Run it
m2    # Delete (never show again)
m3    # Try (preview first)
m4    # Skip (show again later)
```

Or use full command:
```bash
maint <script-name> [1-4]
```

## How It Works

1. **Open new tab** → Suggestion appears once
2. **Type `m1`** → Runs it
3. **Next tab** → Different suggestion

## Aliases

Defined in `~/.zshrc` (lines 633-636):

```bash
alias m1='maint "$_MAINT_CURRENT_SCRIPT" 1'
alias m2='maint "$_MAINT_CURRENT_SCRIPT" 2'
alias m3='maint "$_MAINT_CURRENT_SCRIPT" 3'
alias m4='maint "$_MAINT_CURRENT_SCRIPT" 4'
```

These map to:
- `m1` → Run
- `m2` → Delete permanently
- `m3` → Try (preview first)
- `m4` → Skip (show later)

## Implementation

### Environment Variables

**`MAINT_SESSION_ID`** - Stable session identifier
- Set once per shell in `~/.zshrc` line 3
- Format: `{pid}_{timestamp}`

**`_MAINT_SHOWN`** - Display flag
- Set after first prompt in `~/.zshrc` line 627
- Prevents repeated display

**`_MAINT_CURRENT_SCRIPT`** - Current suggestion
- Extracted from session.json line 628
- Used by m1/m2/m3/m4 aliases

## Configuration

### Scan Directories

Edit `prompt-suggest.py` line 20:
```python
SCRIPT_DIRS = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/Dropbox/Mackup/scripts"),
]
```

### Add Commands

Edit lines 26-31:
```python
COMMON_COMMANDS = [
    {"cmd": "brew cleanup", "desc": "Clean Homebrew", "freq": 168},
]
```

Frequency: 24=daily, 168=weekly, 336=biweekly

### Work Hours

Edit lines 233-235:
```python
if current_hour < 9 or current_hour > 20:
    return None
```

### Colors

Edit lines 33-43 for your theme.

## Examples

### Basic Usage
```bash
# Open new tab - see:
Clean Homebrew cache • brew cleanup | 1=Run 2=Del 3=Try 4=Skip

# Type:
m1

# Output:
→ Running: Clean Homebrew cache
$ brew cleanup -s && brew autoremove
...
✓ Completed
```

### Preview First
```bash
m3
→ Command: Clean Homebrew cache
$ brew cleanup -s && brew autoremove

Press Enter to run, Ctrl+C to cancel
```

### View All
```bash
maint

Available Maintenance Scripts:
  brew      Clean Homebrew          ✓ 2.3h ago
  docker    Remove Docker images    3.2d ago
```

## Files

**Scripts:**
- `prompt-suggest.py` - Main suggester
- `~/.local/bin/maint` - Action handler

**Data:** `~/Library/Application Support/idle-maintenance/`
- `cache.json` - Scripts (5min TTL)
- `state.json` - History
- `session.json` - Current suggestions

## Troubleshooting

### Not showing?
```bash
echo $_MAINT_SHOWN    # Should be empty in new tab
exec zsh              # Reload
```

### m1 not working?
```bash
echo $_MAINT_CURRENT_SCRIPT    # Should show script name
alias m1                       # Should show alias definition
```

If empty, no suggestion was shown. Open new tab.

### Reset
```bash
rm ~/Library/Application\ Support/idle-maintenance/*.json
unset _MAINT_SHOWN _MAINT_CURRENT_SCRIPT
exec zsh
```

## Changes from Original

**Removed:**
- `[Maint]` prefix
- `$ command` (redundant with command name)
- `→ maint script [1-4]` instruction
- `per-directory-history` plugin (using autojump)

**Added:**
- Quick aliases `m1`, `m2`, `m3`, `m4`
- Golden/yellow command name
- Single-line compact format

## Performance

- First shell: ~0.5s
- Cached: ~0.2s
- Session: ~0.05s
- Alias: instant

## Summary

**Simple workflow:**
1. See suggestion
2. Type `m1` to run (or `m2`/`m3`/`m4`)
3. Done

Fast, clean, unobtrusive.
