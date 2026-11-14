param(
    [Parameter(Mandatory=$true)][string[]]$Images,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$IoDevice,
    [string]$Baseline,
    [string]$FreezeBaselineLabel,
    [switch]$CreateIndex,
    [string]$DriverImage = 'minivectordb-protocol-check:driver'
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
function ContainerPath([string]$path) {
    $resolved = [IO.Path]::GetFullPath((Join-Path $projectRoot $path))
    if (-not $resolved.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Benchmark paths must be inside the project.'
    }
    return '/work/' + $resolved.Substring($projectRoot.Length + 1).Replace('\', '/')
}
$dockerRoot = (docker info --format '{{.DockerRootDir}}').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable.' }
$arguments = @('run', '--rm', '--network', 'none', '--cgroupns', 'host',
    '--env', 'PYTHONDONTWRITEBYTECODE=1',
    '--mount', 'type=bind,src=/sys/fs/cgroup,dst=/host-cgroup',
    '--mount', "type=bind,src=$dockerRoot/volumes,dst=/host-volumes",
    '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock',
    '--mount', "type=bind,src=$projectRoot,dst=/work", '--workdir', '/work',
    '--entrypoint', 'python', $DriverImage, '-m', 'performance_checker.paired',
    '--io-device', $IoDevice, '--oracle-cache', '/work/.artifacts/oracle',
    '--output', (ContainerPath $OutputDirectory), '--images') + $Images
if ($Baseline) { $arguments += @('--baseline', (ContainerPath $Baseline)) }
if ($FreezeBaselineLabel) { $arguments += @('--freeze-baseline-label', $FreezeBaselineLabel) }
if ($CreateIndex) { $arguments += '--create-index' }
& docker @arguments
exit $LASTEXITCODE
