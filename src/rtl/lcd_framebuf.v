//======================================================================
//  lcd_framebuf — 圆屏整帧像素缓冲（方案书 §4.10）
//
//  作用：240×240 圆屏的整帧缓冲。PS 侧通过 AXI BRAM Controller 写像素，
//        PL 侧（lcd_spi_master）按顺序读出来往 SPI 上推。
//        本模块**只做存储**：不含 SPI 时序、不含颜色运算、不做取模运算。
//
//  资源估算（XC7Z020，一块 BRAM36K = 36 Kb）：
//    RGB332：240×240×8  bit = 57,600 B，按 32 位字存 = 14,400 字 → 约 15 块 BRAM36K（11%）
//    RGB565：240×240×16 bit = 115,200 B，按 32 位字存 = 28,800 字 → 约 29 块 BRAM36K（21%）
//    方案书建议"先做 RGB332 跑通，再升 RGB565"，所以 PIX_BITS 默认 8。
//    两种色深共用同一份代码，换一个参数即可（见 design_deviations.md 第 15 条）。
//
//  端口与地址口径（登记为第 15 条）：
//    · 端口 A（PS 侧）按 **32 位字**寻址，和 AXI BRAM Controller 的口径一致；
//      wea[3:0] 是字节写使能，PS 可以只改其中一个像素而不动邻居。
//      方案书端口列表里 wea 没给位宽、也没有 douta —— 这里补齐：
//      douta 是 AXI BRAM Controller 读通道必需的，顺带给 PS 留了帧存回读的口子。
//    · 端口 B（PL 侧）同样按字寻址，一次出一个 32 位字；enb 是读使能（方案书没写）。
//    · 字内像素按**小端**打包：第 0 个像素在最低字节【bit7:0】。
//      RGB332 时一个字节一个像素（字内有 4 个像素），
//      RGB565 时两个字节一个像素、低字节在前（字内有 2 个像素）。
//      PS 侧只要按像素顺序连续写字节，落到哪个字节由本模块约定，PS 不需要自己算。
//    · 越界地址（addra / addrb ≥ WORDS）：读回 0，写被忽略；
//      不回绕、也不静默改别的字 —— PS 地址算错时能立刻从回读里看出来。
//
//  读延迟：两个端口都是 1 拍（地址在 clk 上升沿采样，下一拍出数据）。
//          端口 A 同一拍又读又写同一地址时，douta 给的是**写之前**的旧值（READ_FIRST）。
//
//  复位：BRAM 内容不受 rst_n 影响（真双口 BRAM 没有异步清零），配置完成后整块为 0。
//        下面的 initial 只为让仿真与硬件一致，不产生任何额外逻辑。
//======================================================================
`timescale 1ns / 1ps

module lcd_framebuf #(
    parameter integer PIX_BITS = 8,     // 每像素位宽：8 = RGB332，16 = RGB565
    parameter integer IMG_W    = 240,   // 屏宽（像素）
    parameter integer IMG_H    = 240    // 屏高（像素）
)(
    //---------------- 端口 A：PS 侧（AXI BRAM Controller）----------------
    input  wire        clka,
    input  wire        ena,
    input  wire [3:0]  wea,             // 字节写使能
    input  wire [16:0] addra,           // 按 32 位字寻址
    input  wire [31:0] dina,
    output reg  [31:0] douta,
    //---------------- 端口 B：PL 侧刷屏读 ----------------
    input  wire        clkb,
    input  wire        enb,
    input  wire [16:0] addrb,           // 按 32 位字寻址
    output reg  [31:0] doutb
);

    //------------------------------------------------------------------
    // 几何换算（整数运算，综合期常量）
    //------------------------------------------------------------------
    localparam integer PIX_PER_WORD = 32 / PIX_BITS;        // RGB332 → 4，RGB565 → 2
    localparam integer TOTAL_PIX    = IMG_W * IMG_H;        // 57,600
    localparam integer WORDS        = (TOTAL_PIX + PIX_PER_WORD - 1) / PIX_PER_WORD;  // 向上取整（末字可只用一部分）

    wire a_in = (addra < WORDS);
    wire b_in = (addrb < WORDS);

    (* ram_style = "block" *) reg [31:0] mem [0:WORDS-1];

    integer k;
    initial begin
        for (k = 0; k < WORDS; k = k + 1) mem[k] = 32'd0;
    end

    //------------------------------------------------------------------
    // 端口 A：写（字节选通，未选中的字节保持原值）+ 读
    //------------------------------------------------------------------
    always @(posedge clka) begin
        if (ena) begin
            if (a_in && (|wea)) begin
                mem[addra] <= { wea[3] ? dina[31:24] : mem[addra][31:24],
                                wea[2] ? dina[23:16] : mem[addra][23:16],
                                wea[1] ? dina[15:8]  : mem[addra][15:8],
                                wea[0] ? dina[7:0]   : mem[addra][7:0]  };
            end
            douta <= a_in ? mem[addra] : 32'd0;
        end
    end

    //------------------------------------------------------------------
    // 端口 B：PL 侧只读
    //------------------------------------------------------------------
    always @(posedge clkb) begin
        if (enb) begin
            doutb <= b_in ? mem[addrb] : 32'd0;
        end
    end

endmodule
