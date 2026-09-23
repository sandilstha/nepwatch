# Registers the three daily post-close sync tasks with Windows Task Scheduler.
# Run once, from an ordinary (non-admin) PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\register_sync_tasks.ps1
# Re-running is safe: existing tasks of the same name are replaced.
#
# Times are LOCAL machine time. This box runs on Nepal time, so 15:15 local is
# 15:15 NPT. If the machine is ever moved to another timezone, shift these.
#
# Remove them again with:
#     Get-ScheduledTask -TaskPath '\NEPSE\' | Unregister-ScheduledTask -Confirm:$false

$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $root 'scripts\market_close_sync.bat'
$path = '\NEPSE\'

# Every day except Saturday. Friday sessions are irregular but real (16 in the
# last 260 trading days), so Friday must be included. The command itself detects
# a holiday and exits without syncing, so extra days cost nothing.
$days = 'Sunday','Monday','Tuesday','Wednesday','Thursday','Friday'

$jobs = @(
    # --with-adjustments: also refresh bonus/rights-adjusted prices
    # (StockPriceAdjustment). The A/D Radar's Quiet Accumulation list and the
    # portfolio desk both read the adjusted series; a stale table silently
    # falls back to raw closes, which book every bonus ex-date as a crash.
    @{ Name = 'NEPSE Sync 1515 Full';   Time = '15:15'; Args = '--with-adjustments'; Desc = 'Price + floorsheet + adjusted-price sync right after close.' },
    @{ Name = 'NEPSE Sync 1530 Verify'; Time = '15:30'; Args = '--verify'; Desc = 'Verify completeness; re-sync only what is missing.' },
    @{ Name = 'NEPSE Sync 1600 Verify'; Time = '16:00'; Args = '--verify'; Desc = 'Final verification sweep.' }
)

foreach ($j in $jobs) {
    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
                   -Argument "/c `"$bat`" $($j.Args)" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $j.Time
    # StartWhenAvailable: if the machine was asleep at 15:15, run on wake rather
    # than silently skipping the day.
    $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                   -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
                   -MultipleInstances IgnoreNew -DontStopOnIdleEnd

    Register-ScheduledTask -TaskName $j.Name -TaskPath $path -Action $action `
        -Trigger $trigger -Settings $set -Description $j.Desc -Force | Out-Null
    Write-Host "registered $($j.Time)  $($j.Name)"
}

Get-ScheduledTask -TaskPath $path | Select-Object TaskName, State
