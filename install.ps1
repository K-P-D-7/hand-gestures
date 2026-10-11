# Windows installer: creates the Python environment, downloads the hand model
# and adds a "Hand Gestures" shortcut to the Start menu and desktop.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating Python environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv .venv }
    else { throw "Python 3 not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again." }
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r requirements.txt
# mediapipe pulls in the GUI build of OpenCV; keep only the headless one
& $py -m pip uninstall -y -q opencv-contrib-python opencv-python 2>$null
& $py -m pip install -q --force-reinstall --no-deps opencv-contrib-python-headless

if (-not (Test-Path "hand_landmarker.task")) {
    Write-Host "Downloading hand landmark model..."
    Invoke-WebRequest -UseBasicParsing -OutFile "hand_landmarker.task" `
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Hand Gestures.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Hand Gestures.lnk")
)
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = (Resolve-Path ".venv\Scripts\pythonw.exe").Path
    $s.Arguments = "`"$PSScriptRoot\hand_gestures.py`""
    $s.WorkingDirectory = $PSScriptRoot
    $s.Description = "Scroll and switch tabs with hand gestures"
    $s.Save()
}
Write-Host "Installed. Launch 'Hand Gestures' from the Start menu or desktop, or run run.bat"
