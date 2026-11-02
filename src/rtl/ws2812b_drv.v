//======================================================================
//  ws2812b_drv — 灯环纳秒时序引擎（方案书 §4.9）
//
//  作用：把 PS 算好的每颗灯颜色按时序推出去。颜色以 GRB 顺序给出，
//        本模块只负责"什么时候把 DIN 拉高/拉低"，不做任何颜色运算
//        （呼吸、渐变都在 PS，PL 占 CPU 0%——§4.9 的分工原则）。
//
//  时序（WS2812B，方案书 §4.9 表格）：
//    参数        标称      容差      本项目 50 MHz 实现
//    T0H（0 码高）0.40 µs  ±150 ns  20 拍 = 0.400 µs
//    T0L         0.85 µs  ±150 ns  42 拍 = 0.840 µs（−10 ns）
//    T1H         0.80 µs  ±150 ns  40 拍 = 0.800 µs
//    T1L         0.45 µs  ±150 ns  22 拍 = 0.440 µs（−10 ns）
//    位周期      1.25 µs   ±600 ns  62 拍 = 1.240 µs（−10 ns）
//    RESET（低） ≥280 µs   —        15000 拍 = 300 µs
//  注意 T0H+T0L == T1H+T1L == 62，所以"位周期"恒定，只有高电平长度区分 0/1。
//
//  为什么不是方案书的 100 MHz（登记在 docs/design_deviations.md 第 14 条）：
//    §4.9 按 100 MHz 给的是"40 拍 / 80 拍 / 125 拍"。本工程整链跑 50 MHz
//    （FCLK + I2S + 上板 ILA 全部按 50 MHz 验证过），而 1.25 µs 在 50 MHz 下是
//    62.5 拍，不是整数。取 62 拍（1.24 µs）比标称少 10 ns，远在 ±150 ns 的
//    相位容差和 ±600 ns 的周期容差之内，且 0/1 的高电平仍严格是 20/40 拍。
//    参数按 CLK_HZ 自动换算，将来换时钟只需改一个参数。
//
//  接口约定（两条，方案书没写，实现定死，tb 各有用例）：
//    1) **i_update 是上升沿触发**。reg_bank 的 o_led_update 是 1 拍脉冲；
//       用沿触发而不是电平触发，即使 PS 把某一位一直写 1 也不会反复刷灯。
//    2) **刷新期间来的 i_update 被忽略**（不排队、不打断正在发的帧）。
//       一帧 16 颗灯只要 0.6 ms，PS 20 ms 才更新一次，不会丢帧；
//       排队反而会让"改一帧颜色"延迟到下一帧，屏幕和灯环不同步。
//
//  端口差异（登记为第 14 条的一部分）：
//    §4.9 的 `i_led_color` 是数组型端口 `[23:0] i_led_color [0:LED_NUM-1]`，
//    工程规约（AGENTS.md 五）禁用数组型端口，改为展平一维总线
//    `[LED_NUM*24-1:0]`，**低 24 位是第一颗灯**，依次向上。
//
//  面积：纯计数器 + 移位寄存器，无乘法器、无除法器、无 BRAM。
//======================================================================
`timescale 1ns / 1ps

module ws2812b_drv #(
    parameter integer LED_NUM = 16,          // 灯环颗数（§4.9 默认 8，本项目用 16）
    parameter integer CLK_HZ  = 50_000_000   // 时钟频率，用来换算拍数
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire [LED_NUM*24-1:0] i_led_color,   // 展平 GRB：低 24 位 = 第 0 颗灯
    input  wire        i_update,                // 上升沿触发一次刷新
    output reg         o_din,
    output reg         o_busy
);

    //------------------------------------------------------------------
    // 拍数换算（整数运算，CLK_HZ 取整到 MHz）
    //------------------------------------------------------------------
    localparam integer CLK_MHZ = CLK_HZ / 1_000_000;
    localparam integer P_T0H   = (CLK_MHZ * 4)  / 10;    // 0.40 µs
    localparam integer P_T0L   = (CLK_MHZ * 85) / 100;   // 0.85 µs
    localparam integer P_T1H   = (CLK_MHZ * 8)  / 10;    // 0.80 µs
    localparam integer P_T1L   = (CLK_MHZ * 45) / 100;   // 0.45 µs
    localparam integer P_RESET = CLK_MHZ * 300;          // 300 µs（兼容 WS2812B-V5 ≥280 µs）

    localparam integer TOTAL_BITS = LED_NUM * 24;
    localparam integer BIT_CYCLES = P_T0H + P_T0L;       // = P_T1H + P_T1L

    localparam [2:0] S_IDLE    = 3'd0;
    localparam [2:0] S_RST_PRE = 3'd1;   // 帧前 RESET
    localparam [2:0] S_BITS    = 3'd2;   // 逐位发送
    localparam [2:0] S_RST_POST= 3'd3;   // 帧后 RESET（锁存）

    reg [2:0]  state;
    reg [TOTAL_BITS-1:0] bus_r;  // 颜色总线副本，每发完一颗右移 24 位
    reg [23:0] cur;              // 当前这颗灯的 24 位 GRB，MSB 先出
    reg [4:0]  led_i;            // 当前第几颗灯 0..LED_NUM-1
    reg [4:0]  bit_i;            // 这颗灯内第几位 0..23
    reg [11:0] cnt;              // 位内计数 0..BIT_CYCLES-1
    reg [13:0] rcnt;             // RESET 计数（300 µs = 15000 拍）
    reg        upd_d;            // i_update 打拍，用于取上升沿

    wire        upd_pulse = i_update & ~upd_d;
    wire        cur_bit   = cur[23];     // 先发高字节的最高位（G7）
    wire [11:0] hi_len    = cur_bit ? P_T1H[11:0] : P_T0H[11:0];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state  <= S_IDLE;
            o_din  <= 1'b0;
            o_busy <= 1'b0;
            bus_r  <= {TOTAL_BITS{1'b0}};
            cur    <= 24'd0;
            led_i  <= 5'd0;
            bit_i  <= 5'd0;
            cnt    <= 12'd0;
            rcnt   <= 14'd0;
            upd_d  <= 1'b0;
        end else begin
            upd_d <= i_update;

            case (state)
                //--------------------------------------------------
                S_IDLE: begin
                    o_din  <= 1'b0;
                    o_busy <= 1'b0;
                    if (upd_pulse) begin
                        bus_r  <= i_led_color;         // 低 24 位 = 第 0 颗灯
                        cur    <= i_led_color[23:0];   // 第 0 颗灯先发
                        led_i  <= 5'd0;
                        bit_i  <= 5'd0;
                        cnt    <= 12'd0;
                        rcnt   <= 14'd0;
                        o_busy <= 1'b1;
                        state  <= S_RST_PRE;
                    end
                end
                //--------------------------------------------------
                // 帧前 RESET：保证上一帧与这一帧之间有 > 50 µs 的低电平
                S_RST_PRE: begin
                    o_din <= 1'b0;
                    if (rcnt == P_RESET - 1) begin
                        rcnt  <= 14'd0;
                        cnt   <= 12'd0;
                        state <= S_BITS;
                    end else begin
                        rcnt <= rcnt + 14'd1;
                    end
                end
                //--------------------------------------------------
                // 逐位：高电平长度决定 0/1，低电平补齐到位周期
                S_BITS: begin
                    o_din <= (cnt < hi_len) ? 1'b1 : 1'b0;
                    if (cnt == BIT_CYCLES[11:0] - 12'd1) begin
                        cnt <= 12'd0;
                        if (bit_i == 5'd23) begin
                            bit_i <= 5'd0;
                            if (led_i == LED_NUM[4:0] - 5'd1) begin
                                rcnt  <= 14'd0;
                                state <= S_RST_POST;      // 一帧发完
                            end else begin
                                led_i <= led_i + 5'd1;
                                bus_r <= bus_r >> 24;              // 下一颗挪到低 24 位
                                cur   <= bus_r[47:24];             // 右移后的次低 24 位
                            end
                        end else begin
                            bit_i <= bit_i + 5'd1;
                            cur   <= {cur[22:0], 1'b0};
                        end
                    end else begin
                        cnt <= cnt + 12'd1;
                    end
                end
                //--------------------------------------------------
                S_RST_POST: begin
                    o_din <= 1'b0;
                    if (rcnt == P_RESET - 1) begin
                        rcnt   <= 14'd0;
                        o_busy <= 1'b0;
                        state  <= S_IDLE;
                    end else begin
                        rcnt <= rcnt + 14'd1;
                    end
                end
                //--------------------------------------------------
                default: begin
                    state  <= S_IDLE;
                    o_din  <= 1'b0;
                    o_busy <= 1'b0;
                end
            endcase
        end
    end

endmodule