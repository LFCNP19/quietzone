<#
  build\build.ps1 - 从零构建：Block Design -> 综合 -> 实现 -> 位流 -> XSA -> 报告

  用法：
      powershell -ExecutionPolicy Bypass -File build\build.ps1
      powershell -ExecutionPolicy Bypass -File build\build.ps1 -BuildDir D:\qz_build -Jobs 4
      powershell -ExecutionPolicy Bypass -File build\build.ps1 -BdOnly     # 只搭 BD，不综合（约 1 分钟）

  参数：
      -VivadoBin  Vivado 2026.1 的 bin 目录
      -BuildDir   工程与产物目录，默认 build\out（体积约 100 MB，不进提交包）
      -Jobs       综合/实现并行 job 数
      -BdOnly     只跑到 block design 校验通过

  产物（都在 -BuildDir 下）：
      prj\sound_loc_integrated.xpr
      sound_loc_integrated.bit
      sound_loc_integrated.xsa
      reports\timing_summary_routed.rpt / utilization_routed.rpt / drc_routed.rpt

  说明：build\reports 里放的是随包交付的那份位流对应的报告快照，本脚本不会覆盖它。
#>
param(
    [string]$VivadoBin = 'D:\VIVA\2026.1\Vivado\bin',
    [string]$BuildDir  = (Join-Path $PSScriptRoot 'out'),
    [int]$Jobs         = 8,
    [switch]$BdOnly
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
$BuildDir = (Resolve-Path -LiteralPath $BuildDir).Path

$env:QZ_BUILD_DIR = $BuildDir
$env:QZ_JOBS      = "$Jobs"
if ($BdOnly) { $env:SL_STAGE = 'bd' } else { Remove-Item Env:\SL_STAGE -ErrorAction SilentlyContinue }

$vivado = Join-Path $VivadoBin 'vivado.bat'
$tcl    = Join-Path $repo 'src\bd\create_sound_loc_project.tcl'
if (-not (Test-Path -LiteralPath $vivado)) { throw "找不到 Vivado：$vivado" }
if (-not (Test-Path -LiteralPath $tcl))    { throw "找不到构建脚本：$tcl" }

Write-Host "Vivado   : $vivado"
Write-Host "构建目录 : $BuildDir"
Write-Host "阶段     : $(if ($BdOnly) {'bd'} else {'all（BD -> synth -> impl -> bit -> xsa）'})"
Write-Host "开始时间 : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

& $vivado -mode batch -notrace -source $tcl -log (Join-Path $BuildDir 'build.log') -journal (Join-Path $BuildDir 'build.jou')
$code = $LASTEXITCODE
Write-Host "结束时间 : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  exit=$code"
if ($code -ne 0) { throw "Vivado 构建失败（exit $code），看 $BuildDir\build.log" }

$bit = Join-Path $BuildDir 'sound_loc_integrated.bit'
$xsa = Join-Path $BuildDir 'sound_loc_integrated.xsa'
if (-not $BdOnly) {
    foreach ($f in @($bit, $xsa)) {
        if (-not (Test-Path -LiteralPath $f)) { throw "构建结束但缺少产物：$f" }
    }
    Get-Item -LiteralPath $bit, $xsa |
        Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host "时序与资源报告：$BuildDir\reports"
}
Write-Host 'BUILD OK'
