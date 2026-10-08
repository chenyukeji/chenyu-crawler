param(
    [switch]$InstallChromium,
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = 'Stop'

$venvPath = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (-not $PythonExecutable) {
    if (Test-Path -LiteralPath $bundledPython) {
        $PythonExecutable = $bundledPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw 'Python 3.10+ was not found. Pass -PythonExecutable with its full path.'
        }
        $PythonExecutable = $pythonCommand.Source
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonExecutable -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
& $venvPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install collector dependencies.' }

if ($InstallChromium) {
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Chromium.' }
}

Write-Output "Collector environment ready: $venvPython"
