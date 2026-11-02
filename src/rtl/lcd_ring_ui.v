//======================================================================
//  lcd_ring_ui — 圆屏"色环"UI 渲染器（方案书 §5.4 屏幕 UI 规范）
//
//  作用：把测量结果直接画成 240×240 的 RGB565 整帧，写进 lcd_framebuf 的端口 A。
//        PS 侧画 UI 的那条路（§4.12：PS 写帧存）将来可以接管；在 PS 还没跑起来、
//        或者要求 UI 不依赖操作系统的场合，本模块就是屏幕的最终画面来源。
//
//  画面（§5.4 的四点里做了三点，第四点见下）：
//    · 外环  直径 240，环宽 18 px（R_IN=100 → R_OUT=118），从 12 点方向**顺时针**填充，
//            填充比例 = i_fill（0–100）。填过的部分是状态色，没填的是暗轨 #1C2530。
//    · 中央大字 48 px 等宽数字，白 #F2F5F9，显示 dB(A) 的整数部分（<100 自动少画一位）。
//    · 中央副标 24 px 汉字，随状态变色：待机 / 安静区 / 注意区 / 干扰区。
//    · （未做）底部提示语与 8 段频谱条 —— 频谱条要等 8 带能量特征先算出来，
//      现在 feature_calc 只出 4 个标量，硬画 8 根条就是假数据。记在 §18 待办。
//
//  像素级约定（testbench 的金标准就是按这一段写的，改这里必须同步改 tb）：
//    · 背景 #0E1116 铺满整帧（28,800 个字全写一遍）
//    · 圆环：NSTEP=1024 个角向步 × 37 个径向半步（0.5 px 一步）
//        单位向量 Q16，起点 12 点 (vx,vy)=(0,65536)
//        每步顺时针旋转：vx' = (vx·65535 + vy·402)>>16，vy' = (vy·65535 − vx·402)>>16
//        采样点 px = 120 + ((vx·r2 + 65536)>>17)，py = 120 − ((vy·r2 + 65536)>>17)，r2 = 2·r
//        第 k 步填不填：k·100 < i_fill·1024
//        越界像素丢弃；后被覆盖者胜（角向相邻步有重叠，是无害的）
//    · 数字：格 32×48、间隔 6 px，nd 位（nd 由数值定：≥100 三位、≥10 两位、否则一位）
//        整行居中：x0 = (240 − (nd·32 + (nd−1)·6)) / 2，y = 84
//    · 汉字：格 24×24、间隔 4 px，nci 个（IDLE 两个、其余三个），y = 142
//
//  缓动（§5.4 要求"圆环填充有 200 ms 缓动，不能跳变"）：
//    每 2^TICK_BITS 拍把显示值朝目标挪 SLEW 个单位 → 满量程 ≈ 2^19×100/SLEW 拍。
//    取 TICK_BITS=19、SLEW=4 → 满量程 262 ms，接近规范值。
//    两次重绘之间至少隔 2^GAP_BITS 拍（GAP_BITS=20 → 21 ms），避免数字跳动时把帧存写爆。
//
//  资源：无乘法器（只用了 3 个常量乘：×240 写成移位相加、×65535/×402 是旋转常数、
//        ×r2 是 18×9 位）。字模 480×32 bit + 216×24 bit 是 LUT/BRAM 都能装下的常量。
//======================================================================
`timescale 1ns / 1ps

module lcd_ring_ui #(
    parameter integer IMG_W     = 240,
    parameter integer IMG_H     = 240,
    parameter integer R_IN      = 100,     // 圆环内半径
    parameter integer R_OUT     = 118,     // 圆环外半径（环宽 = 18）
    parameter integer NSTEP     = 1024,    // 角向步数
    parameter integer SLEW      = 4,       // 每次缓动 tick 最多移动的单位数（0 = 不平滑）
    parameter integer TICK_BITS = 19,      // 缓动 tick = 2^TICK_BITS 拍
    parameter integer GAP_BITS  = 20       // 两次重绘最小间隔 = 2^GAP_BITS 拍
)(
    input  wire        clk,
    input  wire        rst_n,
    input  wire        i_en,        // 高电平允许绘制（电平，不是脉冲）
    input  wire [7:0]  i_fill,      // 圆环填充比例 0–100
    input  wire [1:0]  i_state,     // 0=IDLE 1=QUIET 2=WARN 3=ALARM
    input  wire [11:0] i_db10,      // dB(A)×10
    input  wire        i_spi_busy,  // 刷屏引擎正在发一帧；为 1 时绝不能动帧存（否则撕裂）
    // 帧存端口 A（32 位字寻址，字节选通写一个像素）
    output reg         o_ena,
    output reg  [3:0]  o_wea,
    output reg  [16:0] o_addra,
    output reg  [31:0] o_dina,
    // 状态
    output reg         o_busy,      // 一帧渲染期间为 1
    output reg         o_done,      // 至少画完过一帧后保持 1
    output wire        o_hold       // 有重绘待做/在做：请刷屏引擎停在帧间空隙等我们
);

    //------------------------------------------------------------------
    // 常量
    //------------------------------------------------------------------
    localparam integer CX    = IMG_W / 2;              // 120
    localparam integer CY    = IMG_H / 2;              // 120
    localparam integer RAD   = 2 * (R_OUT - R_IN) + 1; // 37 个径向半步
    localparam integer NWORD = (IMG_W * IMG_H) / 2;    // 28,800 个字

    localparam integer DW    = 32;                     // 数字格宽
    localparam integer DH    = 48;                     // 数字格高
    localparam integer DGAP  = 6;
    localparam integer DY0   = 84;
    localparam integer TG    = 24;                     // 汉字格宽/高
    localparam integer TTGAP = 4;
    localparam integer TY0   = 142;

    // RGB565 颜色（与 §5.4 的十六进制一一对应，换算见 gen_ring_ui_035.py）
    localparam [15:0] C_BG    = 16'h0882;   // #0E1116 背景
    localparam [15:0] C_TRACK = 16'h1926;   // #1C2530 未填充环
    localparam [15:0] C_IDLE  = 16'h4AAD;   // #4A5568
    localparam [15:0] C_QUIET = 16'h3EF0;   // #3DDC84
    localparam [15:0] C_WARN  = 16'hFE29;   // #FFC44D
    localparam [15:0] C_ALARM = 16'hFAEA;   // #FF5F56
    localparam [15:0] C_NUM   = 16'hF7BF;   // #F2F5F9

    // 顺时针旋转矩阵（Q16）。1024 步累计半径漂移 < 0.5 px，见 gen_ring_ui_035.py 自检
    localparam signed [17:0] ROT_CS = 18'sd65535;
    localparam signed [17:0] ROT_SN = 18'sd402;

    localparam [2:0] PH_IDLE = 3'd0, PH_CLEAR = 3'd1, PH_RING = 3'd2,
                     PH_ROT  = 3'd3, PH_DIG   = 3'd4, PH_TXT  = 3'd5,
                     PH_END  = 3'd6;

    //------------------------------------------------------------------
    // 字模（本文件由 _scratch_archive/gen_ring_ui_035.py 生成，勿手改）
    //   数字：10 个字 × 48 行，每行 32 位，MSB = 最左像素
    //   汉字：9 个字 × 24 行（顺序 待 机 安 静 注 意 干 扰 区），每行 24 位
    //------------------------------------------------------------------
    reg [31:0] digit_rom [0:479];
    reg [23:0] cjk_rom   [0:215];

    initial begin
        digit_rom[  0] = 32'h00000000;
        digit_rom[  1] = 32'h00000000;
        digit_rom[  2] = 32'h00000000;
        digit_rom[  3] = 32'h003FFE00;
        digit_rom[  4] = 32'h007FFF80;
        digit_rom[  5] = 32'h00FFFFC0;
        digit_rom[  6] = 32'h01FFFFE0;
        digit_rom[  7] = 32'h03F80FF0;
        digit_rom[  8] = 32'h07F007F0;
        digit_rom[  9] = 32'h07E003F8;
        digit_rom[ 10] = 32'h0FC001F8;
        digit_rom[ 11] = 32'h0FC000FC;
        digit_rom[ 12] = 32'h1F8000FC;
        digit_rom[ 13] = 32'h1F8000FC;
        digit_rom[ 14] = 32'h1F0000FE;
        digit_rom[ 15] = 32'h1F0001FE;
        digit_rom[ 16] = 32'h3F0007FE;
        digit_rom[ 17] = 32'h3F000FFE;
        digit_rom[ 18] = 32'h3F001FFE;
        digit_rom[ 19] = 32'h3F003FFE;
        digit_rom[ 20] = 32'h3E00FF3E;
        digit_rom[ 21] = 32'h3E01FE3E;
        digit_rom[ 22] = 32'h3E03FC3E;
        digit_rom[ 23] = 32'h3E07F03E;
        digit_rom[ 24] = 32'h3E1FE03E;
        digit_rom[ 25] = 32'h3E3FC03E;
        digit_rom[ 26] = 32'h3E7F803E;
        digit_rom[ 27] = 32'h3FFE003E;
        digit_rom[ 28] = 32'h3FFC003E;
        digit_rom[ 29] = 32'h3FF8007E;
        digit_rom[ 30] = 32'h3FF0007E;
        digit_rom[ 31] = 32'h3FC0007E;
        digit_rom[ 32] = 32'h1F80007E;
        digit_rom[ 33] = 32'h1F8000FC;
        digit_rom[ 34] = 32'h1F8000FC;
        digit_rom[ 35] = 32'h1F8000FC;
        digit_rom[ 36] = 32'h0FC001F8;
        digit_rom[ 37] = 32'h0FE003F8;
        digit_rom[ 38] = 32'h07F007F0;
        digit_rom[ 39] = 32'h07F80FE0;
        digit_rom[ 40] = 32'h03FFFFE0;
        digit_rom[ 41] = 32'h01FFFFC0;
        digit_rom[ 42] = 32'h00FFFF00;
        digit_rom[ 43] = 32'h003FFE00;
        digit_rom[ 44] = 32'h000FF000;
        digit_rom[ 45] = 32'h00000000;
        digit_rom[ 46] = 32'h00000000;
        digit_rom[ 47] = 32'h00000000;
        digit_rom[ 48] = 32'h00000000;
        digit_rom[ 49] = 32'h00000000;
        digit_rom[ 50] = 32'h00000000;
        digit_rom[ 51] = 32'h00000000;
        digit_rom[ 52] = 32'h0007F800;
        digit_rom[ 53] = 32'h001FF800;
        digit_rom[ 54] = 32'h007FF800;
        digit_rom[ 55] = 32'h01FFF800;
        digit_rom[ 56] = 32'h07FFF800;
        digit_rom[ 57] = 32'h1FF9F800;
        digit_rom[ 58] = 32'h0FE1F800;
        digit_rom[ 59] = 32'h0FC1F800;
        digit_rom[ 60] = 32'h0701F800;
        digit_rom[ 61] = 32'h0401F800;
        digit_rom[ 62] = 32'h0001F800;
        digit_rom[ 63] = 32'h0001F800;
        digit_rom[ 64] = 32'h0001F800;
        digit_rom[ 65] = 32'h0001F800;
        digit_rom[ 66] = 32'h0001F800;
        digit_rom[ 67] = 32'h0001F800;
        digit_rom[ 68] = 32'h0001F800;
        digit_rom[ 69] = 32'h0001F800;
        digit_rom[ 70] = 32'h0001F800;
        digit_rom[ 71] = 32'h0001F800;
        digit_rom[ 72] = 32'h0001F800;
        digit_rom[ 73] = 32'h0001F800;
        digit_rom[ 74] = 32'h0001F800;
        digit_rom[ 75] = 32'h0001F800;
        digit_rom[ 76] = 32'h0001F800;
        digit_rom[ 77] = 32'h0001F800;
        digit_rom[ 78] = 32'h0001F800;
        digit_rom[ 79] = 32'h0001F800;
        digit_rom[ 80] = 32'h0001F800;
        digit_rom[ 81] = 32'h0001F800;
        digit_rom[ 82] = 32'h0001F800;
        digit_rom[ 83] = 32'h0001F800;
        digit_rom[ 84] = 32'h0001F800;
        digit_rom[ 85] = 32'h0001F800;
        digit_rom[ 86] = 32'h0001F800;
        digit_rom[ 87] = 32'h0FFFFFFC;
        digit_rom[ 88] = 32'h0FFFFFFC;
        digit_rom[ 89] = 32'h0FFFFFFC;
        digit_rom[ 90] = 32'h0FFFFFFC;
        digit_rom[ 91] = 32'h0FFFFFFC;
        digit_rom[ 92] = 32'h00000000;
        digit_rom[ 93] = 32'h00000000;
        digit_rom[ 94] = 32'h00000000;
        digit_rom[ 95] = 32'h00000000;
        digit_rom[ 96] = 32'h00000000;
        digit_rom[ 97] = 32'h00000000;
        digit_rom[ 98] = 32'h00000000;
        digit_rom[ 99] = 32'h000FF000;
        digit_rom[100] = 32'h007FFE00;
        digit_rom[101] = 32'h01FFFF00;
        digit_rom[102] = 32'h03FFFF80;
        digit_rom[103] = 32'h07FFFFC0;
        digit_rom[104] = 32'h0FF01FE0;
        digit_rom[105] = 32'h07C00FE0;
        digit_rom[106] = 32'h030007F0;
        digit_rom[107] = 32'h000003F0;
        digit_rom[108] = 32'h000003F0;
        digit_rom[109] = 32'h000003F0;
        digit_rom[110] = 32'h000003F0;
        digit_rom[111] = 32'h000003F0;
        digit_rom[112] = 32'h000003F0;
        digit_rom[113] = 32'h000003F0;
        digit_rom[114] = 32'h000003F0;
        digit_rom[115] = 32'h000003F0;
        digit_rom[116] = 32'h000007E0;
        digit_rom[117] = 32'h000007E0;
        digit_rom[118] = 32'h00000FC0;
        digit_rom[119] = 32'h00001FC0;
        digit_rom[120] = 32'h00001F80;
        digit_rom[121] = 32'h00003F00;
        digit_rom[122] = 32'h00007F00;
        digit_rom[123] = 32'h0000FE00;
        digit_rom[124] = 32'h0001FC00;
        digit_rom[125] = 32'h0003F800;
        digit_rom[126] = 32'h0007F000;
        digit_rom[127] = 32'h000FE000;
        digit_rom[128] = 32'h001FC000;
        digit_rom[129] = 32'h003F8000;
        digit_rom[130] = 32'h007F0000;
        digit_rom[131] = 32'h00FE0000;
        digit_rom[132] = 32'h01FC0000;
        digit_rom[133] = 32'h03F80000;
        digit_rom[134] = 32'h07F00000;
        digit_rom[135] = 32'h0FE00000;
        digit_rom[136] = 32'h0FFFFFFE;
        digit_rom[137] = 32'h0FFFFFFE;
        digit_rom[138] = 32'h0FFFFFFE;
        digit_rom[139] = 32'h0FFFFFFE;
        digit_rom[140] = 32'h0FFFFFFE;
        digit_rom[141] = 32'h00000000;
        digit_rom[142] = 32'h00000000;
        digit_rom[143] = 32'h00000000;
        digit_rom[144] = 32'h00000000;
        digit_rom[145] = 32'h00000000;
        digit_rom[146] = 32'h00000000;
        digit_rom[147] = 32'h03FFFC00;
        digit_rom[148] = 32'h07FFFF00;
        digit_rom[149] = 32'h07FFFF80;
        digit_rom[150] = 32'h07FFFFC0;
        digit_rom[151] = 32'h07803FE0;
        digit_rom[152] = 32'h04000FE0;
        digit_rom[153] = 32'h000007E0;
        digit_rom[154] = 32'h000007F0;
        digit_rom[155] = 32'h000003F0;
        digit_rom[156] = 32'h000003F0;
        digit_rom[157] = 32'h000003F0;
        digit_rom[158] = 32'h000003F0;
        digit_rom[159] = 32'h000003E0;
        digit_rom[160] = 32'h000007E0;
        digit_rom[161] = 32'h000007E0;
        digit_rom[162] = 32'h00000FC0;
        digit_rom[163] = 32'h00001F80;
        digit_rom[164] = 32'h0000FF00;
        digit_rom[165] = 32'h007FFE00;
        digit_rom[166] = 32'h007FFC00;
        digit_rom[167] = 32'h007FFF80;
        digit_rom[168] = 32'h007FFFC0;
        digit_rom[169] = 32'h00003FE0;
        digit_rom[170] = 32'h000007F0;
        digit_rom[171] = 32'h000003F8;
        digit_rom[172] = 32'h000001F8;
        digit_rom[173] = 32'h000001F8;
        digit_rom[174] = 32'h000000FC;
        digit_rom[175] = 32'h000000FC;
        digit_rom[176] = 32'h000000FC;
        digit_rom[177] = 32'h000000FC;
        digit_rom[178] = 32'h000000F8;
        digit_rom[179] = 32'h000001F8;
        digit_rom[180] = 32'h000001F8;
        digit_rom[181] = 32'h000003F0;
        digit_rom[182] = 32'h00000FF0;
        digit_rom[183] = 32'h0C003FE0;
        digit_rom[184] = 32'h0FFFFFC0;
        digit_rom[185] = 32'h0FFFFF80;
        digit_rom[186] = 32'h0FFFFE00;
        digit_rom[187] = 32'h0FFFF800;
        digit_rom[188] = 32'h03FFC000;
        digit_rom[189] = 32'h00000000;
        digit_rom[190] = 32'h00000000;
        digit_rom[191] = 32'h00000000;
        digit_rom[192] = 32'h00000000;
        digit_rom[193] = 32'h00000000;
        digit_rom[194] = 32'h00000000;
        digit_rom[195] = 32'h00000000;
        digit_rom[196] = 32'h00007FC0;
        digit_rom[197] = 32'h0000FFC0;
        digit_rom[198] = 32'h0001FFC0;
        digit_rom[199] = 32'h0001FFC0;
        digit_rom[200] = 32'h0003FFC0;
        digit_rom[201] = 32'h0003EFC0;
        digit_rom[202] = 32'h0007EFC0;
        digit_rom[203] = 32'h000FCFC0;
        digit_rom[204] = 32'h000F8FC0;
        digit_rom[205] = 32'h001F8FC0;
        digit_rom[206] = 32'h003F0FC0;
        digit_rom[207] = 32'h003E0FC0;
        digit_rom[208] = 32'h007E0FC0;
        digit_rom[209] = 32'h00FC0FC0;
        digit_rom[210] = 32'h00F80FC0;
        digit_rom[211] = 32'h01F80FC0;
        digit_rom[212] = 32'h01F00FC0;
        digit_rom[213] = 32'h03F00FC0;
        digit_rom[214] = 32'h07E00FC0;
        digit_rom[215] = 32'h07C00FC0;
        digit_rom[216] = 32'h0FC00FC0;
        digit_rom[217] = 32'h1F800FC0;
        digit_rom[218] = 32'h1F000FC0;
        digit_rom[219] = 32'h3F000FC0;
        digit_rom[220] = 32'h7E000FC0;
        digit_rom[221] = 32'h7C000FC0;
        digit_rom[222] = 32'hFFFFFFFF;
        digit_rom[223] = 32'hFFFFFFFF;
        digit_rom[224] = 32'hFFFFFFFF;
        digit_rom[225] = 32'hFFFFFFFF;
        digit_rom[226] = 32'hFFFFFFFF;
        digit_rom[227] = 32'h00000FC0;
        digit_rom[228] = 32'h00000FC0;
        digit_rom[229] = 32'h00000FC0;
        digit_rom[230] = 32'h00000FC0;
        digit_rom[231] = 32'h00000FC0;
        digit_rom[232] = 32'h00000FC0;
        digit_rom[233] = 32'h00000FC0;
        digit_rom[234] = 32'h00000FC0;
        digit_rom[235] = 32'h00000FC0;
        digit_rom[236] = 32'h00000000;
        digit_rom[237] = 32'h00000000;
        digit_rom[238] = 32'h00000000;
        digit_rom[239] = 32'h00000000;
        digit_rom[240] = 32'h00000000;
        digit_rom[241] = 32'h00000000;
        digit_rom[242] = 32'h00000000;
        digit_rom[243] = 32'h07FFFFE0;
        digit_rom[244] = 32'h07FFFFE0;
        digit_rom[245] = 32'h07FFFFE0;
        digit_rom[246] = 32'h07FFFFE0;
        digit_rom[247] = 32'h07FFFFE0;
        digit_rom[248] = 32'h07C00000;
        digit_rom[249] = 32'h07C00000;
        digit_rom[250] = 32'h07C00000;
        digit_rom[251] = 32'h07C00000;
        digit_rom[252] = 32'h07C00000;
        digit_rom[253] = 32'h07C00000;
        digit_rom[254] = 32'h07C00000;
        digit_rom[255] = 32'h07C00000;
        digit_rom[256] = 32'h07C00000;
        digit_rom[257] = 32'h07C00000;
        digit_rom[258] = 32'h07C00000;
        digit_rom[259] = 32'h07FFF000;
        digit_rom[260] = 32'h07FFFE00;
        digit_rom[261] = 32'h07FFFF80;
        digit_rom[262] = 32'h07FFFFC0;
        digit_rom[263] = 32'h07FFFFE0;
        digit_rom[264] = 32'h00003FF0;
        digit_rom[265] = 32'h000007F0;
        digit_rom[266] = 32'h000003F8;
        digit_rom[267] = 32'h000001F8;
        digit_rom[268] = 32'h000001F8;
        digit_rom[269] = 32'h000001F8;
        digit_rom[270] = 32'h000001FC;
        digit_rom[271] = 32'h000000FC;
        digit_rom[272] = 32'h000001F8;
        digit_rom[273] = 32'h000001F8;
        digit_rom[274] = 32'h000001F8;
        digit_rom[275] = 32'h000001F8;
        digit_rom[276] = 32'h000003F0;
        digit_rom[277] = 32'h000007F0;
        digit_rom[278] = 32'h00000FE0;
        digit_rom[279] = 32'h04007FC0;
        digit_rom[280] = 32'h0FFFFF80;
        digit_rom[281] = 32'h0FFFFF00;
        digit_rom[282] = 32'h0FFFFC00;
        digit_rom[283] = 32'h0FFFF000;
        digit_rom[284] = 32'h07FF8000;
        digit_rom[285] = 32'h00000000;
        digit_rom[286] = 32'h00000000;
        digit_rom[287] = 32'h00000000;
        digit_rom[288] = 32'h00000000;
        digit_rom[289] = 32'h00000000;
        digit_rom[290] = 32'h00000000;
        digit_rom[291] = 32'h00003FF0;
        digit_rom[292] = 32'h0001FFF0;
        digit_rom[293] = 32'h000FFFF0;
        digit_rom[294] = 32'h001FFFF0;
        digit_rom[295] = 32'h007FFFF0;
        digit_rom[296] = 32'h00FFC000;
        digit_rom[297] = 32'h00FE0000;
        digit_rom[298] = 32'h01FC0000;
        digit_rom[299] = 32'h03F00000;
        digit_rom[300] = 32'h03E00000;
        digit_rom[301] = 32'h07E00000;
        digit_rom[302] = 32'h07C00000;
        digit_rom[303] = 32'h0FC00000;
        digit_rom[304] = 32'h0F800000;
        digit_rom[305] = 32'h0F800000;
        digit_rom[306] = 32'h0F800000;
        digit_rom[307] = 32'h1F83FE00;
        digit_rom[308] = 32'h1F9FFF80;
        digit_rom[309] = 32'h1FFFFFE0;
        digit_rom[310] = 32'h1FFFFFF0;
        digit_rom[311] = 32'h1FFF1FF8;
        digit_rom[312] = 32'h1FF003F8;
        digit_rom[313] = 32'h1FC001FC;
        digit_rom[314] = 32'h1F8000FC;
        digit_rom[315] = 32'h1F80007E;
        digit_rom[316] = 32'h1F80007E;
        digit_rom[317] = 32'h1F80007E;
        digit_rom[318] = 32'h1F80007E;
        digit_rom[319] = 32'h1F80007E;
        digit_rom[320] = 32'h1F80007E;
        digit_rom[321] = 32'h1F80007E;
        digit_rom[322] = 32'h0F80007E;
        digit_rom[323] = 32'h0FC0007C;
        digit_rom[324] = 32'h0FC000FC;
        digit_rom[325] = 32'h07E000FC;
        digit_rom[326] = 32'h07E001F8;
        digit_rom[327] = 32'h03F803F0;
        digit_rom[328] = 32'h03FE1FF0;
        digit_rom[329] = 32'h01FFFFE0;
        digit_rom[330] = 32'h00FFFF80;
        digit_rom[331] = 32'h003FFF00;
        digit_rom[332] = 32'h000FF800;
        digit_rom[333] = 32'h00000000;
        digit_rom[334] = 32'h00000000;
        digit_rom[335] = 32'h00000000;
        digit_rom[336] = 32'h00000000;
        digit_rom[337] = 32'h00000000;
        digit_rom[338] = 32'h00000000;
        digit_rom[339] = 32'h00000000;
        digit_rom[340] = 32'h1FFFFFFC;
        digit_rom[341] = 32'h1FFFFFFC;
        digit_rom[342] = 32'h1FFFFFFC;
        digit_rom[343] = 32'h1FFFFFFC;
        digit_rom[344] = 32'h000000FC;
        digit_rom[345] = 32'h000000FC;
        digit_rom[346] = 32'h000000F8;
        digit_rom[347] = 32'h000001F8;
        digit_rom[348] = 32'h000001F0;
        digit_rom[349] = 32'h000003F0;
        digit_rom[350] = 32'h000003E0;
        digit_rom[351] = 32'h000007E0;
        digit_rom[352] = 32'h000007C0;
        digit_rom[353] = 32'h00000FC0;
        digit_rom[354] = 32'h00000F80;
        digit_rom[355] = 32'h00001F80;
        digit_rom[356] = 32'h00001F00;
        digit_rom[357] = 32'h00003F00;
        digit_rom[358] = 32'h00003F00;
        digit_rom[359] = 32'h00007E00;
        digit_rom[360] = 32'h00007E00;
        digit_rom[361] = 32'h0000FC00;
        digit_rom[362] = 32'h0000FC00;
        digit_rom[363] = 32'h0001F800;
        digit_rom[364] = 32'h0001F800;
        digit_rom[365] = 32'h0003F000;
        digit_rom[366] = 32'h0003F000;
        digit_rom[367] = 32'h0007E000;
        digit_rom[368] = 32'h0007E000;
        digit_rom[369] = 32'h000FC000;
        digit_rom[370] = 32'h000FC000;
        digit_rom[371] = 32'h001F8000;
        digit_rom[372] = 32'h001F8000;
        digit_rom[373] = 32'h003F0000;
        digit_rom[374] = 32'h003F0000;
        digit_rom[375] = 32'h007E0000;
        digit_rom[376] = 32'h007E0000;
        digit_rom[377] = 32'h00FC0000;
        digit_rom[378] = 32'h00FC0000;
        digit_rom[379] = 32'h01F80000;
        digit_rom[380] = 32'h00000000;
        digit_rom[381] = 32'h00000000;
        digit_rom[382] = 32'h00000000;
        digit_rom[383] = 32'h00000000;
        digit_rom[384] = 32'h00000000;
        digit_rom[385] = 32'h00000000;
        digit_rom[386] = 32'h00000000;
        digit_rom[387] = 32'h007FFF80;
        digit_rom[388] = 32'h00FFFFC0;
        digit_rom[389] = 32'h01FFFFE0;
        digit_rom[390] = 32'h03FFFFF0;
        digit_rom[391] = 32'h07F007F8;
        digit_rom[392] = 32'h0FE001F8;
        digit_rom[393] = 32'h0FC001F8;
        digit_rom[394] = 32'h0F8000FC;
        digit_rom[395] = 32'h0F8000FC;
        digit_rom[396] = 32'h0F8000FC;
        digit_rom[397] = 32'h0F8000FC;
        digit_rom[398] = 32'h0FC001F8;
        digit_rom[399] = 32'h0FC001F8;
        digit_rom[400] = 32'h0FE003F8;
        digit_rom[401] = 32'h07F007F0;
        digit_rom[402] = 32'h03FC0FE0;
        digit_rom[403] = 32'h03FF3FC0;
        digit_rom[404] = 32'h00FFFF80;
        digit_rom[405] = 32'h007FFF00;
        digit_rom[406] = 32'h001FFC00;
        digit_rom[407] = 32'h003FFF00;
        digit_rom[408] = 32'h00FFFF80;
        digit_rom[409] = 32'h01FE7FE0;
        digit_rom[410] = 32'h03FC1FF0;
        digit_rom[411] = 32'h07F00FF0;
        digit_rom[412] = 32'h0FE003F8;
        digit_rom[413] = 32'h0FC001FC;
        digit_rom[414] = 32'h1FC000FC;
        digit_rom[415] = 32'h1F8000FC;
        digit_rom[416] = 32'h1F8000FC;
        digit_rom[417] = 32'h1F8000FE;
        digit_rom[418] = 32'h1F8000FE;
        digit_rom[419] = 32'h1F8000FC;
        digit_rom[420] = 32'h1F8000FC;
        digit_rom[421] = 32'h1FC001FC;
        digit_rom[422] = 32'h1FE003FC;
        digit_rom[423] = 32'h0FF807F8;
        digit_rom[424] = 32'h07FFFFF0;
        digit_rom[425] = 32'h03FFFFE0;
        digit_rom[426] = 32'h01FFFFC0;
        digit_rom[427] = 32'h00FFFF00;
        digit_rom[428] = 32'h001FF800;
        digit_rom[429] = 32'h00000000;
        digit_rom[430] = 32'h00000000;
        digit_rom[431] = 32'h00000000;
        digit_rom[432] = 32'h00000000;
        digit_rom[433] = 32'h00000000;
        digit_rom[434] = 32'h00000000;
        digit_rom[435] = 32'h000FF000;
        digit_rom[436] = 32'h003FFE00;
        digit_rom[437] = 32'h00FFFF00;
        digit_rom[438] = 32'h01FFFFC0;
        digit_rom[439] = 32'h03FC3FC0;
        digit_rom[440] = 32'h07F00FE0;
        digit_rom[441] = 32'h0FC003F0;
        digit_rom[442] = 32'h0FC003F0;
        digit_rom[443] = 32'h1F8001F8;
        digit_rom[444] = 32'h1F8001F8;
        digit_rom[445] = 32'h3F0000F8;
        digit_rom[446] = 32'h3F0000FC;
        digit_rom[447] = 32'h3F0000FC;
        digit_rom[448] = 32'h3F0000FC;
        digit_rom[449] = 32'h3F0000FC;
        digit_rom[450] = 32'h3F0000FC;
        digit_rom[451] = 32'h3F00007C;
        digit_rom[452] = 32'h1F80007C;
        digit_rom[453] = 32'h1F8000FC;
        digit_rom[454] = 32'h1FC001FC;
        digit_rom[455] = 32'h0FE007FC;
        digit_rom[456] = 32'h0FFC3FFC;
        digit_rom[457] = 32'h07FFFFFC;
        digit_rom[458] = 32'h03FFFFFC;
        digit_rom[459] = 32'h00FFFCFC;
        digit_rom[460] = 32'h003FE0FC;
        digit_rom[461] = 32'h000000FC;
        digit_rom[462] = 32'h000000F8;
        digit_rom[463] = 32'h000000F8;
        digit_rom[464] = 32'h000001F8;
        digit_rom[465] = 32'h000001F0;
        digit_rom[466] = 32'h000003F0;
        digit_rom[467] = 32'h000003F0;
        digit_rom[468] = 32'h000007E0;
        digit_rom[469] = 32'h00001FC0;
        digit_rom[470] = 32'h00003FC0;
        digit_rom[471] = 32'h0001FF80;
        digit_rom[472] = 32'h07FFFF00;
        digit_rom[473] = 32'h07FFFC00;
        digit_rom[474] = 32'h07FFF800;
        digit_rom[475] = 32'h07FFC000;
        digit_rom[476] = 32'h07FC0000;
        digit_rom[477] = 32'h00000000;
        digit_rom[478] = 32'h00000000;
        digit_rom[479] = 32'h00000000;
    end

    initial begin
        cjk_rom[  0]   = 24'h070300;
        cjk_rom[  1]   = 24'h0E0300;
        cjk_rom[  2]   = 24'h1C0300;
        cjk_rom[  3]   = 24'h387FFC;
        cjk_rom[  4]   = 24'h707FFC;
        cjk_rom[  5]   = 24'hF00300;
        cjk_rom[  6]   = 24'hE30300;
        cjk_rom[  7]   = 24'h470300;
        cjk_rom[  8]   = 24'h0E8380;
        cjk_rom[  9]   = 24'h0CFFFF;
        cjk_rom[ 10]   = 24'h1CF03B;
        cjk_rom[ 11]   = 24'h3C0030;
        cjk_rom[ 12]   = 24'h7C0030;
        cjk_rom[ 13]   = 24'hFCFFFF;
        cjk_rom[ 14]   = 24'hDCFFFF;
        cjk_rom[ 15]   = 24'h9C0038;
        cjk_rom[ 16]   = 24'h1C1030;
        cjk_rom[ 17]   = 24'h1C3830;
        cjk_rom[ 18]   = 24'h1C1C30;
        cjk_rom[ 19]   = 24'h1C1C30;
        cjk_rom[ 20]   = 24'h1C0830;
        cjk_rom[ 21]   = 24'h1C0030;
        cjk_rom[ 22]   = 24'h1C01F0;
        cjk_rom[ 23]   = 24'h1C00E0;
        cjk_rom[ 24]   = 24'h0C0000;
        cjk_rom[ 25]   = 24'h0C0FF0;
        cjk_rom[ 26]   = 24'h0C0FF0;
        cjk_rom[ 27]   = 24'h0C0C30;
        cjk_rom[ 28]   = 24'h0C0C30;
        cjk_rom[ 29]   = 24'hFFCC30;
        cjk_rom[ 30]   = 24'hFFCC30;
        cjk_rom[ 31]   = 24'h1C0C30;
        cjk_rom[ 32]   = 24'h1C0C30;
        cjk_rom[ 33]   = 24'h1C0C30;
        cjk_rom[ 34]   = 24'h3D0C30;
        cjk_rom[ 35]   = 24'h3F0C30;
        cjk_rom[ 36]   = 24'h7D8C30;
        cjk_rom[ 37]   = 24'h7DDC30;
        cjk_rom[ 38]   = 24'hEC9C30;
        cjk_rom[ 39]   = 24'hEC1830;
        cjk_rom[ 40]   = 24'hCC1830;
        cjk_rom[ 41]   = 24'h8C1830;
        cjk_rom[ 42]   = 24'h8C3830;
        cjk_rom[ 43]   = 24'h0C3830;
        cjk_rom[ 44]   = 24'h0C7031;
        cjk_rom[ 45]   = 24'h0C7033;
        cjk_rom[ 46]   = 24'h0CE03F;
        cjk_rom[ 47]   = 24'h0CC01F;
        cjk_rom[ 48]   = 24'h003800;
        cjk_rom[ 49]   = 24'h001800;
        cjk_rom[ 50]   = 24'h001800;
        cjk_rom[ 51]   = 24'h3FFFFC;
        cjk_rom[ 52]   = 24'h3FFFFC;
        cjk_rom[ 53]   = 24'h30000C;
        cjk_rom[ 54]   = 24'h30600C;
        cjk_rom[ 55]   = 24'h30700C;
        cjk_rom[ 56]   = 24'h006000;
        cjk_rom[ 57]   = 24'h00C000;
        cjk_rom[ 58]   = 24'h00C000;
        cjk_rom[ 59]   = 24'hFFFFFF;
        cjk_rom[ 60]   = 24'hFFFFFF;
        cjk_rom[ 61]   = 24'h0380E0;
        cjk_rom[ 62]   = 24'h0300C0;
        cjk_rom[ 63]   = 24'h0701C0;
        cjk_rom[ 64]   = 24'h0FC380;
        cjk_rom[ 65]   = 24'h03FF00;
        cjk_rom[ 66]   = 24'h003F00;
        cjk_rom[ 67]   = 24'h001FC0;
        cjk_rom[ 68]   = 24'h0079F8;
        cjk_rom[ 69]   = 24'h03F07C;
        cjk_rom[ 70]   = 24'h7F801F;
        cjk_rom[ 71]   = 24'h7E0006;
        cjk_rom[ 72]   = 24'h0C0380;
        cjk_rom[ 73]   = 24'h0C0300;
        cjk_rom[ 74]   = 24'hFFE7F8;
        cjk_rom[ 75]   = 24'hFFE7F8;
        cjk_rom[ 76]   = 24'h0C0C38;
        cjk_rom[ 77]   = 24'h7FDC30;
        cjk_rom[ 78]   = 24'hFFD870;
        cjk_rom[ 79]   = 24'h0C1FFE;
        cjk_rom[ 80]   = 24'h0C27FE;
        cjk_rom[ 81]   = 24'hFFE0C6;
        cjk_rom[ 82]   = 24'h0000C6;
        cjk_rom[ 83]   = 24'h0000C6;
        cjk_rom[ 84]   = 24'hFFDFFF;
        cjk_rom[ 85]   = 24'hE0C0E7;
        cjk_rom[ 86]   = 24'hC0C0C6;
        cjk_rom[ 87]   = 24'hFFC0C6;
        cjk_rom[ 88]   = 24'hFFCFFE;
        cjk_rom[ 89]   = 24'hC0CFFE;
        cjk_rom[ 90]   = 24'hE0C0C6;
        cjk_rom[ 91]   = 24'hFFC0C0;
        cjk_rom[ 92]   = 24'hC0C0C0;
        cjk_rom[ 93]   = 24'hC0C0C0;
        cjk_rom[ 94]   = 24'hC3C7C0;
        cjk_rom[ 95]   = 24'hC387C0;
        cjk_rom[ 96]   = 24'h200E00;
        cjk_rom[ 97]   = 24'h700700;
        cjk_rom[ 98]   = 24'h3C0380;
        cjk_rom[ 99]   = 24'h1C0100;
        cjk_rom[100]   = 24'h0CFFFE;
        cjk_rom[101]   = 24'h00FFFE;
        cjk_rom[102]   = 24'h000380;
        cjk_rom[103]   = 24'h000300;
        cjk_rom[104]   = 24'hE00300;
        cjk_rom[105]   = 24'hF80300;
        cjk_rom[106]   = 24'h380300;
        cjk_rom[107]   = 24'h180300;
        cjk_rom[108]   = 24'h00FFFE;
        cjk_rom[109]   = 24'h00FFFE;
        cjk_rom[110]   = 24'h000300;
        cjk_rom[111]   = 24'h0C0300;
        cjk_rom[112]   = 24'h0C0300;
        cjk_rom[113]   = 24'h1C0300;
        cjk_rom[114]   = 24'h180300;
        cjk_rom[115]   = 24'h380300;
        cjk_rom[116]   = 24'h700300;
        cjk_rom[117]   = 24'h700300;
        cjk_rom[118]   = 24'hE3FFFF;
        cjk_rom[119]   = 24'hE3FFFF;
        cjk_rom[120]   = 24'h001C00;
        cjk_rom[121]   = 24'h001C00;
        cjk_rom[122]   = 24'h7FFFFE;
        cjk_rom[123]   = 24'h71818C;
        cjk_rom[124]   = 24'h0381C0;
        cjk_rom[125]   = 24'h018180;
        cjk_rom[126]   = 24'hFFFFFF;
        cjk_rom[127]   = 24'hFFFFFF;
        cjk_rom[128]   = 24'h000000;
        cjk_rom[129]   = 24'h1FFFF8;
        cjk_rom[130]   = 24'h1FFFF8;
        cjk_rom[131]   = 24'h1C0038;
        cjk_rom[132]   = 24'h1C0038;
        cjk_rom[133]   = 24'h1FFFF8;
        cjk_rom[134]   = 24'h1C0038;
        cjk_rom[135]   = 24'h1C0038;
        cjk_rom[136]   = 24'h1FFFF8;
        cjk_rom[137]   = 24'h000800;
        cjk_rom[138]   = 24'h001804;
        cjk_rom[139]   = 24'h631C0E;
        cjk_rom[140]   = 24'h630C46;
        cjk_rom[141]   = 24'hE300E7;
        cjk_rom[142]   = 24'hC3FFE3;
        cjk_rom[143]   = 24'hC3FFC0;
        cjk_rom[144]   = 24'h3FFFFC;
        cjk_rom[145]   = 24'h3FFFFC;
        cjk_rom[146]   = 24'h001800;
        cjk_rom[147]   = 24'h001800;
        cjk_rom[148]   = 24'h001800;
        cjk_rom[149]   = 24'h001800;
        cjk_rom[150]   = 24'h001800;
        cjk_rom[151]   = 24'h001800;
        cjk_rom[152]   = 24'h001800;
        cjk_rom[153]   = 24'h001800;
        cjk_rom[154]   = 24'hFFFFFF;
        cjk_rom[155]   = 24'hFFFFFF;
        cjk_rom[156]   = 24'h001800;
        cjk_rom[157]   = 24'h001800;
        cjk_rom[158]   = 24'h001800;
        cjk_rom[159]   = 24'h001800;
        cjk_rom[160]   = 24'h001800;
        cjk_rom[161]   = 24'h001800;
        cjk_rom[162]   = 24'h001800;
        cjk_rom[163]   = 24'h001800;
        cjk_rom[164]   = 24'h001800;
        cjk_rom[165]   = 24'h001800;
        cjk_rom[166]   = 24'h001800;
        cjk_rom[167]   = 24'h001800;
        cjk_rom[168]   = 24'h1C0600;
        cjk_rom[169]   = 24'h1C0630;
        cjk_rom[170]   = 24'h1C0638;
        cjk_rom[171]   = 24'h1C061C;
        cjk_rom[172]   = 24'h0C060C;
        cjk_rom[173]   = 24'h1C0600;
        cjk_rom[174]   = 24'hFF8600;
        cjk_rom[175]   = 24'hFFBFFF;
        cjk_rom[176]   = 24'h0C3FFE;
        cjk_rom[177]   = 24'h0C06C0;
        cjk_rom[178]   = 24'h0C06C0;
        cjk_rom[179]   = 24'h0D06C0;
        cjk_rom[180]   = 24'h1F0EC0;
        cjk_rom[181]   = 24'h3E0CC0;
        cjk_rom[182]   = 24'hFC0CC0;
        cjk_rom[183]   = 24'hCC0CC0;
        cjk_rom[184]   = 24'h0C1CC0;
        cjk_rom[185]   = 24'h0C18C0;
        cjk_rom[186]   = 24'h0C38C0;
        cjk_rom[187]   = 24'h0C30C0;
        cjk_rom[188]   = 24'h0C70C3;
        cjk_rom[189]   = 24'h1CE0E3;
        cjk_rom[190]   = 24'h7DE0FF;
        cjk_rom[191]   = 24'h79C07E;
        cjk_rom[192]   = 24'h7FFFFF;
        cjk_rom[193]   = 24'h600000;
        cjk_rom[194]   = 24'h600000;
        cjk_rom[195]   = 24'h600020;
        cjk_rom[196]   = 24'h608038;
        cjk_rom[197]   = 24'h61C070;
        cjk_rom[198]   = 24'h61E060;
        cjk_rom[199]   = 24'h6078E0;
        cjk_rom[200]   = 24'h603DC0;
        cjk_rom[201]   = 24'h601F80;
        cjk_rom[202]   = 24'h600780;
        cjk_rom[203]   = 24'h6007C0;
        cjk_rom[204]   = 24'h600FE0;
        cjk_rom[205]   = 24'h601CF0;
        cjk_rom[206]   = 24'h603878;
        cjk_rom[207]   = 24'h60703C;
        cjk_rom[208]   = 24'h61E01C;
        cjk_rom[209]   = 24'h67C00C;
        cjk_rom[210]   = 24'h630000;
        cjk_rom[211]   = 24'h600000;
        cjk_rom[212]   = 24'h600000;
        cjk_rom[213]   = 24'h7FFFFF;
        cjk_rom[214]   = 24'h7FFFFF;
        cjk_rom[215]   = 24'h000000;
    end

    //------------------------------------------------------------------
    // 渲染期间锁存的参数（一次重绘内部恒定，屏幕不会半新半旧）
    //------------------------------------------------------------------
    reg [7:0]  r_fill;
    reg [1:0]  r_state;
    reg [11:0] r_db;

    wire [11:0] v10   = r_db / 12'd10;                    // 整数 dB(A)
    wire [11:0] hh    = v10 / 12'd100;
    wire [11:0] rem   = v10 - hh * 12'd100;
    wire [11:0] tt    = rem / 12'd10;
    wire [11:0] uu    = rem - tt * 12'd10;
    wire [1:0]  nd    = (v10 >= 12'd100) ? 2'd3 : (v10 >= 12'd10) ? 2'd2 : 2'd1;
    wire [11:0] totw  = nd * 12'd32 + (nd - 2'd1) * 12'd6;
    wire [11:0] dx0   = (IMG_W - totw) >> 1;

    wire [1:0]  nci   = (r_state == 2'd0) ? 2'd2 : 2'd3;
    wire [11:0] ttw   = nci * 12'd24 + (nci - 2'd1) * 12'd4;
    wire [11:0] tx0   = (IMG_W - ttw) >> 1;

    // 第 cdi 位数字（0 = 最高位）
    function [3:0] dsel;
        input [1:0]  n;
        input [1:0]  di;
        input [11:0] h;
        input [11:0] t;
        input [11:0] u;
        begin
            case (n)
                2'd3:    dsel = (di == 2'd0) ? h[3:0] : (di == 2'd1) ? t[3:0] : u[3:0];
                2'd2:    dsel = (di == 2'd0) ? t[3:0] : u[3:0];
                default: dsel = u[3:0];
            endcase
        end
    endfunction

    // 第 ci 个汉字
    function [3:0] csel;
        input [1:0] st;
        input [1:0] ci;
        begin
            case (st)
                2'd0:    csel = (ci == 2'd0) ? 4'd0 : 4'd1;                        // 待机
                2'd1:    csel = (ci == 2'd0) ? 4'd2 : (ci == 2'd1) ? 4'd3 : 4'd8;   // 安静区
                2'd2:    csel = (ci == 2'd0) ? 4'd4 : (ci == 2'd1) ? 4'd5 : 4'd8;   // 注意区
                default: csel = (ci == 2'd0) ? 4'd6 : (ci == 2'd1) ? 4'd7 : 4'd8;   // 干扰区
            endcase
        end
    endfunction

    function [15:0] scol;
        input [1:0] st;
        begin
            case (st)
                2'd1:    scol = C_QUIET;
                2'd2:    scol = C_WARN;
                2'd3:    scol = C_ALARM;
                default: scol = C_IDLE;
            endcase
        end
    endfunction

    //------------------------------------------------------------------
    // 状态与计数器
    //------------------------------------------------------------------
    reg [2:0]  ph;
    reg [15:0] cword;      // 清屏字计数
    reg [15:0] ck;         // 角向步
    reg [8:0]  cm;         // 径向半步
    reg signed [17:0] vx, vy;
    reg [1:0]  cdi, cci;
    reg [5:0]  clx, cly;

    //------------------------------------------------------------------
    // 圆环一个采样点：单位向量 × 半径（Q16 × 半步数 → Q17 取整）
    //------------------------------------------------------------------
    wire [8:0] r2v = R_IN * 2 + cm;
    wire signed [31:0] mux = ($signed(vx) * $signed({1'b0, r2v}) + 32'sd65536) >>> 17;
    wire signed [31:0] muy = ($signed(vy) * $signed({1'b0, r2v}) + 32'sd65536) >>> 17;
    wire signed [31:0] px32 = CX + mux;
    wire signed [31:0] py32 = CY - muy;
    wire px_ok = (px32 >= 0) && (px32 < IMG_W);
    wire py_ok = (py32 >= 0) && (py32 < IMG_H);

    wire [31:0] arc_l = ck * 32'd100;
    wire [31:0] arc_r = {24'd0, r_fill} * 32'd1024;
    wire ring_filled = (arc_l < arc_r);

    //------------------------------------------------------------------
    // 旋转一步（40 位中间量：18×18 位乘出来是 36 位，32 位会在 ±65536×65535 处溢出）
    //------------------------------------------------------------------
    wire signed [39:0] rpm = ($signed(vx) * ROT_CS) + ($signed(vy) * ROT_SN);
    wire signed [39:0] rpn = ($signed(vy) * ROT_CS) - ($signed(vx) * ROT_SN);
    wire signed [17:0] rot_x = rpm >>> 16;      // 别忘右移：这是 Q16 定点乘
    wire signed [17:0] rot_y = rpn >>> 16;

    //------------------------------------------------------------------
    // 字模查表
    //------------------------------------------------------------------
    wire [3:0]  dnum  = dsel(nd, cdi, hh, tt, uu);
    wire [15:0] didx  = dnum * 16'd48 + cly;
    wire [31:0] drow  = digit_rom[didx];
    wire [3:0]  cnum  = csel(r_state, cci);
    wire [15:0] cidx  = cnum * 16'd24 + cly;
    wire [23:0] crow  = cjk_rom[cidx];

    wire [16:0] d_col = dx0 + cdi * 16'd38 + clx;              // DW + DGAP = 38
    wire [16:0] t_col = tx0 + cci * 16'd28 + clx;              // TG + TTGAP = 28
    wire [16:0] d_idx = (DY0 + cly) * 16'd240 + d_col;
    wire [16:0] t_idx = (TY0 + cly) * 16'd240 + t_col;

    //------------------------------------------------------------------
    // 本拍要不要写、写哪里、写什么
    //------------------------------------------------------------------
    reg        wr_en;
    reg        wr_full;
    reg [16:0] wr_idx;
    reg [15:0] wr_c;

    always @(*) begin
        wr_en   = 1'b0;
        wr_full = 1'b0;
        wr_idx  = 17'd0;
        wr_c    = 16'd0;
        case (ph)
            PH_CLEAR: begin
                wr_en   = 1'b1;
                wr_full = 1'b1;                 // 整字写，两个像素都是背景色
                wr_c    = C_BG;
                wr_idx  = cword << 1;           // 字地址 ×2 = 像素索引
            end
            PH_RING: begin
                if (px_ok && py_ok) begin
                    wr_en  = 1'b1;
                    wr_c   = ring_filled ? scol(r_state) : C_TRACK;
                    wr_idx = py32[7:0] * 16'd240 + px32[7:0];
                end
            end
            PH_DIG: begin
                if ((cdi < nd) && drow[31 - clx[4:0]]) begin
                    wr_en  = 1'b1;
                    wr_c   = C_NUM;
                    wr_idx = d_idx;
                end
            end
            PH_TXT: begin
                if ((cci < nci) && crow[23 - clx[4:0]]) begin
                    wr_en  = 1'b1;
                    wr_c   = scol(r_state);
                    wr_idx = t_idx;
                end
            end
            default: ;
        endcase
    end

    //------------------------------------------------------------------
    // 缓动、重绘触发、主状态机
    //------------------------------------------------------------------
    reg [7:0]  shown;
    reg [7:0]  painted_fill;
    reg [11:0] painted_db;
    reg [1:0]  painted_state;

    reg [TICK_BITS-1:0] tick;
    reg [GAP_BITS-1:0]  gap;

    wire tick_hit = (tick == {TICK_BITS{1'b1}});
    wire gap_ok   = (gap  == {GAP_BITS{1'b1}});
    wire [11:0] db_int = i_db10 / 12'd10;          // 画面上的整数 dB(A)（触发判据用它，避免 0.1 dB 抖动）

    wire sig_chg = (shown != painted_fill) || (db_int != painted_db) || (i_state != painted_state);

    // 想画 = 允许画 + 过了最小间隔 + 内容真的变了
    wire want_draw = i_en && gap_ok && sig_chg;

    // 组合给出 o_hold（**必须组合**）：万一它晚一拍，刷屏引擎可能正好在那一拍
    // 开了新帧，撕裂就又回来了。它只依赖内部状态与 i_en，不会形成组合环。
    assign o_hold = (ph != PH_IDLE) || want_draw;

    always @(posedge clk) begin
        if (!rst_n) begin
            ph            <= PH_IDLE;
            cword         <= 16'd0;
            ck            <= 16'd0;
            cm            <= 9'd0;
            vx            <= 18'sd0;
            vy            <= 18'sd65536;
            cdi           <= 2'd0;
            cci           <= 2'd0;
            clx           <= 6'd0;
            cly           <= 6'd0;
            r_fill        <= 8'd0;
            r_state       <= 2'd0;
            r_db          <= 12'd0;
            shown         <= 8'd0;
            painted_fill  <= 8'd0;
            painted_db    <= 12'd0;
            painted_state <= 2'd0;
            tick          <= {TICK_BITS{1'b0}};
            gap           <= {GAP_BITS{1'b1}};      // 上电即可画第一帧
            o_ena         <= 1'b0;
            o_wea         <= 4'h0;
            o_addra       <= 17'd0;
            o_dina        <= 32'd0;
            o_busy        <= 1'b0;
            o_done        <= 1'b0;
        end else begin
            // ---- 帧存写口（组合好的 wr_* 打一拍出去）----
            o_ena   <= wr_en;
            o_addra <= wr_idx[16:1];
            o_wea   <= wr_full ? 4'hF : (wr_idx[0] ? 4'b1100 : 4'b0011);
            o_dina  <= wr_full              ? {wr_c, wr_c} :
                       (wr_idx[0]           ? {wr_c, 16'h0000} : {16'h0000, wr_c});

            // ---- 缓动 tick 与最小重绘间隔 ----
            tick <= tick + 1'b1;
            if (!gap_ok) gap <= gap + 1'b1;

            if (SLEW == 0) begin
                shown <= i_fill;                     // 不平滑：直接跟随（testbench 用这个配置）
            end else if (tick_hit) begin
                if (shown < i_fill)
                    shown <= ((i_fill - shown) > SLEW) ? (shown + SLEW) : i_fill;
                else if (shown > i_fill)
                    shown <= ((shown - i_fill) > SLEW) ? (shown - SLEW) : i_fill;
            end

            case (ph)
                PH_IDLE: begin
                    o_busy <= 1'b0;
                    if (want_draw && !i_spi_busy) begin   // 等刷屏引擎把当前帧发完，别抢帧存
                        r_fill        <= shown;
                        r_db          <= i_db10;
                        r_state       <= i_state;
                        painted_fill  <= shown;
                        painted_db    <= db_int;
                        painted_state <= i_state;
                        cword         <= 16'd0;
                        ck            <= 16'd0;
                        cm            <= 9'd0;
                        vx            <= 18'sd0;
                        vy            <= 18'sd65536;
                        cdi           <= 2'd0;
                        cci           <= 2'd0;
                        clx           <= 6'd0;
                        cly           <= 6'd0;
                        gap           <= {GAP_BITS{1'b0}};
                        o_busy        <= 1'b1;
                        ph            <= PH_CLEAR;
                    end
                end

                PH_CLEAR: begin
                    if (cword == NWORD - 1) begin
                        cword <= 16'd0;
                        ph    <= PH_RING;
                    end else begin
                        cword <= cword + 1'b1;
                    end
                end

                PH_RING: begin
                    if (cm == RAD - 1) begin
                        cm <= 9'd0;
                        ph <= PH_ROT;
                    end else begin
                        cm <= cm + 1'b1;
                    end
                end

                PH_ROT: begin
                    vx <= rot_x;
                    vy <= rot_y;
                    if (ck == NSTEP - 1) begin
                        ck <= 16'd0;
                        ph <= PH_DIG;
                    end else begin
                        ck <= ck + 1'b1;
                        ph <= PH_RING;
                    end
                end

                PH_DIG: begin
                    if (cdi >= nd) begin
                        cdi <= 2'd0;
                        clx <= 6'd0;
                        cly <= 6'd0;
                        cci <= 2'd0;
                        ph  <= PH_TXT;
                    end else if (clx == DW - 1) begin
                        clx <= 6'd0;
                        if (cly == DH - 1) begin
                            cly <= 6'd0;
                            cdi <= cdi + 1'b1;
                        end else begin
                            cly <= cly + 1'b1;
                        end
                    end else begin
                        clx <= clx + 1'b1;
                    end
                end

                PH_TXT: begin
                    if (cci >= nci) begin
                        cci <= 2'd0;
                        clx <= 6'd0;
                        cly <= 6'd0;
                        ph  <= PH_END;
                    end else if (clx == TG - 1) begin
                        clx <= 6'd0;
                        if (cly == TG - 1) begin
                            cly <= 6'd0;
                            cci <= cci + 1'b1;
                        end else begin
                            cly <= cly + 1'b1;
                        end
                    end else begin
                        clx <= clx + 1'b1;
                    end
                end

                PH_END: begin
                    o_busy <= 1'b0;
                    o_done <= 1'b1;
                    ph     <= PH_IDLE;
                end

                default: ph <= PH_IDLE;
            endcase
        end
    end

endmodule
