<#
  一键跑两个自检 testbench（Vivado 2026.1 XSim）

  用法：
      powershell -ExecutionPolicy Bypass -File sim\run_sim.ps1
      powershell -ExecutionPolicy Bypass -File sim\run_sim.ps1 -VivadoBin "D:\VIVA\2026.1\Vivado\bin"

  做了什么：
      1. xvlog 编译 2. xelab 精化 3. xsim -runall 跑完
      两套 testbench 各自独立编译，互不干扰：
        tb_acq_top        采集通路（I2S 收 -> 乒乓 BRAM -> PS 读帧），Verilog
        tb_ws2812b_drv    WS2812B 时序（在 o_din 上做游程解码逐位比对），SystemVerilog

  产物：
      sim\logs\tb_acq_top_xsim.log
      sim\logs\tb_ws2812b_drv_xsim.log
      sim\logs\run_summary.txt
  注意：xsim 自己会在工作目录写 xsim.log，本脚本的控制台输出另存为 console_*.txt，避免抢占文件名。
#>
param(
    [string]$VivadoBin = 'D:\VIVA\2026.1\Vivado\bin',
    [string]$WorkRoot  = (Join-Path $env:TEMP ('qz_sim_' + (Get-Date -Format 'yyyyMMdd_HHmmss')))
)

$ErrorActionPreference = 'Stop'
$repo   = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir, $WorkRoot | Out-Null

function Invoke-Testbench {
    param(
        [string]$Name,
        [string[]]$Sources,
        [string]$Snapshot,
        [switch]$SystemVerilog
    )
    $dir = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Push-Location $dir
    try {
        $vlogArgs = @()
        if ($SystemVerilog) { $vlogArgs += '-sv' }
        $vlogArgs += $Sources
        & "$VivadoBin\xvlog.bat" @vlogArgs            2>&1 | Out-File -Encoding utf8 console_xvlog.txt
        & "$VivadoBin\xelab.bat" -debug typical $Name -s $Snapshot 2>&1 | Out-File -Encoding utf8 console_xelab.txt
        & "$VivadoBin\xsim.bat"  $Snapshot -runall    2>&1 | Out-File -Encoding utf8 console_xsim.txt
    }
    finally { Pop-Location }
    Copy-Item (Join-Path $dir 'console_xsim.txt') (Join-Path $logDir "$Name`_xsim.log") -Force
    $text = Get-Content (Join-Path $dir 'console_xsim.txt') -Raw
    $passed = ($text -match '>>> PASS <<<') -or ($text -match 'RESULT: PASS')
    $info = [pscustomobject]@{ Testbench = $Name; Result = $(if ($passed) {'PASS'} else {'FAIL'}); Log = "$Name`_xsim.log" }
    Write-Host ("[{0}] {1}  -> sim\logs\{0}_xsim.log" -f $Name, $info.Result)
    return $info
}

$results = @()
$results += Invoke-Testbench -Name 'tb_acq_top' -Snapshot 'acq_sim' -Sources @(
    (Join-Path $repo 'sim\tb\tb_acq_top.v'),
    (Join-Path $repo 'src\rtl\acq_top.v'),
    (Join-Path $repo 'src\rtl\acq_to_bram.v'),
    (Join-Path $repo 'src\rtl\i2s_rx.v')
)
$results += Invoke-Testbench -Name 'tb_ws2812b_drv' -Snapshot 'ws_sim' -SystemVerilog -Sources @(
    (Join-Path $repo 'sim\tb\tb_ws2812b_drv.sv'),
    (Join-Path $repo 'src\rtl\ws2812b_drv.v')
)

$results | Format-Table -AutoSize | Out-String | Write-Host
$results | Format-Table -AutoSize | Out-String | Set-Content -Path (Join-Path $logDir 'run_summary.txt') -Encoding utf8

if ($results.Result -contains 'FAIL') { exit 1 }
Write-Host 'ALL TESTBENCHES PASS'
