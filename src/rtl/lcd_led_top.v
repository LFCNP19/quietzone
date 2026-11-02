`timescale 1ns / 1ps
//======================================================================
// lcd_led_top —— 圆屏色环 + 灯环 顶层封装（把 quietzone 的 4 个模块拼起来）
//   · lcd_framebuf   (RGB565 帧存，29 块 BRAM36K)
//   · lcd_spi_master (25 MHz 硬件 SPI，27fps 扫屏)
//   · lcd_ring_ui    (色环 + dB 大字 + 汉字状态)
//   · ws2812b_drv    (16 颗灯环)
//  控制来自一个 AXI GPIO（PS 写寄存器）：
//     ctrl1[7:0]=fill  ctrl1[9:8]=state  ctrl1[21:10]=db10
//     ctrl2[23:0]=led_color(GRB)  ctrl2[24]=led_update
//======================================================================
module lcd_led_top (
    input  wire        clk_50m,
    input  wire        rst_n,
    input  wire [31:0] ctrl1,
    input  wire [31:0] ctrl2,
    output wire        lcd_scl,
    output wire        lcd_sda,
    output wire        lcd_cs,
    output wire        lcd_dc,
    output wire        lcd_rst,
    output wire        lcd_blk,
    output wire        ws2812_din
);

    wire [7:0]  fill       = ctrl1[7:0];
    wire [1:0]  state      = ctrl1[9:8];
    wire [11:0] db10       = ctrl1[21:10];
    wire [23:0] led_color  = ctrl2[23:0];
    wire        led_update = ctrl2[24];

    // ---- 内部互连 ----
    wire        ring_ena;
    wire [3:0]  ring_wea;
    wire [16:0] ring_addra;
    wire [31:0] ring_dina;
    wire [31:0] fb_douta;      // 未用
    wire        spi_pix_req;
    wire [16:0] spi_pix_addr;
    wire [31:0] fb_doutb;
    reg         pix_valid_r;
    wire        ring_hold;
    wire        spi_busy;

    always @(posedge clk_50m) begin
        if (!rst_n) pix_valid_r <= 1'b0;
        else        pix_valid_r <= spi_pix_req;
    end

    lcd_framebuf #(
        .PIX_BITS(16), .IMG_W(240), .IMG_H(240)
    ) u_fb (
        .clka (clk_50m),
        .ena  (ring_ena),
        .wea  (ring_wea),
        .addra(ring_addra),
        .dina (ring_dina),
        .douta(fb_douta),
        .clkb (clk_50m),
        .enb  (spi_pix_req),
        .addrb(spi_pix_addr),
        .doutb(fb_doutb)
    );

    lcd_ring_ui u_ring (
        .clk        (clk_50m),
        .rst_n      (rst_n),
        .i_en       (1'b1),
        .i_fill     (fill),
        .i_state    (state),
        .i_db10     (db10),
        .i_spi_busy (spi_busy),
        .o_ena      (ring_ena),
        .o_wea      (ring_wea),
        .o_addra    (ring_addra),
        .o_dina     (ring_dina),
        .o_busy     (),
        .o_done     (),
        .o_hold     (ring_hold)
    );

    lcd_spi_master #(
        .CLK_HZ  (50_000_000),
        .SPI_HZ  (25_000_000),
        .IMG_W   (240),
        .IMG_H   (240),
        .PIX_BITS(16)
    ) u_lcd (
        .clk         (clk_50m),
        .rst_n       (rst_n),
        .i_pix       (fb_doutb),
        .i_pix_valid (pix_valid_r),
        .i_buf_sel   (1'b0),
        .i_hold      (ring_hold),
        .o_scl       (lcd_scl),
        .o_sda       (lcd_sda),
        .o_cs        (lcd_cs),
        .o_dc        (lcd_dc),
        .o_rst       (lcd_rst),
        .o_pix_req   (spi_pix_req),
        .o_pix_addr  (spi_pix_addr),
        .o_busy      (spi_busy)
    );

    ws2812b_drv #(
        .LED_NUM(16), .CLK_HZ(50_000_000)
    ) u_led (
        .clk        (clk_50m),
        .rst_n      (rst_n),
        .i_led_color({16{led_color}}),
        .i_update   (led_update),
        .o_din      (ws2812_din),
        .o_busy     ()
    );

    assign lcd_blk = 1'b1;

endmodule
