#=====================================================================
# run_bd_impl.tcl - 对已建好的 sound_loc_integrated 工程重跑综合/实现/位流
#
# 前置：create_sound_loc_project.tcl 已经建过工程（本脚本不重建 Block Design）
# 用法：vivado -mode batch -nolog -nojournal -notrace -source src/bd/run_bd_impl.tcl
# 环境变量：
#   QZ_BUILD_DIR  工程与产物目录，默认 <repo>/../quietzone_build/sound_loc_final
#   QZ_JOBS       并行 job 数，默认 8
# 产物：
#   <QZ_BUILD_DIR>/sound_loc_integrated.bit
#   <QZ_BUILD_DIR>/reports/*.rpt
#=====================================================================
set script_dir [file normalize [file dirname [info script]]]
set root_dir   [file normalize "$script_dir/.."]
if {[info exists ::env(QZ_BUILD_DIR)]} {
    set build_dir [file normalize $::env(QZ_BUILD_DIR)]
} else {
    set build_dir [file normalize "$root_dir/../quietzone_build/sound_loc_final"]
}
set jobs 8
if {[info exists ::env(QZ_JOBS)]} { set jobs $::env(QZ_JOBS) }

set prj "$build_dir/prj/sound_loc_integrated.xpr"
set rap "$build_dir/reports"
file mkdir $rap
if {![file exists $prj]} {
    error "找不到工程 $prj，请先跑 create_sound_loc_project.tcl"
}

open_project $prj
puts "IMPL_STEP: project opened"

reset_run synth_1
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
set sp [get_property PROGRESS [get_runs synth_1]]
puts "SYNTH_PROGRESS: $sp  STATUS: [get_property STATUS [get_runs synth_1]]"
if {$sp != "100%"} { error "synthesis did not complete" }

launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
set ip [get_property PROGRESS [get_runs impl_1]]
puts "IMPL_PROGRESS: $ip  STATUS: [get_property STATUS [get_runs impl_1]]"
if {$ip != "100%"} { error "implementation did not complete" }

open_run impl_1
report_utilization        -hierarchical -file [file join $rap utilization_routed.rpt]
report_timing_summary     -delay_type min_max -max_paths 20 -file [file join $rap timing_summary_routed.rpt]
report_drc                -file [file join $rap drc_routed.rpt]
report_clock_utilization  -file [file join $rap clock_utilization_routed.rpt]
puts "IMPL_STEP: reports written"

set bit [lsort -decreasing [glob -nocomplain [file join [get_property DIRECTORY [get_runs impl_1]] *.bit]]]
if {[llength $bit] == 0} { error "impl_1 没有产出 .bit" }
set bit [lindex $bit 0]
file copy -force $bit "$build_dir/sound_loc_integrated.bit"
puts "IMPL_BIT: $bit"
puts "IMPL_DONE"
