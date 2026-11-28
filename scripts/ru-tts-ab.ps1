#requires -Version 7.0
<#
.SYNOPSIS
Compare local Silero and Windows SAPI Russian voices on the same synthetic text.
.EXAMPLE
./scripts/ru-tts-ab.ps1 -ModelPath ./models/v5_ru.pt -OutputDirectory ../tts-run
.NOTES
Requires Windows, a Russian SAPI voice, and a Python environment containing
this project, torch, numpy and soundfile. Supply only a trusted Silero package:
loading a PyTorch pickle executes code. No model is downloaded. For comparable
runs use the same model, Python environment and installed SAPI voice.
OutputDirectory must be a new directory outside the project; by default a new
ru-tts-ab-* directory is created in the OS temp directory. Delete it after use.
#>
param(
    [Parameter(Mandatory)][string]$ModelPath,
    [string]$Python = 'python',
    [string]$OutputDirectory = (Join-Path ([IO.Path]::GetTempPath()) ('ru-tts-ab-' + [guid]::NewGuid()))
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'The SAPI comparison requires Windows.' }
if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) { throw 'Model file not found.' }
$model = (Resolve-Path -LiteralPath $ModelPath).ProviderPath
$repo = (Get-Item -LiteralPath $PSScriptRoot).Parent.FullName
$out = [IO.Path]::GetFullPath($OutputDirectory, $PWD.ProviderPath)
$relative = [IO.Path]::GetRelativePath($repo, $out)
if (-not ([IO.Path]::IsPathRooted($relative) -or $relative.StartsWith('..\'))) {
    throw 'Choose a new output directory outside the project.'
}
$voice = New-Object -ComObject SAPI.SpVoice
$token = $voice.GetVoices('Language=419') | Select-Object -First 1
if (-not $token) { throw 'Install a Russian SAPI voice first.' }
$voice.Voice = $token
$null = New-Item -ItemType Directory -Path $out
$a = Join-Path $out 'silero.wav'
$b = Join-Path $out 'sapi.wav'
$text = 'На полке стоит синий кубик. Рядом лежит зелёный шар.'

$timer = [Diagnostics.Stopwatch]::StartNew()
@'
import sys
import soundfile as sf
import torch
from cielink_asr.tts import SileroTTS
model, text, out = sys.argv[1:]
torch.manual_seed(0)
torch.set_num_threads(4)
voice = SileroTTS(model_loader=lambda: torch.package.PackageImporter(model).load_pickle('tts_models', 'model'), speaker='baya', sample_rate=24000)
sf.write(out, voice.synthesize(text).detach().cpu().numpy(), 24000, subtype='PCM_16')
'@ | & $Python - $model $text $a *> $null
if ($LASTEXITCODE -ne 0) { throw 'Silero synthesis failed. Check the Python environment and model.' }
$sileroSeconds = $timer.Elapsed.TotalSeconds

$timer.Restart()
$stream = New-Object -ComObject SAPI.SpFileStream
$stream.Format.Type = 26 # SAFT24kHz16BitMono
$stream.Open($b, 3, $false)
try {
    $voice.AllowAudioOutputFormatChangesOnNextSet = $false
    $voice.AudioOutputStream = $stream
    [void]$voice.Speak($text, 16) # SVSFIsNotXML
} finally { $stream.Close() }
$sapiSeconds = $timer.Elapsed.TotalSeconds

@'
import sys
import numpy as np
import soundfile as sf
for path in sys.argv[1:]:
    info = sf.info(path)
    audio, _ = sf.read(path)
    if (info.samplerate, info.channels, info.subtype) != (24000, 1, 'PCM_16') or not np.any(audio):
        sys.exit(1)
'@ | & $Python - $a $b *> $null
if ($LASTEXITCODE -ne 0) { throw 'Output validation failed.' }
'Validated silero.wav and sapi.wav: mono 24 kHz PCM16. Elapsed: Silero {0:F2}s (includes Python startup), SAPI {1:F2}s.' -f $sileroSeconds, $sapiSeconds
