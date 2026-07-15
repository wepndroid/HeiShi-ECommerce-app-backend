param(
    [int]$BackendPort = 8001
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $backendRoot '.env'

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Backend .env was not found: $envFile"
}

$secretLine = Get-Content -LiteralPath $envFile |
    Where-Object { $_ -match '^STRIPE_SECRET_KEY=' } |
    Select-Object -First 1

if (-not $secretLine) {
    throw 'STRIPE_SECRET_KEY is missing from Backend/.env'
}

$secretKey = $secretLine.Substring($secretLine.IndexOf('=') + 1).Trim()
if (-not $secretKey.StartsWith('sk_test_')) {
    throw 'Local webhook forwarding requires a Stripe test-mode secret key.'
}

$forwardUrl = "http://127.0.0.1:$BackendPort/v1/payments/webhooks/stripe"
Write-Host "Forwarding Stripe test events to $forwardUrl"
Write-Host 'Keep this window open during the escrow test.'

& npx.cmd -y '@stripe/cli' listen `
    --api-key $secretKey `
    --forward-to $forwardUrl `
    --events 'payment_intent.succeeded,checkout.session.completed'

exit $LASTEXITCODE
