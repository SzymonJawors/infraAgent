$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$binary = Join-Path $root "tools\terraform-mcp-server.exe"

if (-not (Test-Path $binary)) {
    throw "Terraform MCP server binary was not found at $binary"
}

$existing = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Terraform MCP is already listening on 127.0.0.1:8080."
    exit 0
}

Start-Process `
    -FilePath $binary `
    -ArgumentList "streamable-http", "--transport-host", "127.0.0.1", "--transport-port", "8080", "--toolsets", "registry" `
    -WorkingDirectory $root

Write-Output "Terraform MCP started at http://127.0.0.1:8080/mcp"
