# Загрузка папки arch-bench-public на GitHub одним запуском.
# Запускается двойным щелчком по ЗАГРУЗИТЬ_НА_GITHUB.bat. Вход в GitHub — через браузер
# (пароли и токены в скрипт не вводятся). Сам скрипт и .bat в репозиторий не попадают.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Fail($msg) {
    Write-Host ""
    Write-Host "ОШИБКА: $msg" -ForegroundColor Red
    Read-Host "Нажмите Enter, чтобы закрыть"
    exit 1
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Ensure-Tool($cmd, $wingetId, $title) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "Устанавливаю $title ..." -ForegroundColor Cyan
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "не найден winget. Установите $title вручную и запустите файл снова."
    }
    winget install --id $wingetId -e --source winget --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Fail "$title установлен, но не найден. Закройте окно и запустите файл ещё раз."
    }
}

Write-Host "=== Загрузка проекта на GitHub ===" -ForegroundColor Green
Write-Host "Папка: $PSScriptRoot"
Write-Host ""

Ensure-Tool "git" "Git.Git" "Git"
Ensure-Tool "gh" "GitHub.cli" "GitHub CLI"

# 1. Вход в GitHub через браузер (один раз)
gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Сейчас откроется браузер для входа в GitHub. Скопируйте показанный код и подтвердите вход." -ForegroundColor Cyan
    gh auth login --hostname github.com --git-protocol https --web
    if ($LASTEXITCODE -ne 0) { Fail "вход в GitHub не выполнен." }
}
gh auth setup-git | Out-Null

$login = (gh api user --jq .login).Trim()
$uid = (gh api user --jq .id).Trim()
if (-not $login) { Fail "не удалось получить имя пользователя GitHub." }
Write-Host "Вы вошли как: $login" -ForegroundColor Green

# 2. Локальный репозиторий (адрес почты — скрытый адрес GitHub, личный e-mail не публикуется)
if (-not (Test-Path ".git")) {
    git init -b main | Out-Null
}
git config user.name "$login"
git config user.email "$uid+$login@users.noreply.github.com"
$exclude = ".git\info\exclude"
foreach ($f in @("upload_to_github.ps1", "*.bat")) {
    if (-not (Select-String -LiteralPath $exclude -SimpleMatch $f -Quiet)) { Add-Content -LiteralPath $exclude -Value $f -Encoding UTF8 }
}

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "arch-bench: код и результаты серии 2" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "не удалось создать коммит." }
}

# 3. Репозиторий на GitHub
git remote get-url origin 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Репозиторий уже связан с GitHub — отправляю изменения..." -ForegroundColor Cyan
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { Fail "не удалось отправить изменения." }
} else {
    $name = Read-Host "Название репозитория (Enter = arch-bench)"
    if (-not $name) { $name = "arch-bench" }
    $vis = Read-Host "Доступ: 1 — открытый (виден всем), 2 — закрытый (только приглашённым). Enter = 1"
    $flag = if ($vis -eq "2") { "--private" } else { "--public" }
    gh repo create $name $flag --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) { Fail "не удалось создать репозиторий (возможно, имя уже занято — запустите снова и укажите другое)." }
}

Write-Host ""
Write-Host "Готово! Открываю репозиторий в браузере..." -ForegroundColor Green
gh repo view --web
Read-Host "Нажмите Enter, чтобы закрыть"
