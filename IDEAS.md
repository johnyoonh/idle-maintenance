# Idle Maintenance Extension Ideas

## 1. iTerm2 Tab Cleanup
*   **Goal:** Close inactive terminal tabs that haven't had a command run in X hours.
*   **Trigger:** Return from idle.
*   **Actions:** 
    *   **Keep:** Leave the tab open.
    *   **Delete:** Close the tab/session.
    *   **Try:** Focus the tab to see what was running.
    *   **Skip:** Ask again later.

## 2. Agent-Deck Session Cleanup
*   **Goal:** Clean up stale AI agent sessions or deck contexts that are no longer relevant.
*   **Trigger:** Periodic audit upon return from idle.
*   **Actions:** Archive, Delete, or Resume session.

## 3. File Cleanup (Downloads/Desktop Audit)
*   **Goal:** Flag large or old files in cluttered directories.
*   **Trigger:** Files older than 30 days or larger than 1GB.
*   **Actions:** Move to Archive, Delete to Trash, QuickLook (Try).

## 4. TickTick Task Review
*   **Goal:** Surface overdue or "Someday" tasks that need a decision.
*   **Actions:** Complete, Delete, Open in TickTick (Try), Reschedule (Skip).

## 5. Financial Audit (High Transaction Confirmation)
*   **Goal:** Flag major expenses from bank exports for manual confirmation.
*   **Trigger:** Transaction > $500 found in latest statement.
*   **Actions:** Mark Confirmed, Delete (Flag as Error), Search Receipt (Try).

## 6. System Health / Process Audit
*   **Goal:** Identify high-resource background processes.
*   **Trigger:** Process using > 2GB RAM or > 50% CPU for extended periods.
*   **Actions:** Ignore, Kill Process, Open Activity Monitor (Try).
