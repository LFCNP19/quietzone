`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////
//  tb_acq_top.v  ——  采集通路的自检仿真
//
//  【验证什么】
//    1. 四个麦克风同时送固定的测试数据，看写进 BRAM 的 64 位对不对；
//    2. 看地址序列是不是"格号连续递增、满箱后跳到另一箱"；
//    3. 用一个 PS 模型轮询握手，看乒乓能不能让 PL 连续写不停顿。
//
//  【为什么用 FRAME=16 而不是 1024】
//    1024 下一帧要 1024 × 1024 = 104 万拍，跑 8 帧太慢。
//    把帧长缩到 16，一帧只要 16 × 1024 = 1.6 万拍，
//    状态机、握手、地址逻辑完全一样，只是数得快 —— 几秒钟就能出结果。
//    真实工程里 FRAME 用 1024，差别只在参数。
//////////////////////////////////////////////////////////////////////////////
module tb_acq_top;

    //--------------------------------------------------------------
    // 参数
    //--------------------------------------------------------------
    localparam integer FRAME = 16;

    //--------------------------------------------------------------
    // 四个通道的固定测试图案
    //   低 8 位填 0：DUT 只取高 16 位（[23:8]）存进 BRAM，
    //   所以这里让低 8 位为 0，期望值一眼能看懂。
    //--------------------------------------------------------------
    localparam [23:0] D0 = 24'h123400;
    localparam [23:0] D1 = 24'hABCD00;
    localparam [23:0] D2 = 24'h0F0F00;
    localparam [23:0] D3 = 24'hF0F000;

    // 期望写进 BRAM 的 64 位：{CH3, CH2, CH1, CH0}
    localparam [63:0] EXPECT = {D3[23:8], D2[23:8], D1[23:8], D0[23:8]};

    //--------------------------------------------------------------
    // 时钟
    //--------------------------------------------------------------
    reg clk = 1'b0;
    always #10 clk = ~clk;          // 周期 20 ns = 50 MHz

    //--------------------------------------------------------------
    // 麦克风数据源（移位寄存器，MSB 先出）
    //--------------------------------------------------------------
    reg [23:0] sh0, sh1, sh2, sh3;

    wire sd0 = sh0[23];
    wire sd1 = sh1[23];
    wire sd2 = sh2[23];
    wire sd3 = sh3[23];

    //--------------------------------------------------------------
    // DUT 端口信号
    //--------------------------------------------------------------
    wire        mic_sck, mic_ws;
    wire        bram_clk, bram_rst, bram_en, bram_we;
    wire [10:0] bram_addr;
    wire [63:0] bram_din;
    wire [1:0]  wr_cnt_ps;

    reg  [1:0]  rd_cnt = 2'd0;      // 模拟 PS 的账本

    //--------------------------------------------------------------
    // DUT
    //--------------------------------------------------------------
    acq_top #(
        .FRAME (FRAME)
    ) dut (
        .clk_50m   (clk),
        .mic_sck   (mic_sck),
        .mic_ws    (mic_ws),
        .mic_sd0   (sd0),
        .mic_sd1   (sd1),
        .mic_sd2   (sd2),
        .mic_sd3   (sd3),
        .rd_cnt_ps (rd_cnt),
        .wr_cnt_ps (wr_cnt_ps),
        .bram_clk  (bram_clk),
        .bram_rst  (bram_rst),
        .bram_en   (bram_en),
        .bram_we   (bram_we),
        .bram_addr (bram_addr),
        .bram_din  (bram_din)
    );

    //--------------------------------------------------------------
    // 麦克风模型：跟着 DUT 内部的 cnt 走
    //   数据在 SCK 下降沿（cnt[3:0]==0）更新，DUT 在 cnt[3:0]==7 采样。
    //   第 1 个 SCK 周期（bit_idx=0）装载数据，从 bit_idx=1 起才在 SD 上
    //   呈现 MSB —— 正好对应 I2S 的"MSB 延迟 1 位"。
    //--------------------------------------------------------------
    wire [9:0] dcnt   = dut.cnt;      // 层次引用：直接观察 DUT 内部计数器
    wire       drst_n = dut.rst_n;

    always @(posedge clk) begin
        if (!drst_n) begin
            sh0 <= D0;  sh1 <= D1;  sh2 <= D2;  sh3 <= D3;
        end
        else if (dcnt[9] == 1'b0 && dcnt[3:0] == 4'd0) begin
            if (dcnt[8:4] == 5'd0) begin
                // 一帧开始，装载数据（此时 SD 上呈现 MSB）
                sh0 <= D0;  sh1 <= D1;  sh2 <= D2;  sh3 <= D3;
            end
            else if (dcnt[8:4] >= 5'd2 && dcnt[8:4] <= 5'd25) begin
                // 之后每个 SCK 周期左移一位
                sh0 <= {sh0[22:0], 1'b0};
                sh1 <= {sh1[22:0], 1'b0};
                sh2 <= {sh2[22:0], 1'b0};
                sh3 <= {sh3[22:0], 1'b0};
            end
        end
    end

    //--------------------------------------------------------------
    // PS 模型：轮询账本，发现新数据就"读"一区，读完立刻记账
    //--------------------------------------------------------------
    reg       ps_busy = 1'b0;
    integer   ps_wait = 0;
    integer   frame_read = 0;

    always @(posedge clk) begin
        if (bram_rst) begin
            rd_cnt    <= 2'd0;
            ps_busy   <= 1'b0;
            ps_wait   <= 0;
        end
        else if (ps_busy) begin
            if (ps_wait == 0) begin
                rd_cnt     <= rd_cnt + 2'd1;    // 读完，立刻记账
                ps_busy    <= 1'b0;
                frame_read = frame_read + 1;
            end
            else begin
                ps_wait <= ps_wait - 1;
            end
        end
        else if (wr_cnt_ps != rd_cnt) begin
            ps_busy <= 1'b1;
            ps_wait <= 200;      // 模拟 PS 读一区数据的耗时（200 拍 = 4 µs）
        end
    end

    //--------------------------------------------------------------
    // 行为级 BRAM 模型 + 在线检查
    //--------------------------------------------------------------
    reg     [63:0] mem [0:2047];
    integer        addr_hit [0:2047];
    integer        wr_total = 0;
    integer        wr_err   = 0;
    integer        gap_err  = 0;
    integer        vld_cnt  = 0;

    reg     [10:0] prev_addr = 11'd0;
    reg            addr_run  = 1'b0;

    integer i, k;
    integer sum_a = 0;
    integer sum_b = 0;

    always @(posedge clk) begin
        if (bram_we) begin
            // 1) 模拟 BMG 的写入行为
            mem[bram_addr] <= bram_din;

            // 2) 数据正确性检查
            wr_total = wr_total + 1;
            addr_hit[bram_addr] = addr_hit[bram_addr] + 1;
            if (bram_din !== EXPECT) begin
                wr_err = wr_err + 1;
                if (wr_err <= 5)
                    $display("  [DATA ERR] t=%0t addr=%0d din=%h expect=%h",
                             $time, bram_addr, bram_din, EXPECT);
            end

            // 3) 地址连续性检查
            //    同一箱内地址应该逐格 +1；
            //    允许的跳变只有"区0末格 -> 区1首格"和"区1末格 -> 区0首格"。
            if (addr_run) begin
                if (bram_addr != prev_addr + 11'd1) begin
                    if (!((prev_addr == FRAME-1 && bram_addr == FRAME) ||
                          (prev_addr == 2*FRAME-1 && bram_addr == 0))) begin
                        gap_err = gap_err + 1;
                        if (gap_err <= 5)
                            $display("  [ADDR ERR] t=%0t prev=%0d now=%0d",
                                     $time, prev_addr, bram_addr);
                    end
                end
            end
            prev_addr <= bram_addr;
            addr_run  <= 1'b1;
        end
    end

    // 采样点计数（每 1024 拍应该来一次）
    always @(posedge clk) if (dut.all_vld) vld_cnt = vld_cnt + 1;

    //--------------------------------------------------------------
    // 主流程
    //--------------------------------------------------------------
    initial begin
        for (i = 0; i < 2048; i = i + 1) addr_hit[i] = 0;

        wait (drst_n == 1'b1);
        $display("[%0t] reset released, start acquisition...", $time);

        wait (frame_read >= 6);          // 等 PS 读走 6 帧
        repeat (100) @(posedge clk);

        sum_a = 0;
        sum_b = 0;
        for (k = 0; k < FRAME; k = k + 1)        sum_a = sum_a + addr_hit[k];
        for (k = FRAME; k < 2*FRAME; k = k + 1)  sum_b = sum_b + addr_hit[k];

        $display("");
        $display("================ RESULT ================");
        $display(" BRAM writes total     : %0d", wr_total);
        $display(" data errors           : %0d", wr_err);
        $display(" address sequence errs : %0d", gap_err);
        $display(" sample_valid pulses   : %0d", vld_cnt);
        $display(" frames read by PS     : %0d", frame_read);
        $display(" writes per frame      : %0d (expect %0d)",
                 wr_total / frame_read, FRAME);
        $display("----------------------------------------");
        $display(" bank0 (addr 0..%0d)      written %0d times",
                 FRAME-1, sum_a);
        $display(" bank1 (addr %0d..%0d)     written %0d times",
                 FRAME, 2*FRAME-1, sum_b);
        $display("========================================");
        if (wr_err == 0 && gap_err == 0)
            $display(" >>> PASS <<<");
        else
            $display(" >>> FAIL <<<");
        $finish;
    end

    // 超时保护
    initial begin
        #6000000;
        $display("");
        $display("!!! TIMEOUT - simulation aborted !!!");
        $display(" frames read by PS = %0d, BRAM writes = %0d", frame_read, wr_total);
        $finish;
    end

endmodule
