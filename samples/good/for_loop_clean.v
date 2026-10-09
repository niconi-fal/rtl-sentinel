// ============================================================================
//  for_loop_clean.v —— 边界用例：含 for 循环的时序块
//  GROUND TRUTH: （无）
//
//  该样本专门用于回归验证 R003 的一个历史缺陷：
//  for 循环头 `(i = 0; i < 4; i = i + 1)` 中的循环控制赋值曾被误判为
//  "时序块误用阻塞赋值"。修复后本文件必须保持零告警。
// ============================================================================
module for_loop_clean (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [5:0] acc
);

    integer i;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc <= 6'd0;
        end else begin
            acc <= acc + 6'd1;
            for (i = 0; i < 4; i = i + 1) begin
                acc <= acc + i[5:0];
            end
        end
    end

endmodule
