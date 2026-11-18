$ErrorActionPreference = 'Stop'
$state = Join-Path $PSScriptRoot '../../.phase1'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$rawSample = Join-Path $state 'chinese-sample.wav'
$pcmSample = Join-Path $state 'chinese-sample-16k.wav'
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voice = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'zh-CN' } |
        Select-Object -First 1
    if (-not $voice) { throw 'Install a zh-CN Windows speech voice or supply your own Chinese audio.' }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $speaker.SetOutputToWaveFile($rawSample)
    $speaker.Speak('今天我们来整理桌面。首先，把经常使用的物品放在手边。然后，用标签标记文件夹。最后，留出一块空白区域，方便记录新的想法。')
} finally {
    $speaker.Dispose()
}
ffmpeg -nostdin -v error -y -i $rawSample -ar 16000 -ac 1 -c:a pcm_s16le $pcmSample
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
