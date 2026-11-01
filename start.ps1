$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

# остановить старые копии бота и туннеля
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'app\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }

Write-Host "Запускаю туннель..." -ForegroundColor Cyan
$log = Join-Path $dir "tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
Start-Process -FilePath $cf -ArgumentList "tunnel","--protocol","http2","--url","http://localhost:8080" -RedirectStandardError $log -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 40 -and -not $url; $i++) {
    Start-Sleep 1
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
}
if (-not $url) { Write-Host "Не удалось получить адрес туннеля. Проверьте интернет." -ForegroundColor Red; Read-Host "Enter для выхода"; exit 1 }

# записать адрес в .env (без BOM)
$envPath = Join-Path $dir ".env"
$lines = [System.IO.File]::ReadAllLines($envPath) | ForEach-Object { if ($_ -match '^WEBAPP_URL=') { "WEBAPP_URL=$url" } else { $_ } }
[System.IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Адрес мини-аппа: $url" -ForegroundColor Green
Write-Host "Бот запущен. Не закрывайте это окно, пока бот должен работать." -ForegroundColor Green
python app.py
