$start = 0
$end = 3

$startTime = "15:23"
$baseTime = Get-Date $startTime
$scriptDir = $PSScriptRoot

for ($i = $start; $i -le $end; $i++) {
    $folder = $i.ToString("0000")
    $file = "$folder.wav"

    if (Test-Path -Path $file) {
        if (-not (Test-Path -Path $folder)) {
            New-Item -ItemType Directory -Path $folder | Out-Null
        }
        Move-Item -Path $file -Destination $folder
    }
}

if ([string]::IsNullOrEmpty($scriptDir)) { 
    $scriptDir = (Get-Location).Path 
}

for ($i = $start; $i -le $end; $i++) {
    $folderName = $i.ToString("0000")
    $folderPath = Join-Path -Path $scriptDir -ChildPath $folderName

    if (Test-Path -Path $folderPath -PathType Container) {
        $minutesToAdd = ($i - $start) * 30
        $currentTime = $baseTime.AddMinutes($minutesToAdd)
        $fileName = "Incepe la" + $currentTime.ToString("HH-mm")
        
        $filePath = Join-Path -Path $folderPath -ChildPath $fileName
        
        if (-not (Test-Path -Path $filePath)) {
            New-Item -ItemType File -Path $filePath | Out-Null
        }
    }
}