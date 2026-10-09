// ============================================================================
//  inst_buggy.v —— 模块实例化中的典型缺陷（跨文件规则 R009 / R010 的用例）
//  GROUND TRUTH: R009@22 R009@22 R010@22 R004@40
//
//  依赖 samples/good/sub_mod.v 提供的端口定义（跨文件模块索引）。
// ============================================================================
module inst_buggy (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] din,
    output wire [7:0] dout
);

    wire [3:0]  narrow;
    wire [15:0] wide;
    reg  [7:0]  shadow;

    assign dout = shadow;

    // [R009] .din 是 8 位端口却接了 4 位的 narrow；.flag 是 1 位端口却接了 16 位的 wide
    // [R010] .enable 端口漏连
    sub_mod u_sub (
        .clk   (clk),
        .rst_n (rst_n),
        .din   (narrow),
        .dout  (),
        .flag  (wide)
    );

    // [R004] sub_mod 的输出端口 .dout 连到已被 always 块驱动的 shadow
    sub_mod u_shadow (
        .clk   (clk),
        .rst_n (rst_n),
        .din   (din),
        .dout  (shadow),
        .flag  (),
        .enable(1'b1)
    );

    always @(posedge clk) begin
        if (!rst_n) shadow <= 8'd0;
        else        shadow <= din;
    end

endmodule
