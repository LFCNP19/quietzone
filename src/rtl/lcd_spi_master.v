//======================================================================
//  lcd_spi_master — 圆屏 SPI 刷屏引擎（方案书 §4.10 后半）
//
//  分工（方案书原文："PL：spi 位引擎 + 帧存读地址生成 + 自动循环刷新 + 双缓冲切换"）：
//    PL 负责：① SPI 位引擎（模式 0，MSB 先出）② 帧存读地址生成
//             ③ 一帧发完自动从头再来（自动循环刷新）④ 帧边界切换双缓冲
//     PS 负责：只把像素写进帧存，不碰任何 SPI 时序。
//
//  上电流程：拉低 LCD_RST → 等 RST_MS → 放开 → 等 POR_MS → 挨个发初始化命令 → 
//            进入"每帧：窗口命令 + 像素流"的无限循环。
//
//  每帧要发的字节（11 个窗口字节 + 2×像素个数 个数据字节）：
//    0x2A(CASET) XS高 XS低 XE高 XE低 | 0x2B(RASET) YS高 YS低 YE高 YE低 | 0x2C(RAMWR) 像素...
//    XS/YS 固定 0，XE/YE = IMG_W-1 / IMG_H-1。窗口命令里 DC=0，像素数据 DC=1。
//
//  色深（登记为设计偏差第 16 条）：
//    PIX_BITS=8（RGB332，默认）时帧存里 1 个字节 1 个像素，但 **GC9A01 的 COLMOD 只支持
//    12/16/18 位（0x03/0x05/0x06），没有 8 位**，所以本模块把 8 位像素按位扩展成 RGB565
//    再发（R3→R5、G3→G6、B2→B5，纯位复制，无乘法器）。也就是说 RGB332 省的是 BRAM，
//    不省 SPI 带宽；两种色深的 SPI 传输量都是 16 位/像素。
//    PIX_BITS=16（RGB565）时帧存里就是 16 位像素，直接发。
//
//  像素取数握手（和 lcd_framebuf 端口 B 对接）：
//    本模块拉高 o_pix_req 并给出字地址 o_pix_addr；数据源在 i_pix_valid 拉高的那一拍
//    给出对应的 32 位字，本模块锁存后继续发。顶层接线：
//      fb.enb = o_pix_req;  fb.addrb = o_pix_addr;
//      i_pix = fb.doutb;    i_pix_valid = o_pix_req 延迟 1 拍;   // BRAM 读延迟 1 拍
//    req 会一直保持到 valid 到来，所以延迟大于 1 拍的数据源也能接。
//
//  双缓冲：i_buf_sel 在本帧开始时锁存，本帧地址一律加上 buf_sel×每帧字数。
//    这样刷新过程中即使 PS 改了选择位，本帧也不会中途换到别的缓冲。
//    ⚠ 双缓冲要求帧存按 2 倍深度例化（RGB332 时 32 块 BRAM）；
//      本工程默认单缓冲（i_buf_sel 接 0，帧存 16 块），见偏差第 16 条。
//
//  o_busy：整个"刷一帧"期间为 1（从发窗口命令开始到最后一个像素发完），
//          直接接 reg_bank 的 STATUS.bit1「刷屏忙」。
//
//  i_hold（037 轮新增）：高电平时**不开新帧**，停在帧间空隙里把 CS 拉高。
//    用途：帧存是单缓冲，"面板读帧存"和"UI 改帧存"同时发生就会出现一条
//    自上而下（或自下而上）移动的撕裂横线。色环 UI 在改帧存前先把 o_hold 拉高，
//    本模块就把当前帧发完、然后在 S_GAP 里等到 i_hold 落下再开下一帧，
//    于是"改帧存"与"读帧存"永不同时进行，屏幕上不再有那条线。
//    代价：每次重绘让刷屏停 ~1 ms（一帧 37 ms），肉眼看不出来。
//
//  三个必须在实现里守住的点（019 轮仿真查出来并修掉，细节见 design_deviations 第 16 条）：
//    1) **下单握手靠 tx_pend，不靠 tx_busy**。tx_busy 比 tx_start 晚一拍才拉起，
//       只判 !tx_busy 会在同一个字节上连下两次单，把每隔一个字节悄悄吞掉。
//       三处下单点（S_INIT / S_WIN / S_PIX）一律 `if (!tx_busy && !tx_pend)`，
//       下单同时 `tx_pend <= 1`，`tx_done` 时清 0。
//    2) **帧尾要等 tx_done 才收 CS**。末字节"下完单"不等于"发完了"，
//       早收 CS 会把最后一个像素截断（屏幕上看像坏点）。用 frame_ending 走等 tx_done 分支。
//    3) **每帧字数向上取整**：字数 = (NPIX + SUB - 1) / SUB，不能向下取整，
//       否则像素数不是每字像素数整数倍时末字被整字丢掉。
//    延时拍数用**常量函数** cmd_dly_cyc() 算（SLEEP_CYC / DISPON_CYC），不要写成
//    "毫秒数 × 每毫秒拍数"的运行时乘法——那样会白摆一个 DSP48E1。
//
//  面积：位引擎是移位寄存器 + 计数器；地址生成是加 1 计数器；
//       初始化序列表是 185×9 bit 的 ROM（综合成 LUT/小块 RAM），无乘法器、无除法器。
//  实测（2026.1，xc7z020clg400-2，模块级综合）：LUT 221 / FF 159 / BRAM 0 / DSP 0；
//       WNS +15.922 ns、WHS +0.120 ns @ 20 ns。240×240 RGB332 一帧 43.80 ms ≈ 22.8 fps。
//======================================================================
`timescale 1ns / 1ps

module lcd_spi_master #(
    parameter integer CLK_HZ   = 50_000_000,   // 系统时钟
    parameter integer SPI_HZ   = 25_000_000,   // 目标 SCL 频率（50 MHz 时钟下最高 25 MHz）
    parameter integer IMG_W    = 240,
    parameter integer IMG_H    = 240,
    parameter integer PIX_BITS = 8,            // 8 = RGB332（内部扩到 RGB565 再发），16 = RGB565
    parameter integer RST_MS   = 10,           // LCD_RST 拉低时长（ms）
    parameter integer POR_MS   = 120,          // 放开复位后等多久再发命令（ms）
    parameter integer MS_TICKS = 0             // 1 ms 等于几拍；0 = 自动 = CLK_HZ/1000（仿真可调小）
)(
    input  wire        clk,
    input  wire        rst_n,
    // 帧存接口（对接 lcd_framebuf 端口 B）
    input  wire [31:0] i_pix,
    input  wire        i_pix_valid,
    input  wire        i_buf_sel,              // 双缓冲选择，帧边界生效
    input  wire        i_hold,                 // 高电平：暂停开新帧（给色环 UI 改帧存的时间窗）
    // SPI 引脚
    output reg         o_scl,
    output reg         o_sda,
    output reg         o_cs,
    output reg         o_dc,
    output reg         o_rst,
    // 帧存读请求
    output reg         o_pix_req,
    output reg  [16:0] o_pix_addr,
    // 状态
    output reg         o_busy
);

    //------------------------------------------------------------------
    // 常量换算
    //------------------------------------------------------------------
    localparam integer HALF = (CLK_HZ + SPI_HZ) / (2 * SPI_HZ);   // SCL 半周期拍数（四舍五入）
    localparam [15:0]  HALF16 = HALF;
    localparam integer MS   = (MS_TICKS > 0) ? MS_TICKS : (CLK_HZ / 1000);

    localparam integer SUB   = 32 / PIX_BITS;      // 一个字里几个像素：RGB332→4，RGB565→2
    localparam integer SUBW  = (SUB == 4) ? 2 : 1; // 字内像素序号位宽
    localparam integer WB    = 2 * SUB;            // 一个字的**字节**数（每像素 2 个字节）
    localparam integer NPIX  = IMG_W * IMG_H;      // 一帧像素数
    localparam integer NWORD = (NPIX + SUB - 1) / SUB;   // 一帧字数（向上取整：像素数不是每字像素数整数倍时，末字只用一部分）
    localparam integer NPST  = 2 * NPIX;           // 一帧像素数据字节数

    localparam integer INIT_N = 185;               // 初始化序列表长度（tb 会核对）
    localparam [7:0]  INIT_N8 = INIT_N;
    localparam [16:0] NWORD17 = NWORD;             // 每帧字数
    localparam [18:0] NPST19  = NPST;              // 每帧像素数据字节数
    localparam integer GAP_C   = 32;               // 帧间 CS 拉高保持的拍数
    localparam [31:0] GAP_C32 = GAP_C;             // 同上，显式 32 位版本
    localparam integer SLEEP_MS = 120;             // 0x11 sleep out 之后
    localparam integer DISPON_MS = 20;             // 0x29 display on 之后
    localparam integer SLEEP_CYC  = SLEEP_MS  * MS; // 上面两条换成拍数（常量，便于综合常量化）
    localparam integer DISPON_CYC = DISPON_MS * MS;

    localparam [8:0] CMD = 1'b0;                   // 序列表里的 DC 标志
    localparam [8:0] DAT = 1'b1;

    //------------------------------------------------------------------
    // 初始化序列表（GC9A01 标准上电序列；{DC 标志, 8 位数据}）
    // 延时按命令码识别（0x11 → 120 ms、0x29 → 20 ms），增删条目不会让延时错位
    //------------------------------------------------------------------
    reg [8:0] init_rom [0:INIT_N-1];
    initial begin
        init_rom[  0] = {CMD,8'hEF}; init_rom[  1] = {CMD,8'hEB}; init_rom[  2] = {DAT,8'h14}; init_rom[  3] = {CMD,8'hFE};
        init_rom[  4] = {CMD,8'hEF}; init_rom[  5] = {CMD,8'hEB}; init_rom[  6] = {DAT,8'h14}; init_rom[  7] = {CMD,8'h84};
        init_rom[  8] = {DAT,8'h40}; init_rom[  9] = {CMD,8'h85}; init_rom[ 10] = {DAT,8'hFF}; init_rom[ 11] = {CMD,8'h86};
        init_rom[ 12] = {DAT,8'hFF}; init_rom[ 13] = {CMD,8'h87}; init_rom[ 14] = {DAT,8'hFF}; init_rom[ 15] = {CMD,8'h88};
        init_rom[ 16] = {DAT,8'h0A}; init_rom[ 17] = {CMD,8'h89}; init_rom[ 18] = {DAT,8'h21}; init_rom[ 19] = {CMD,8'h8A};
        init_rom[ 20] = {DAT,8'h00}; init_rom[ 21] = {CMD,8'h8B}; init_rom[ 22] = {DAT,8'h80}; init_rom[ 23] = {CMD,8'h8C};
        init_rom[ 24] = {DAT,8'h01}; init_rom[ 25] = {CMD,8'h8D}; init_rom[ 26] = {DAT,8'h01}; init_rom[ 27] = {CMD,8'h8E};
        init_rom[ 28] = {DAT,8'hFF}; init_rom[ 29] = {CMD,8'h8F}; init_rom[ 30] = {DAT,8'hFF}; init_rom[ 31] = {CMD,8'hB6};
        init_rom[ 32] = {DAT,8'h00}; init_rom[ 33] = {DAT,8'h20}; init_rom[ 34] = {CMD,8'h36}; init_rom[ 35] = {DAT,8'h08};
        init_rom[ 36] = {CMD,8'h3A}; init_rom[ 37] = {DAT,8'h05}; init_rom[ 38] = {CMD,8'h90}; init_rom[ 39] = {DAT,8'h08};
        init_rom[ 40] = {DAT,8'h08}; init_rom[ 41] = {DAT,8'h08}; init_rom[ 42] = {DAT,8'h08}; init_rom[ 43] = {CMD,8'hBD};
        init_rom[ 44] = {DAT,8'h06}; init_rom[ 45] = {CMD,8'hBC}; init_rom[ 46] = {DAT,8'h00}; init_rom[ 47] = {CMD,8'hFF};
        init_rom[ 48] = {DAT,8'h60}; init_rom[ 49] = {DAT,8'h01}; init_rom[ 50] = {DAT,8'h04}; init_rom[ 51] = {CMD,8'hC3};
        init_rom[ 52] = {DAT,8'h13}; init_rom[ 53] = {CMD,8'hC4}; init_rom[ 54] = {DAT,8'h13}; init_rom[ 55] = {CMD,8'hC9};
        init_rom[ 56] = {DAT,8'h22}; init_rom[ 57] = {CMD,8'hBE}; init_rom[ 58] = {DAT,8'h11}; init_rom[ 59] = {CMD,8'hE1};
        init_rom[ 60] = {DAT,8'h10}; init_rom[ 61] = {DAT,8'h0E}; init_rom[ 62] = {CMD,8'hDF}; init_rom[ 63] = {DAT,8'h21};
        init_rom[ 64] = {DAT,8'h0C}; init_rom[ 65] = {DAT,8'h02}; init_rom[ 66] = {CMD,8'hF0}; init_rom[ 67] = {DAT,8'h45};
        init_rom[ 68] = {DAT,8'h09}; init_rom[ 69] = {DAT,8'h08}; init_rom[ 70] = {DAT,8'h08}; init_rom[ 71] = {DAT,8'h26};
        init_rom[ 72] = {DAT,8'h2A}; init_rom[ 73] = {CMD,8'hF1}; init_rom[ 74] = {DAT,8'h43}; init_rom[ 75] = {DAT,8'h70};
        init_rom[ 76] = {DAT,8'h72}; init_rom[ 77] = {DAT,8'h36}; init_rom[ 78] = {DAT,8'h37}; init_rom[ 79] = {DAT,8'h6F};
        init_rom[ 80] = {CMD,8'hF2}; init_rom[ 81] = {DAT,8'h45}; init_rom[ 82] = {DAT,8'h09}; init_rom[ 83] = {DAT,8'h08};
        init_rom[ 84] = {DAT,8'h08}; init_rom[ 85] = {DAT,8'h26}; init_rom[ 86] = {DAT,8'h2A}; init_rom[ 87] = {CMD,8'hF3};
        init_rom[ 88] = {DAT,8'h43}; init_rom[ 89] = {DAT,8'h70}; init_rom[ 90] = {DAT,8'h72}; init_rom[ 91] = {DAT,8'h36};
        init_rom[ 92] = {DAT,8'h37}; init_rom[ 93] = {DAT,8'h6F}; init_rom[ 94] = {CMD,8'hED}; init_rom[ 95] = {DAT,8'h1B};
        init_rom[ 96] = {DAT,8'h0B}; init_rom[ 97] = {CMD,8'hAE}; init_rom[ 98] = {DAT,8'h77}; init_rom[ 99] = {CMD,8'hCD};
        init_rom[100] = {DAT,8'h63}; init_rom[101] = {CMD,8'h70}; init_rom[102] = {DAT,8'h07}; init_rom[103] = {DAT,8'h07};
        init_rom[104] = {DAT,8'h04}; init_rom[105] = {DAT,8'h0E}; init_rom[106] = {DAT,8'h0F}; init_rom[107] = {DAT,8'h09};
        init_rom[108] = {DAT,8'h07}; init_rom[109] = {DAT,8'h08}; init_rom[110] = {DAT,8'h03}; init_rom[111] = {CMD,8'hE8};
        init_rom[112] = {DAT,8'h34}; init_rom[113] = {CMD,8'h62}; init_rom[114] = {DAT,8'h18}; init_rom[115] = {DAT,8'h0D};
        init_rom[116] = {DAT,8'h71}; init_rom[117] = {DAT,8'hED}; init_rom[118] = {DAT,8'h70}; init_rom[119] = {DAT,8'h70};
        init_rom[120] = {DAT,8'h18}; init_rom[121] = {DAT,8'h0F}; init_rom[122] = {DAT,8'h71}; init_rom[123] = {DAT,8'hEF};
        init_rom[124] = {DAT,8'h70}; init_rom[125] = {DAT,8'h70}; init_rom[126] = {CMD,8'h63}; init_rom[127] = {DAT,8'h18};
        init_rom[128] = {DAT,8'h11}; init_rom[129] = {DAT,8'h71}; init_rom[130] = {DAT,8'hF1}; init_rom[131] = {DAT,8'h70};
        init_rom[132] = {DAT,8'h70}; init_rom[133] = {DAT,8'h18}; init_rom[134] = {DAT,8'h13}; init_rom[135] = {DAT,8'h71};
        init_rom[136] = {DAT,8'hF3}; init_rom[137] = {DAT,8'h70}; init_rom[138] = {DAT,8'h70}; init_rom[139] = {CMD,8'h64};
        init_rom[140] = {DAT,8'h28}; init_rom[141] = {DAT,8'h29}; init_rom[142] = {DAT,8'hF1}; init_rom[143] = {DAT,8'h01};
        init_rom[144] = {DAT,8'hF1}; init_rom[145] = {DAT,8'h00}; init_rom[146] = {DAT,8'h07}; init_rom[147] = {CMD,8'h66};
        init_rom[148] = {DAT,8'h3C}; init_rom[149] = {DAT,8'h00}; init_rom[150] = {DAT,8'hCD}; init_rom[151] = {DAT,8'h67};
        init_rom[152] = {DAT,8'h45}; init_rom[153] = {DAT,8'h45}; init_rom[154] = {DAT,8'h10}; init_rom[155] = {DAT,8'h00};
        init_rom[156] = {DAT,8'h00}; init_rom[157] = {DAT,8'h00}; init_rom[158] = {CMD,8'h67}; init_rom[159] = {DAT,8'h00};
        init_rom[160] = {DAT,8'h3C}; init_rom[161] = {DAT,8'h00}; init_rom[162] = {DAT,8'h00}; init_rom[163] = {DAT,8'h00};
        init_rom[164] = {DAT,8'h01}; init_rom[165] = {DAT,8'h54}; init_rom[166] = {DAT,8'h10}; init_rom[167] = {DAT,8'h32};
        init_rom[168] = {DAT,8'h98}; init_rom[169] = {CMD,8'h74}; init_rom[170] = {DAT,8'h10}; init_rom[171] = {DAT,8'h85};
        init_rom[172] = {DAT,8'h80}; init_rom[173] = {DAT,8'h00}; init_rom[174] = {DAT,8'h00}; init_rom[175] = {DAT,8'h4E};
        init_rom[176] = {DAT,8'h00}; init_rom[177] = {CMD,8'h98}; init_rom[178] = {DAT,8'h3E}; init_rom[179] = {DAT,8'h28};
        init_rom[180] = {CMD,8'h35}; init_rom[181] = {DAT,8'h00}; init_rom[182] = {CMD,8'h21}; init_rom[183] = {CMD,8'h11};
        init_rom[184] = {CMD,8'h29};
    end

    function [31:0] cmd_dly_cyc(input [8:0] e);
        cmd_dly_cyc = (e[8] == 1'b0 && e[7:0] == 8'h11) ? SLEEP_CYC :
                      (e[8] == 1'b0 && e[7:0] == 8'h29) ? DISPON_CYC : 32'd0;
    endfunction

    //------------------------------------------------------------------
    // RGB332 → RGB565 按位扩展（R3→R5、G3→G6、B2→B5，纯位复制）
    //------------------------------------------------------------------
    function [15:0] expand332(input [7:0] c);
        expand332 = { c[7],c[6],c[5],c[7],c[6],          // R5
                      c[4],c[3],c[2],c[4],c[3],c[2],     // G6
                      c[1],c[0],c[1],c[0],c[1] };        // B5
    endfunction

    //------------------------------------------------------------------
    // 窗口命令字节表
    //------------------------------------------------------------------
    reg [3:0] wstep;
    function [7:0] win_byte(input [3:0] s);
        case (s)
            4'd0:    win_byte = 8'h2A;                     // CASET
            4'd1:    win_byte = 8'h00;                     // XS[15:8]
            4'd2:    win_byte = 8'h00;                     // XS[7:0]
            4'd3:    win_byte = (IMG_W - 1) >> 8;          // XE[15:8]
            4'd4:    win_byte = (IMG_W - 1) & 8'hFF;       // XE[7:0]
            4'd5:    win_byte = 8'h2B;                     // RASET
            4'd6:    win_byte = 8'h00;                     // YS[15:8]
            4'd7:    win_byte = 8'h00;                     // YS[7:0]
            4'd8:    win_byte = (IMG_H - 1) >> 8;          // YE[15:8]
            4'd9:    win_byte = (IMG_H - 1) & 8'hFF;       // YE[7:0]
            default: win_byte = 8'h2C;                     // RAMWR
        endcase
    endfunction
    wire win_is_cmd = (wstep == 4'd0) || (wstep == 4'd5) || (wstep == 4'd10);

    //------------------------------------------------------------------
    // 位引擎：tx_start 一拍启动，把 tx_data 按 SPI 模式 0 发出去
    //   SCL 空闲低；SDA 在 SCL 低电平期间改变，从机在 SCL 上升沿采样。
    //------------------------------------------------------------------
    reg        tx_start, tx_dc;
    reg  [7:0] tx_data;
    reg        tx_busy, tx_done;
    reg        tx_pend;            // 已下单、还没发完：tx_busy 要一拍后才拉起，靠它挡住重复下单
    reg  [7:0] shf;
    reg  [3:0] bn;
    reg        ph;
    reg [15:0] hc;

    //------------------------------------------------------------------
    // 052 轮：预取槽（1 深）——把刷屏率从 22.8 fps 拉到 ≥ 25 fps
    //
    //   原来每个字节要走一遍
    //       tx_done -> 清 tx_pend -> 下 tx_start -> tx_busy 拉起   （共 4 拍死时间）
    //   240×240 一帧 115200 个字节就是 46 万拍 ≈ 9.2 ms 白浪。
    //   加上这个深 1 的预取槽以后：一个字节发完的**那一拍**就直接把下一个
    //   字节装进移位器（SCL 低电平不留额外空隙），稳态回到 16 拍/字节；
    //   槽空的时候才退回原来的停机握手（tx_done）。
    //
    //   为什么能这么写：槽由**本块独占写**，状态机只递字节
    //   （push / push_data / push_dc），nb_valid 是回给状态机的“槽还空着”信号。
    //   这样就不会出现 049 轮那种“两个 always 块写同一个寄存器”的多驱动坑
    //   （仿真恰好对、综合后被当成常量）。
    //------------------------------------------------------------------
    reg        nb_valid;           // 预取槽里有字节
    reg  [7:0] nb_data;
    reg        nb_dc;
    reg        push;               // 状态机 -> 本块：递一个字节进槽（1 拍脉冲）
    reg  [7:0] push_data;
    reg        push_dc;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            tx_busy <= 1'b0; tx_done <= 1'b0;
            o_scl <= 1'b0; o_sda <= 1'b0; o_dc <= 1'b0;
            shf <= 8'd0; bn <= 4'd0; ph <= 1'b0; hc <= 16'd0;
            nb_valid <= 1'b0; nb_data <= 8'd0; nb_dc <= 1'b0;
        end else begin
            tx_done <= 1'b0;
            if (tx_start) begin
                // 老路径（初始化序列表）仍然有效；新开一字节时槽里的东西作废
                shf      <= tx_data;
                o_sda    <= tx_data[7];
                o_dc     <= tx_dc;
                o_scl    <= 1'b0;
                bn       <= 4'd0;
                ph       <= 1'b0;
                hc       <= 16'd0;
                tx_busy  <= 1'b1;
                nb_valid <= 1'b0;
            end else if (!tx_busy && nb_valid) begin
                // 空闲但槽里有字节：直接起步（省掉 tx_start 的两拍）
                shf      <= nb_data;
                o_sda    <= nb_data[7];
                o_dc     <= nb_dc;
                o_scl    <= 1'b0;
                bn       <= 4'd0;
                ph       <= 1'b0;
                hc       <= 16'd0;
                tx_busy  <= 1'b1;
                nb_valid <= 1'b0;
            end else if (tx_busy) begin
                if (hc == HALF16 - 16'd1) begin
                    hc <= 16'd0;
                    if (!ph) begin
                        ph    <= 1'b1;
                        o_scl <= 1'b1;                 // 从机在这一沿采样
                    end else begin
                        o_scl <= 1'b0;
                        ph    <= 1'b0;
                        if (bn == 4'd7) begin
                            if (nb_valid) begin
                                // 无缝接上下一个字节：这一拍就把它装进移位器
                                shf      <= nb_data;
                                o_sda    <= nb_data[7];
                                o_dc     <= nb_dc;
                                bn       <= 4'd0;
                                hc       <= 16'd0;
                                nb_valid <= 1'b0;
                            end else begin
                                tx_busy <= 1'b0;
                                tx_done <= 1'b1;
                            end
                        end else begin
                            bn    <= bn + 4'd1;
                            shf   <= {shf[6:0], 1'b0};
                            o_sda <= shf[6];
                        end
                    end
                end else begin
                    hc <= hc + 16'd1;
                end
            end
            // push 放在最后：与消费碰在同一拍时（实际上不会）也以新推进来的那个为准
            if (push) begin
                nb_data  <= push_data;
                nb_dc    <= push_dc;
                nb_valid <= 1'b1;
            end
        end
    end

    //------------------------------------------------------------------
    // 主时序
    //------------------------------------------------------------------
    localparam [2:0] S_POR  = 3'd0,
                     S_WAKE = 3'd1,
                     S_INIT = 3'd2,
                     S_WIN  = 3'd3,
                     S_PIX  = 3'd4,
                     S_GAP  = 3'd5;

    reg [2:0]  state;
    reg [31:0] wait_clk;
    reg [7:0]  init_i;
    reg [31:0] dly_cyc;
    reg [18:0] pstep;              // 像素数据字节序号 0..NPST-1
    reg [31:0] word_r;
    reg        word_ok;
    reg [16:0] word_tag;           // 052：word_r 里装的是哪一个字（和 word_idx 比就知道需不需要重取）
    reg        buf_lat;
    reg        frame_ending;   // 最后一个像素字节已启动，等它移完再收 CS

    // 像素阶段由 pstep 组合推出
    wire [16:0] px_idx    = pstep[17:1];                  // 第几个像素
    wire        px_hi     = ~pstep[0];                    // 1 = 高字节
    wire [1:0]  sub_idx   = px_idx[SUBW-1:0];             // 字内第几个像素
    wire [16:0] word_idx  = px_idx >> SUBW;               // 帧内第几个字
    // 052：原来用 need_word 判“该取字了”，现在改成 word_tag 标签比较
    //       （同样算法、不再依赖 pstep 位段），故 need_word 已删。
    wire [16:0] frame_base = buf_lat ? NWORD17 : 17'd0;
    wire        word_hit  = word_ok && (word_tag == word_idx);   // 052：当前字节所属的那个字已经在手上

    wire [PIX_BITS-1:0] pix_raw = word_r[sub_idx*PIX_BITS +: PIX_BITS];
    wire [15:0]         pix565  = (PIX_BITS == 8) ? expand332(pix_raw) : pix_raw;
    wire [7:0]          pix_byte = px_hi ? pix565[15:8] : pix565[7:0];


    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_POR;
            wait_clk <= RST_MS * MS;
            init_i <= 8'd0; dly_cyc <= 32'd0; tx_pend <= 1'b0;
            push <= 1'b0; push_data <= 8'd0; push_dc <= 1'b0; word_tag <= 17'd0;
            wstep <= 4'd0; pstep <= 19'd0;
            word_r <= 32'd0; word_ok <= 1'b0; buf_lat <= 1'b0; frame_ending <= 1'b0;
            tx_start <= 1'b0; tx_data <= 8'd0; tx_dc <= 1'b0;
            o_cs <= 1'b1; o_rst <= 1'b0; o_busy <= 1'b0;
            o_pix_req <= 1'b0; o_pix_addr <= 17'd0;
        end else begin
            tx_start <= 1'b0;
            push     <= 1'b0;                 // 052：push 是 1 拍脉冲，默认拉低
            if (tx_done) tx_pend <= 1'b0;
            case (state)
                //----------------------------------------------
                // 上电复位：RST 拉低 RST_MS 毫秒
                //----------------------------------------------
                S_POR: begin
                    o_rst <= 1'b0;
                    if (wait_clk == 32'd0) begin
                        wait_clk <= POR_MS * MS;
                        state    <= S_WAKE;
                    end else begin
                        wait_clk <= wait_clk - 32'd1;
                    end
                end
                //----------------------------------------------
                // 放开复位后等 POR_MS 毫秒再发命令
                //----------------------------------------------
                S_WAKE: begin
                    o_rst <= 1'b1;
                    if (wait_clk == 32'd0) begin
                        state  <= S_INIT;
                        init_i <= 8'd0;
                        o_cs   <= 1'b0;
                    end else begin
                        wait_clk <= wait_clk - 32'd1;
                    end
                end
                //----------------------------------------------
                // 初始化序列：一个字节接一个字节，含两条毫秒级延时
                //----------------------------------------------
                S_INIT: begin
                    if (tx_done) begin
                        wait_clk <= dly_cyc;            // 这个字节发完后再等这么久（拍数）
                    end else if (!tx_busy && !tx_pend) begin
                        if (wait_clk != 32'd0) begin
                            wait_clk <= wait_clk - 32'd1;
                        end else if (init_i == INIT_N8) begin
                            if (!i_hold) begin              // 有重绘在等，先把时间窗让出去
                                state <= S_WIN;
                                wstep <= 4'd0;
                                buf_lat <= i_buf_sel;       // 帧边界锁存双缓冲选择
                                o_busy <= 1'b1;
                                frame_ending <= 1'b0;
                            end
                        end else begin
                            tx_data  <= init_rom[init_i][7:0];
                            tx_dc    <= init_rom[init_i][8];
                            tx_start <= 1'b1;
                            tx_pend  <= 1'b1;
                            dly_cyc  <= cmd_dly_cyc(init_rom[init_i]);
                            init_i   <= init_i + 8'd1;
                        end
                    end
                end
                //----------------------------------------------
                // 窗口命令：0x2A + XS/XE，0x2B + YS/YE，0x2C
                //----------------------------------------------
                S_WIN: begin
                    if (wstep == 4'd11) begin
                        // 11 个窗口字节已经全部递进预取槽（顺序由槽保证）
                        state   <= S_PIX;
                        pstep   <= 19'd0;
                        word_ok <= 1'b0;
                    end else if (!nb_valid && !push) begin
                        // !push 不是多余：nb_valid 是“槽已经装上”，而本块看到它要等一拍；
                        // 不加 !push 会在连续两拍推两个字节进去，后一个把前一个顶掉
                        // （052 轮实测：窗口字节 2/4/6/8 就是这么丢的）。
                        push      <= 1'b1;
                        push_data <= win_byte(wstep);
                        push_dc   <= win_is_cmd ? 1'b0 : 1'b1;
                        wstep     <= wstep + 4'd1;
                    end
                end
                //----------------------------------------------
                // 像素数据：每个像素 2 个字节（高字节先），DC=1
                //----------------------------------------------
                S_PIX: begin
                    if (frame_ending) begin
                        // 最后一个像素字节已经移完，这一拍才收 CS。
                        // 三个附加条件是必要的：“刚推进去、引擎还没拿走”那一拍，
                        // 上一个字节的 tx_done 恰好还在总线上，不加这三个条件会把末字节截断。
                        if (tx_done && !tx_busy && !nb_valid) begin
                            o_cs         <= 1'b1;            // 一帧发完
                            o_busy       <= 1'b0;
                            pstep        <= 19'd0;
                            state        <= S_GAP;
                            wait_clk     <= GAP_C32;
                            frame_ending <= 1'b0;
                        end
                    end else if (!nb_valid && !push) begin
                        if (!word_hit) begin
                            o_pix_req  <= 1'b1;
                            o_pix_addr <= frame_base + word_idx;
                            if (i_pix_valid) begin
                                o_pix_req <= 1'b0;
                                word_r    <= i_pix;
                                word_tag  <= word_idx;
                                word_ok   <= 1'b1;
                            end
                        end else begin
                            push      <= 1'b1;
                            push_data <= pix_byte;
                            push_dc   <= 1'b1;
                            if (pstep == NPST19 - 19'd1) begin
                                frame_ending <= 1'b1;        // 末字节：等它移完再进 S_GAP
                            end else begin
                                pstep <= pstep + 19'd1;
                            end
                        end
                    end
                end
                //----------------------------------------------
                // 帧间空隙：CS 拉高若干拍，然后自动开始下一帧
                //----------------------------------------------
                S_GAP: begin
                    if (wait_clk == 32'd0) begin
                        if (!i_hold) begin          // 重绘期间停在帧间空隙，等它写完再开新帧
                            state   <= S_WIN;
                            wstep   <= 4'd0;
                            buf_lat <= i_buf_sel;
                            o_cs    <= 1'b0;
                            o_busy  <= 1'b1;
                            frame_ending <= 1'b0;
                        end
                    end else begin
                        wait_clk <= wait_clk - 32'd1;
                    end
                end
                default: state <= S_POR;
            endcase
        end
    end

    //------------------------------------------------------------------
    // 仿真自检：序列表末尾必须是 0x11(sleep out) / 0x29(display on)，
    // 增删条目忘了同步 INIT_N 时这里会报（不产生任何逻辑）。
    // 条数 185 与 50 条命令码的完整核对在 tb/tb_lcd_spi_master.sv 里做。
    //------------------------------------------------------------------
    // synthesis translate_off
    initial begin
        #1;
        if (init_rom[INIT_N-1] !== {CMD, 8'h29} || init_rom[INIT_N-2] !== {CMD, 8'h11})
            $display("  [lcd_spi_master] 初始化序列表末尾不是 0x11/0x29，请核对 INIT_N=%0d", INIT_N);
    end
    // synthesis translate_on

endmodule
