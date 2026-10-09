// ============================================================================
//  sub_mod.v —— 被实例化的参考子模块（配套 inst_buggy.v 使用）
//  GROUND TRUTH: （无）
//
//  该模块同时是 R009 / R010 的端口定义来源：静态分析必须依赖跨文件模块索引
//  才能知道每个端口的方向与位宽，否则实例化的任何连接都无法判断。
// ============================================================================
module sub_mod (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [7:0]  din,
    input  wire        enable,
    output reg  [7:0]  dout,
    output reg         flag
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dout <= 8'd0;
            flag <= 1'b0;
        end else if (enable) begin
            dout <= din;
            flag <= |din;
        end else begin
            dout <= dout;
            flag <= flag;
        end
    end

endmodule
