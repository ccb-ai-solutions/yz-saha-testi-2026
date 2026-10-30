# Unlimited-OCR (baidu/Unlimited-OCR) in its own Python environment, .venv-ocr.
# Its code was tested with transformers 4.57.1 / torch 2.10.0 / torchvision 0.25.0 (model card), while .venv has
# transformers 5.x - so the model runs in .venv-ocr and the harness talks to it (harness/local/ocr_worker.py).
#   powershell -ExecutionPolicy Bypass -File scripts\setup_unlimited_ocr.ps1                  # shows what it would do
#   powershell -ExecutionPolicy Bypass -File scripts\setup_unlimited_ocr.ps1 -Yes             # install (~3 GB) + model (6.7 GB)
#   powershell -ExecutionPolicy Bypass -File scripts\setup_unlimited_ocr.ps1 -Yes -SkipDownload   # install only
# Downloads only (PyTorch index, PyPI, Hugging Face) - nothing is sent to any benchmark provider. It waits while a
# latency session is running (a 10 GB download would share the network with the measurement).
param(
    [switch]$Yes,
    [switch]$SkipDownload,
    [string]$Cuda = "cu126",
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv-ocr"
$Py = Join-Path $Venv "Scripts\python.exe"
$Base = $Python
if (-not $Base) { $Base = Join-Path $Root ".venv\Scripts\python.exe"; if (-not (Test-Path $Base)) { $Base = "python" } }
$Model = "baidu/Unlimited-OCR"
$Rev = "07dea832e22aefee32ad281d4b80551282e1c168"          # the commit configs/models.yaml pins
$Req = Join-Path $Root "requirements-ocr.txt"
$TorchIdx = "https://download.pytorch.org/whl/$Cuda"
Write-Host "Unlimited-OCR environment (nothing is sent to a benchmark provider):"
Write-Host "  1. create $Venv (python -m venv, from $Base)"
Write-Host "  2. torch==2.10.0 torchvision==0.25.0 from $TorchIdx (~2.5 GB)"
Write-Host "  3. pip install -r requirements-ocr.txt (transformers==4.57.1 + the model card's tested versions)"
Write-Host "  4. check that torch sees the GPU"
if (-not $SkipDownload) { Write-Host "  5. download $Model @ $($Rev.Substring(0, 7)) into the Hugging Face cache (~6.7 GB)" }
if (-not $Yes) { Write-Host "Nothing done. Re-run with -Yes."; exit 0 }

$lock = Join-Path $Root "results\.latency.lock"
while (Test-Path $lock) {
    $age = ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalSeconds
    if ($age -gt 120) { break }                               # stale: the heartbeat is every 15 s
    Write-Host "$(Get-Date -Format HH:mm) a latency session is running - waiting (checks every minute)"
    Start-Sleep -Seconds 60
}
if (-not (Test-Path $Py)) {
    & $Base -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "could not create $Venv with $Base" }
}
& $Py -m pip install --upgrade pip
& $Py -m pip install "torch==2.10.0" "torchvision==0.25.0" --index-url $TorchIdx
if ($LASTEXITCODE -ne 0) { throw "torch 2.10.0 from $TorchIdx failed - try another CUDA build: -Cuda cu128" }
& $Py -m pip install -r $Req
if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements-ocr.txt failed" }
& $Py -c "import torch, transformers; ok = torch.cuda.is_available(); print('torch', torch.__version__, '| CUDA', torch.version.cuda, '| GPU', torch.cuda.get_device_name(0) if ok else 'NOT VISIBLE', '| transformers', transformers.__version__); raise SystemExit(0 if ok else 3)"
if ($LASTEXITCODE -ne 0) { throw "torch in .venv-ocr does not see the GPU (driver / CUDA build) - Unlimited-OCR needs it" }
if (-not $SkipDownload) {
    & $Py -c "from huggingface_hub import snapshot_download; p = snapshot_download('$Model', revision='$Rev', allow_patterns=['*.json', '*.py', '*.safetensors', '*.txt', 'LICENSE']); print('model files in', p)"
    if ($LASTEXITCODE -ne 0) { throw "model download failed (re-run: finished files are kept)" }
}
Write-Host ""
Write-Host "Done. Smoke test on 2 pages (local, nothing sent):"
Write-Host "  .\.venv\Scripts\python.exe harness/run.py run --suite ocr --models unlimited-ocr --limit 2 --session uocr-smoke --yes"
