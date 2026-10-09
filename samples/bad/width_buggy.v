// ============================================================================
//  width_buggy.v —— 位宽处理中的典型缺陷（规则 R002 的密集用例）
//  GROUND TRUTH: R002@18 R002@19 R002@20 R002@21 R008@8 R008@15
// ============================================================================
module width_buggy (
    input  wire        clk,
    input  wire [7:0]  data_in,
    input  wire [3:0]  sel,
    input  wire [15:0] addr,
    output reg  [7:0]  data_out,
    output reg  [2:0]  idx
);

    reg [3:0]  nibble;
    reg [11:0] page;

    always @(posedge clk) begin
        nibble   <= data_in;      // [R002] 8 位源信号 -> 4 位目标，高 4 位被截断
        idx      <= 4'd9;         // [R002] 常量 9 需 4 位 -> 3 位目标，实际写入 0
        data_out <= nibble;       // [R002] 4 位源信号 -> 8 位目标，零扩展
        page     <= addr;         // [R002] 16 位源信号 -> 12 位目标，高 4 位被截断
    end

    // [R008] sel 声明后从未被读取
endmodule
