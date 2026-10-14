# Mako installer: makes the current folder a Mako workspace.
#
#   irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/xwize/mako/main/install.ps1))) -Project myproj
#
# Parameters (the second form, or .\install.ps1 -Project myproj):
#   -Project <name>   project folder / git repo name (asked for if missing)
#   -Ref <ref>        branch or tag of Mako to install (default: main)
#   -Repo <url|path>  where to get Mako from (default: the GitHub repo; a local
#                     clone or folder works too, for testing)
#   -Prefix, -Agents, -Base   passed to `mako init` (issue prefix, worker slots, base branch)
#   -Upgrade          the folder is already a workspace: install the downloaded Mako into it
#   -Force            don't ask before setting up in a folder that isn't empty
#
# It needs Python 3 (as `python`) and git. No admin rights, no global PATH changes:
# everything goes into .mako/ in this folder. The download is a shallow git clone
# into a temp folder, deleted afterwards.
#
# Plain ASCII and no `exit`: under `irm | iex` this runs in the caller's session,
# so it must not close their window or leave preferences changed.

param(
    [string]$Project,
    [string]$Ref = "main",
    [string]$Repo = "https://github.com/xwize/mako.git",
    [string]$Prefix,
    [string]$Agents,
    [string]$Base,
    [switch]$Upgrade,
    [switch]$Force
)

& {
    # Failures are checked explicitly ($LASTEXITCODE, Fail). Not "Stop": on
    # Windows PowerShell 5.1 that turns any stderr from git or python into an error.

    function Fail([string]$msg) {
        Write-Host "mako install: $msg" -ForegroundColor Red
        throw "mako-install-failed"
    }
    function Warn([string]$msg) { Write-Host "warning: $msg" -ForegroundColor Yellow }
    function Has([string]$cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

    # The Microsoft Store stub `python` exists on PATH but prints a message and fails,
    # so check by running it, not by finding it.
    function PythonMajor([string]$exe, [string[]]$pre) {
        try {
            $out = & $exe @pre -c "import sys; print(sys.version_info[0])" 2>$null
            if ($LASTEXITCODE -eq 0) { return "$out".Trim() }
        } catch { }
        return ""
    }

    try {
        Write-Host "Mako installer" -ForegroundColor Cyan
        $root = (Get-Location).ProviderPath

        # --- prerequisites ---------------------------------------------------
        # Mako's shims, launcher and MCP configs all run `python`, so that exact
        # command must be a real Python 3; `py -3` alone is not enough.
        if ((Has "python") -and ((PythonMajor "python" @()) -eq "3")) {
            Write-Host "  python   $((& python --version 2>&1) -join ' ')"
        } elseif ((Has "py") -and ((PythonMajor "py" @("-3")) -eq "3")) {
            Fail ("Python 3 is installed (py -3 works) but ``python`` is not on PATH, and Mako runs ``python``.`n" +
                  "  Re-run the Python installer, choose Modify, and tick 'Add Python to environment variables'.")
        } else {
            Fail ("Python 3 not found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'.`n" +
                  "  (A ``python`` that opens the Microsoft Store is only a stub: turn it off in Settings >`n" +
                  "  Apps > Advanced app settings > App execution aliases, or install Python.)")
        }
        if (-not (Has "git")) {
            Fail "git not found. Install Git for Windows from https://git-scm.com/download/win and open a new terminal."
        }
        Write-Host "  git      $((& git --version) -join ' ')"
        if (Has "wt") { Write-Host "  wt       found" }
        else { Warn "Windows Terminal (wt) not found; mako.launch.bat needs it. Get it from the Microsoft Store." }
        if (Has "claude") { Write-Host "  claude   found" }
        else { Warn "Claude Code (claude) not found; the agent panes need it. See https://claude.com/claude-code" }

        # --- what to do here -------------------------------------------------
        $isWorkspace = Test-Path (Join-Path $root ".mako\config.json")
        if ($isWorkspace -and -not $Upgrade) {
            Fail ("$root is already a Mako workspace (.mako\config.json exists); nothing changed.`n" +
                  "  To install the latest Mako into it, re-run with -Upgrade.")
        }
        if ($Upgrade -and -not $isWorkspace) {
            Fail "-Upgrade: $root is not a Mako workspace (no .mako\config.json)."
        }
        if (-not $isWorkspace) {
            if (-not $Project) { $Project = (Read-Host "Project name (folder for the git repo)").Trim() }
            if ($Project -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
                Fail "bad project name '$Project' (letters, digits, '.', '_', '-')"
            }
            $existing = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop | Where-Object { $_.Name -ne $Project })
            if ($existing.Count -gt 0 -and -not $Force) {
                Write-Host "$root is not empty ($($existing.Count) items, e.g. $($existing[0].Name))."
                $ok = Read-Host "Set up a Mako workspace here anyway? [y/N]"
                if ($ok -notmatch '^(y|yes)$') { Fail "cancelled; nothing changed." }
            }
        }

        # --- get Mako ----------------------------------------------------------
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("mako-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
        try {
            Write-Host "Downloading Mako ($Ref) from $Repo"
            $gitOut = & git -c advice.detachedHead=false clone -q --depth 1 --branch $Ref $Repo $tmp 2>&1
            if ($LASTEXITCODE -ne 0) { Fail "git clone failed:`n$($gitOut -join "`n")" }
            $script = Join-Path $tmp "src\mako.py"
            if (-not (Test-Path $script)) { Fail "no src\mako.py in $Repo at $Ref" }

            # --- mako init -----------------------------------------------------
            # init copies mako.py and its templates into .mako\, so the temp clone
            # is not needed afterwards. On a workspace it upgrades in place.
            $initArgs = @($script, "init")
            if (-not $isWorkspace) {
                $initArgs += $Project
                if ($Prefix) { $initArgs += @("--prefix", $Prefix) }
                if ($Agents) { $initArgs += @("--agents", $Agents) }
                if ($Base) { $initArgs += @("--base", $Base) }
            }
            & python @initArgs
            if ($LASTEXITCODE -ne 0) { Fail "mako init failed (see above)." }
        } finally {
            if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
        }

        Write-Host ""
        Write-Host "Next steps:" -ForegroundColor Cyan
        Write-Host "  1. Open this folder as a vault in Obsidian (Open folder as vault) to see kanban\board.md."
        Write-Host "     Trust the vault when asked, so the Kanban plugin can load."
        Write-Host "  2. Make a first issue:   .\mako.cmd new feature `"My first feature`""
        Write-Host "  3. Start the agents:     .\mako.launch.bat"
        Write-Host "     (orchestrator on the left, one Claude pane per worker slot)"
    } catch {
        if ("$_" -ne "mako-install-failed") {
            Write-Host "mako install: $_" -ForegroundColor Red
        }
    }
}
