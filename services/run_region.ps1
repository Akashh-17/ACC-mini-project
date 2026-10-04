param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("aws", "azure", "gcp")]
    [string]$Cloud,
    [string]$Python = "python"
)

$regions = @{
    aws = "aws-ap-south-1"
    azure = "azure-indiasouthcentral"
    gcp = "gcp-asia-south1"
}

$standardServices = @{
    aws = "recommend-api"
    azure = "admin-api"
    gcp = "notify-api"
}

$services = @(
    @{ Name = "payments-api"; Port = 8081 }
    @{ Name = "fraud-check"; Port = 8082 }
    @{ Name = "inventory-check"; Port = 8083 }
    @{ Name = $standardServices[$Cloud]; Port = 8084 }
)

foreach ($service in $services) {
    $env:SERVICE_NAME = $service.Name
    $env:SERVICE_REGION = $regions[$Cloud]
    $env:SERVICE_PORT = [string]$service.Port
    Start-Process -FilePath $Python -ArgumentList "-m", "services.app" -WorkingDirectory (Get-Location)
    Write-Host "Started $($service.Name) on port $($service.Port) for $($regions[$Cloud])"
}