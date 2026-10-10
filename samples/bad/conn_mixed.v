// ============================================================================
//  conn_mixed.v —— 同一实例内混用按位置与按名连接
//  GROUND TRUTH: R013@18
//
//  Verilog LRM 禁止在同一实例化里混用两种连接方式。此处第一个端口按位置
//  连接，其余按名连接 —— 语法上不应通过，需统一为同一种。
//  依赖 samples/good/sub_mod.v 的端口定义（跨文件模块索引）。
// ============================================================================
module conn_mixed (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] din,
    output wire [7:0] dout
);

    wire flag;

    sub_mod u0 (clk, .rst_n(rst_n), .din(din), .enable(1'b1), .dout(dout), .flag(flag));

endmodule
