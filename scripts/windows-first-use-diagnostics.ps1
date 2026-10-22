# Read-only diagnostics for first-use Docker Desktop installation failures.
$ErrorActionPreference = 'Continue'
$os = Get-CimInstance Win32_OperatingSystem
$computer = Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{
    Windows = $os.Caption
    Build = $os.BuildNumber
    ProductType = $os.ProductType
    HypervisorPresent = $computer.HypervisorPresent
    FreeDiskGB = [Math]::Round((Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'))).Free / 1GB, 2)
} | Format-List
Get-CimInstance Win32_Processor | Select-Object VirtualizationFirmwareEnabled, SecondLevelAddressTranslationExtensions
Get-CimInstance Win32_OptionalFeature -Filter "Name='VirtualMachinePlatform'" | Select-Object Name, InstallState
& "$env:SystemRoot\System32\wsl.exe" --version
& "$env:SystemRoot\System32\wsl.exe" --status
$roots = @((Join-Path $env:LOCALAPPDATA 'Docker'), (Join-Path $env:ProgramData 'DockerDesktop'))
foreach ($root in $roots) {
    foreach ($name in @('install-log.txt', 'install-log-admin.txt', 'install-log-user.txt')) {
        $log = Join-Path $root $name
        if (Test-Path -LiteralPath $log) {
            Get-Item -LiteralPath $log | Select-Object FullName, LastWriteTime, Length
            Get-Content -LiteralPath $log -Tail 40
        }
    }
}
