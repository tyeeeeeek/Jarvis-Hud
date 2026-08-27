# Runs Jarvis's autonomous nightly self-improvement pass.
# Scheduled via Windows Task Scheduler (task name: JarvisSelfImprove) --
# see README's "24/7 self-improvement" section to inspect/disable/change it.

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$LogDir = Join-Path $env:USERPROFILE ".jarvis\self_improve_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$RunLog = Join-Path $LogDir "run_$Stamp.log"

$ClaudeExe = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
$Prompt = Get-Content -Raw -Path (Join-Path $ProjectDir "self_improve.md")

"[$Stamp] Starting self-improve run" | Out-File -FilePath $RunLog -Encoding utf8

try {
    & $ClaudeExe -p $Prompt `
        --permission-mode bypassPermissions `
        --allowedTools "Read Write Edit Glob Grep Bash" `
        --tools "Read Write Edit Glob Grep Bash" `
        *>> $RunLog
} catch {
    "[$Stamp] claude.exe invocation failed: $_" | Out-File -FilePath $RunLog -Append -Encoding utf8
}

# Safety net: if the agent made changes but didn't commit them (e.g. it
# crashed mid-run), capture them anyway rather than losing the work or
# leaving the working tree dirty for the next run.
git -C $ProjectDir add -A 2>&1 | Out-Null
$Status = git -C $ProjectDir status --porcelain
if ($Status) {
    git -C $ProjectDir commit -m "auto-wrapper: uncommitted changes from self-improve run $Stamp" 2>&1 | Out-File -FilePath $RunLog -Append -Encoding utf8
}

"[$Stamp] Run complete" | Out-File -FilePath $RunLog -Append -Encoding utf8
