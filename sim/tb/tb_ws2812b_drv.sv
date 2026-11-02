//======================================================================
// tb_ws2812b_drv — WS2812B 纳秒时序引擎自检 testbench
//
// 验证思路：**不看 RTL 内部信号**，只在 o_din 上做游程（run-length）解码，
//   把真正发出去的波形还原成位流，再和输入颜色逐位比对。
//   每一位都必须满足：高电平 = 20 拍（0 码）或 40 拍（1 码），
//   对应低电平 = 42 拍或 22 拍，且高+低 = 62 拍（位周期恒定）。
//
// 采样用 negedge（50 MHz、20 ns 周期，DUT 在 posedge 更新，半周期后采不会踩竞争）。
//
// 覆盖：
//   T1  一帧 16 颗灯：384 位逐位还原比对 + 每位宽度精确 20/42/40/22 + 帧前 RESET ≥ 300 µs
//   T2  忙时 i_update 被忽略：帧中插一次更新，最终发出去的仍是第一帧的颜色，且不再补发
//   T3  帧间连续更新：第二帧颜色能正确发出
//   T4  GRB 顺序：第一颗灯 0xA1B2C3，第一个发出的必须是 G=0xA1（不是 R=0xB2）
//   R1  随机两帧颜色比对
//   T5  LED_NUM=8 参数化：方案书 §4.9 标称 8 颗灯也要能正确成帧
//
// 解码后的 dec_bus 采用和 DUT 输入完全相同的布局：dec_bus[j*24 +: 24] = 第 j 颗灯，
// 所以整帧比对可以直接 dec_bus == colors。整帧比对用 chk_bus（384 位），
// 不能用 chk_eq（形参是 longint，会截断到低 64 位）。
//======================================================================
`timescale 1ns / 1ps

module tb_ws2812b_drv;

    localparam integer LED_NUM = 16;
    localparam integer TOTAL   = LED_NUM * 24;      // 384 位
    localparam integer T0H = 20, T0L = 42;
    localparam integer T1H = 40, T1L = 22;
    localparam integer BITC = 62;                   // 位周期（拍）
    localparam integer RSTC = 15000;                // 帧后 RESET（拍）= 300 µs
    localparam integer MAX_RUNS = 1024;

    logic clk = 1'b0;
    always #10 clk = ~clk;                          // 50 MHz（20 ns）

    logic              rst_n;
    logic [TOTAL-1:0]  led_color;
    logic              i_update;
    wire               o_din;
    wire               o_busy;

    ws2812b_drv #(.LED_NUM(LED_NUM), .CLK_HZ(50_000_000)) dut (
        .clk (clk), .rst_n (rst_n),
        .i_led_color (led_color),
        .i_update (i_update),
        .o_din (o_din), .o_busy (o_busy)
    );

    //==================================================================
    // 第二份 DUT：LED_NUM=8（方案书 §4.9 的标称配置），验证参数化
    //   cap_sel = 0 时采 16 颗灯的 o_din，= 1 时采 8 颗灯的 o_din8
    //==================================================================
    localparam integer TOTAL8 = 8 * 24;          // 192 位

    logic [TOTAL8-1:0] led_color8;
    logic              i_update8 = 1'b0;
    wire               o_din8, o_busy8;
    logic              cap_sel = 1'b0;
    wire               cap_din = cap_sel ? o_din8 : o_din;
    wire               cap_upd = cap_sel ? i_update8 : i_update;

    ws2812b_drv #(.LED_NUM(8), .CLK_HZ(50_000_000)) dut8 (
        .clk (clk), .rst_n (rst_n),
        .i_led_color (led_color8),
        .i_update (i_update8),
        .o_din (o_din8), .o_busy (o_busy8)
    );

    //==================================================================
    // 游程采集（negedge 采样，长度单位 = 时钟拍）
    //==================================================================
    int   run_len [0:MAX_RUNS-1];
    logic run_lvl [0:MAX_RUNS-1];
    int   run_n;
    int   cur_len;
    logic din_d;

    always @(negedge clk) begin
        if (!rst_n) begin
            din_d   = 1'b0;
            cur_len = 0;
            run_n   = 0;
        end else begin
            if (cap_din !== din_d) begin
                if (run_n < MAX_RUNS) begin
                    run_len[run_n] = cur_len;
                    run_lvl[run_n] = din_d;
                    run_n = run_n + 1;
                end
                din_d   = cap_din;
                cur_len = 1;
            end else begin
                cur_len = cur_len + 1;
            end
        end
    end

    // 时间戳：i_update 拉高、第一个 din 上升沿
    time  t_upd, t_first_hi;
    logic upd_p, din_p, hi_flag;

    always @(posedge clk) begin
        if (cap_upd && !upd_p) t_upd = $time;
        if (cap_din && !din_p && !hi_flag) begin
            t_first_hi = $time;
            hi_flag    = 1'b1;
        end
        upd_p <= cap_upd;
        din_p <= cap_din;
    end

    //==================================================================
    // 检查辅助
    //==================================================================
    int errors = 0;
    int checks = 0;
    string tag = "";
    int i;

    task automatic chk_ok(input string what, input bit cond);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                $display("  [FAIL] %s : %s", tag, what);
            end
        end
    endtask

    task automatic chk_eq(input string what, input longint got, input longint exp);
        begin
            checks = checks + 1;
            if (got !== exp) begin
                errors = errors + 1;
                $display("  [FAIL] %s : %s got=%0d exp=%0d", tag, what, got, exp);
            end
        end
    endtask

    // 整帧颜色是 384 位，longint 只有 64 位，直接塞进 chk_eq 会静默截断
    // （两边都只留低 64 位，可能假通过也可能假失败）。整帧比对一律走这个任务。
    task automatic chk_bus(input string what,
                           input logic [TOTAL-1:0] got,
                           input logic [TOTAL-1:0] exp);
        begin
            checks = checks + 1;
            if (got !== exp) begin
                errors = errors + 1;
                $display("  [FAIL] %s : %s", tag, what);
                $display("         got = %h", got);
                $display("         exp = %h", exp);
            end
        end
    endtask

    //==================================================================
    // 帧解码：把 run 数组还原成位流
    //==================================================================
    logic [TOTAL-1:0] dec_bus;
    int               dec_bits, dec_err, tail_low;

    // 采集器只在电平跳变时落盘，而帧尾那段低电平（含帧后 RESET）之后不再跳变，
    // 所以要在这里把待记的那笔补上，否则最后一位会丢（只剩 383 位）。
    task automatic flush_capture;
        begin
            if (run_n < MAX_RUNS) begin
                run_len[run_n] = cur_len;
                run_lvl[run_n] = din_d;
                run_n = run_n + 1;
            end
        end
    endtask

    task automatic decode_frame;
        int k, hi_v, lo_v, bi, exp_lo, gi, gb;
        logic bit_v;      // 必须声明成 1 位：int 是 32 位，拼进 24 位寄存器会被截断掉高位
        logic [23:0] grp;
        begin
            flush_capture;
            dec_bus  = {TOTAL{1'b0}};
            dec_bits = 0;
            dec_err  = 0;
            tail_low = 0;
            hi_v     = -1;
            bi       = 0;
            gi       = 0;      // 已经拼好的第几颗灯
            gb       = 0;      // 这颗灯内已经收到几位
            grp      = 24'd0;
            for (k = 0; k < run_n; k = k + 1) begin
                if (run_lvl[k] === 1'b1) begin
                    hi_v = run_len[k];
                    // 高电平只能是 20 拍（0 码）或 40 拍（1 码）
                    if (hi_v !== T0H && hi_v !== T1H) begin
                        dec_err = dec_err + 1;
                        $display("  [宽度错] 第 %0d 个高电平段 = %0d 拍（应 20 或 40）", k, hi_v);
                    end
                end else begin
                    if (hi_v >= 0) begin
                        lo_v  = run_len[k];
                        bit_v = (hi_v === T1H);
                        if (bi == TOTAL - 1) begin
                            // 最后一位的低电平段里还含着帧后 RESET
                            exp_lo   = (bit_v ? T1L : T0L) + RSTC;
                            tail_low = lo_v;
                            if (lo_v < RSTC + 22) begin
                                dec_err = dec_err + 1;
                                $display("  [尾 RESET 太短] lo=%0d 拍", lo_v);
                            end
                        end else begin
                            exp_lo = bit_v ? T1L : T0L;
                            if (lo_v !== exp_lo) begin
                                dec_err = dec_err + 1;
                                $display("  [宽度错] 第 %0d 位 低电平 = %0d 拍（应 %0d）",
                                         bi, lo_v, exp_lo);
                            end
                            if (hi_v + lo_v !== BITC) begin
                                dec_err = dec_err + 1;
                                $display("  [周期错] 第 %0d 位 高+低 = %0d 拍（应 %0d）",
                                         bi, hi_v + lo_v, BITC);
                            end
                        end
                        if (bi < TOTAL) begin
                            // 按 DUT 的总线布局拼：第 j 颗灯放在 dec_bus[j*24 +: 24]，
                            // 颗内仍是 MSB 先到，这样 dec_bus 可以直接和 i_led_color 比对。
                            grp = {grp[22:0], bit_v};
                            gb  = gb + 1;
                            if (gb == 24) begin
                                dec_bus[gi*24 +: 24] = grp;
                                gi = gi + 1;
                                gb = 0;
                            end
                            bi = bi + 1;
                        end else begin
                            dec_err = dec_err + 1;
                            $display("  [位数过多] 已经解出 %0d 位还在继续", bi);
                        end
                        hi_v = -1;
                    end
                end
            end
            dec_bits = bi;
        end
    endtask

    //==================================================================
    // 8 颗灯版本解码（复用同一套游程数组，布局同样对齐 DUT 输入总线）
    //==================================================================
    logic [TOTAL-1:0] dec_bus8;          // 只有低 TOTAL8 位有效，高位恒 0
    int               dec8_bits, dec8_err, dec8_tail;

    task automatic decode_frame8;
        int k, hi_v, lo_v, bi, gi, gb;
        logic bit_v;
        logic [23:0] grp;
        begin
            flush_capture;
            dec_bus8  = {TOTAL{1'b0}};
            dec8_bits = 0;
            dec8_err  = 0;
            dec8_tail = 0;
            hi_v = -1; bi = 0; gi = 0; gb = 0; grp = 24'd0;
            for (k = 0; k < run_n; k = k + 1) begin
                if (run_lvl[k] === 1'b1) begin
                    hi_v = run_len[k];
                    if (hi_v !== T0H && hi_v !== T1H) begin
                        dec8_err = dec8_err + 1;
                        $display("  [宽度错] 第 %0d 个高电平段 = %0d 拍（应 20 或 40）", k, hi_v);
                    end
                end else if (hi_v >= 0) begin
                    lo_v  = run_len[k];
                    bit_v = (hi_v === T1H);
                    if (bi == TOTAL8 - 1) begin
                        dec8_tail = lo_v;                     // 末位低电平里含着帧后 RESET
                        if (lo_v < RSTC + 22) begin
                            dec8_err = dec8_err + 1;
                            $display("  [尾 RESET 太短] lo=%0d 拍", lo_v);
                        end
                    end else if (lo_v !== (bit_v ? T1L : T0L)) begin
                        dec8_err = dec8_err + 1;
                        $display("  [宽度错] 第 %0d 位 低电平 = %0d 拍（应 %0d）",
                                 bi, lo_v, bit_v ? T1L : T0L);
                    end else if (hi_v + lo_v !== BITC) begin
                        dec8_err = dec8_err + 1;
                        $display("  [周期错] 第 %0d 位 高+低 = %0d 拍（应 %0d）",
                                 bi, hi_v + lo_v, BITC);
                    end
                    if (bi < TOTAL8) begin
                        grp = {grp[22:0], bit_v};
                        gb  = gb + 1;
                        if (gb == 24) begin
                            dec_bus8[gi*24 +: 24] = grp;
                            gi = gi + 1;
                            gb = 0;
                        end
                        bi = bi + 1;
                    end else begin
                        dec8_err = dec8_err + 1;
                        $display("  [位数过多] 8 颗灯只该有 %0d 位，还在继续", TOTAL8);
                    end
                    hi_v = -1;
                end
            end
            dec8_bits = bi;
        end
    endtask

    //==================================================================
    // 激励任务
    //==================================================================
    task automatic arm_capture;
        begin
            @(negedge clk);
            run_n   = 0;
            cur_len = 0;
            din_d   = cap_din;
            hi_flag = 1'b0;
            upd_p   = cap_upd;
        end
    endtask

    task automatic fire_update(input logic [TOTAL-1:0] colors);
        begin
            @(negedge clk);
            led_color = colors;
            i_update  = 1'b1;
            @(negedge clk);
            i_update  = 1'b0;
        end
    endtask

    task automatic wait_idle;
        begin
            wait (o_busy === 1'b1);            // 先确认真的忙起来
            while (o_busy !== 1'b0) @(negedge clk);
            repeat (3) @(negedge clk);
        end
    endtask

    task automatic do_reset;
        begin
            rst_n     = 1'b0;
            i_update  = 1'b0;
            i_update8 = 1'b0;
            led_color = {TOTAL{1'b0}};
            led_color8 = {TOTAL8{1'b0}};
            repeat (5) @(posedge clk);
            rst_n = 1'b1;
            repeat (3) @(posedge clk);
            @(negedge clk);
        end
    endtask

    task automatic fire_update8(input logic [TOTAL8-1:0] colors);
        begin
            @(negedge clk);
            led_color8 = colors;
            i_update8  = 1'b1;
            @(negedge clk);
            i_update8  = 1'b0;
        end
    endtask

    task automatic wait_idle8;
        begin
            wait (o_busy8 === 1'b1);
            while (o_busy8 !== 1'b0) @(negedge clk);
            repeat (3) @(negedge clk);
        end
    endtask

    //==================================================================
    // 主流程
    //==================================================================
    logic [TOTAL-1:0] colors_a, colors_b;
    int pre_ns;

    initial begin
        $display("=== tb_ws2812b_drv 开始 ===");
        do_reset;

        //--------------------------------------------------------------
        $display("[T1] 一帧 16 颗灯：位流还原 + 每位宽度 + 帧前 RESET");
        tag = "T1 单帧";
        for (i = 0; i < LED_NUM; i = i + 1)
            colors_a[i*24 +: 24] = 24'h010203 + i * 24'h0001011;
        colors_a[23:0] = 24'h000000;         // 第 0 颗全 0（全是 0 码）
        colors_a[47:24] = 24'hFFFFFF;        // 第 1 颗全 1（全是 1 码）
        colors_a[TOTAL-1 -: 24] = 24'hA1B2C3;// 第 15 颗：GRB 三字节互不相同
        arm_capture;
        fire_update(colors_a);
        wait_idle;
        decode_frame;
        chk_eq("解出的位数", dec_bits, TOTAL);
        chk_eq("位数/宽度错误数", dec_err, 0);
        chk_bus("还原的位流 == 输入颜色", dec_bus, colors_a);
        chk_ok("帧尾低电平 ≥ 300 µs + 末位低", tail_low >= RSTC + 22);
        chk_eq("游程段数（1 段前导低 + 384 高 + 384 低）", run_n, 1 + TOTAL*2);
        pre_ns = int'(t_first_hi - t_upd);
        chk_ok($sformatf("帧前 RESET = %0d ns（要求 ≥ 300000）", pre_ns),
               pre_ns >= 300000 && pre_ns <= 320000);
        $display("  [信息] 帧前 RESET = %0d ns，帧尾低电平 = %0d 拍", pre_ns, tail_low);

        //--------------------------------------------------------------
        $display("[T2] 忙时 i_update 被忽略（不打断、不补发）");
        tag = "T2 忙时忽略";
        colors_b = colors_a;
        colors_b[23:0] = 24'hFF0000;         // 若被采纳，第 0 颗灯会变
        arm_capture;
        fire_update(colors_a);
        wait (o_busy === 1'b1);
        repeat (100) @(negedge clk);         // 帧中间插一次更新
        fire_update(colors_b);
        wait_idle;
        decode_frame;
        chk_eq("忙时插更新后位数", dec_bits, TOTAL);
        chk_eq("忙时插更新后错误数", dec_err, 0);
        chk_eq("忙时插更新后游程段数（没有多发）", run_n, 1 + TOTAL*2);
        chk_bus("发出去的仍是第一帧颜色", dec_bus, colors_a);
        chk_eq("忙结束后 o_busy 应为 0", {31'd0, o_busy}, 0);
        chk_eq("忙结束后 o_din 应为 0", {31'd0, o_din},  0);
        repeat (200) @(negedge clk);
        chk_eq("没有补发第二帧", {31'd0, o_busy}, 0);

        //--------------------------------------------------------------
        $display("[T3] 帧间连续更新");
        tag = "T3 连续两帧";
        arm_capture;
        fire_update(colors_b);
        wait_idle;
        decode_frame;
        chk_eq("第二帧位数", dec_bits, TOTAL);
        chk_eq("第二帧错误数", dec_err, 0);
        chk_bus("第二帧颜色", dec_bus, colors_b);

        //--------------------------------------------------------------
        $display("[T4] GRB 顺序：第 0 颗 0xA1B2C3，先发 G");
        tag = "T4 GRB";
        colors_a = {TOTAL{1'b0}};
        colors_a[23:0] = 24'hA1B2C3;
        arm_capture;
        fire_update(colors_a);
        wait_idle;
        decode_frame;
        chk_eq("第 0 颗解出 = 0xA1B2C3", dec_bus[23:0], 24'hA1B2C3);
        chk_eq("最先发出的 8 位是 G=0xA1", dec_bus[23:16], 8'hA1);
        chk_eq("第 1~15 颗全 0", dec_bus[TOTAL-1:24], {(TOTAL-24){1'b0}});

        //--------------------------------------------------------------
        $display("[R1] 随机两帧");
        tag = "R1 随机";
        for (i = 0; i < 2; i = i + 1) begin
            int unsigned rv;
            rv = $urandom();
            for (int unsigned j = 0; j < LED_NUM; j = j + 1)
                colors_a[j*24 +: 24] = $urandom() & 24'hFFFFFF;
            arm_capture;
            fire_update(colors_a);
            wait_idle;
            decode_frame;
            chk_eq($sformatf("随机帧 %0d 位数", i), dec_bits, TOTAL);
            chk_eq($sformatf("随机帧 %0d 错误数", i), dec_err, 0);
            chk_bus($sformatf("随机帧 %0d 颜色", i), dec_bus, colors_a);
        end

        //--------------------------------------------------------------
        //--------------------------------------------------------------
        $display("[T5] LED_NUM=8 参数化：方案书 §4.9 标称 8 颗灯");
        tag = "T5 八颗灯";
        cap_sel = 1'b1;
        led_color8 = {TOTAL8{1'b0}};
        led_color8[23:0]           = 24'hFF0000;   // 第 0 颗
        led_color8[47:24]          = 24'h00FF00;   // 第 1 颗
        led_color8[71:48]          = 24'h0000FF;   // 第 2 颗
        led_color8[95:72]          = 24'hFFFFFF;   // 第 3 颗（全 1 码）
        led_color8[119:96]         = 24'h000000;   // 第 4 颗（全 0 码）
        led_color8[143:120]        = 24'h123456;   // 第 5 颗
        led_color8[167:144]        = 24'hABCDEF;   // 第 6 颗
        led_color8[TOTAL8-1 -: 24] = 24'h0F0F0F;   // 第 7 颗（最后一颗）
        arm_capture;
        fire_update8(led_color8);
        wait_idle8;
        decode_frame8;
        chk_eq("8 颗灯解出位数", dec8_bits, TOTAL8);
        chk_eq("8 颗灯宽度/周期错误数", dec8_err, 0);
        chk_eq("8 颗灯游程段数（1 + 192 高 + 192 低）", run_n, 1 + TOTAL8*2);
        chk_bus("8 颗灯颜色（第 j 颗在 [j*24 +: 24]）",
                dec_bus8, {{(TOTAL-TOTAL8){1'b0}}, led_color8});
        chk_ok("8 颗灯帧尾低电平 ≥ 300 µs + 末位低", dec8_tail >= RSTC + 22);
        $display("  [信息] 8 颗灯帧尾低电平 = %0d 拍", dec8_tail);

        if (errors == 0) $display("\nRESULT: PASS  (checks=%0d, errors=0)", checks);
        else             $display("\nRESULT: FAIL  (checks=%0d, errors=%0d)", checks, errors);
        $finish;
    end

endmodule